"""Read folders from Lightroom Classic catalogs (read-only)."""
from __future__ import annotations

import glob
import os
import sqlite3
import urllib.parse
from pathlib import Path

from .config import Config


def catalog_folders(cfg: Config) -> dict[Path, Path]:
    """{folder: catalog} for all root folders linked in Lightroom catalogs."""
    out: dict[Path, Path] = {}
    files = {f for pat in cfg.get("paths.lightroom_catalogs", [])
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
    """Catalogs whose folders are affected by moving files into/out of `folder`."""
    folder = Path(folder).expanduser().resolve()
    hits = []
    for root, cat in catalog_folders(cfg).items():
        if root == folder or root.is_relative_to(folder) or folder.is_relative_to(root):
            hits.append(f"{cat.name}: {root}")
    return hits
