"""Resolve patterns such as "{Year}/{Camera}" into folder and file names.

Patterns are stored with English placeholder names. The UI shows them in the app language, and
placeholder names of every supported language (and of earlier versions) are accepted as input."""
from __future__ import annotations

import re
import string
from pathlib import Path, PurePosixPath

from . import legacy
from .config import Config
from .i18n import LANGUAGES, catalog, t
from .metadata import Meta
from .scan import FileEntry

PLACEHOLDERS = ["Year", "Month", "MonthName", "Day", "Quarter", "Week", "WeekYear", "Weekday", "Hour", "Minute",
                "Second", "Camera", "Make", "Lens", "Type", "Ext", "Name", "Folder", "Source"]
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


class PatternError(ValueError):
    pass


def _aliases() -> dict[str, str]:
    """Placeholder name in any language -> English name."""
    out = dict(legacy.PLACEHOLDERS)
    for lang in LANGUAGES:
        cat = catalog(lang)
        out.update({cat[f"ph.{p}"]: p for p in PLACEHOLDERS if f"ph.{p}" in cat})
    out.update({p: p for p in PLACEHOLDERS})
    return out


def canonical(pattern: str) -> str:
    """Placeholder names in the app language (or older ones) -> English names."""
    aliases = _aliases()
    return _PLACEHOLDER_RE.sub(lambda m: "{%s}" % aliases.get(m.group(1), m.group(1)), pattern)


def localize(pattern: str) -> str:
    """English placeholder names -> names in the app language (for display)."""
    return _PLACEHOLDER_RE.sub(
        lambda m: "{%s}" % (t(f"ph.{m.group(1)}") if m.group(1) in PLACEHOLDERS else m.group(1)), pattern)


def placeholders_in(pattern: str) -> list[str]:
    try:
        return [f for _, f, _, _ in string.Formatter().parse(canonical(pattern)) if f is not None]
    except ValueError as e:
        raise PatternError(t("pattern.malformed", error=str(e))) from e


def validate(pattern: str) -> None:
    unknown = [p for p in placeholders_in(pattern) if p not in PLACEHOLDERS]
    if unknown:
        raise PatternError(t("pattern.unknown", names=", ".join("{%s}" % u for u in unknown),
                             allowed=", ".join("{%s}" % t(f"ph.{p}") for p in PLACEHOLDERS)))
    if pattern.startswith("/") or ".." in PurePosixPath(pattern).parts:
        raise PatternError(t("pattern.not_relative"))


class PatternRenderer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.unknown = cfg.get("metadata.unknown") or t("meta.unknown")
        self.months = cfg.get("patterns.month_names") or [t(f"month.{i}") for i in range(1, 13)]
        bad = cfg.get("patterns.invalid_chars", "")
        self.bad_re = re.compile("[" + re.escape(bad) + r"\x00-\x1f]") if bad else re.compile(r"[\x00-\x1f]")
        self.repl = cfg.get("patterns.replacement_char", "_")

    def values(self, e: FileEntry, m: Meta) -> dict[str, str]:
        d = m.date
        u = self.unknown
        return {
            "Year": f"{d.year:04d}" if d else u,
            "Month": f"{d.month:02d}" if d else u,
            "MonthName": (self.months[d.month - 1] if d and len(self.months) >= 12 else (f"{d.month:02d}" if d else u)),
            "Day": f"{d.day:02d}" if d else u,
            "Quarter": f"Q{(d.month - 1) // 3 + 1}" if d else u,
            "Week": f"{t('week_prefix')}{d.isocalendar()[1]:02d}" if d else u,
            "WeekYear": f"{d.isocalendar()[0]:04d}" if d else u,
            "Weekday": t(f"weekday.{d.weekday()}") if d else u,
            "Hour": f"{d.hour:02d}" if d else u,
            "Minute": f"{d.minute:02d}" if d else u,
            "Second": f"{d.second:02d}" if d else u,
            "Camera": m.camera or u,
            "Make": m.make or u,
            "Lens": m.lens or u,
            "Type": t(f"group.{self.cfg.file_group(e.ext)}"),
            "Ext": e.ext.upper(),
            "Name": e.path.stem,
            "Folder": e.path.parent.name,
            "Source": e.label,
        }

    def _clean(self, s: str) -> str:
        s = self.bad_re.sub(self.repl, s).strip().strip(".")
        return s or self.unknown

    def folder(self, pattern: str, e: FileEntry, m: Meta) -> Path:
        validate(pattern)
        vals = {k: self._clean(v) for k, v in self.values(e, m).items()}
        rendered = canonical(pattern).format(**vals)
        parts = [self._clean(p) for p in rendered.split("/") if p.strip()]
        return Path(*parts) if parts else Path(".")

    def filename(self, pattern: str, e: FileEntry, m: Meta) -> str:
        if not pattern.strip():
            return e.path.name
        validate(pattern)
        vals = {k: self._clean(v) for k, v in self.values(e, m).items()}
        stem = self._clean(canonical(pattern).format(**vals).replace("/", self.repl))
        return stem + e.path.suffix
