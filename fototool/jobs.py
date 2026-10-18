"""Die drei Aufgaben: Abgleich, Duplikate, Sortieren (+ Rückgängig)."""
from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .actions import FileOps, Result
from .config import Config
from .context import Cancelled, Declined, RunContext
from .hashing import HashCache, Hasher, group_by_key
from .journal import Journal
from .metadata import NAME_FIELDS, Meta, MetadataReader, assign_names, unassigned_names
from .patterns import PatternRenderer, placeholders_in, validate
from .report import report_dirs, write_report
from .scan import FileEntry, Scanner

STATUS_TEXT = {
    "vorhanden": "Bereits im Ziel",
    "fehlt": "Fehlt im Ziel",
    "doppelt_in_quelle": "Doppelt in Quelle (nur 1× kopiert)",
    "kopiert": "Kopiert",
    "verschoben": "Verschoben",
    "identisch_vorhanden": "Identisch vorhanden – übersprungen",
    "kollision_uebersprungen": "Namenskollision – übersprungen",
    "ueberschrieben": "Überschrieben",
    "geschuetzt": "Geschützt – nicht verändert",
    "fehler": "Fehler",
    "behalten": "Original (behalten)",
    "duplikat": "Duplikat",
    "papierkorb": "In Papierkorb",
    "bereits_richtig": "Liegt schon richtig",
    "leer": "Leere Datei (0 Byte) – vermutlich defekt",
    "sidecar_ergaenzt": "Sidecar ergänzt",
    "sidecar_aktualisiert": "Sidecar aktualisiert",
    "sidecar_fehlt": "Sidecar fehlt im Ziel",
    "sidecar_geaendert": "Sidecar geändert",
    "sidecar_ziel_neuer": "Sidecar im Ziel neuer – nicht ersetzt",
    "nur_in_quelle": "Nur im Quellordner (liegt im Ziel, sonst nirgends)",
}
# Im Probelauf: was passieren würde
DRY_TEXT = {
    "kopiert": "Würde kopiert",
    "verschoben": "Würde verschoben",
    "ueberschrieben": "Würde überschrieben",
    "papierkorb": "Würde in Papierkorb",
    "sidecar_ergaenzt": "Sidecar würde ergänzt",
    "sidecar_aktualisiert": "Sidecar würde aktualisiert",
}


class AccessError(PermissionError):
    """Ordner nicht lesbar/erreichbar – Lauf wird abgebrochen statt falscher Ergebnisse."""


ACCESS_HINT = ("\n\nFotoTool braucht Zugriff: Systemeinstellungen → Datenschutz & Sicherheit → "
               "Festplattenvollzugriff → FotoTool.app hinzufügen (bzw. bei Nachfrage „Erlauben“), "
               "dann FotoTool neu starten.")


def ensure_access(path: Path, must_exist: bool = True) -> None:
    """Prüft, ob ein Ordner wirklich lesbar ist (bzw. angelegt werden kann)."""
    parts = path.parts
    if len(parts) >= 3 and parts[1] == "Volumes" and not os.path.ismount(os.path.join("/", *parts[:3])):
        raise AccessError(f"Laufwerk „{parts[2]}“ ist nicht angeschlossen: {path}")
    try:
        os.stat(path)
    except FileNotFoundError:
        if must_exist:
            raise FileNotFoundError(f"Ordner nicht gefunden: {path}")
        parent = path.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        ensure_access(parent)
        if not os.access(parent, os.W_OK):
            raise AccessError(f"Keine Schreibrechte in {parent}" + ACCESS_HINT)
        return
    except PermissionError:
        raise AccessError(f"Kein Zugriff auf {path}" + ACCESS_HINT)
    if path.is_dir():
        try:
            os.listdir(path)
        except PermissionError:
            raise AccessError(f"Kein Zugriff auf {path}" + ACCESS_HINT)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


@dataclass
class JobResult:
    title: str
    summary: dict[str, object] = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    reports: list[Path] = field(default_factory=list)
    journal: Path | None = None
    cancelled: bool = False


# --- Parameter (auch für Profile speicherbar) ---------------------------------
@dataclass
class SyncParams:
    quellen: list[str]
    ziel: str
    fehlende_kopieren: bool = True
    ablage: str = "struktur"  # struktur | muster
    muster: str = ""
    dateiname: str = ""
    probelauf: bool = True
    bestaetigt: bool = False


@dataclass
class DupParams:
    ordner: str
    behalten: str = "aeltestes"
    aktion: str = "bericht"  # bericht | verschieben | papierkorb
    duplikat_ordner: str = "_Duplikate"
    probelauf: bool = True
    bestaetigt: bool = False


@dataclass
class SortParams:
    quelle: str
    ziel: str
    muster: str = "{Jahr}/{Jahr}-{Monat}-{Tag}"
    dateiname: str = ""
    modus: str = "copy"  # copy | move
    probelauf: bool = True
    bestaetigt: bool = False


PARAM_TYPES = {"abgleich": SyncParams, "duplikate": DupParams, "sortieren": SortParams}


def params_to_dict(p) -> dict:
    d = asdict(p)
    d.pop("bestaetigt", None)
    return d


def params_from_dict(job: str, d: dict):
    cls = PARAM_TYPES[job]
    known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
    return cls(**known)


# --- Basis ---------------------------------------------------------------------
class Job:
    name = "job"

    def __init__(self, cfg: Config, ctx: RunContext):
        self.cfg = cfg
        self.ctx = ctx
        self.cache = HashCache(cfg.data_dir / "cache.db")
        self.hasher = Hasher(cfg, self.cache)
        self.scanner = Scanner(cfg)
        self.method = cfg.get("vergleich.methode", "hash")
        self.last_scan_errors: list[str] = []
        # Zwischenstand – damit ein abgebrochener Lauf trotzdem einen (Teil-)Bericht bekommt
        self.res: JobResult | None = None
        self.ops: FileOps | None = None
        self.journal: Journal | None = None
        self.report_target: Path | None = None

    def comparison(self) -> tuple[str, bool]:
        """(Beschreibung der Vergleichsmethode, sicher?)"""
        if self.method == "name_groesse":
            return "nur Name + Größe – verschiedene Bilder können als gleich gelten", False
        if self.method == "name_groesse_datum":
            return "Name + Größe + Aufnahmedatum – verschiedene Bilder können als gleich gelten", False
        if not self.hasher.full:
            return "Schnellmodus: Größe + Anfang/Ende – minimal unsicher", False
        return "Inhalt (Prüfsumme)", True

    def note_comparison(self, res: JobResult) -> None:
        text, safe = self.comparison()
        res.summary["Vergleich"] = text if safe else "⚠️ " + text
        if not safe:
            self.ctx.message(f"⚠️ Vergleich: {text}", logging.WARNING)

    def review_names(self, meta: dict[Path, Meta], *patterns: str) -> None:
        """Namen aus den Metadaten ohne Zuordnung vor dem ersten Schritt bestätigen lassen und speichern."""
        if not self.cfg.get("metadaten.namen_pruefen", True):
            return
        used = {ph for pat in patterns if pat for ph in placeholders_in(pat)}
        todo = unassigned_names(self.cfg, list(meta.values()), [ph for ph in NAME_FIELDS if ph in used])
        if not todo:
            return
        chosen = self.ctx.review_names(todo)
        if chosen is None:  # keine Oberfläche (Kommandozeile)
            names = ", ".join(n for found in todo.values() for n in found)
            self.ctx.message(f"⚠️ Namen ohne Zuordnung (Einstellungen → Metadaten): {names}", logging.WARNING)
            return
        assign_names(self.cfg, list(meta.values()), chosen)
        self.cfg.save()
        self.ctx.message("Namen übernommen: " + ", ".join(f"{a} → {b}" for names in chosen.values()
                                                          for a, b in names.items()))

    def scan(self, root: Path, res: JobResult) -> list[FileEntry]:
        ensure_access(root)
        self.ctx.message(f"Durchsuche {root} …")
        entries, errors = self.scanner.scan(root, self.ctx)
        res.errors += errors
        self.last_scan_errors = errors
        self.ctx.message(f"  {len(entries)} Dateien ({human_size(sum(e.size for e in entries))})")
        if errors:
            self.ctx.message(f"  ⚠️ {len(errors)} nicht lesbar (z.B. {errors[0]})", logging.WARNING)
        return entries

    def split_empty(self, entries: list[FileEntry], res: JobResult) -> list[FileEntry]:
        """Leere Dateien (0 Byte) als defekt melden statt vergleichen – sie wären sonst alle „gleich“."""
        if not self.cfg.get("scan.leere_dateien_als_defekt", True):
            return entries
        empty = [e for e in entries if e.size == 0]
        for e in empty:
            res.rows.append({"Status": STATUS_TEXT["leer"], "Datei": str(e.path), "Größe": "0 B",
                             "Info": "nicht verglichen, nicht kopiert"})
        if empty:
            self.ctx.message(f"  ⚠️ {len(empty)} leere Datei(en) (0 Byte) – vermutlich defekt", logging.WARNING)
        return [e for e in entries if e.size > 0]

    def group(self, entries: list[FileEntry], res: JobResult, need=None) -> list[list[FileEntry]]:
        if self.method == "hash":
            return self.hasher.group_identical(entries, self.ctx, res.errors, need)
        groups = group_by_key(entries, lambda e: (e.path.name.lower(), e.size))
        if self.method == "name_groesse_datum":
            need = need or (lambda g: len(g) > 1)
            todo = [e for g in groups if need(g) for e in g]
            meta = MetadataReader(self.cfg).read(todo, self.ctx) if todo else {}
            out = [g for g in groups if not need(g)]
            for g in groups:
                if need(g):
                    out += group_by_key(g, lambda e: meta.get(e.path, Meta()).datum)
            return out
        return groups

    def finish(self, res: JobResult, ops: FileOps | None, journal: Journal | None,
               target: Path | None, dry_run: bool, columns: list[str]) -> JobResult:
        if ops:
            ops.flush_trash()
            fixed = set()
            for row in res.rows:  # Bericht darf nicht „In Papierkorb“ sagen, wenn der Finder es verweigert hat
                path = Path(row.get("Datei") or "")
                if row.get("Status") == STATUS_TEXT["papierkorb"] and path in ops.trash_failed:
                    row["Status"] = STATUS_TEXT["fehler"]
                    row["Info"] = f"Papierkorb fehlgeschlagen: {ops.trash_failed[path]}"
                    fixed.add(path)
            res.errors += [f"Papierkorb: {p}: {why}" for p, why in ops.trash_failed.items() if p not in fixed]
        if journal:
            journal.close()
            if journal.file.exists():
                res.journal = journal.file
        self.cache.close()
        if res.cancelled:
            res.summary["⚠️ Abgebrochen"] = "Lauf vorzeitig beendet – Bericht unvollständig"
        counts = Counter(r["Status"] for r in res.rows)
        res.summary.update({k: v for k, v in counts.most_common()})
        if res.errors:
            res.summary["Lese-/Schreibfehler"] = len(res.errors)
            res.rows += [{"Status": "Fehler", "Datei": e} for e in res.errors]
        res.summary["Probelauf"] = "ja" if dry_run else "nein"
        res.reports = write_report(self.cfg, report_dirs(self.cfg, target, dry_run), self.name,
                                   res.title, res.summary, columns, res.rows, dry_run)
        return res

    def finish_cancelled(self, dry_run: bool) -> JobResult:
        res = self.res or JobResult(self.name)
        res.cancelled = True
        return self.finish(res, self.ops, self.journal, self.report_target if self.ops else None,
                           dry_run, self.columns)

    def _row(self, r: Result, dry_run: bool, **extra) -> dict:
        text = STATUS_TEXT.get(r.status, r.status)
        if dry_run and r.status in DRY_TEXT:
            text = DRY_TEXT[r.status]
        return {"Status": text, "Datei": str(r.src),
                "Ziel": str(r.dst or ""), "Info": r.info, **extra}


# --- 1. Abgleich -------------------------------------------------------------
class SyncJob(Job):
    name = "abgleich"
    columns = ["Status", "Datei", "Größe", "Ziel", "Info"]

    def run(self, p: SyncParams) -> JobResult:
        res = self.res = JobResult("Abgleich Quelle → Ziel")
        target = Path(p.ziel).expanduser()
        sources = [Path(s).expanduser() for s in p.quellen]
        if p.ablage == "muster":
            validate(p.muster)
            if p.dateiname:
                validate(p.dateiname)
        ensure_access(target, must_exist=False)  # zuerst prüfen, bevor lange gescannt wird
        self.note_comparison(res)
        # Quelle im Ziel (z.B. Ziel/Neu): prüfen, ob die Dateien sonst irgendwo im Ziel liegen
        inner = [s for s in sources if s.is_relative_to(target)]
        src_entries: list[FileEntry] = []
        seen: set[Path] = set()
        for s in sources:
            entries = self.scan(s, res)
            if s not in inner:  # Ziel liegt in der Quelle: Ziel nicht als Quelle zählen
                entries = [e for e in entries if not e.path.is_relative_to(target)]
            for e in entries:  # überlappende Quellen (Ordner + Unterordner): jede Datei nur einmal
                if e.path not in seen:
                    seen.add(e.path)
                    src_entries.append(e)
        tgt_entries: list[FileEntry] = []
        target_incomplete = 0
        if target.exists():
            tgt_entries = self.scan(target, res)
            target_incomplete = len(self.last_scan_errors)
            if inner:
                tgt_entries = [e for e in tgt_entries if not any(e.path.is_relative_to(s) for s in inner)]
        else:
            self.ctx.message(f"Ziel existiert noch nicht und wird angelegt: {target}")
        if target_incomplete:
            msg = (f"{target_incomplete} Ordner/Dateien im Ziel nicht lesbar – dort vorhandene Dateien "
                   f"würden fälschlich als fehlend gelten.")
            if p.fehlende_kopieren and not p.probelauf:
                raise AccessError(msg + " Abbruch, damit nichts doppelt kopiert wird." + ACCESS_HINT)
            self.ctx.message("⚠️ " + msg, logging.WARNING)
        n_src_all = len(src_entries)
        src_entries = self.split_empty(src_entries, res)
        if self.cfg.get("scan.leere_dateien_als_defekt", True):
            tgt_entries = [e for e in tgt_entries if e.size > 0]
        src_ids = {id(e) for e in src_entries}

        def need(g):  # nur Gruppen mit Quelldatei und mehr als einem Mitglied sind relevant
            return len(g) > 1 and any(id(e) in src_ids for e in g)

        self.ctx.message("Vergleiche Inhalte …")
        if self.method == "hash":
            groups = self.hasher.match_sync(src_entries, tgt_entries, self.ctx, res.errors)
        else:
            groups = self.group(src_entries + tgt_entries, res, need)
        missing: list[FileEntry] = []
        sync_sidecars = self.cfg.get("abgleich.sidecars_abgleichen", True)
        sidecar_src: dict[Path, Path] = {}  # Ziel-Sidecar -> neueste passende Quell-Sidecar
        for g in groups:
            in_src = [e for e in g if id(e) in src_ids]
            in_tgt = [e for e in g if id(e) not in src_ids]
            if not in_src:
                continue
            in_src.sort(key=lambda e: -len(e.sidecars))  # die Kopie mit Sidecars bevorzugen
            if in_tgt and sync_sidecars:
                self._collect_sidecars(in_src, in_tgt[0], sidecar_src)
            if in_tgt:
                for e in in_src:
                    res.rows.append({"Status": STATUS_TEXT["vorhanden"], "Datei": str(e.path),
                                     "Größe": human_size(e.size), "Ziel": str(in_tgt[0].path)})
            elif any(in_src[0].path.is_relative_to(s) for s in inner):
                for e in in_src:  # liegt physisch schon im Ziel – nicht noch einmal kopieren
                    res.rows.append({"Status": STATUS_TEXT["nur_in_quelle"], "Datei": str(e.path),
                                     "Größe": human_size(e.size)})
            else:
                missing.append(in_src[0])
                for e in in_src[1:]:
                    res.rows.append({"Status": STATUS_TEXT["doppelt_in_quelle"], "Datei": str(e.path),
                                     "Größe": human_size(e.size), "Info": f"gleich wie {in_src[0].path}"})
        sidecar_todo = self._compare_sidecars(sidecar_src, res)
        res.summary = {**res.summary, "Quellen": ", ".join(map(str, sources)), "Ziel": str(target),
                       "Dateien in Quellen": n_src_all, "Dateien im Ziel": len(tgt_entries),
                       "Fehlen im Ziel": len(missing),
                       "Größe fehlend": human_size(sum(e.size for e in missing))}
        if target_incomplete:
            res.summary["⚠️ Ziel unvollständig gelesen"] = f"{target_incomplete} nicht lesbar – Ergebnis unsicher"
        if sidecar_todo:
            res.summary["Sidecars fehlend/geändert"] = len(sidecar_todo)
        self.ctx.message(f"→ {len(missing)} Dateien fehlen im Ziel ({res.summary['Größe fehlend']})"
                         + (f", {len(sidecar_todo)} Sidecars fehlen/geändert" if sidecar_todo else ""))

        ops = journal = None
        if p.fehlende_kopieren and (missing or sidecar_todo):
            journal = self.journal = None if p.probelauf else Journal(self.cfg.data_dir, self.name)
            ops = self.ops = FileOps(self.cfg, self.hasher, journal, p.probelauf)
            self.report_target = target
            meta = {}
            renderer = PatternRenderer(self.cfg)
            if p.ablage == "muster":
                meta = MetadataReader(self.cfg).read(missing, self.ctx)
                self.review_names(meta, p.muster, p.dateiname)
            levels = self.cfg.get("abgleich.quellordner_als_ebene", True)
            for i, e in enumerate(missing, 1):
                self.ctx.check()
                if p.ablage == "muster":
                    m = meta.get(e.path, Meta())
                    dst = target / renderer.folder(p.muster, e, m) / renderer.filename(p.dateiname, e, m)
                else:
                    dst = target / (e.label if levels else "") / e.rel
                r = ops.place(e, dst, "copy", overwrite_ok=p.bestaetigt)
                res.rows.append(self._row(r, p.probelauf, **{"Größe": human_size(e.size)}))
                self.ctx.progress("Kopieren" if not p.probelauf else "Vorschau", i, len(missing))
            replace = self.cfg.get("abgleich.sidecar_geaendert", "neuer_ersetzt") == "neuer_ersetzt"
            for kind, sc, dst in sidecar_todo:
                self.ctx.check()
                if kind == "fehlt":
                    r = ops.add_sidecar(sc, dst)
                elif kind == "geaendert" and replace:
                    r = ops.replace_sidecar(sc, dst)
                else:
                    r = Result("sidecar_geaendert" if kind == "geaendert" else "sidecar_ziel_neuer", sc, dst)
                res.rows.append(self._row(r, p.probelauf))
        else:
            for e in missing:
                res.rows.append({"Status": STATUS_TEXT["fehlt"], "Datei": str(e.path), "Größe": human_size(e.size)})
            for kind, sc, dst in sidecar_todo:
                status = {"fehlt": "sidecar_fehlt", "geaendert": "sidecar_geaendert"}.get(kind, "sidecar_ziel_neuer")
                res.rows.append({"Status": STATUS_TEXT[status], "Datei": str(sc), "Ziel": str(dst)})
        # Bericht nur ins Ziel, wenn dort auch etwas kopiert wurde – sonst in den Datenordner
        report_target = target if (ops and target.exists()) else None
        return self.finish(res, ops, journal, report_target, p.probelauf, self.columns)


    def _collect_sidecars(self, in_src: list[FileEntry], match: FileEntry, out: dict[Path, Path]) -> None:
        """Sidecars der Quelle der passenden Ziel-Sidecar zuordnen (bei mehreren Quellen: die neueste)."""
        for e in in_src:
            for sc in e.sidecars:
                same = [t for t in match.sidecars if t.suffix.lower() == sc.suffix.lower()]
                dst = same[0] if same else FileOps._sidecar_dst(sc, e.path, match.path)
                if not same and dst.exists():
                    continue  # gehört im Ziel zu einer anderen Datei (z.B. RAW neben JPG)
                try:
                    if dst not in out or sc.stat().st_mtime > out[dst].stat().st_mtime:
                        out[dst] = sc
                except OSError:
                    continue

    def _compare_sidecars(self, pairs: dict[Path, Path], res: JobResult) -> list[tuple[str, Path, Path]]:
        """[(art, quelle, ziel)] mit art = fehlt | geaendert | ziel_neuer; identische entfallen."""
        todo = []
        for dst, sc in sorted(pairs.items()):
            try:
                if not dst.exists():
                    todo.append(("fehlt", sc, dst))
                    continue
                a, b = sc.stat(), dst.stat()
                if a.st_size == b.st_size and self.hasher.full_hash_file(sc) == self.hasher.full_hash_file(dst):
                    continue
                # 2 s Spielraum: exFAT/FAT speichern Zeiten nur auf 2 Sekunden genau
                todo.append(("geaendert" if a.st_mtime > b.st_mtime + 2 else "ziel_neuer", sc, dst))
            except OSError as err:
                res.errors.append(f"{sc}: {err.strerror or err}")
        return todo


# --- 2. Duplikate ------------------------------------------------------------
KEEP_RULES = {
    "aeltestes": lambda e: (e.mtime, str(e.path)),
    "neuestes": lambda e: (-e.mtime, str(e.path)),
    "kuerzester_pfad": lambda e: (len(str(e.path)), str(e.path)),
    "laengster_pfad": lambda e: (-len(str(e.path)), str(e.path)),
    "erster": lambda e: str(e.path),
}


class DupJob(Job):
    name = "duplikate"
    columns = ["Gruppe", "Status", "Datei", "Größe", "Ziel", "Info"]

    def run(self, p: DupParams) -> JobResult:
        res = self.res = JobResult("Duplikate im Ordner")
        root = Path(p.ordner).expanduser()
        self.note_comparison(res)
        dup_root = root / p.duplikat_ordner
        entries = self.scan(root, res)
        n_all = len(entries)
        # Schon aussortierte Duplikate nicht erneut finden (auch bei eigenem Ordnernamen)
        entries = [e for e in entries if not e.path.is_relative_to(dup_root)]
        entries = self.split_empty(entries, res)
        self.ctx.message("Suche Duplikate …")
        groups = sorted((g for g in self.group(entries, res) if len(g) > 1),
                        key=lambda g: str(min(e.path for e in g)))
        rule = KEEP_RULES.get(p.behalten, KEEP_RULES["aeltestes"])
        journal = self.journal = None if (p.probelauf or p.aktion == "bericht") else Journal(self.cfg.data_dir, self.name)
        ops = self.ops = FileOps(self.cfg, self.hasher, journal, p.probelauf)
        self.report_target = root if p.aktion != "bericht" else None
        wasted = 0
        for n, g in enumerate(groups, 1):
            self.ctx.check()
            # Geschützte Dateien (Bibliotheken) bevorzugt behalten – die dürfen eh nicht weg
            g.sort(key=lambda e: (not ops.is_protected(e.path), rule(e)))
            keep, dups = g[0], g[1:]
            res.rows.append({"Gruppe": n, "Status": STATUS_TEXT["behalten"], "Datei": str(keep.path),
                             "Größe": human_size(keep.size)})
            for e in dups:
                wasted += e.size
                if p.aktion == "verschieben":
                    r = ops.set_aside(e, dup_root, root)
                elif p.aktion == "papierkorb" and (p.bestaetigt or p.probelauf):
                    r = ops.trash(e)
                else:
                    r = Result("duplikat", e.path, None)
                res.rows.append({"Gruppe": n, **self._row(r, p.probelauf, **{"Größe": human_size(e.size)})})
            self.ctx.progress("Duplikate behandeln", n, len(groups))
        res.summary = {**res.summary, "Ordner": str(root), "Dateien": n_all, "Duplikat-Gruppen": len(groups),
                       "Überzählige Dateien": sum(len(g) - 1 for g in groups),
                       "Platz durch Duplikate": human_size(wasted), "Aktion": p.aktion,
                       "Behalten-Regel": p.behalten}
        if empty := sum(1 for r in res.rows if r["Status"] == STATUS_TEXT["leer"]):
            res.summary["Leere Dateien (defekt)"] = empty
        self.ctx.message(f"→ {len(groups)} Gruppen, {res.summary['Überzählige Dateien']} überzählig "
                         f"({res.summary['Platz durch Duplikate']})")
        return self.finish(res, ops, journal, root if p.aktion != "bericht" else None,
                           p.probelauf, self.columns)


# --- 3. Sortieren ------------------------------------------------------------
class SortJob(Job):
    name = "sortieren"
    columns = ["Status", "Datei", "Ziel", "Datum", "Datumsquelle", "Kamera", "Info"]

    def run(self, p: SortParams) -> JobResult:
        res = self.res = JobResult("Sortieren nach Metadaten")
        validate(p.muster)
        if p.dateiname:
            validate(p.dateiname)
        src, target = Path(p.quelle).expanduser(), Path(p.ziel).expanduser()
        ensure_access(target, must_exist=False)
        entries = self.scan(src, res)
        if target != src and target.is_relative_to(src):
            entries = [e for e in entries if not e.path.is_relative_to(target)]
        meta = MetadataReader(self.cfg).read(entries, self.ctx)
        self.review_names(meta, p.muster, p.dateiname)
        renderer = PatternRenderer(self.cfg)
        journal = self.journal = None if p.probelauf else Journal(self.cfg.data_dir, self.name)
        ops = self.ops = FileOps(self.cfg, self.hasher, journal, p.probelauf)
        self.report_target = target
        mode = "move" if p.modus == "move" else "copy"
        for i, e in enumerate(sorted(entries, key=lambda e: str(e.path)), 1):
            self.ctx.check()
            m = meta.get(e.path, Meta())
            dst = target / renderer.folder(p.muster, e, m) / renderer.filename(p.dateiname, e, m)
            extra = {"Datum": m.datum.strftime("%Y-%m-%d %H:%M:%S") if m.datum else "",
                     "Datumsquelle": m.datum_quelle, "Kamera": m.kamera}
            if dst == e.path:
                res.rows.append({"Status": STATUS_TEXT["bereits_richtig"], "Datei": str(e.path), **extra})
                continue
            r = ops.place(e, dst, mode, overwrite_ok=p.bestaetigt)
            row = self._row(r, p.probelauf, **extra)
            if e.size == 0 and self.cfg.get("scan.leere_dateien_als_defekt", True):
                row["Info"] = (row["Info"] + " – leere Datei (0 Byte), vermutlich defekt").lstrip(" –")
            res.rows.append(row)
            self.ctx.progress("Sortieren" if not p.probelauf else "Vorschau", i, len(entries))
        res.summary = {"Quelle": str(src), "Ziel": str(target), "Muster": p.muster,
                       "Dateiname": p.dateiname or "(unverändert)", "Modus": p.modus,
                       "Dateien": len(entries),
                       "Ohne Aufnahmedatum (EXIF)": sum(1 for m in meta.values()
                                                        if m.datum_quelle in ("", "datei_mtime"))}
        return self.finish(res, ops, journal, target, p.probelauf, self.columns)


JOBS = {"abgleich": SyncJob, "duplikate": DupJob, "sortieren": SortJob}


def run_job(cfg: Config, ctx: RunContext, job: str, params) -> JobResult | None:
    j = JOBS[job](cfg, ctx)
    try:
        return j.run(params)
    except Declined:
        ctx.message("Abgebrochen – nichts verändert.")
        return None
    except Cancelled:
        ctx.message("Abgebrochen – Teilbericht wird geschrieben.")
        return j.finish_cancelled(params.probelauf)
    finally:
        j.cache.close()
