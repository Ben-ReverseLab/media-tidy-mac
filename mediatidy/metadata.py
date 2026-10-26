"""Read metadata (capture date, camera, ...) with exiftool."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Config
from .context import RunContext
from .i18n import t
from .scan import FileEntry

# Placeholders filled with names from the metadata -> Meta attribute; mapping in metadata.<attribute>_names
NAME_FIELDS = {"Camera": "camera", "Make": "make", "Lens": "lens"}

EXIFTOOL_CANDIDATES = ["/opt/homebrew/bin/exiftool", "/usr/local/bin/exiftool", "/usr/bin/exiftool"]
SYSTEM_PERL = "/usr/bin/perl"


def bundled_exiftool() -> Path | None:
    """exiftool shipped inside the app (packaging/build.sh puts it into the bundle); None when run from source."""
    if not getattr(sys, "frozen", False):
        return None
    script = Path(getattr(sys, "_MEIPASS", "")) / "exiftool" / "exiftool"
    return script if script.is_file() else None


def find_exiftool(cfg: Config) -> list[str] | None:
    """Command to start exiftool: configured path, then the bundled copy (run by the system Perl), then PATH
    and the usual install locations. None = not found."""
    configured = (cfg.get("paths.exiftool") or "").strip()
    if configured:
        p = os.path.expanduser(configured)
        return [p] if os.access(p, os.X_OK) else None
    bundled = bundled_exiftool()
    if bundled and os.access(SYSTEM_PERL, os.X_OK):
        return [SYSTEM_PERL, str(bundled)]
    found = shutil.which("exiftool") or next((p for p in EXIFTOOL_CANDIDATES if os.access(p, os.X_OK)), None)
    return [found] if found else None


@dataclass
class Meta:
    date: datetime | None = None
    date_source: str = ""
    camera: str = ""
    make: str = ""
    lens: str = ""
    raw: dict[str, str] = field(default_factory=dict)  # placeholder -> name as written in the file


def name_map(cfg: Config, placeholder: str) -> dict[str, str]:
    return cfg.get(f"metadata.{NAME_FIELDS[placeholder]}_names", {}) or {}


def unassigned_names(cfg: Config, metas: list[Meta], placeholders: list[str]) -> dict[str, dict[str, tuple[str, int]]]:
    """placeholder -> {name in file: (proposal, number of files)} for names without a mapping in the config."""
    out: dict[str, dict[str, tuple[str, int]]] = {}
    for ph in placeholders:
        known, found = name_map(cfg, ph), {}
        for m in metas:
            raw = m.raw.get(ph, "")
            if raw and raw not in known:
                proposal, n = found.get(raw, (getattr(m, NAME_FIELDS[ph]), 0))
                found[raw] = (proposal, n + 1)
        if found:
            out[ph] = dict(sorted(found.items()))
    return out


def assign_names(cfg: Config, metas: list[Meta], chosen: dict[str, dict[str, str]]) -> None:
    """Put the mapping into the config (without saving) and apply it to the metadata already read."""
    for ph, names in chosen.items():
        cfg.set(f"metadata.{NAME_FIELDS[ph]}_names", {**name_map(cfg, ph), **names})
        for m in metas:
            if m.raw.get(ph) in names:
                setattr(m, NAME_FIELDS[ph], names[m.raw[ph]])


def parse_exif_date(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    s = value.strip()[:19]
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y:%m:%d"):
        try:
            d = datetime.strptime(s if fmt != "%Y:%m:%d" else s[:10], fmt)
        except ValueError:
            continue
        return None if d.year < 1900 else d  # "0000:00:00 00:00:00"
    return None


class MetadataReader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.exiftool = find_exiftool(cfg)
        self.date_fields = cfg.get("metadata.date_fields", [])
        self.camera_fields = cfg.get("metadata.camera_fields", [])
        self.make_fields = cfg.get("metadata.make_fields", [])
        self.lens_fields = cfg.get("metadata.lens_fields", [])
        self.names = {ph: name_map(cfg, ph) for ph in NAME_FIELDS}
        self.strip_make = cfg.get("metadata.strip_make_from_camera", False)
        self.batch = max(1, int(cfg.get("metadata.batch_size", 200)))

    def _tags(self) -> list[str]:
        fields = self.date_fields + self.camera_fields + self.make_fields + self.lens_fields
        return [f for f in dict.fromkeys(fields) if f != "file_mtime"]

    def _run_exiftool(self, paths: list[Path]) -> dict[str, dict]:
        if not self.exiftool or not paths:
            return {}
        with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as f:
            f.write("\n".join(str(p) for p in paths))
            argfile = f.name
        try:
            cmd = [*self.exiftool, "-json", "-fast", "-charset", "filename=utf8", "-api", "largefilesupport=1"]
            cmd += [f"-{tag}" for tag in self._tags()] + ["-@", argfile]
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
            if not res.stdout.strip():
                return {}
            return {item.get("SourceFile", ""): item for item in json.loads(res.stdout)}
        except (OSError, json.JSONDecodeError):
            return {}
        finally:
            os.unlink(argfile)

    def _first(self, raw: dict, fields: list[str]) -> str:
        for f in fields:
            v = raw.get(f)
            if v not in (None, ""):
                return str(v).strip()
        return ""

    def _build(self, e: FileEntry, raw: dict) -> Meta:
        m = Meta()
        for f in self.date_fields:
            if f == "file_mtime":
                m.date, m.date_source = datetime.fromtimestamp(e.mtime), f
                break
            d = parse_exif_date(raw.get(f))
            if d:
                m.date, m.date_source = d, f
                break
        m.make = self._first(raw, self.make_fields)
        m.camera = self._first(raw, self.camera_fields)
        m.lens = self._first(raw, self.lens_fields)
        m.raw = {ph: getattr(m, attr) for ph, attr in NAME_FIELDS.items()}
        if self.strip_make and m.make:
            first_word = m.make.split()[0]
            if m.camera.lower().startswith(first_word.lower()):
                m.camera = m.camera[len(first_word):].strip()
        for ph, attr in NAME_FIELDS.items():
            if m.raw[ph] in self.names[ph]:
                setattr(m, attr, self.names[ph][m.raw[ph]])
        return m

    def read(self, entries: list[FileEntry], ctx: RunContext) -> dict[Path, Meta]:
        out: dict[Path, Meta] = {}
        if not self.exiftool:
            ctx.message("⚠️ " + t("meta.no_exiftool"))
        for i in range(0, len(entries), self.batch):
            ctx.check()
            chunk = entries[i:i + self.batch]
            raw = self._run_exiftool([e.path for e in chunk])
            for e in chunk:
                out[e.path] = self._build(e, raw.get(str(e.path), {}))
            ctx.progress("metadata", min(i + self.batch, len(entries)), len(entries))
        return out
