"""CSV and HTML reports."""
from __future__ import annotations

import csv
import html
import logging
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import Config
from .i18n import language, t

log = logging.getLogger("mediatidy")

TARGET_REPORT_DIR = "_MediaTidy-Reports"


def report_dirs(cfg: Config, target: Path | None, dry_run: bool) -> list[Path]:
    """The first folder is local: browsers often may not read external drives, but the report must
    always open. Real runs also put a copy into <target>/_MediaTidy-Reports."""
    custom = (cfg.get("reports.folder") or "").strip()
    if custom:
        return [Path(custom).expanduser()]
    dirs = [cfg.data_dir / "reports"]
    if not dry_run and target is not None:  # a dry run doesn't change the target
        dirs.append(target / TARGET_REPORT_DIR)
    return dirs


def write_report(cfg: Config, directories: list[Path], job: str, title: str, summary: list[tuple[str, object]],
                 headers: list[str], rows: list[list[object]], dry_run: bool,
                 status_column: int | None = None) -> list[Path]:
    directory, *copies = directories
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    name = f"{stamp}_{job}{'_DRYRUN' if dry_run else ''}"
    base, n = directory / name, 2
    while base.with_suffix(".csv").exists() or base.with_suffix(".html").exists():  # run in the same second
        base, n = directory / f"{name}_{n}", n + 1
    written: list[Path] = []
    if cfg.get("reports.csv", True):
        p = base.with_suffix(".csv")
        with p.open("w", newline="", encoding="utf-8-sig") as f:  # BOM -> Excel detects umlauts
            w = csv.writer(f, delimiter=";")
            w.writerow(headers)
            w.writerows(rows)
        written.append(p)
    if cfg.get("reports.html", True):
        p = base.with_suffix(".html")
        esc = html.escape
        summ = "".join(f"<tr><th>{esc(str(k))}</th><td>{esc(str(v))}</td></tr>" for k, v in summary)
        head = "".join(f"<th>{esc(c)}</th>" for c in headers)
        status = (lambda r: str(r[status_column])) if status_column is not None else (lambda r: "")
        body = "\n".join(f'<tr data-status="{esc(status(r))}">' + "".join(f"<td>{esc(str(c))}</td>" for c in r)
                         + "</tr>" for r in rows)
        counts = Counter(status(r) for r in rows)
        status_filter = ""
        if status_column is not None and len(counts) > 1:  # one status only: nothing to choose
            options = "".join(f'<option value="{esc(k)}">{esc(k)} ({n})</option>' for k, n in counts.most_common())
            status_filter = (f'<select id="s"><option value="">{esc(t("report.all_statuses", n=len(rows)))}</option>'
                             f"{options}</select>")
        p.write_text(f"""<!doctype html><html lang="{language()}"><meta charset="utf-8">
<title>{esc(title)}</title>
<style>
body{{font:14px -apple-system,system-ui,sans-serif;margin:24px;color:#222;background:#fff}}
table{{border-collapse:collapse;margin:12px 0}} th,td{{border:1px solid #ddd;padding:4px 8px;text-align:left;font-size:13px}}
th{{background:#f4f4f4}} tbody tr:nth-child(even){{background:#fafafa}}
.dry{{background:#fff3cd;padding:8px 12px;border-radius:6px;display:inline-block}}
input,select{{padding:6px;margin:8px 8px 8px 0;font:inherit}} input{{width:320px}}
@media (prefers-color-scheme:dark){{body{{background:#1e1e1e;color:#ddd}} th{{background:#333}} th,td{{border-color:#444}}
tbody tr:nth-child(even){{background:#262626}} .dry{{background:#5c4b14}}}}
</style>
<h1>{esc(title)}</h1>
{f'<p class="dry">{esc(t("report.dry_note"))}</p>' if dry_run else ''}
<p>{esc(t("report.created", time=datetime.now().strftime(t("format.datetime"))))}</p>
<table>{summ}</table>
<input id="q" placeholder="{esc(t("report.filter"))}">{status_filter}
<table id="d"><thead><tr>{head}</tr></thead><tbody>
{body}
</tbody></table>
<script>
const q = document.getElementById("q"), s = document.getElementById("s");
function filter() {{
  const text = q.value.toLowerCase(), st = s ? s.value : "";
  for (const r of document.querySelectorAll("#d tbody tr"))
    r.hidden = !(r.textContent.toLowerCase().includes(text) && (!st || r.dataset.status === st));
}}
q.addEventListener("input", filter);
if (s) s.addEventListener("change", filter);
</script></html>""", encoding="utf-8")
        written.append(p)
    local = list(written)
    for d in copies:
        try:
            d.mkdir(parents=True, exist_ok=True)
            written += [Path(shutil.copy2(p, d)) for p in local]
        except OSError as e:
            log.warning("Copying the report to %s failed: %s", d, e)
    return written
