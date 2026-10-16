"""Inhaltsvergleich: Größe -> Teil-Hash -> voller Hash, mit SQLite-Cache."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Hashable, Iterable

from .config import Config
from .context import RunContext
from .scan import FileEntry


class HashCache:
    def __init__(self, db_file: Path):
        self.db = sqlite3.connect(db_file, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("""CREATE TABLE IF NOT EXISTS hashes (
            path TEXT, size INTEGER, mtime REAL, algo TEXT, kind TEXT, digest TEXT,
            PRIMARY KEY (path, algo, kind))""")
        self.db.commit()

    def get(self, e: FileEntry, algo: str, kind: str) -> str | None:
        with self.lock:
            row = self.db.execute(
                "SELECT size, mtime, digest FROM hashes WHERE path=? AND algo=? AND kind=?",
                (str(e.path), algo, kind)).fetchone()
        if row and row[0] == e.size and abs(row[1] - e.mtime) < 1e-3:
            return row[2]
        return None

    def put(self, e: FileEntry, algo: str, kind: str, digest: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO hashes VALUES (?,?,?,?,?,?)",
                            (str(e.path), e.size, e.mtime, algo, kind, digest))

    def commit(self) -> None:
        with self.lock:
            if self.db:
                self.db.commit()

    def close(self) -> None:
        with self.lock:
            if self.db:
                self.db.commit()
                self.db.close()
                self.db = None


class Hasher:
    def __init__(self, cfg: Config, cache: HashCache | None = None):
        self.algo = cfg.get("vergleich.hash_algorithmus", "blake2b")
        hashlib.new(self.algo)  # prüft, ob Algorithmus existiert
        self.partial = int(cfg.get("vergleich.teilhash_kb", 1024)) * 1024
        self.bufsize = max(64, int(cfg.get("vergleich.lesepuffer_kb", 4096))) * 1024
        self.threads = max(1, int(cfg.get("vergleich.threads", 2)))
        self.cache = cache

    def full_hash_file(self, path: Path) -> str:
        h = hashlib.new(self.algo)
        with open(path, "rb") as f:
            while chunk := f.read(self.bufsize):
                h.update(chunk)
        return h.hexdigest()

    def _partial_hash(self, e: FileEntry) -> str:
        h = hashlib.new(self.algo)
        with open(e.path, "rb") as f:
            if e.size <= 2 * self.partial:
                h.update(f.read())
            else:
                h.update(f.read(self.partial))
                f.seek(-self.partial, 2)
                h.update(f.read(self.partial))
        return h.hexdigest()

    def digest(self, e: FileEntry, kind: str) -> str:
        if self.cache and (d := self.cache.get(e, self.algo, kind)):
            return d
        d = self.full_hash_file(e.path) if kind == "full" else self._partial_hash(e)
        if self.cache:
            self.cache.put(e, self.algo, kind, d)
        return d

    def _refine(self, groups: list[list[FileEntry]], kind: str, ctx: RunContext,
                errors: list[str]) -> list[list[FileEntry]]:
        todo = [e for g in groups for e in g]
        if not todo:
            return []
        digests: dict[int, str] = {}
        label = "Teilvergleich" if kind == "partial" else "Inhaltsvergleich"
        with ThreadPoolExecutor(self.threads) as pool:
            futures = {pool.submit(self.digest, e, kind): i for i, e in enumerate(todo)}
            for n, fut in enumerate(as_completed(futures), 1):
                if ctx.cancelled:
                    for f in futures:
                        f.cancel()
                    ctx.check()
                i = futures[fut]
                try:
                    digests[i] = fut.result()
                except OSError as err:
                    errors.append(f"{todo[i].path}: {err.strerror or err}")
                if n % 20 == 0 or n == len(todo):
                    ctx.progress(label, n, len(todo))
                    if self.cache and n % 500 == 0:
                        self.cache.commit()
        if self.cache:
            self.cache.commit()
        out: list[list[FileEntry]] = []
        idx = 0
        for g in groups:
            sub: dict[str, list[FileEntry]] = {}
            for e in g:
                if idx in digests:
                    sub.setdefault(digests[idx], []).append(e)
                idx += 1
            out.extend(sub.values())
        return out

    def group_identical(self, entries: Iterable[FileEntry], ctx: RunContext,
                        errors: list[str], need: Callable[[list[FileEntry]], bool] | None = None
                        ) -> list[list[FileEntry]]:
        """Gruppen inhaltsgleicher Dateien. Einzelgänger kommen als 1er-Gruppe zurück.
        `need(gruppe)` entscheidet, ob eine Gruppe weiter verfeinert werden muss."""
        need = need or (lambda g: len(g) > 1)
        by_size: dict[int, list[FileEntry]] = {}
        for e in entries:
            by_size.setdefault(e.size, []).append(e)
        final = [g for g in by_size.values() if not need(g)]
        pending = [g for g in by_size.values() if need(g)]
        for kind in ("partial", "full"):
            refined = self._refine(pending, kind, ctx, errors)
            final += [g for g in refined if not need(g)]
            pending = [g for g in refined if need(g)]
        return final + pending


def group_by_key(entries: Iterable[FileEntry], key: Callable[[FileEntry], Hashable]) -> list[list[FileEntry]]:
    groups: dict[Hashable, list[FileEntry]] = {}
    for e in entries:
        groups.setdefault(key(e), []).append(e)
    return list(groups.values())
