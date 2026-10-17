"""Journal jeder Dateiaktion -> Rückgängig machen + Wiederaufnahme."""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from .context import RunContext


class Journal:
    def __init__(self, data_dir: Path, job: str):
        self.dir = data_dir / "journal"
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.file = self.dir / f"{stamp}_{job}.jsonl"
        self._fh = None

    def record(self, action: str, src: Path, dst: Path | None, **extra) -> None:
        if self._fh is None:
            # Eigene Datei je Lauf – auch wenn zwei Läufe in derselben Sekunde starten
            base, n = self.file.stem, 2
            while True:
                try:
                    self._fh = self.file.open("x", encoding="utf-8")
                    break
                except FileExistsError:
                    self.file = self.dir / f"{base}_{n}.jsonl"
                    n += 1
        entry = {"t": time.time(), "aktion": action, "von": str(src), "nach": str(dst) if dst else None, **extra}
        self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def list_journals(data_dir: Path) -> list[Path]:
    d = data_dir / "journal"
    return sorted(d.glob("*.jsonl"), reverse=True) if d.exists() else []


def read_entries(file: Path) -> list[dict]:
    out = []
    for line in file.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # abgebrochene letzte Zeile
    return out


def undo(file: Path, ctx: RunContext, dry_run: bool = False) -> tuple[int, list[str]]:
    """Macht einen Lauf rückgängig: Kopien löschen, Verschiebungen zurückverschieben."""
    entries = [e for e in read_entries(file) if not e.get("rueckgaengig")]
    done, problems = 0, []
    for i, e in enumerate(reversed(entries), 1):
        ctx.check()
        src, dst = Path(e["von"]), Path(e["nach"]) if e.get("nach") else None
        act = e["aktion"]
        try:
            if act == "kopiert" and dst:
                if dst.exists():
                    if not dry_run:
                        dst.unlink()
                    done += 1
            elif act in ("verschoben", "duplikat_verschoben") and dst:
                if src.exists():
                    problems.append(f"Ziel für Rückverschiebung belegt: {src}")
                elif dst.exists():
                    if not dry_run:
                        src.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(dst), str(src))
                    done += 1
                else:
                    problems.append(f"Datei fehlt: {dst}")
            elif act == "sidecar_aktualisiert" and dst:
                backup = Path(e.get("sicherung") or "")
                if backup.is_file():
                    if not dry_run:
                        shutil.copy2(backup, dst)
                    done += 1
                else:
                    problems.append(f"Alte Version fehlt, nicht wiederherstellbar: {dst}")
            elif act == "papierkorb":
                problems.append(f"Aus Papierkorb manuell wiederherstellen: {src.name}")
            elif act == "ueberschrieben":
                problems.append(f"Überschriebene Datei nicht wiederherstellbar: {dst}")
        except OSError as err:
            problems.append(f"{dst}: {err}")
        ctx.progress("Rückgängig", i, len(entries))
    if not dry_run:
        with file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), "aktion": "lauf_rueckgaengig", "von": str(file),
                                 "rueckgaengig": True}, ensure_ascii=False) + "\n")
        file.rename(file.with_name(file.stem + "_RUECKGAENGIG.jsonl"))
    return done, problems
