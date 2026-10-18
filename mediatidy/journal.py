"""Journal of every file action -> undo + resume."""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import legacy
from .context import RunContext
from .i18n import t

UNDONE_SUFFIX = "_UNDONE"


class Journal:
    def __init__(self, data_dir: Path, job: str):
        self.dir = data_dir / "journal"
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.file = self.dir / f"{stamp}_{job}.jsonl"
        self._fh = None

    def record(self, action: str, src: Path, dst: Path | None, **extra) -> None:
        if self._fh is None:
            # One file per run – even if two runs start in the same second
            base, n = self.file.stem, 2
            while True:
                try:
                    self._fh = self.file.open("x", encoding="utf-8")
                    break
                except FileExistsError:
                    self.file = self.dir / f"{base}_{n}.jsonl"
                    n += 1
        entry = {"t": time.time(), "action": action, "src": str(src), "dst": str(dst) if dst else None, **extra}
        self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def list_journals(data_dir: Path) -> list[Path]:
    d = data_dir / "journal"
    return sorted(d.glob("*.jsonl"), reverse=True) if d.exists() else []


def is_undone(file: Path) -> bool:
    return UNDONE_SUFFIX in file.name or legacy.UNDONE_SUFFIX in file.name


def read_entries(file: Path) -> list[dict]:
    out = []
    for line in file.read_text(encoding="utf-8").splitlines():
        try:
            out.append(legacy.journal_entry(json.loads(line)))
        except json.JSONDecodeError:
            continue  # interrupted last line
    return out


def undo(file: Path, ctx: RunContext, dry_run: bool = False) -> tuple[int, list[str]]:
    """Undo a run: delete copies, move moved files back."""
    entries = [e for e in read_entries(file) if not e.get("undone")]
    done, problems = 0, []
    for i, e in enumerate(reversed(entries), 1):
        ctx.check()
        src, dst = Path(e["src"]), Path(e["dst"]) if e.get("dst") else None
        act = e["action"]
        try:
            if act == "copied" and dst:
                if dst.exists():
                    if not dry_run:
                        dst.unlink()
                    done += 1
            elif act in ("moved", "duplicate_moved") and dst:
                if src.exists():
                    problems.append(t("undo.target_taken", path=str(src)))
                elif dst.exists():
                    if not dry_run:
                        src.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(dst), str(src))
                    done += 1
                else:
                    problems.append(t("undo.file_missing", path=str(dst)))
            elif act == "sidecar_updated" and dst:
                backup = Path(e.get("backup") or "")
                if backup.is_file():
                    if not dry_run:
                        shutil.copy2(backup, dst)
                    done += 1
                else:
                    problems.append(t("undo.backup_missing", path=str(dst)))
            elif act == "folder_created":
                # only remove when empty (a Finder .DS_Store doesn't count)
                if not dry_run and src.is_dir() and {p.name for p in src.iterdir()} <= {".DS_Store"}:
                    (src / ".DS_Store").unlink(missing_ok=True)
                    src.rmdir()
            elif act == "trashed":
                problems.append(t("undo.restore_from_trash", name=src.name))
            elif act == "overwritten":
                problems.append(t("undo.overwritten", path=str(dst)))
        except OSError as err:
            problems.append(f"{dst or src}: {err}")
        ctx.progress("undo", i, len(entries))
    if not dry_run:
        with file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), "action": "run_undone", "src": str(file),
                                 "undone": True}, ensure_ascii=False) + "\n")
        file.rename(file.with_name(file.stem + UNDONE_SUFFIX + ".jsonl"))
    return done, problems
