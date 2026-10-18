"""Translations: texts live in locales/<language>.toml as flat "key" = "text" entries."""
from __future__ import annotations

import locale
import os
import subprocess
import tomllib
from functools import cache
from pathlib import Path

LOCALE_DIR = Path(__file__).with_name("locales")
LANGUAGES = ("en", "de")
_current = "en"


@cache
def catalog(lang: str) -> dict[str, str]:
    with (LOCALE_DIR / f"{lang}.toml").open("rb") as f:
        return tomllib.load(f)


def system_language() -> str:
    """First macOS preferred language we have texts for, else English."""
    candidates: list[str] = []
    try:
        out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True,
                             timeout=2).stdout
        candidates += [s.strip(' ",()\n') for s in out.split(",")]
    except (OSError, subprocess.TimeoutExpired):
        pass
    candidates += [os.environ.get("LANG", ""), locale.getlocale()[0] or ""]
    for c in candidates:
        code = c.strip().lower()[:2]
        if code in LANGUAGES:
            return code
    return "en"


def set_language(lang: str) -> str:
    """'auto' | 'en' | 'de'. Returns the language now in use."""
    global _current
    _current = lang if lang in LANGUAGES else system_language()
    return _current


def language() -> str:
    return _current


def t(key: str, /, **values) -> str:
    text = catalog(_current).get(key) or catalog("en").get(key) or key
    return text.format(**values) if values else text


def has(key: str) -> bool:
    return key in catalog(_current) or key in catalog("en")
