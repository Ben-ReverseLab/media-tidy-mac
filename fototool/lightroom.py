"""Ordner aus Lightroom-Classic-Katalogen lesen (nur lesend)."""
from __future__ import annotations

import glob
import os
import sqlite3
import urllib.parse
from pathlib import Path

from .config import Config


def catalog_folders(cfg: Config) -> dict[Path, Path]:
    """{Ordner: Katalog} aller in Lightroom-Katalogen verknüpften Stammordner."""
    out: dict[Path, Path] = {}
    files = {f for pat in cfg.get("pfade.lightroom_kataloge", [])
             for f in glob.glob(os.path.expanduser(pat))}
    for cat in sorted(files):
        uri = "file:" + urllib.parse.quote(cat) + "?mode=ro"
        try:
            con = sqlite3.connect(uri, uri=True, timeout=2)
            for (p,) in con.execute("SELECT absolutePath FROM AgLibraryRootFolder"):
                out[Path(p)] = Path(cat)
            con.close()
        except sqlite3.Error:
            continue
    return out


def affected_catalogs(cfg: Config, folder: Path) -> list[str]:
    """Kataloge, deren Ordner von einer Verschiebung in/aus `folder` betroffen sind."""
    folder = Path(folder).expanduser().resolve()
    hits = []
    for root, cat in catalog_folders(cfg).items():
        if root == folder or root.is_relative_to(folder) or folder.is_relative_to(root):
            hits.append(f"{cat.name}: {root}")
    return hits
