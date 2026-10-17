"""Dateiaktionen: kopieren, verschieben, Papierkorb – mit Probelauf, Journal, Schutz."""
from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .hashing import Hasher
from .journal import Journal
from .scan import FileEntry

TMP_SUFFIX = ".fototool-tmp"


@dataclass
class Result:
    status: str  # kopiert | verschoben | identisch_vorhanden | kollision_uebersprungen | ueberschrieben | geschuetzt | fehler
    src: Path
    dst: Path | None
    info: str = ""


class ProtectedError(Exception):
    pass


class FileOps:
    def __init__(self, cfg: Config, hasher: Hasher, journal: Journal | None, dry_run: bool):
        self.cfg = cfg
        self.hasher = hasher
        self.journal = journal
        self.dry_run = dry_run
        self.collision = cfg.get("aktionen.kollision", "suffix")
        self.suffix = cfg.get("aktionen.suffix_muster", "_{n}")
        self.verify = cfg.get("aktionen.kopie_pruefen", True)
        self.keep_times = cfg.get("aktionen.zeitstempel_erhalten", True)
        self.protected = cfg.get("aktionen.geschuetzt", [])
        self.with_sidecars = cfg.get("dateien.sidecars_mitnehmen", True)
        self.reserved: set[Path] = set()  # im Probelauf geplante Ziele
        self._trash_queue: list[Path] = []
        self.trash_failed: dict[Path, str] = {}  # Pfad -> Grund, falls der Finder nicht löschen konnte
        self._backup_n = 0

    # --- Hilfen --------------------------------------------------------------
    def is_protected(self, p: Path) -> bool:
        s = str(p)
        return any(fnmatch.fnmatch(s, pat) for pat in self.protected)

    def _exists(self, p: Path) -> bool:
        return p in self.reserved or p.exists()

    def _same_content(self, a: Path, b: Path) -> bool:
        if b in self.reserved and not b.exists():
            return False
        try:
            if a.stat().st_size != b.stat().st_size:
                return False
            return self.hasher.full_hash_file(a) == self.hasher.full_hash_file(b)
        except OSError:
            return False

    def _with_suffix_n(self, dst: Path) -> Path:
        n = 1
        while True:
            cand = dst.with_name(dst.stem + self.suffix.format(n=n) + dst.suffix)
            if not self._exists(cand):
                return cand
            n += 1

    def _mkdirs(self, d: Path) -> None:
        """Ordner anlegen und neu angelegte im Journal vermerken (Rückgängig entfernt sie wieder)."""
        new = []
        p = d
        while not p.exists() and p != p.parent:
            new.append(p)
            p = p.parent
        d.mkdir(parents=True, exist_ok=True)
        if self.journal:
            for p in reversed(new):
                self.journal.record("ordner_angelegt", p, None)

    def _copy_file(self, src: Path, dst: Path) -> None:
        self._mkdirs(dst.parent)
        tmp = dst.with_name(dst.name + TMP_SUFFIX)
        try:
            if self.keep_times:
                shutil.copy2(src, tmp)
            else:
                shutil.copyfile(src, tmp)
            if self.verify and self.hasher.full_hash_file(src) != self.hasher.full_hash_file(tmp):
                raise OSError("Prüfsumme nach dem Kopieren stimmt nicht")
            os.replace(tmp, dst)
        finally:
            if tmp.exists():
                tmp.unlink()

    def _move_file(self, src: Path, dst: Path) -> None:
        if self.is_protected(src):
            raise ProtectedError(f"Geschützt, wird nicht verschoben: {src}")
        self._mkdirs(dst.parent)
        if os.stat(src).st_dev == os.stat(dst.parent).st_dev:
            if dst.exists():
                raise FileExistsError(str(dst))
            os.rename(src, dst)
            return
        self._copy_file(src, dst)  # anderes Laufwerk: kopieren + prüfen + löschen
        os.unlink(src)

    @staticmethod
    def _sidecar_dst(sidecar: Path, src: Path, dst: Path) -> Path:
        if sidecar.name.lower().startswith(src.name.lower() + "."):  # IMG.CR3.xmp
            return dst.with_name(dst.name + sidecar.suffix)
        return dst.with_name(dst.stem + sidecar.suffix)  # IMG.xmp

    # --- Öffentliche Aktion --------------------------------------------------
    def place(self, e: FileEntry, dst: Path, mode: str, overwrite_ok: bool = False) -> Result:
        """Datei an dst kopieren (mode="copy") oder verschieben (mode="move")."""
        src = e.path
        if mode == "move" and self.is_protected(src):
            return Result("geschuetzt", src, dst, "Bibliothek/geschützter Ordner – nur Kopieren möglich")
        try:
            status_overwrite = False
            if self._exists(dst):
                if self._same_content(src, dst):
                    info = "Quelle bleibt liegen (ist ein Duplikat) – aufräumen über „Duplikate“" if mode == "move" else ""
                    return Result("identisch_vorhanden", src, dst, info)
                if self.collision == "ueberspringen":
                    return Result("kollision_uebersprungen", src, dst, "anderer Inhalt mit gleichem Namen")
                if self.collision == "ueberschreiben" and overwrite_ok:
                    status_overwrite = True
                else:
                    dst = self._with_suffix_n(dst)
            self.reserved.add(dst)
            status = "verschoben" if mode == "move" else "kopiert"
            if status_overwrite:
                status = "ueberschrieben"
            if not self.dry_run:
                if status_overwrite and dst.exists():
                    if self.journal:
                        self.journal.record("ueberschrieben", src, dst)
                    if mode == "move":
                        if self.is_protected(src):
                            raise ProtectedError(str(src))
                        dst.unlink()
                        self._move_file(src, dst)
                    else:
                        self._copy_file(src, dst)
                elif mode == "move":
                    self._move_file(src, dst)
                    if self.journal:
                        self.journal.record("verschoben", src, dst)
                else:
                    self._copy_file(src, dst)
                    if self.journal:
                        self.journal.record("kopiert", src, dst)
            info = ""
            if self.with_sidecars and e.sidecars:
                info = self._place_sidecars(e, dst, mode)
            return Result(status, src, dst, info)
        except (OSError, ProtectedError) as err:
            return Result("fehler", src, dst, str(err))

    def _place_sidecars(self, e: FileEntry, dst: Path, mode: str) -> str:
        names = []
        for sc in e.sidecars:
            sdst = self._sidecar_dst(sc, e.path, dst)
            if self._exists(sdst):
                continue
            self.reserved.add(sdst)
            names.append(sc.name)
            if self.dry_run or not sc.exists():
                continue
            if mode == "move" and not self.is_protected(sc):
                self._move_file(sc, sdst)
                if self.journal:
                    self.journal.record("verschoben", sc, sdst)
            else:
                self._copy_file(sc, sdst)
                if self.journal:
                    self.journal.record("kopiert", sc, sdst)
        return ("+ " + ", ".join(names)) if names else ""

    def set_aside(self, e: FileEntry, dup_root: Path, base: Path) -> Result:
        """Duplikat nach dup_root verschieben, relative Struktur zu base bleibt erhalten."""
        if self.is_protected(e.path):
            return Result("geschuetzt", e.path, None, "geschützt – nicht verschoben")
        try:
            rel = e.path.relative_to(base)
        except ValueError:
            rel = e.rel
        dst = dup_root / rel
        if self._exists(dst):
            dst = self._with_suffix_n(dst)
        self.reserved.add(dst)
        if not self.dry_run:
            try:
                self._move_file(e.path, dst)
                if self.journal:
                    self.journal.record("duplikat_verschoben", e.path, dst)
                for sc in e.sidecars:
                    sdst = self._sidecar_dst(sc, e.path, dst)
                    if sc.exists() and not sdst.exists():
                        self._move_file(sc, sdst)
                        if self.journal:
                            self.journal.record("duplikat_verschoben", sc, sdst)
            except (OSError, ProtectedError) as err:
                return Result("fehler", e.path, dst, str(err))
        return Result("verschoben", e.path, dst)

    # --- Sidecars bereits gesicherter Dateien (Abgleich) -------------------------
    def add_sidecar(self, sc: Path, dst: Path) -> Result:
        """Fehlende Sidecar neben die schon gesicherte Datei kopieren."""
        if self._exists(dst):
            return Result("identisch_vorhanden", sc, dst)
        self.reserved.add(dst)
        if not self.dry_run:
            try:
                self._copy_file(sc, dst)
            except OSError as err:
                return Result("fehler", sc, dst, str(err))
            if self.journal:
                self.journal.record("kopiert", sc, dst)
        return Result("sidecar_ergaenzt", sc, dst)

    def replace_sidecar(self, sc: Path, dst: Path) -> Result:
        """Geänderte Sidecar im Ziel ersetzen; die alte Version wird für Rückgängig aufbewahrt."""
        if not self.dry_run:
            if self.journal:
                backup_dir = self.journal.dir / (self.journal.file.stem + "_alte_sidecars")
            else:
                backup_dir = self.cfg.data_dir / "alte_sidecars"
            try:
                backup_dir.mkdir(parents=True, exist_ok=True)
                self._backup_n += 1
                backup = backup_dir / f"{self._backup_n:05d}_{dst.name}"
                shutil.copy2(dst, backup)
                self._copy_file(sc, dst)
            except OSError as err:
                return Result("fehler", sc, dst, str(err))
            if self.journal:
                self.journal.record("sidecar_aktualisiert", sc, dst, sicherung=str(backup))
        return Result("sidecar_aktualisiert", sc, dst)

    def trash(self, e: FileEntry) -> Result:
        if self.is_protected(e.path):
            return Result("geschuetzt", e.path, None, "geschützt – nicht gelöscht")
        if not self.dry_run:
            self._trash_queue.append(e.path)
            self._trash_queue.extend(sc for sc in e.sidecars if sc.exists())
            if len(self._trash_queue) >= 50:
                self.flush_trash()
        return Result("papierkorb", e.path, None)

    def flush_trash(self) -> list[str]:
        """Dateien über den Finder in den Papierkorb legen (wiederherstellbar).
        Was nicht gelöscht werden konnte, steht danach in self.trash_failed."""
        errors: list[str] = []
        queue, self._trash_queue = self._trash_queue, []
        if not queue:
            return errors
        # "POSIX file" muss außerhalb des Finder-Blocks entstehen, sonst Fehler -1728.
        # Jede Datei einzeln, damit ein Fehler nicht den Rest verhindert.
        script = ['on run argv', 'set failed to {}', 'repeat with p in argv', 'try',
                  'set f to POSIX file (p as text)', 'tell application "Finder" to delete f',
                  'on error msg', 'set end of failed to (p as text) & tab & msg', 'end try', 'end repeat',
                  "set AppleScript's text item delimiters to linefeed", 'return failed as text', 'end run']
        cmd = ["osascript"] + sum((["-e", s] for s in script), []) + [str(p) for p in queue]
        failed: dict[Path, str] = {}
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60 + 5 * len(queue))
            if res.returncode != 0:
                failed = {p: res.stderr.strip() or "Finder-Fehler" for p in queue}
            for line in res.stdout.splitlines():
                path, _, msg = line.partition("\t")
                if path:
                    failed[Path(path)] = msg.strip()
        except (OSError, subprocess.TimeoutExpired) as err:
            failed = {p: str(err) for p in queue}
        for p in queue:
            if p not in failed and p.exists():  # zur Sicherheit: liegt noch da = nicht gelöscht
                failed[p] = "Datei liegt noch am Ort"
            if p in failed:
                self.trash_failed[p] = failed[p]
                errors.append(f"Papierkorb: {p}: {failed[p]}")
            elif self.journal:
                self.journal.record("papierkorb", p, None)
        return errors
