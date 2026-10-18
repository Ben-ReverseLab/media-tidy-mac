"""Muster wie "{Jahr}/{Kamera}" für Ordner- und Dateinamen auflösen."""
from __future__ import annotations

import re
import string
from pathlib import Path, PurePosixPath

from .config import Config
from .metadata import Meta
from .scan import FileEntry

PLACEHOLDERS = {
    "Jahr": "Aufnahmejahr (2025)",
    "Monat": "Monat zweistellig (08)",
    "MonatName": "Monatsname (August)",
    "Tag": "Tag zweistellig (09)",
    "Quartal": "Quartal (Q3)",
    "KW": "Kalenderwoche (KW32)",
    "KWJahr": "Jahr der Kalenderwoche – für {KWJahr}/{KW} (31.12.2025 → 2026/KW01)",
    "Wochentag": "Wochentag (Mo..So)",
    "Stunde": "Stunde (14)",
    "Minute": "Minute (05)",
    "Sekunde": "Sekunde (59)",
    "Kamera": "Kameramodell",
    "Hersteller": "Kamerahersteller",
    "Objektiv": "Objektiv",
    "Typ": "RAW / Foto / Video",
    "Endung": "Dateiendung (CR3)",
    "Name": "Originaler Dateiname ohne Endung",
    "Ordner": "Name des bisherigen Ordners",
    "Quelle": "Name des Quellordners",
}
_WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


class PatternError(ValueError):
    pass


def placeholders_in(pattern: str) -> list[str]:
    try:
        return [f for _, f, _, _ in string.Formatter().parse(pattern) if f is not None]
    except ValueError as e:
        raise PatternError(f"Muster fehlerhaft: {e}") from e


def validate(pattern: str) -> None:
    unknown = [p for p in placeholders_in(pattern) if p not in PLACEHOLDERS]
    if unknown:
        raise PatternError("Unbekannte Platzhalter: " + ", ".join("{%s}" % u for u in unknown)
                           + "\nErlaubt: " + ", ".join("{%s}" % p for p in PLACEHOLDERS))
    if pattern.startswith("/") or ".." in PurePosixPath(pattern).parts:
        raise PatternError("Muster darf nicht mit / beginnen oder .. enthalten")


class PatternRenderer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.unknown = cfg.get("metadaten.unbekannt", "Unbekannt")
        self.months = cfg.get("muster.monatsnamen", [])
        bad = cfg.get("muster.ungueltige_zeichen", "")
        self.bad_re = re.compile("[" + re.escape(bad) + r"\x00-\x1f]") if bad else re.compile(r"[\x00-\x1f]")
        self.repl = cfg.get("muster.ersatzzeichen", "_")

    def values(self, e: FileEntry, m: Meta) -> dict[str, str]:
        d = m.datum
        u = self.unknown
        return {
            "Jahr": f"{d.year:04d}" if d else u,
            "Monat": f"{d.month:02d}" if d else u,
            "MonatName": (self.months[d.month - 1] if d and len(self.months) >= 12 else (f"{d.month:02d}" if d else u)),
            "Tag": f"{d.day:02d}" if d else u,
            "Quartal": f"Q{(d.month - 1) // 3 + 1}" if d else u,
            "KW": f"KW{d.isocalendar()[1]:02d}" if d else u,
            "KWJahr": f"{d.isocalendar()[0]:04d}" if d else u,
            "Wochentag": _WEEKDAYS[d.weekday()] if d else u,
            "Stunde": f"{d.hour:02d}" if d else u,
            "Minute": f"{d.minute:02d}" if d else u,
            "Sekunde": f"{d.second:02d}" if d else u,
            "Kamera": m.kamera or u,
            "Hersteller": m.hersteller or u,
            "Objektiv": m.objektiv or u,
            "Typ": self.cfg.file_type(e.ext),
            "Endung": e.ext.upper(),
            "Name": e.path.stem,
            "Ordner": e.path.parent.name,
            "Quelle": e.label,
        }

    def _clean(self, s: str) -> str:
        s = self.bad_re.sub(self.repl, s).strip().strip(".")
        return s or self.unknown

    def folder(self, pattern: str, e: FileEntry, m: Meta) -> Path:
        validate(pattern)
        vals = {k: self._clean(v) for k, v in self.values(e, m).items()}
        rendered = pattern.format(**vals)
        parts = [self._clean(p) for p in rendered.split("/") if p.strip()]
        return Path(*parts) if parts else Path(".")

    def filename(self, pattern: str, e: FileEntry, m: Meta) -> str:
        if not pattern.strip():
            return e.path.name
        validate(pattern)
        vals = {k: self._clean(v) for k, v in self.values(e, m).items()}
        stem = self._clean(pattern.format(**vals).replace("/", self.repl))
        return stem + e.path.suffix
