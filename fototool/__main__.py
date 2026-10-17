"""Kommandozeile: python3 -m fototool <befehl> …  (ohne Befehl startet die GUI)."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import Config, Profiles
from .context import Cancelled, RunContext
from .deps import check, setup_logging, tk_available
from .jobs import DupParams, SortParams, SyncParams, params_from_dict, run_job
from .journal import list_journals, undo


def _ctx() -> RunContext:
    last = {"phase": None, "pct": -1}

    def prog(phase, done, total):
        if total:
            pct = done * 100 // total
            if pct == last["pct"] and phase == last["phase"] and done < total:
                return
            last["pct"] = pct
            end = "\n" if done >= total else ""
            print(f"\r  {phase}: {done}/{total} ({pct} %)", end=end, flush=True)
        elif phase != last["phase"]:
            print(f"  {phase} …")
        last["phase"] = phase

    return RunContext(on_message=print, on_progress=prog)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fototool", description="Fotos abgleichen, Duplikate finden, sortieren")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--config-dir", help="anderer Einstellungsordner (Standard ~/.fototool)")
    ap.add_argument("--ausfuehren", action="store_true", help="wirklich ausführen (Standard: Probelauf)")
    ap.add_argument("--ja", action="store_true", help="Rückfragen (Papierkorb/Überschreiben) bestätigen")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("abgleich", help="Quellen mit Ziel vergleichen, fehlende kopieren")
    s.add_argument("--quelle", action="append", required=False, default=[])
    s.add_argument("--ziel")
    s.add_argument("--nur-bericht", action="store_true")
    s.add_argument("--ablage", choices=["struktur", "muster"])
    s.add_argument("--muster")
    s.add_argument("--dateiname", default="")

    d = sub.add_parser("duplikate", help="Duplikate in einem Ordner finden")
    d.add_argument("--ordner")
    d.add_argument("--behalten", choices=["aeltestes", "neuestes", "kuerzester_pfad", "laengster_pfad", "erster"])
    d.add_argument("--aktion", choices=["bericht", "verschieben", "papierkorb"])

    o = sub.add_parser("sortieren", help="Nach Metadaten in Ordnerstruktur sortieren")
    o.add_argument("--quelle")
    o.add_argument("--ziel")
    o.add_argument("--muster")
    o.add_argument("--dateiname", default="")
    o.add_argument("--verschieben", action="store_true")

    for p in (s, d, o):
        p.add_argument("--profil", help="gespeichertes Profil verwenden")

    sub.add_parser("profile", help="gespeicherte Profile anzeigen")
    u = sub.add_parser("rueckgaengig", help="letzten Lauf rückgängig machen")
    u.add_argument("--datei", help="bestimmte Journal-Datei")
    sub.add_parser("pruefen", help="Abhängigkeiten und Zugriffsrechte prüfen")

    a = ap.parse_args(argv)
    cfg = Config(a.config_dir)
    setup_logging(cfg)

    if a.cmd is None:
        if not tk_available():
            print("GUI nicht verfügbar (tkinter fehlt): brew install python-tk@3.14", file=sys.stderr)
            return 1
        from .gui import run_gui
        run_gui(cfg)
        return 0
    if a.cmd == "pruefen":
        hints = check(cfg)
        print("\n".join(hints) if hints else "Alles in Ordnung.")
        return 0
    if a.cmd == "profile":
        for n in Profiles(cfg).names():
            print(n)
        return 0
    ctx = _ctx()
    if a.cmd == "rueckgaengig":
        files = list_journals(cfg.data_dir)
        files = [f for f in files if "_RUECKGAENGIG" not in f.name]
        target = next((f for f in files if a.datei and a.datei in str(f)), files[0] if files else None)
        if not target:
            print("Kein Lauf zum Rückgängigmachen gefunden.")
            return 1
        done, problems = undo(target, ctx, dry_run=not a.ausfuehren)
        print(f"{'Rückgängig' if a.ausfuehren else 'Probelauf Rückgängig'}: {done} Dateien ({target.name})")
        for p in problems:
            print("  ⚠️", p)
        return 0

    dry = not a.ausfuehren
    if a.profil:
        prof = Profiles(cfg).load(a.profil)
        params = params_from_dict(prof.get("aufgabe", a.cmd), prof.get("parameter", {}))
        params.probelauf = dry
    elif a.cmd == "abgleich":
        if not a.quelle or not a.ziel:
            ap.error("--quelle und --ziel nötig (oder --profil)")
        params = SyncParams(a.quelle, a.ziel, not a.nur_bericht,
                            a.ablage or cfg.get("abgleich.fehlende_ablage"),
                            a.muster or cfg.get("muster.standard"), a.dateiname, dry)
    elif a.cmd == "duplikate":
        if not a.ordner:
            ap.error("--ordner nötig (oder --profil)")
        params = DupParams(a.ordner, a.behalten or cfg.get("duplikate.behalten"),
                           a.aktion or cfg.get("duplikate.aktion"), cfg.get("duplikate.ordner"), dry)
    else:
        if not a.quelle or not a.ziel:
            ap.error("--quelle und --ziel nötig (oder --profil)")
        params = SortParams(a.quelle, a.ziel, a.muster or cfg.get("muster.standard"),
                            a.dateiname or cfg.get("muster.dateiname"),
                            "move" if a.verschieben else "copy", dry)
    params.bestaetigt = a.ja or not cfg.get("aktionen.bestaetigen", True)
    try:
        res = run_job(cfg, ctx, a.cmd, params)
    except Cancelled:
        print("Abgebrochen.")
        return 130
    except (OSError, ValueError) as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 2
    print()
    for k, v in res.summary.items():
        print(f"  {k}: {v}")
    for r in res.reports:
        print(f"  Bericht: {r}")
    if res.cancelled:
        print("\nAbgebrochen – Bericht unvollständig.")
        return 130
    if dry:
        print("\nProbelauf – nichts verändert. Mit --ausfuehren wirklich ausführen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
