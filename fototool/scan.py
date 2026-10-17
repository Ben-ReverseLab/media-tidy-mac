"""Ordner rekursiv nach Mediendateien durchsuchen."""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .context import RunContext


@dataclass
class FileEntry:
    path: Path
    size: int
    mtime: float
    root: Path  # Quellordner, aus dem die Datei stammt
    sidecars: list[Path] = field(default_factory=list)
    rel_override: Path | None = None  # Pfad im Ziel für Dateien aus Bibliotheken
    source_label: str = ""  # Name der Quelle im Ziel (vom Scanner gesetzt)

    @property
    def rel(self) -> Path:
        """Relativer Pfad zur Quelle; Bibliotheks-Interna (z.B. .../originals) entfallen."""
        return self.rel_override or self.path.relative_to(self.root)

    @property
    def label(self) -> str:
        """Name der Quelle (Bibliotheken ohne Endung, damit im Ziel kein Paket entsteht)."""
        return self.source_label or self.root.name

    @property
    def ext(self) -> str:
        return self.path.suffix.lower().lstrip(".")


class Scanner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.exts = cfg.extensions()
        self.sidecar_exts = {e.lower() for e in cfg.get("dateien.sidecar", [])}
        self.ignore = cfg.get("scan.ignorieren", [])
        self.skip_hidden = cfg.get("scan.versteckte_ignorieren", True)
        self.follow = cfg.get("scan.symlinks_folgen", False)
        self.libraries = cfg.get("scan.bibliotheken", [])

    def _ignored(self, name: str) -> bool:
        if self.skip_hidden and name.startswith("."):
            return True
        return any(fnmatch.fnmatch(name, pat) for pat in self.ignore)

    def _library_rule(self, name: str) -> dict | None:
        for rule in self.libraries:
            if name.lower().endswith(rule.get("endung", "\0").lower()):
                return rule
        return None

    def _walk(self, directory: Path, ctx: RunContext, errors: list[str], rel_base: Path | None = None):
        """Liefert (DirEntry, rel_base). rel_base: (Ordner im Paket, Ersatzpfad) oder None."""
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError as e:
            errors.append(f"{directory}: {e.strerror or e}")
            return
        for e in entries:
            ctx.check()
            if self._ignored(e.name):
                continue
            try:
                if e.is_dir(follow_symlinks=self.follow):
                    rule = self._library_rule(e.name)
                    if rule:
                        yield from self._walk_library(Path(e.path), rule, ctx, errors)
                    else:
                        yield from self._walk(Path(e.path), ctx, errors, rel_base)
                elif e.is_file(follow_symlinks=self.follow):
                    yield e, rel_base
            except OSError as err:
                errors.append(f"{e.path}: {err.strerror or err}")

    def _walk_library(self, lib: Path, rule: dict, ctx: RunContext, errors: list[str]):
        subs = rule.get("unterordner", [])
        if not subs:
            return
        try:
            matches = sorted({p for pat in subs for p in lib.glob(pat) if p.is_dir()})
        except OSError as e:
            errors.append(f"{lib}: {e.strerror or e}")
            return
        for sub in matches:
            yield from self._walk(sub, ctx, errors, (sub, lib.with_suffix("")))

    def scan(self, root: Path, ctx: RunContext) -> tuple[list[FileEntry], list[str]]:
        """Liefert (Mediendateien, Fehlermeldungen)."""
        root = Path(root).expanduser()
        errors: list[str] = []
        media: list[FileEntry] = []
        sidecars: dict[Path, list[Path]] = {}
        rule = self._library_rule(root.name)
        # Nur Bibliotheken verlieren ihre Endung – "Urlaub 2025.08" bleibt "Urlaub 2025.08"
        label = root.with_suffix("").name if rule else root.name
        walker = self._walk_library(root, rule, ctx, errors) if rule else self._walk(root, ctx, errors)
        for n, (e, rel_base) in enumerate(walker, 1):
            p = Path(e.path)
            ext = p.suffix.lower().lstrip(".")
            if ext in self.sidecar_exts:
                sidecars.setdefault(_key(p.with_suffix("")), []).append(p)
                continue
            if ext not in self.exts:
                continue
            try:
                st = e.stat(follow_symlinks=self.follow)
            except OSError as err:
                errors.append(f"{p}: {err.strerror or err}")
                continue
            rel = None
            if rel_base:
                inner, replacement = rel_base
                rel = p.relative_to(inner)
                if replacement != root.with_suffix(""):  # Bibliothek liegt innerhalb der Quelle
                    rel = replacement.relative_to(root) / rel
            media.append(FileEntry(p, st.st_size, st.st_mtime, root, rel_override=rel, source_label=label))
            if n % 500 == 0:
                ctx.progress(f"Durchsuche {root.name}", len(media), 0)
        self._assign_sidecars(media, sidecars)
        return media, errors

    def _assign_sidecars(self, media: list[FileEntry], sidecars: dict[str, list[Path]]) -> None:
        """"IMG_1.CR3.xmp" gehört genau zu IMG_1.CR3. "IMG_1.xmp" kann zu mehreren Dateien passen
        (RAW+JPG, Live Photo HEIC+MOV) – dann bekommt sie nur eine: RAW vor Foto vor Video,
        damit z.B. ein JPG-Duplikat nie die XMP-Bearbeitung der RAW mitnimmt."""
        by_stem: dict[str, list[FileEntry]] = {}
        for m in media:
            by_stem.setdefault(_key(m.path.with_suffix("")), []).append(m)
        for stem, members in by_stem.items():
            owner = min(members, key=lambda m: (TYPE_PRIORITY.get(self.cfg.file_type(m.ext), 9), m.path.name))
            for m in members:
                m.sidecars = (sidecars.get(stem, []) if m is owner else []) + sidecars.get(_key(m.path), [])


TYPE_PRIORITY = {"RAW": 0, "Foto": 1, "Video": 2}


def _key(p: Path) -> str:
    """Vergleichsschlüssel ohne Groß-/Kleinschreibung (macOS-Dateisysteme unterscheiden sie nicht)."""
    return str(p).lower()
