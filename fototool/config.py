"""Einstellungen: defaults.toml + ~/.fototool/config.toml, Profile, Migration."""
from __future__ import annotations

import copy
import json
import logging
import re
import tomllib
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULTS_FILE = Path(__file__).with_name("defaults.toml")
CONFIG_VERSION = 1
USER_DIR_DEFAULT = Path("~/.fototool").expanduser()


def load_defaults() -> dict:
    with DEFAULTS_FILE.open("rb") as f:
        return tomllib.load(f)


# --- Migration ---------------------------------------------------------------
# Für jede neue Config-Version eine Funktion version -> version+1 eintragen.
MIGRATIONS: dict[int, Any] = {
    # 1: lambda cfg: {..., "version": 2},
}


def migrate(cfg: dict) -> dict:
    version = int(cfg.get("version", 1))
    while version < CONFIG_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            log.warning("Keine Migration von Config-Version %s – Werte werden übernommen", version)
            break
        cfg = step(cfg)
        version = int(cfg.get("version", version + 1))
        log.info("Config migriert auf Version %s", version)
    cfg["version"] = CONFIG_VERSION
    return cfg


# --- Zusammenführen + Prüfen -------------------------------------------------
def _same_kind(default: Any, value: Any) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


def merge(defaults: dict, user: dict, path: str = "", warnings: list[str] | None = None) -> dict:
    """Nutzerwerte über Standardwerte legen; falscher Typ -> Warnung + Standard."""
    warnings = warnings if warnings is not None else []
    result = copy.deepcopy(defaults)
    for key, value in user.items():
        full = f"{path}.{key}" if path else key
        if key not in defaults:
            # Unbekannte Schlüssel behalten (z.B. freie Tabellen wie kamera_namen, neuere Versionen)
            result[key] = value
            continue
        default = defaults[key]
        if isinstance(default, dict) and isinstance(value, dict):
            result[key] = merge(default, value, full, warnings)
        elif _same_kind(default, value):
            result[key] = value
        else:
            warnings.append(f"Ungültiger Wert für '{full}': {value!r} – Standard {default!r} wird verwendet")
    return result


def diff(defaults: dict, cfg: dict) -> dict:
    """Nur die vom Standard abweichenden Werte (für schlanke Nutzer-Config)."""
    out: dict = {}
    for key, value in cfg.items():
        if key not in defaults:
            out[key] = value
        elif isinstance(value, dict) and isinstance(defaults[key], dict):
            sub = diff(defaults[key], value)
            if sub:
                out[key] = sub
        elif value != defaults[key]:
            out[key] = value
    return out


# --- TOML schreiben (tomllib kann nur lesen) ---------------------------------
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str) -> str:
    return k if _BARE_KEY.match(k) else json.dumps(k, ensure_ascii=False)


def _val(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_key(k)} = {_val(x)}" for k, x in v.items()) + "}"
    raise TypeError(f"Nicht als TOML speicherbar: {v!r}")


def dumps_toml(data: dict, prefix: str = "") -> str:
    lines: list[str] = []
    tables: list[tuple[str, dict]] = []
    arrays_of_tables: list[tuple[str, list]] = []
    for k, v in data.items():
        name = f"{prefix}.{_key(k)}" if prefix else _key(k)
        if isinstance(v, dict):
            tables.append((name, v))
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            arrays_of_tables.append((name, v))
        else:
            lines.append(f"{_key(k)} = {_val(v)}")
    out = "\n".join(lines)
    for name, table in tables:
        out += f"\n\n[{name}]\n" + dumps_toml(table, name)
    for name, items in arrays_of_tables:
        for item in items:
            out += f"\n\n[[{name}]]\n" + dumps_toml(item, name)
    return out.strip() + "\n"


# --- Config-Objekt -----------------------------------------------------------
class Config:
    """Zugriff per cfg["vergleich"]["methode"] oder cfg.get("vergleich.methode")."""

    def __init__(self, user_dir: Path | None = None):
        self.defaults = load_defaults()
        self.user_dir = Path(user_dir or USER_DIR_DEFAULT).expanduser()
        self.warnings: list[str] = []
        self.data = copy.deepcopy(self.defaults)
        self.reload()

    @property
    def user_file(self) -> Path:
        return self.user_dir / "config.toml"

    @property
    def data_dir(self) -> Path:
        p = Path(self.get("pfade.datenordner") or self.user_dir).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def reload(self) -> None:
        self.warnings = []
        user: dict = {}
        if self.user_file.exists():
            try:
                with self.user_file.open("rb") as f:
                    user = tomllib.load(f)
            except (tomllib.TOMLDecodeError, OSError) as e:
                self.warnings.append(f"Config {self.user_file} unlesbar ({e}) – Standardwerte aktiv")
                user = {}
        user = migrate(user)
        self.data = merge(self.defaults, user, warnings=self.warnings)
        for w in self.warnings:
            log.warning(w)

    def save(self) -> None:
        self.user_dir.mkdir(parents=True, exist_ok=True)
        changed = diff(self.defaults, self.data)
        changed["version"] = CONFIG_VERSION
        tmp = self.user_file.with_suffix(".tmp")
        tmp.write_text("# FotoTool – eigene Einstellungen (nur Abweichungen vom Standard)\n"
                       + dumps_toml(changed), encoding="utf-8")
        tmp.replace(self.user_file)

    def reset(self) -> None:
        self.data = copy.deepcopy(self.defaults)
        self.save()

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def extensions(self, groups: list[str] | None = None) -> set[str]:
        groups = groups if groups is not None else self.get("dateien.aktive_gruppen", [])
        exts: set[str] = set()
        for g in groups:
            exts.update(e.lower().lstrip(".") for e in self.get(f"dateien.{g}", []))
        return exts

    def file_type(self, ext: str) -> str:
        ext = ext.lower().lstrip(".")
        for g in ("raw", "foto", "video", "sidecar"):
            if ext in self.get(f"dateien.{g}", []):
                return {"raw": "RAW", "foto": "Foto", "video": "Video", "sidecar": "Sidecar"}[g]
        return "Sonstige"


# --- Profile (gespeicherte Aufträge) -----------------------------------------
class Profiles:
    def __init__(self, cfg: Config):
        self.dir = cfg.data_dir / "profile"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _file(self, name: str) -> Path:
        safe = re.sub(r"[^\w\- äöüÄÖÜß→.]", "_", name).strip() or "profil"
        return self.dir / f"{safe}.json"

    def names(self) -> list[str]:
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")).get("name", p.stem))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def load(self, name: str) -> dict:
        return json.loads(self._file(name).read_text(encoding="utf-8"))

    def save(self, name: str, job: dict) -> None:
        data = {"name": name, "version": CONFIG_VERSION, **job}
        self._file(name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def delete(self, name: str) -> None:
        self._file(name).unlink(missing_ok=True)
