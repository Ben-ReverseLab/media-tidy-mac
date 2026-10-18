"""Start-Prüfung: Abhängigkeiten, Zugriffsrechte, Logging."""
from __future__ import annotations

import logging
import logging.handlers
import os
import threading
import time
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
    status = probe_volumes()
    blocked = [v for v, st in status.items() if st == "gesperrt"]
    hanging = [v for v, st in status.items() if st == "haengt"]
    if hanging:
        hints.append(f"Laufwerk reagiert nicht: {', '.join(hanging)} – ggf. auswerfen und neu anschließen.")
    if blocked:
        hints.append(f"Kein Zugriff auf: {', '.join(blocked)}.\n"
                     "  Systemeinstellungen → Datenschutz & Sicherheit → Festplattenvollzugriff:\n"
                     "  FotoTool.app hinzufügen (bzw. Terminal, falls über FotoTool.command gestartet),\n"
                     "  dann FotoTool neu starten. Für externe Platten reicht meist „Wechseldatenträger“ erlauben.")
    return hints


def probe_volumes(timeout: float = 3.0) -> dict[str, str]:
    """Laufwerk -> "ok" | "gesperrt" | "fehler" | "haengt". Parallel mit Zeitlimit: manche Geräte
    (z.B. manche E-Reader) blockieren beim Auflisten, das darf den Programmstart nicht aufhalten."""
    status: dict[str, str] = {}

    def probe(v: Path) -> None:
        try:
            os.listdir(v)
            status[v.name] = "ok"
        except PermissionError:
            status[v.name] = "gesperrt"
        except OSError:
            status[v.name] = "fehler"

    try:
        vols = list(Path("/Volumes").iterdir())
    except OSError:
        return {}
    threads = [threading.Thread(target=probe, args=(v,), daemon=True) for v in vols]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return {v.name: status.get(v.name, "haengt") for v in vols}


def tk_available() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        return False
