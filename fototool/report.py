"""CSV- und HTML-Berichte."""
from __future__ import annotations

import csv
import html
from datetime import datetime
from pathlib import Path

from .config import Config


def report_dir(cfg: Config, target: Path | None, dry_run: bool) -> Path:
    custom = (cfg.get("berichte.ordner") or "").strip()
    if custom:
        d = Path(custom).expanduser()
    elif dry_run or target is None:
        d = cfg.data_dir / "berichte"  # Probelauf verändert den Zielordner nicht
    else:
        d = target / "_FotoTool-Berichte"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_report(cfg: Config, directory: Path, job: str, title: str, summary: dict[str, object],
                 columns: list[str], rows: list[dict], dry_run: bool) -> list[Path]:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    base = directory / f"{stamp}_{job}{'_PROBELAUF' if dry_run else ''}"
    written: list[Path] = []
    if cfg.get("berichte.csv", True):
        p = base.with_suffix(".csv")
        with p.open("w", newline="", encoding="utf-8-sig") as f:  # BOM -> Excel erkennt Umlaute
            w = csv.DictWriter(f, fieldnames=columns, delimiter=";", extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        written.append(p)
    if cfg.get("berichte.html", True):
        p = base.with_suffix(".html")
        esc = html.escape
        summ = "".join(f"<tr><th>{esc(str(k))}</th><td>{esc(str(v))}</td></tr>" for k, v in summary.items())
        head = "".join(f"<th>{esc(c)}</th>" for c in columns)
        body = "\n".join("<tr>" + "".join(f"<td>{esc(str(r.get(c, '')))}</td>" for c in columns) + "</tr>"
                         for r in rows)
        p.write_text(f"""<!doctype html><html lang="de"><meta charset="utf-8">
<title>{esc(title)}</title>
<style>
body{{font:14px -apple-system,system-ui,sans-serif;margin:24px;color:#222;background:#fff}}
table{{border-collapse:collapse;margin:12px 0}} th,td{{border:1px solid #ddd;padding:4px 8px;text-align:left;font-size:13px}}
th{{background:#f4f4f4}} tbody tr:nth-child(even){{background:#fafafa}}
.dry{{background:#fff3cd;padding:8px 12px;border-radius:6px;display:inline-block}}
input{{padding:6px;width:320px;margin:8px 0}}
@media (prefers-color-scheme:dark){{body{{background:#1e1e1e;color:#ddd}} th{{background:#333}} th,td{{border-color:#444}}
tbody tr:nth-child(even){{background:#262626}} .dry{{background:#5c4b14}}}}
</style>
<h1>{esc(title)}</h1>
{'<p class="dry">Probelauf – es wurde nichts verändert.</p>' if dry_run else ''}
<p>Erstellt: {datetime.now():%d.%m.%Y %H:%M}</p>
<table>{summ}</table>
<input placeholder="Filtern…" oninput="for(const r of document.querySelectorAll('#d tbody tr'))r.hidden=!r.textContent.toLowerCase().includes(this.value.toLowerCase())">
<table id="d"><thead><tr>{head}</tr></thead><tbody>
{body}
</tbody></table></html>""", encoding="utf-8")
        written.append(p)
    return written
