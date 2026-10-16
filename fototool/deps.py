"""Start-Prüfung: Abhängigkeiten, Zugriffsrechte, Logging."""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from .config import Config
from .metadata import find_exiftool


def setup_logging(cfg: Config) -> Path:
    log_dir = cfg.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file = log_dir / "fototool.log"
    handler = logging.handlers.RotatingFileHandler(file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
    return file


def check(cfg: Config) -> list[str]:
    """Liste verständlicher Hinweise (leer = alles ok)."""
    hints = list(cfg.warnings)
    if not find_exiftool(cfg):
        hints.append("exiftool fehlt – Sortieren nach Aufnahmedatum/Kamera nur eingeschränkt.\n"
                     "  Installieren: brew install exiftool")
    blocked = []
    vols = Path("/Volumes")
    try:
        for v in vols.iterdir():
            try:
                os.listdir(v)
            except PermissionError:
                blocked.append(v.name)
            except OSError:
                pass
    except OSError:
        pass
    if blocked:
        hints.append(f"Kein Zugriff auf: {', '.join(blocked)}.\n"
                     "  Systemeinstellungen → Datenschutz & Sicherheit → Festplattenvollzugriff:\n"
                     "  FotoTool.app hinzufügen (bzw. Terminal, falls über FotoTool.command gestartet),\n"
                     "  dann FotoTool neu starten. Für externe Platten reicht meist „Wechseldatenträger“ erlauben.")
    return hints


def tk_available() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        return False
