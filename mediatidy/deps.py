"""Start-up check: dependencies, access rights, logging."""
from __future__ import annotations

import logging
import logging.handlers
import os
import threading
import time
from pathlib import Path

from .config import Config
from .i18n import t
from .metadata import bundled_exiftool, find_exiftool


def setup_logging(cfg: Config) -> Path:
    log_dir = cfg.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file = log_dir / "mediatidy.log"
    handler = logging.handlers.RotatingFileHandler(file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
    return file


def check(cfg: Config) -> list[str]:
    """List of readable hints (empty = all good)."""
    hints = list(cfg.warnings)
    if not find_exiftool(cfg):
        hints.append(t("check.no_perl" if bundled_exiftool() else "check.no_exiftool"))
    status = probe_volumes()
    blocked = [v for v, st in status.items() if st == "blocked"]
    hanging = [v for v, st in status.items() if st == "hanging"]
    if hanging:
        hints.append(t("check.drive_hanging", drives=", ".join(hanging)))
    if blocked:
        hints.append(t("check.drive_blocked", drives=", ".join(blocked)))
    return hints


def probe_volumes(timeout: float = 3.0) -> dict[str, str]:
    """drive -> "ok" | "blocked" | "error" | "hanging". In parallel with a time limit: some devices
    (e.g. some e-readers) block when listed, which must not hold up the start of the app."""
    status: dict[str, str] = {}

    def probe(v: Path) -> None:
        try:
            os.listdir(v)
            status[v.name] = "ok"
        except PermissionError:
            status[v.name] = "blocked"
        except OSError:
            status[v.name] = "error"

    try:
        vols = list(Path("/Volumes").iterdir())
    except OSError:
        return {}
    threads = [threading.Thread(target=probe, args=(v,), daemon=True) for v in vols]
    for th in threads:
        th.start()
    deadline = time.monotonic() + timeout
    for th in threads:
        th.join(max(0.0, deadline - time.monotonic()))
    return {v.name: status.get(v.name, "hanging") for v in vols}


def tk_available() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        return False
