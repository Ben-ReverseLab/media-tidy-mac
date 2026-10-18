"""Command line: python3 -m mediatidy <command> …  (without a command the GUI starts)."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import Config, Profiles
from .context import Cancelled, RunContext
from .deps import check, setup_logging, tk_available
from .i18n import set_language, t
from .jobs import (DupParams, SortParams, SyncParams, default_dup_folder, params_from_dict, run_job,
                   summary_items)
from .journal import is_undone, list_journals, undo


def _ctx() -> RunContext:
    last = {"phase": None, "pct": -1}

    def prog(phase, done, total):
        label = t(f"phase.{phase}")
        if total:
            pct = done * 100 // total
            if pct == last["pct"] and phase == last["phase"] and done < total:
                return
            last["pct"] = pct
            end = "\n" if done >= total else ""
            print(f"\r  {label}: {done}/{total} ({pct} %)", end=end, flush=True)
        elif phase != last["phase"]:
            print(f"  {label} …")
        last["phase"] = phase

    return RunContext(on_message=print, on_progress=prog)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="mediatidy", description=t("cli.description"))
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--config-dir", help=t("cli.config_dir"))
    ap.add_argument("--execute", action="store_true", help=t("cli.execute"))
    ap.add_argument("--yes", action="store_true", help=t("cli.yes"))
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("sync", help=t("cli.sync"))
    s.add_argument("--source", action="append", default=[], help=t("cli.sources"))
    s.add_argument("--target", help=t("cli.target"))
    s.add_argument("--report-only", action="store_true", help=t("cli.report_only"))
    s.add_argument("--placement", choices=["structure", "pattern"], help=t("cli.placement"))
    s.add_argument("--pattern", help=t("cli.pattern"))
    s.add_argument("--filename", default="", help=t("cli.filename"))

    d = sub.add_parser("duplicates", help=t("cli.duplicates"))
    d.add_argument("--folder", help=t("cli.folder"))
    d.add_argument("--keep", choices=["oldest", "newest", "shortest_path", "longest_path", "first"], help=t("cli.keep"))
    d.add_argument("--action", choices=["report", "move", "trash"], help=t("cli.action"))

    o = sub.add_parser("sort", help=t("cli.sort"))
    o.add_argument("--source", help=t("cli.source"))
    o.add_argument("--target", help=t("cli.target"))
    o.add_argument("--pattern", help=t("cli.pattern"))
    o.add_argument("--filename", default="", help=t("cli.filename"))
    o.add_argument("--move", action="store_true", help=t("cli.move"))

    for p in (s, d, o):
        p.add_argument("--profile", help=t("cli.profile"))

    sub.add_parser("profiles", help=t("cli.profiles"))
    u = sub.add_parser("undo", help=t("cli.undo"))
    u.add_argument("--file", help=t("cli.undo_file"))
    sub.add_parser("check", help=t("cli.check"))
    return ap


def main(argv: list[str] | None = None) -> int:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config-dir")
    cfg = Config(pre.parse_known_args(argv)[0].config_dir)
    set_language(cfg.get("ui.language", "auto"))
    ap = build_parser()
    a = ap.parse_args(argv)
    setup_logging(cfg)

    if a.cmd is None:
        if not tk_available():
            print(t("cli.no_tk"), file=sys.stderr)
            return 1
        from .gui import run_gui
        run_gui(cfg)
        return 0
    if a.cmd == "check":
        hints = check(cfg)
        print("\n".join(hints) if hints else t("cli.all_good"))
        return 0
    if a.cmd == "profiles":
        for n in Profiles(cfg).names():
            print(n)
        return 0
    ctx = _ctx()
    if a.cmd == "undo":
        files = [f for f in list_journals(cfg.data_dir) if not is_undone(f)]
        target = next((f for f in files if a.file and a.file in str(f)), files[0] if files else None)
        if not target:
            print(t("cli.nothing_to_undo"))
            return 1
        done, problems = undo(target, ctx, dry_run=not a.execute)
        print(t("cli.undone" if a.execute else "cli.undo_preview", n=done, name=target.name))
        for p in problems:
            print("  ⚠️", p)
        return 0

    dry = not a.execute
    if a.profile:
        prof = Profiles(cfg).load(a.profile)
        params = params_from_dict(prof.get("job", a.cmd), prof.get("params", {}))
        params.dry_run = dry
    elif a.cmd == "sync":
        if not a.source or not a.target:
            ap.error(t("cli.need_source_target"))
        params = SyncParams(a.source, a.target, not a.report_only,
                            a.placement or cfg.get("sync.missing_placement"),
                            a.pattern or cfg.get("patterns.default"), a.filename, dry)
    elif a.cmd == "duplicates":
        if not a.folder:
            ap.error(t("cli.need_folder"))
        params = DupParams(a.folder, a.keep or cfg.get("duplicates.keep"),
                           a.action or cfg.get("duplicates.action"), default_dup_folder(cfg), dry)
    else:
        if not a.source or not a.target:
            ap.error(t("cli.need_source_target"))
        params = SortParams(a.source, a.target, a.pattern or cfg.get("patterns.default"),
                            a.filename or cfg.get("patterns.filename"),
                            "move" if a.move else "copy", dry)
    params.confirmed = a.yes or not cfg.get("actions.confirm", True)
    try:
        res = run_job(cfg, ctx, a.cmd, params)
    except Cancelled:
        print(t("msg.cancelled"))
        return 130
    except (OSError, ValueError) as e:
        print(t("cli.error", error=str(e)), file=sys.stderr)
        return 2
    print()
    for k, v in summary_items(res):
        print(f"  {k}: {v}")
    for r in res.reports:
        print("  " + t("msg.report", path=str(r)))
    if res.cancelled:
        print("\n" + t("cli.cancelled_incomplete"))
        return 130
    if dry:
        print("\n" + t("cli.dry_run_hint"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
