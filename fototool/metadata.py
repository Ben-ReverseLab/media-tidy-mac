"""Metadaten (Aufnahmedatum, Kamera, ...) per exiftool lesen."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config
from .context import RunContext
from .scan import FileEntry

EXIFTOOL_CANDIDATES = ["/opt/homebrew/bin/exiftool", "/usr/local/bin/exiftool", "/usr/bin/exiftool"]


def find_exiftool(cfg: Config) -> str | None:
    configured = (cfg.get("pfade.exiftool") or "").strip()
    if configured:
        p = os.path.expanduser(configured)
        return p if os.access(p, os.X_OK) else None
    found = shutil.which("exiftool")
    if found:
        return found
    return next((p for p in EXIFTOOL_CANDIDATES if os.access(p, os.X_OK)), None)


@dataclass
class Meta:
    datum: datetime | None = None
    datum_quelle: str = ""
    kamera: str = ""
    hersteller: str = ""
    objektiv: str = ""


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
        self.date_fields = cfg.get("metadaten.datum_felder", [])
        self.camera_fields = cfg.get("metadaten.kamera_felder", [])
        self.make_fields = cfg.get("metadaten.hersteller_felder", [])
        self.lens_fields = cfg.get("metadaten.objektiv_felder", [])
        self.camera_names = cfg.get("metadaten.kamera_namen", {}) or {}
        self.strip_make = cfg.get("metadaten.hersteller_aus_kamera_entfernen", False)
        self.batch = max(1, int(cfg.get("metadaten.batch_groesse", 200)))

    def _tags(self) -> list[str]:
        fields = self.date_fields + self.camera_fields + self.make_fields + self.lens_fields
        return [f for f in dict.fromkeys(fields) if f != "datei_mtime"]

    def _run_exiftool(self, paths: list[Path]) -> dict[str, dict]:
        if not self.exiftool or not paths:
            return {}
        with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as f:
            f.write("\n".join(str(p) for p in paths))
            argfile = f.name
        try:
            cmd = [self.exiftool, "-json", "-fast", "-charset", "filename=utf8", "-api", "largefilesupport=1"]
            cmd += [f"-{t}" for t in self._tags()] + ["-@", argfile]
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
            if f == "datei_mtime":
                m.datum, m.datum_quelle = datetime.fromtimestamp(e.mtime), f
                break
            d = parse_exif_date(raw.get(f))
            if d:
                m.datum, m.datum_quelle = d, f
                break
        m.hersteller = self._first(raw, self.make_fields)
        cam = self._first(raw, self.camera_fields)
        if cam in self.camera_names:
            cam = self.camera_names[cam]
        elif self.strip_make and m.hersteller:
            first_word = m.hersteller.split()[0]
            if cam.lower().startswith(first_word.lower()):
                cam = cam[len(first_word):].strip()
        m.kamera = cam
        m.objektiv = self._first(raw, self.lens_fields)
        return m

    def read(self, entries: list[FileEntry], ctx: RunContext) -> dict[Path, Meta]:
        out: dict[Path, Meta] = {}
        if not self.exiftool:
            ctx.message("⚠️ exiftool nicht gefunden – nur Dateidatum verfügbar (brew install exiftool)")
        for i in range(0, len(entries), self.batch):
            ctx.check()
            chunk = entries[i:i + self.batch]
            raw = self._run_exiftool([e.path for e in chunk])
            for e in chunk:
                out[e.path] = self._build(e, raw.get(str(e.path), {}))
            ctx.progress("Metadaten lesen", min(i + self.batch, len(entries)), len(entries))
        return out
