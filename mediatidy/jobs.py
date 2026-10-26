"""The three jobs: sync, duplicates, sort.

Jobs work with language-independent codes: rows are {"status": code, "file": ..., ...} and summary keys
are codes as well. summary_items() and table() turn them into text in the app language."""
from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import legacy
from .actions import FileOps, Result
from .config import Config
from .context import Cancelled, Declined, RunContext
from .hashing import HashCache, Hasher, group_by_key
from .i18n import has, t
from .journal import Journal
from .metadata import NAME_FIELDS, Meta, MetadataReader, assign_names, unassigned_names
from .patterns import PatternRenderer, canonical, placeholders_in, validate
from .report import report_dirs, write_report
from .scan import FileEntry, Scanner


def status_text(code: str, dry_run: bool = False) -> str:
    """Status code -> text; in a dry run "copied" reads "Would be copied" etc."""
    if dry_run and has(f"status.dry.{code}"):
        return t(f"status.dry.{code}")
    return t(f"status.{code}")


class AccessError(PermissionError):
    """Folder not readable/reachable – the run stops instead of producing wrong results."""


def access_hint() -> str:
    return "\n\n" + t("access.hint")


def ensure_access(path: Path, must_exist: bool = True) -> None:
    """Check that a folder can really be read (or created)."""
    parts = path.parts
    if len(parts) >= 3 and parts[1] == "Volumes" and not os.path.ismount(os.path.join("/", *parts[:3])):
        raise AccessError(t("access.not_connected", drive=parts[2], path=str(path)))
    try:
        os.stat(path)
    except FileNotFoundError:
        if must_exist:
            raise FileNotFoundError(t("access.folder_not_found", path=str(path)))
        parent = path.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        ensure_access(parent)
        if not os.access(parent, os.W_OK):
            raise AccessError(t("access.not_writable", path=str(parent)) + access_hint())
        return
    except PermissionError:
        raise AccessError(t("access.denied", path=str(path)) + access_hint())
    if path.is_dir():
        try:
            os.listdir(path)
        except PermissionError:
            raise AccessError(t("access.denied", path=str(path)) + access_hint())


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


@dataclass
class JobResult:
    title: str
    summary: dict[str, object] = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    reports: list[Path] = field(default_factory=list)
    journal: Path | None = None
    cancelled: bool = False
    dry_run: bool = True
    columns: list[str] = field(default_factory=list)


def summary_items(res: JobResult) -> list[tuple[str, object]]:
    """Summary as (label, value) in the app language."""
    out = []
    for key, value in res.summary.items():
        if key.startswith("count."):
            out.append((status_text(key[6:], res.dry_run), value))
        else:
            out.append((t(f"summary.{key}"), value))
    return out


def table(res: JobResult) -> tuple[list[str], list[list[object]]]:
    """Report table (headers, rows) in the app language."""
    headers = [t(f"column.{c}") for c in res.columns]
    rows = [[status_text(r["status"], res.dry_run) if c == "status" else r.get(c, "") for c in res.columns]
            for r in res.rows]
    return headers, rows


# --- Parameters (can be saved as profiles) ---------------------------------------
@dataclass
class SyncParams:
    sources: list[str]
    target: str
    copy_missing: bool = True
    placement: str = "structure"  # structure | pattern
    pattern: str = ""
    filename: str = ""
    dry_run: bool = True
    confirmed: bool = False


@dataclass
class DupParams:
    folder: str
    keep: str = "oldest"
    action: str = "report"  # report | move | trash
    dup_folder: str = ""  # empty = duplicates.folder or the language default
    dry_run: bool = True
    confirmed: bool = False


@dataclass
class SortParams:
    source: str
    target: str
    pattern: str = "{Year}/{Year}-{Month}-{Day}"
    filename: str = ""
    mode: str = "copy"  # copy | move
    dry_run: bool = True
    confirmed: bool = False


PARAM_TYPES = {"sync": SyncParams, "duplicates": DupParams, "sort": SortParams}


def params_to_dict(p) -> dict:
    d = asdict(p)
    d.pop("confirmed", None)
    return d


def params_from_dict(job: str, d: dict):
    job = legacy.job_name(job)
    cls = PARAM_TYPES[job]
    d = legacy.params(job, d)
    known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
    return cls(**known)


def default_dup_folder(cfg: Config) -> str:
    return cfg.get("duplicates.folder") or t("dup_folder_default")


# --- Base ------------------------------------------------------------------------
class Job:
    name = "job"
    columns: list[str] = []

    def __init__(self, cfg: Config, ctx: RunContext):
        self.cfg = cfg
        self.ctx = ctx
        self.cache = HashCache(cfg.data_dir / "cache.db")
        self.hasher = Hasher(cfg, self.cache)
        self.scanner = Scanner(cfg)
        self.method = cfg.get("compare.method", "hash")
        self.last_scan_errors: list[str] = []
        # Intermediate state – so that a cancelled run still gets a (partial) report
        self.res: JobResult | None = None
        self.ops: FileOps | None = None
        self.journal: Journal | None = None
        self.report_target: Path | None = None

    def new_result(self, dry_run: bool) -> JobResult:
        self.res = JobResult(t(f"title.{self.name}"), dry_run=dry_run, columns=list(self.columns))
        return self.res

    def comparison(self) -> tuple[str, bool]:
        """(description of the comparison method, safe?)"""
        if self.method == "name_size":
            return t("compare.name_size"), False
        if self.method == "name_size_date":
            return t("compare.name_size_date"), False
        if not self.hasher.full:
            return t("compare.quick"), False
        return t("compare.hash"), True

    def note_comparison(self, res: JobResult) -> None:
        text, safe = self.comparison()
        res.summary["comparison"] = text if safe else "⚠️ " + text
        if not safe:
            self.ctx.message("⚠️ " + t("msg.comparison", text=text), logging.WARNING)

    def review_names(self, meta: dict[Path, Meta], *patterns: str) -> None:
        """Let the user confirm names from the metadata without a mapping before the first step, and save them."""
        if not self.cfg.get("metadata.review_names", True):
            return
        used = {ph for pat in patterns if pat for ph in placeholders_in(pat)}
        todo = unassigned_names(self.cfg, list(meta.values()), [ph for ph in NAME_FIELDS if ph in used])
        if not todo:
            return
        chosen = self.ctx.review_names(todo)
        if chosen is None:  # no UI (command line)
            names = ", ".join(n for found in todo.values() for n in found)
            self.ctx.message("⚠️ " + t("msg.names_unassigned", names=names), logging.WARNING)
            return
        assign_names(self.cfg, list(meta.values()), chosen)
        self.cfg.save()
        self.ctx.message(t("msg.names_saved", names=", ".join(f"{a} → {b}" for names in chosen.values()
                                                                 for a, b in names.items())))

    def scan(self, root: Path, res: JobResult) -> list[FileEntry]:
        ensure_access(root)
        self.ctx.message(t("msg.scanning", path=str(root)))
        entries, errors = self.scanner.scan(root, self.ctx)
        res.errors += errors
        self.last_scan_errors = errors
        self.ctx.message("  " + t("msg.files_found", n=len(entries), size=human_size(sum(e.size for e in entries))))
        if errors:
            self.ctx.message("  ⚠️ " + t("msg.unreadable", n=len(errors), example=errors[0]), logging.WARNING)
        return entries

    def split_empty(self, entries: list[FileEntry], res: JobResult) -> list[FileEntry]:
        """Report empty files (0 bytes) as damaged instead of comparing them – they would all be "identical"."""
        if not self.cfg.get("scan.empty_files_as_defect", True):
            return entries
        empty = [e for e in entries if e.size == 0]
        for e in empty:
            res.rows.append({"status": "empty", "file": str(e.path), "size": "0 B", "info": t("info.not_compared")})
        if empty:
            self.ctx.message("  ⚠️ " + t("msg.empty_files", n=len(empty)), logging.WARNING)
        return [e for e in entries if e.size > 0]

    def group(self, entries: list[FileEntry], res: JobResult, need=None) -> list[list[FileEntry]]:
        if self.method == "hash":
            return self.hasher.group_identical(entries, self.ctx, res.errors, need)
        groups = group_by_key(entries, lambda e: (e.path.name.lower(), e.size))
        if self.method == "name_size_date":
            need = need or (lambda g: len(g) > 1)
            todo = [e for g in groups if need(g) for e in g]
            meta = MetadataReader(self.cfg).read(todo, self.ctx) if todo else {}
            out = [g for g in groups if not need(g)]
            for g in groups:
                if need(g):
                    out += group_by_key(g, lambda e: meta.get(e.path, Meta()).date)
            return out
        return groups

    def finish(self, res: JobResult, ops: FileOps | None, journal: Journal | None,
               target: Path | None) -> JobResult:
        if ops:
            ops.flush_trash()
            fixed = set()
            for row in res.rows:  # the report must not say "Moved to Trash" if the Finder refused
                path = Path(row.get("file") or "")
                if row.get("status") == "trashed" and path in ops.trash_failed:
                    row["status"] = "error"
                    row["info"] = t("info.trash_failed", reason=ops.trash_failed[path])
                    fixed.add(path)
            res.errors += [t("action.trash_failed", path=str(p), reason=why)
                           for p, why in ops.trash_failed.items() if p not in fixed]
        if journal:
            journal.close()
            if journal.file.exists():
                res.journal = journal.file
        self.cache.close()
        if res.cancelled:
            res.summary["cancelled"] = t("summary.cancelled_text")
        counts = Counter(r["status"] for r in res.rows)
        res.summary.update({f"count.{k}": v for k, v in counts.most_common()})
        if res.errors:
            res.summary["read_write_errors"] = len(res.errors)
            res.rows += [{"status": "error", "file": e} for e in res.errors]
        res.summary["dry_run"] = t("yes") if res.dry_run else t("no")
        headers, rows = table(res)
        res.reports = write_report(self.cfg, report_dirs(self.cfg, target, res.dry_run), self.name,
                                   res.title, summary_items(res), headers, rows, res.dry_run,
                                   res.columns.index("status") if "status" in res.columns else None)
        return res

    def finish_cancelled(self, dry_run: bool) -> JobResult:
        res = self.res or self.new_result(dry_run)
        res.cancelled = True
        return self.finish(res, self.ops, self.journal, self.report_target if self.ops else None)

    def _row(self, r: Result, **extra) -> dict:
        return {"status": r.status, "file": str(r.src), "target": str(r.dst or ""), "info": r.info, **extra}


# --- 1. Sync -----------------------------------------------------------------------
class SyncJob(Job):
    name = "sync"
    columns = ["status", "file", "size", "target", "info"]

    def run(self, p: SyncParams) -> JobResult:
        res = self.new_result(p.dry_run)
        target = Path(p.target).expanduser()
        sources = [Path(s).expanduser() for s in p.sources]
        if p.placement == "pattern":
            validate(p.pattern)
            if p.filename:
                validate(p.filename)
        ensure_access(target, must_exist=False)  # check first, before a long scan
        self.note_comparison(res)
        # Source inside the target (e.g. Target/New): check whether its files are anywhere else in the target
        inner = [s for s in sources if s.is_relative_to(target)]
        src_entries: list[FileEntry] = []
        seen: set[Path] = set()
        for s in sources:
            entries = self.scan(s, res)
            if s not in inner:  # target inside the source: don't count the target as source
                entries = [e for e in entries if not e.path.is_relative_to(target)]
            for e in entries:  # overlapping sources (folder + subfolder): each file only once
                if e.path not in seen:
                    seen.add(e.path)
                    src_entries.append(e)
        tgt_entries: list[FileEntry] = []
        target_incomplete = 0
        if target.exists():
            tgt_entries = self.scan(target, res)
            target_incomplete = len(self.last_scan_errors)
            if inner:
                tgt_entries = [e for e in tgt_entries if not any(e.path.is_relative_to(s) for s in inner)]
        else:
            self.ctx.message(t("msg.target_created", path=str(target)))
        if target_incomplete:
            msg = t("msg.target_incomplete", n=target_incomplete)
            if p.copy_missing and not p.dry_run:
                raise AccessError(msg + " " + t("msg.abort_no_double_copy") + access_hint())
            self.ctx.message("⚠️ " + msg, logging.WARNING)
        n_src_all = len(src_entries)
        src_entries = self.split_empty(src_entries, res)
        if self.cfg.get("scan.empty_files_as_defect", True):
            tgt_entries = [e for e in tgt_entries if e.size > 0]
        src_ids = {id(e) for e in src_entries}

        def need(g):  # only groups with a source file and more than one member matter
            return len(g) > 1 and any(id(e) in src_ids for e in g)

        self.ctx.message(t("msg.comparing"))
        if self.method == "hash":
            groups = self.hasher.match_sync(src_entries, tgt_entries, self.ctx, res.errors)
        else:
            groups = self.group(src_entries + tgt_entries, res, need)
        missing: list[FileEntry] = []
        sync_sidecars = self.cfg.get("sync.sync_sidecars", True)
        sidecar_src: dict[Path, Path] = {}  # target sidecar -> newest matching source sidecar
        for g in groups:
            in_src = [e for e in g if id(e) in src_ids]
            in_tgt = [e for e in g if id(e) not in src_ids]
            if not in_src:
                continue
            in_src.sort(key=lambda e: -len(e.sidecars))  # prefer the copy with sidecars
            if in_tgt and sync_sidecars:
                self._collect_sidecars(in_src, in_tgt[0], sidecar_src)
            if in_tgt:
                for e in in_src:
                    res.rows.append({"status": "in_target", "file": str(e.path), "size": human_size(e.size),
                                     "target": str(in_tgt[0].path)})
            elif any(in_src[0].path.is_relative_to(s) for s in inner):
                for e in in_src:  # already physically in the target – don't copy it again
                    res.rows.append({"status": "only_in_source", "file": str(e.path), "size": human_size(e.size)})
            else:
                missing.append(in_src[0])
                for e in in_src[1:]:
                    res.rows.append({"status": "duplicate_in_source", "file": str(e.path),
                                     "size": human_size(e.size), "info": t("info.same_as", path=str(in_src[0].path))})
        sidecar_todo = self._compare_sidecars(sidecar_src, res)
        missing_size = human_size(sum(e.size for e in missing))
        res.summary = {**res.summary, "sources": ", ".join(map(str, sources)), "target": str(target),
                       "files_in_sources": n_src_all, "files_in_target": len(tgt_entries),
                       "missing": len(missing), "missing_size": missing_size}
        if target_incomplete:
            res.summary["target_incomplete"] = t("summary.target_incomplete_text", n=target_incomplete)
        if sidecar_todo:
            res.summary["sidecars_todo"] = len(sidecar_todo)
        self.ctx.message("→ " + t("msg.missing_result", n=len(missing), size=missing_size)
                         + (", " + t("msg.sidecars_todo", n=len(sidecar_todo)) if sidecar_todo else ""))

        ops = journal = None
        if p.copy_missing and (missing or sidecar_todo):
            journal = self.journal = None if p.dry_run else Journal(self.cfg.data_dir, self.name)
            ops = self.ops = FileOps(self.cfg, self.hasher, journal, p.dry_run)
            self.report_target = target
            meta = {}
            renderer = PatternRenderer(self.cfg)
            if p.placement == "pattern":
                meta = MetadataReader(self.cfg).read(missing, self.ctx)
                self.review_names(meta, p.pattern, p.filename)
            levels = self.cfg.get("sync.source_folder_as_level", True)
            for i, e in enumerate(missing, 1):
                self.ctx.check()
                if p.placement == "pattern":
                    m = meta.get(e.path, Meta())
                    dst = target / renderer.folder(p.pattern, e, m) / renderer.filename(p.filename, e, m)
                else:
                    dst = target / (e.label if levels else "") / e.rel
                r = ops.place(e, dst, "copy", overwrite_ok=p.confirmed)
                res.rows.append(self._row(r, size=human_size(e.size)))
                self.ctx.progress("copy" if not p.dry_run else "preview", i, len(missing))
            replace = self.cfg.get("sync.changed_sidecar", "newer_replaces") == "newer_replaces"
            for kind, sc, dst in sidecar_todo:
                self.ctx.check()
                if kind == "missing":
                    r = ops.add_sidecar(sc, dst)
                elif kind == "changed" and replace:
                    r = ops.replace_sidecar(sc, dst)
                else:
                    r = Result(f"sidecar_{kind}", sc, dst)
                res.rows.append(self._row(r))
        else:
            for e in missing:
                res.rows.append({"status": "missing", "file": str(e.path), "size": human_size(e.size)})
            for kind, sc, dst in sidecar_todo:
                res.rows.append({"status": f"sidecar_{kind}", "file": str(sc), "target": str(dst)})
        # Put the report into the target only if something was copied there – otherwise into the data folder
        report_target = target if (ops and target.exists()) else None
        return self.finish(res, ops, journal, report_target)

    def _collect_sidecars(self, in_src: list[FileEntry], match: FileEntry, out: dict[Path, Path]) -> None:
        """Map the source's sidecars to the matching target sidecar (several sources: the newest one)."""
        for e in in_src:
            for sc in e.sidecars:
                same = [x for x in match.sidecars if x.suffix.lower() == sc.suffix.lower()]
                dst = same[0] if same else FileOps._sidecar_dst(sc, e.path, match.path)
                if not same and dst.exists():
                    continue  # belongs to another file in the target (e.g. RAW next to JPG)
                try:
                    if dst not in out or sc.stat().st_mtime > out[dst].stat().st_mtime:
                        out[dst] = sc
                except OSError:
                    continue

    def _compare_sidecars(self, pairs: dict[Path, Path], res: JobResult) -> list[tuple[str, Path, Path]]:
        """[(kind, source, target)] with kind = missing | changed | target_newer; identical ones drop out."""
        todo = []
        for dst, sc in sorted(pairs.items()):
            try:
                if not dst.exists():
                    todo.append(("missing", sc, dst))
                    continue
                a, b = sc.stat(), dst.stat()
                if a.st_size == b.st_size and self.hasher.full_hash_file(sc) == self.hasher.full_hash_file(dst):
                    continue
                # 2 s leeway: exFAT/FAT only store times to 2 seconds
                todo.append(("changed" if a.st_mtime > b.st_mtime + 2 else "target_newer", sc, dst))
            except OSError as err:
                res.errors.append(f"{sc}: {err.strerror or err}")
        return todo


# --- 2. Duplicates -------------------------------------------------------------------
KEEP_RULES = {
    "oldest": lambda e: (e.mtime, str(e.path)),
    "newest": lambda e: (-e.mtime, str(e.path)),
    "shortest_path": lambda e: (len(str(e.path)), str(e.path)),
    "longest_path": lambda e: (-len(str(e.path)), str(e.path)),
    "first": lambda e: str(e.path),
}


class DupJob(Job):
    name = "duplicates"
    columns = ["group", "status", "file", "size", "target", "info"]

    def run(self, p: DupParams) -> JobResult:
        res = self.new_result(p.dry_run)
        root = Path(p.folder).expanduser()
        self.note_comparison(res)
        dup_root = root / (p.dup_folder or default_dup_folder(self.cfg))
        entries = self.scan(root, res)
        n_all = len(entries)
        # Don't find duplicates that were already set aside (also with a custom folder name)
        entries = [e for e in entries if not e.path.is_relative_to(dup_root)]
        entries = self.split_empty(entries, res)
        self.ctx.message(t("msg.finding_duplicates"))
        groups = sorted((g for g in self.group(entries, res) if len(g) > 1),
                        key=lambda g: str(min(e.path for e in g)))
        rule = KEEP_RULES.get(p.keep, KEEP_RULES["oldest"])
        journal = self.journal = None if (p.dry_run or p.action == "report") else Journal(self.cfg.data_dir, self.name)
        ops = self.ops = FileOps(self.cfg, self.hasher, journal, p.dry_run)
        self.report_target = root if p.action != "report" else None
        wasted = 0
        for n, g in enumerate(groups, 1):
            self.ctx.check()
            # Prefer keeping protected files (libraries) – they may not go anyway
            g.sort(key=lambda e: (not ops.is_protected(e.path), rule(e)))
            keep, dups = g[0], g[1:]
            res.rows.append({"group": n, "status": "kept", "file": str(keep.path), "size": human_size(keep.size)})
            for e in dups:
                wasted += e.size
                if p.action == "move":
                    r = ops.set_aside(e, dup_root, root)
                elif p.action == "trash" and (p.confirmed or p.dry_run):
                    r = ops.trash(e)
                else:
                    r = Result("duplicate", e.path, None)
                res.rows.append({"group": n, **self._row(r, size=human_size(e.size))})
            self.ctx.progress("duplicates", n, len(groups))
        surplus = sum(len(g) - 1 for g in groups)
        res.summary = {**res.summary, "folder": str(root), "files": n_all, "dup_groups": len(groups),
                       "surplus_files": surplus, "wasted_space": human_size(wasted),
                       "action": t(f"choice.{p.action}"), "keep_rule": t(f"choice.{p.keep}")}
        if empty := sum(1 for r in res.rows if r["status"] == "empty"):
            res.summary["empty_files"] = empty
        self.ctx.message("→ " + t("msg.dup_result", groups=len(groups), surplus=surplus, size=human_size(wasted)))
        return self.finish(res, ops, journal, root if p.action != "report" else None)


# --- 3. Sort ---------------------------------------------------------------------------
class SortJob(Job):
    name = "sort"
    columns = ["status", "file", "target", "date", "date_source", "camera", "info"]

    def run(self, p: SortParams) -> JobResult:
        res = self.new_result(p.dry_run)
        validate(p.pattern)
        if p.filename:
            validate(p.filename)
        src, target = Path(p.source).expanduser(), Path(p.target).expanduser()
        ensure_access(target, must_exist=False)
        entries = self.scan(src, res)
        if target != src and target.is_relative_to(src):
            entries = [e for e in entries if not e.path.is_relative_to(target)]
        meta = MetadataReader(self.cfg).read(entries, self.ctx)
        self.review_names(meta, p.pattern, p.filename)
        renderer = PatternRenderer(self.cfg)
        journal = self.journal = None if p.dry_run else Journal(self.cfg.data_dir, self.name)
        ops = self.ops = FileOps(self.cfg, self.hasher, journal, p.dry_run)
        self.report_target = target
        mode = "move" if p.mode == "move" else "copy"
        for i, e in enumerate(sorted(entries, key=lambda e: str(e.path)), 1):
            self.ctx.check()
            m = meta.get(e.path, Meta())
            dst = target / renderer.folder(p.pattern, e, m) / renderer.filename(p.filename, e, m)
            extra = {"date": m.date.strftime("%Y-%m-%d %H:%M:%S") if m.date else "",
                     "date_source": m.date_source, "camera": m.camera}
            if dst == e.path:
                res.rows.append({"status": "already_in_place", "file": str(e.path), **extra})
                continue
            r = ops.place(e, dst, mode, overwrite_ok=p.confirmed)
            row = self._row(r, **extra)
            if e.size == 0 and self.cfg.get("scan.empty_files_as_defect", True):
                row["info"] = (row["info"] + " – " + t("info.empty_file")).lstrip(" –")
            res.rows.append(row)
            self.ctx.progress("sort" if not p.dry_run else "preview", i, len(entries))
        res.summary = {"source": str(src), "target": str(target), "pattern": canonical(p.pattern),
                       "filename": canonical(p.filename) or t("summary.unchanged"), "mode": t(f"choice.{p.mode}"),
                       "files": len(entries),
                       "without_exif_date": sum(1 for m in meta.values() if m.date_source in ("", "file_mtime"))}
        return self.finish(res, ops, journal, target)


JOBS = {"sync": SyncJob, "duplicates": DupJob, "sort": SortJob}


def run_job(cfg: Config, ctx: RunContext, job: str, params) -> JobResult | None:
    j = JOBS[legacy.job_name(job)](cfg, ctx)
    try:
        return j.run(params)
    except Declined:
        ctx.message(t("msg.declined"))
        return None
    except Cancelled:
        ctx.message(t("msg.cancelled_partial"))
        return j.finish_cancelled(params.dry_run)
    finally:
        j.cache.close()
