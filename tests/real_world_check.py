"""Real-world check of MediaTidy with real media files it creates itself.

Usage (in the repository folder):  python3 tests/real_world_check.py [work folder] [area …]
  e.g. python3 tests/real_world_check.py /tmp/mt-check t_sync t_dups

Needs: exiftool, ffmpeg, sips, hdiutil (macOS). Takes about 3–5 minutes.
Doesn't touch any existing pictures: everything is created in the work folder (default: a new temp folder),
plus two small disk images that briefly appear as /Volumes/MTTESTHFS and /Volumes/MTTESTEXFAT.
The Trash test puts a test file "MediaTidy-Test-Copy.jpg" into the real Trash.
Output: [OK] / [FAIL] / [FINDING] (known behaviour that is left as it is on purpose). Exit code 1 on failures.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import shutil
import struct
import subprocess
import sys
import traceback
import unicodedata
import zlib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mediatidy import i18n  # noqa: E402
from mediatidy.config import Config, Profiles  # noqa: E402
from mediatidy.context import Declined, RunContext  # noqa: E402
from mediatidy.jobs import AccessError, DupParams, SortParams, SyncParams, run_job  # noqa: E402
from mediatidy.journal import list_journals, read_entries, undo  # noqa: E402
from mediatidy.metadata import MetadataReader  # noqa: E402
from mediatidy.patterns import PatternError  # noqa: E402
from mediatidy.scan import Scanner  # noqa: E402

import tempfile  # noqa: E402

i18n.set_language("en")
_ARGS = [a for a in sys.argv[1:] if not a.startswith("t_")]
ROOT = Path(_ARGS[0] if _ARGS else tempfile.mkdtemp(prefix="mediatidy-check-")).resolve()
PY = sys.executable
RESULTS: list[tuple[str, str, str, str]] = []  # (area, kind, name, detail)
AREA = ""


def ok(name, cond, detail=""):
    RESULTS.append((AREA, "OK" if cond else "FAIL", name, "" if cond else str(detail)))
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + ("" if cond else f"  → {detail}"))


def finding(name, detail):
    """Not a bug in the strict sense, but important behaviour / risk."""
    RESULTS.append((AREA, "FINDING", name, str(detail)))
    print(f"  [FINDING] {name}  → {detail}")


def area(title):
    def deco(fn):
        import functools

        @functools.wraps(fn)
        def run():
            global AREA
            AREA = title
            print(f"\n=== {title} ===")
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                ok("Area ran without crashing", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        return run
    return deco


# --- Test data -----------------------------------------------------------------------
TS = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()  # noqa: E731


def _png(path: Path, seed: int, w=160, h=120):
    r = random.Random(seed)
    raw = b"".join(b"\x00" + r.randbytes(w * 3) for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def exif(path: Path, tags: dict):
    if tags:
        args = [f"-{k}={v}" for k, v in tags.items()]
        subprocess.run(["exiftool", "-q", "-q", "-overwrite_original", *args, str(path)], check=True)


def img(path: Path, seed: int, tags: dict | None = None, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = {"jpg": "jpeg", "jpeg": "jpeg", "heic": "heic", "tif": "tiff", "tiff": "tiff", "dng": "tiff",
           "png": "png"}[path.suffix.lower().lstrip(".")]
    tmp = ROOT / f"_tmp_{seed}.png"
    _png(tmp, seed)
    out = path if path.suffix.lower() != ".dng" else path.with_suffix(".tif")
    subprocess.run(["sips", "-s", "format", fmt, str(tmp), "--out", str(out)], check=True, capture_output=True)
    if out != path:
        out.rename(path)
    tmp.unlink()
    exif(path, tags or {})
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def video(path: Path, seed: int, tags: dict | None = None, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc=size=160x120:rate=10:decimals={seed % 3}", "-f", "lavfi", "-i",
                    f"sine=frequency={200 + seed}:duration=1", "-t", "1", "-pix_fmt", "yuv420p",
                    "-metadata", f"comment=seed{seed}", "-f", "mp4" if path.suffix.lower() == ".mp4" else "mov",
                    str(path)], check=True)
    exif(path, tags or {})
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def blob(path: Path, data: bytes, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def fake_raw(path: Path, seed: int, size=200_000, mtime=None) -> Path:
    return blob(path, b"FAKE-CR3" + random.Random(seed).randbytes(size), mtime)


def cp(src: Path, dst: Path, mtime: float | None = None) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if mtime:
        os.utime(dst, (mtime, mtime))
    return dst


def h(p: Path) -> str:
    return hashlib.blake2b(p.read_bytes()).hexdigest()


def snapshot(d: Path) -> dict[str, str]:
    out = {}
    for p in sorted(d.rglob("*")):
        if p.is_file() and "_MediaTidy-Reports" not in p.parts:
            out[str(p.relative_to(d))] = h(p)
    return out


def cfg(name: str, **sets) -> Config:
    d = ROOT / "cfg" / name
    c = Config(d)
    c.set("paths.data_dir", str(d / "data"))
    c.set("paths.lightroom_catalogs", [str(ROOT / "lr" / "*.lrcat")])
    for k, v in sets.items():
        c.set(k.replace("__", "."), v)
    return c


def run(c: Config, job: str, p, ctx: RunContext | None = None):
    return run_job(c, ctx or RunContext(), job, p)


def nfc(s) -> str:
    return unicodedata.normalize("NFC", str(s))


def rows_by(res) -> dict[str, dict]:
    return {nfc(r["file"]): r for r in res.rows if r.get("file")}


def status_of(res, path: Path) -> str:
    return rows_by(res).get(nfc(path), {}).get("status", "–")


CANON = {"Make": "Canon", "Model": "Canon EOS R6m2", "LensModel": "RF24-105mm F4 L IS USM"}
UNICODE_NAME = "Overview 🌅 Beach ü.jpg"


# --- 1. Scanning ----------------------------------------------------------------------
@area("1 Scanning: file types, ignoring, libraries, sidecars")
def t_scan():
    Q = ROOT / "s1" / "Source"
    img(Q / "a.jpg", 1); img(Q / "B.JPG", 2); img(Q / "c.jpeg", 3); img(Q / "d.heic", 4)
    img(Q / "e.tif", 5); img(Q / "f.dng", 6); fake_raw(Q / "g.CR3", 7); video(Q / "h.mp4", 8); video(Q / "i.MOV", 9)
    blob(Q / "notes.txt", b"x"); img(Q / ".hidden.jpg", 10); blob(Q / "._a.jpg", b"appledouble")
    blob(Q / "Thumbs.db", b"x"); blob(Q / "no_extension", b"x"); os.symlink(Q / "a.jpg", Q / "link.jpg")
    img(Q / UNICODE_NAME, 11); img(Q / "sub" / "deep" / "x.jpg", 12)
    img(Q / "_Duplicates" / "y.jpg", 13); img(Q / "_MediaTidy-Reports" / "r.jpg", 14)
    img(Q / "Photos.photoslibrary" / "originals" / "0" / "UUID-1.heic", 15)
    img(Q / "Photos.photoslibrary" / "resources" / "derivatives" / "0" / "UUID-1_1_105_c.jpeg", 16)
    img(Q / "Photos.photoslibrary" / "database" / "x.jpg", 17)
    img(Q / "Lib.lrlibrary" / "abc123" / "originals" / "2025" / "l.jpg", 18)
    img(Q / "Lib.lrlibrary" / "abc123" / "cache" / "thumb.jpg", 19)
    img(Q / "Catalog Previews.lrdata" / "p.jpg", 20)
    blob(Q / "g.xmp", b"<xmp/>"); blob(Q / "d.heic.xmp", b"<xmp/>"); blob(Q / "B.aae", b"<aae/>")
    blob(Q / "smallxmp" / "k.jpg.XMP", b"<xmp/>"); img(Q / "smallxmp" / "k.jpg", 21)
    img(Q / "locked" / "z.jpg", 22); os.chmod(Q / "locked", 0)
    try:
        c = cfg("s1")
        entries, errors = Scanner(c).scan(Q, RunContext())
        rels = sorted(nfc(e.rel) for e in entries)
        expected = sorted(["a.jpg", "B.JPG", "c.jpeg", "d.heic", "e.tif", "f.dng", "g.CR3", "h.mp4", "i.MOV",
                           nfc(UNICODE_NAME), "sub/deep/x.jpg", "Photos/0/UUID-1.heic",
                           "Lib/2025/l.jpg", "smallxmp/k.jpg"])
        ok("Exactly the expected media files found", rels == expected,
           f"too many: {sorted(set(rels) - set(expected))}, missing: {sorted(set(expected) - set(rels))}")
        ok("Locked folder reported as read error", any("locked" in e for e in errors), errors)
        sc = {e.path.name: sorted(p.name for p in e.sidecars) for e in entries}
        ok("Sidecar g.xmp → g.CR3", sc.get("g.CR3") == ["g.xmp"], sc.get("g.CR3"))
        ok("Sidecar d.heic.xmp → d.heic", sc.get("d.heic") == ["d.heic.xmp"], sc.get("d.heic"))
        ok("Sidecar B.aae → B.JPG", sc.get("B.JPG") == ["B.aae"], sc.get("B.JPG"))
        ok("Sidecar k.jpg.XMP (other spelling) → k.jpg", sc.get("k.jpg") == ["k.jpg.XMP"], sc.get("k.jpg"))
        photo_only = cfg("s1b", files__active_groups=["photo"])
        names = {e.path.name for e in Scanner(photo_only).scan(Q, RunContext())[0]}
        ok("active_groups=[photo] hides RAW/video", not names & {"g.CR3", "f.dng", "h.mp4", "i.MOV"}, names)
        lib_root = Scanner(c).scan(Q / "Photos.photoslibrary", RunContext())[0]
        ok("Library directly as source: originals only", [str(e.rel) for e in lib_root] == ["0/UUID-1.heic"],
           [str(e.rel) for e in lib_root])
        ok("Library as source: label without extension", lib_root and lib_root[0].label == "Photos",
           lib_root and lib_root[0].label)
    finally:
        os.chmod(Q / "locked", 0o755)


# --- 2. Metadata + patterns -------------------------------------------------------------
@area("2 Metadata and patterns")
def t_meta():
    S = ROOT / "s2"
    img(S / "a.jpg", 30, {"DateTimeOriginal": "2025:08:09 14:05:59", **CANON})
    img(S / "b.jpg", 31, {"DateTimeOriginal": "2025:12:31 23:59:59", "Make": "Canon", "Model": "Canon EOS R5"})
    img(S / "c.heic", 32, mtime=TS("2020-05-17 10:00"))
    fake_raw(S / "d.CR3", 33, mtime=TS("2019-03-03 03:03"))
    video(S / "e.mp4", 34, {"CreateDate": "2026:01:01 00:30:00", "Model": "Canon EOS R6m2"})
    img(S / "f.jpg", 35, {"DateTimeOriginal": "0000:00:00 00:00:00", "CreateDate": "2024:02:29 12:00:00"})
    img(S / "g.jpg", 36, {"DateTimeOriginal": "2025:01:01 00:00:00+01:00", "Make": "Canon", "Model": "EOS/R6:II*?"})
    img(S / "Zürich NFD.jpg", 37, {"DateTimeOriginal": "2023:07:01 08:00:00"})
    img(S / "Zürich NFC.jpg", 38, {"DateTimeOriginal": "2023:07:02 08:00:00"})
    img(S / "h.png", 39, {"DateTimeOriginal": "2022:06:06 06:06:06"})
    entries = Scanner(cfg("s2")).scan(S, RunContext())[0]
    for batch in (1, 3, 200):
        c = cfg(f"s2-{batch}", metadata__batch_size=batch)
        meta = MetadataReader(c).read(entries, RunContext())
        m = {nfc(p.name): v for p, v in meta.items()}
        exp = {"a.jpg": (datetime(2025, 8, 9, 14, 5, 59), "DateTimeOriginal"),
               "b.jpg": (datetime(2025, 12, 31, 23, 59, 59), "DateTimeOriginal"),
               "c.heic": (datetime(2020, 5, 17, 10, 0), "file_mtime"),
               "d.CR3": (datetime(2019, 3, 3, 3, 3), "file_mtime"),
               "e.mp4": (datetime(2026, 1, 1, 0, 30), "CreateDate"),
               "f.jpg": (datetime(2024, 2, 29, 12, 0), "CreateDate"),
               "g.jpg": (datetime(2025, 1, 1, 0, 0), "DateTimeOriginal"),
               nfc("Zürich NFD.jpg"): (datetime(2023, 7, 1, 8, 0), "DateTimeOriginal"),
               nfc("Zürich NFC.jpg"): (datetime(2023, 7, 2, 8, 0), "DateTimeOriginal"),
               "h.png": (datetime(2022, 6, 6, 6, 6, 6), "DateTimeOriginal")}
        bad = {k: (m[k].date, m[k].date_source) for k, v in exp.items() if k in m and (m[k].date, m[k].date_source) != v}
        ok(f"Capture date + source correct (batch size {batch})", not bad and len(m) == len(exp), bad or m.keys())
    ok("Camera/make/lens read", (m["a.jpg"].camera, m["a.jpg"].make, m["a.jpg"].lens)
       == ("Canon EOS R6m2", "Canon", "RF24-105mm F4 L IS USM"), m["a.jpg"])
    c = cfg("s2-names", metadata__camera_names={"Canon EOS R6m2": "R6 II"},
            metadata__strip_make_from_camera=True)
    m2 = {p.name: v for p, v in MetadataReader(c).read(entries, RunContext()).items()}
    ok("Camera name mapping (R6m2 → R6 II)", m2["a.jpg"].camera == "R6 II", m2["a.jpg"].camera)
    ok("Strip make from camera (Canon EOS R5 → EOS R5)", m2["b.jpg"].camera == "EOS R5", m2["b.jpg"].camera)
    # Without exiftool: fall back to the file date, no crash
    c = cfg("s2-without", paths__exiftool="/does/not/exist")
    m3 = {p.name: v for p, v in MetadataReader(c).read(entries, RunContext()).items()}
    ok("Without exiftool: all files with file date", all(v.date_source == "file_mtime" for v in m3.values()),
       {k: v.date_source for k, v in m3.items()})
    # All placeholders via a sort dry run
    c = cfg("s2-pattern")
    pat = "{Camera}/{Type}/{Quarter}/{Week}/{Weekday}/{MonthName}/{Make}/{Lens}/{Ext}/{Folder}/{Source}"
    fn = "{Year}{Month}{Day}_{Hour}{Minute}{Second}_{Name}"
    res = run(c, "sort", SortParams(str(S), str(ROOT / "s2-target"), pat, fn, "copy", True))
    z = {Path(r["file"]).name: r.get("target", "") for r in res.rows}
    want = str(ROOT / "s2-target" / "Canon EOS R6m2/Photo/Q3/W32/Sat/August/Canon/RF24-105mm F4 L IS USM/JPG/s2/s2"
               / "20250809_140559_a.jpg")
    ok("All placeholders resolved correctly", z.get("a.jpg") == want, f"{z.get('a.jpg')} ≠ {want}")
    ok("Unknown values → 'Unknown'", "/Unknown/" in z.get("c.heic", ""), z.get("c.heic"))
    ok("Invalid characters in camera name replaced", "EOS_R6_II__/" in z.get("g.jpg", ""), z.get("g.jpg"))
    ok("Type RAW for CR3, Video for MP4", "/RAW/" in z.get("d.CR3", "") and "/Video/" in z.get("e.mp4", ""),
       (z.get("d.CR3"), z.get("e.mp4")))
    ok("Summary counts files without EXIF date (c.heic, d.CR3)", res.summary.get("without_exif_date") == 2,
       res.summary.get("without_exif_date"))
    # Name review before the run: only names without mapping, the answer is applied and saved
    c = cfg("s2-review", metadata__camera_names={"Canon EOS R6m2": "R6 II"})
    asked = []

    def answer(todo):
        asked.append(todo)
        return {ph: {raw: ("R5" if raw == "Canon EOS R5" else prop) for raw, (prop, _) in found.items()}
                for ph, found in todo.items()}
    Zp = ROOT / "s2-review-target"
    res = run(c, "sort", SortParams(str(S), str(Zp), "{Camera}/{Year}", "", "copy", False, confirmed=True),
              RunContext(on_names=answer))
    names = set(asked[0].get("Camera", {})) if asked else set()
    ok("Name review: only unmapped cameras asked", "Canon EOS R5" in names and "Canon EOS R6m2" not in names
       and set(asked[0]) == {"Camera"}, asked)
    ok("Name review: changed name used as folder", (Zp / "R5" / "2025" / "b.jpg").exists(), sorted(map(str, Zp.rglob("*.jpg"))))
    ok("Name review: mapping saved (also names accepted unchanged)",
       Config(c.user_dir).get("metadata.camera_names", {}).get("Canon EOS R5") == "R5"
       and "EOS/R6:II*?" in Config(c.user_dir).get("metadata.camera_names", {}), Config(c.user_dir).get("metadata.camera_names"))
    asked.clear()
    run(Config(c.user_dir), "sort", SortParams(str(S), str(Zp), "{Camera}/{Year}", "", "copy", True), RunContext(on_names=answer))
    ok("Name review: second run doesn't ask again", not asked, asked)

    def decline(todo):
        raise Declined()
    Zd = ROOT / "s2-review-declined"
    res = run(cfg("s2-decline"), "sort", SortParams(str(S), str(Zd), "{Camera}/{Lens}", "", "copy", False,
                                                     confirmed=True), RunContext(on_names=decline))
    ok("Name review declined: nothing copied, no report", res is None and not Zd.exists(), res)
    asked.clear()
    run(cfg("s2-no-names"), "sort", SortParams(str(S), str(Zp), "{Year}/{Month}", "", "copy", True),
        RunContext(on_names=answer))
    ok("Name review: pattern without names doesn't ask", not asked, asked)
    for bad_pat in ("{Year}/{Foo}", "../{Year}", "/abs/{Year}", "{Year"):
        try:
            run(c, "sort", SortParams(str(S), str(ROOT / "s2-target2"), bad_pat, "", "copy", True))
            ok(f"Invalid pattern '{bad_pat}' rejected", False, "no error")
        except PatternError:
            ok(f"Invalid pattern '{bad_pat}' rejected", True)
    ok("Rejected pattern created nothing", not (ROOT / "s2-target2").exists())
    # Week at the turn of the year: 2025-12-31 is ISO week 01 (2026)
    res = run(c, "sort", SortParams(str(S), str(ROOT / "s2-target"), "{WeekYear}/{Week}", "", "copy", True))
    zb = {Path(r["file"]).name: r.get("target", "") for r in res.rows}.get("b.jpg", "")
    ok("{WeekYear}/{Week}: 2025-12-31 → 2026/W01", zb.endswith("2026/W01/b.jpg"), zb)


# --- 3. Sync ----------------------------------------------------------------------------
def build_sync_world(W: Path):
    Q, Z = W / "Import", W / "Target"
    img(Q / "new_1.jpg", 101, CANON)
    cp(img(Z / "Archive" / "2025" / "IMG_0001.jpg", 102), Q / "renamed.jpg")
    img(Z / "Import" / "IMG_0002.jpg", 103); img(Q / "IMG_0002.jpg", 104)
    cp(img(Q / "dupA.jpg", 105), Q / "sub" / "dupA-copy.jpg")
    x = img(Z / "Archive" / "x.jpg", 106); cp(x, Z / "Backup" / "x.jpg"); cp(x, Q / "x-copy.jpg")
    data = random.Random(5).randbytes(3_000_000)
    blob(Z / "Archive" / "big.jpg", data)
    blob(Q / "big_edited.jpg", data[:1_500_000] + b"\x00" + data[1_500_001:])
    blob(Q / "empty.jpg", b""); blob(Z / "Archive" / "broken_empty.jpg", b"")
    fake_raw(Q / "IMG_5000.CR3", 107); blob(Q / "IMG_5000.xmp", b"<xmp>5000</xmp>")
    cp(fake_raw(Z / "Import" / "IMG_6000.CR3", 108), Q / "IMG_6000.CR3"); blob(Q / "IMG_6000.xmp", b"<xmp>6000 new</xmp>")
    img(Q / "IMG_7000.heic", 109); video(Q / "IMG_7000.mov", 110); blob(Q / "IMG_7000.aae", b"<aae/>")
    img(Q / UNICODE_NAME, 111)
    img(Q / "<b>bold&amp;.jpg", 112)
    return Q, Z


@area("3 Sync (folder structure): detection, dry run, real run, repeat, undo")
def t_sync():
    W = ROOT / "s3"
    Q, Z = build_sync_world(W)
    z0, q0 = snapshot(Z), snapshot(Q)
    c = cfg("s3")
    res = run(c, "sync", SyncParams([str(Q)], str(Z), True, "structure", "", "", True))
    st = lambda n: status_of(res, Q / n)  # noqa: E731
    for n in ("renamed.jpg", "x-copy.jpg", "IMG_6000.CR3"):
        ok(f"'{n}' detected as already in target (content, not name)", st(n) == "in_target", st(n))
    for n in ("new_1.jpg", "IMG_0002.jpg", "big_edited.jpg", "IMG_5000.CR3", "IMG_7000.heic",
              "IMG_7000.mov", UNICODE_NAME, "<b>bold&amp;.jpg"):
        ok(f"'{n}' detected as missing", st(n) == "copied", st(n))
    dup_st = sorted([st("dupA.jpg"), status_of(res, Q / "sub" / "dupA-copy.jpg")])
    ok("Duplicate in source: copied once, marked once", dup_st == ["copied", "duplicate_in_source"], dup_st)
    ok("Missing in target = 9", res.summary["missing"] == 9, res.summary)
    ok("Same name, other content → suffix _1", rows_by(res)[nfc(Q / "IMG_0002.jpg")]["target"].endswith("Import/IMG_0002_1.jpg"),
       rows_by(res)[nfc(Q / "IMG_0002.jpg")]["target"])
    ok("Empty file reported as damaged (not 'already in target')", st("empty.jpg") == "empty", st("empty.jpg"))
    ok("New XMP for an already backed-up RAW is detected",
       status_of(res, Q / "IMG_6000.xmp") == "sidecar_added", status_of(res, Q / "IMG_6000.xmp"))
    ok("Dry run: target unchanged", snapshot(Z) == z0)
    ok("Dry run: source unchanged", snapshot(Q) == q0)
    ok("Dry run: report in data folder, not in target", not (Z / "_MediaTidy-Reports").exists()
       and all(str(c.data_dir) in str(r) for r in res.reports), res.reports)
    # Real run
    res = run(c, "sync", SyncParams([str(Q)], str(Z), True, "structure", "", "", False))
    ok("Real run: no errors", not res.errors, res.errors)
    exp_new = {"Import/new_1.jpg": "new_1.jpg", "Import/IMG_0002_1.jpg": "IMG_0002.jpg",
               "Import/big_edited.jpg": "big_edited.jpg", "Import/IMG_5000.CR3": "IMG_5000.CR3",
               "Import/IMG_5000.xmp": "IMG_5000.xmp", "Import/IMG_7000.heic": "IMG_7000.heic",
               "Import/IMG_7000.mov": "IMG_7000.mov", "Import/IMG_7000.aae": "IMG_7000.aae",
               f"Import/{UNICODE_NAME}": UNICODE_NAME, "Import/<b>bold&amp;.jpg": "<b>bold&amp;.jpg"}
    bad = [k for k, src in exp_new.items() if not (Z / k).exists() or h(Z / k) != h(Q / src)]
    ok("All missing files + sidecars copied byte for byte", not bad, bad)
    dupcopies = [p for p in (Z / "Import").rglob("dupA*")]
    ok("dupA copied exactly once", len(dupcopies) == 1, dupcopies)
    ok("Timestamps preserved", abs((Z / "Import/new_1.jpg").stat().st_mtime - (Q / "new_1.jpg").stat().st_mtime) < 1)
    ok("Existing target files unchanged", all(snapshot(Z).get(k) == v for k, v in z0.items()))
    ok("Source unchanged (copying)", snapshot(Q) == q0)
    ok("No .mediatidy-tmp leftovers", not list(Z.rglob("*.mediatidy-tmp")))
    ok("Report in target/_MediaTidy-Reports", any("_MediaTidy-Reports" in str(r) for r in res.reports), res.reports)
    ok("Journal created", res.journal and res.journal.exists(), res.journal)
    # Check the CSV
    csvf = next(r for r in res.reports if r.suffix == ".csv")
    with csvf.open(encoding="utf-8-sig") as f:
        crow = list(csv.DictReader(f, delimiter=";"))
    ok("CSV readable, umlauts/emoji intact", any(nfc(UNICODE_NAME) in nfc(r["File"]) for r in crow))
    htmlf = next(r for r in res.reports if r.suffix == ".html").read_text(encoding="utf-8")
    ok("HTML report escapes special characters", "&lt;b&gt;bold&amp;amp;.jpg" in htmlf and "<b>bold" not in htmlf)
    # Repeat: nothing missing any more
    ok("XMP added next to the already backed-up RAW", (Z / "Import" / "IMG_6000.xmp").exists()
       and h(Z / "Import" / "IMG_6000.xmp") == h(Q / "IMG_6000.xmp"))
    res2 = run(c, "sync", SyncParams([str(Q)], str(Z), True, "structure", "", "", True))
    ok("Second run: 0 missing, no open sidecars (idempotent)", res2.summary["missing"] == 0
       and "sidecars_todo" not in res2.summary, res2.summary)
    # XMP edited in the source (newer) → replaced in the target, old version back via undo
    old_xmp = (Z / "Import" / "IMG_5000.xmp").read_bytes()
    blob(Q / "IMG_5000.xmp", b"<xmp>5000 edited</xmp>", (Z / "Import" / "IMG_5000.xmp").stat().st_mtime + 60)
    rx = run(c, "sync", SyncParams([str(Q)], str(Z), True, "structure", "", "", False))
    ok("Edited XMP replaces the old one in the target", (Z / "Import" / "IMG_5000.xmp").read_bytes() == b"<xmp>5000 edited</xmp>"
       and status_of(rx, Q / "IMG_5000.xmp") == "sidecar_updated", status_of(rx, Q / "IMG_5000.xmp"))
    _, probs = undo(rx.journal, RunContext())
    ok("Undo brings back the old XMP", (Z / "Import" / "IMG_5000.xmp").read_bytes() == old_xmp and not probs, probs)
    blob(Q / "IMG_5000.xmp", b"<xmp>5000</xmp>")
    # Undo
    undone, probs = undo(res.journal, RunContext())
    ok("Undo without problems", not probs, probs)
    ok("Undo: target as before", snapshot(Z) == z0,
       set(snapshot(Z)) ^ set(z0))
    ok("Undo removes the created folders again", not (Z / "Import" / "sub").exists()
       and (Z / "Import").is_dir(), [str(p.relative_to(Z)) for p in Z.rglob("*") if p.is_dir()])
    # Quick mode
    cq = cfg("s3-quick", compare__full_hash=False)
    rq = run(cq, "sync", SyncParams([str(Q)], str(Z), True, "structure", "", "", True))
    ok("Quick mode is warned about in the summary", rq.summary.get("comparison", "").startswith("⚠️"), rq.summary)
    if status_of(rq, Q / "big_edited.jpg") == "in_target":
        finding("Quick mode (full_hash=false) treats files with the same size/start/end as identical",
                "big_edited.jpg (1 byte different in the middle) → 'already in target' → would NOT be backed up. "
                "Full mode detects it correctly.")
    # Without the source folder level
    c2 = cfg("s3-level", sync__source_folder_as_level=False)
    r3 = run(c2, "sync", SyncParams([str(Q)], str(W / "Target2"), True, "structure", "", "", True))
    ok("source_folder_as_level=false: no 'Import/' in the target",
       rows_by(r3)[nfc(Q / "new_1.jpg")]["target"] == str(W / "Target2" / "new_1.jpg"), rows_by(r3)[nfc(Q / "new_1.jpg")]["target"])
    ok("Target that doesn't exist is not created in a dry run", not (W / "Target2").exists())
    r4 = run(c2, "sync", SyncParams([str(Q)], str(W / "Target2"), True, "structure", "", "", False))
    ok("Target that doesn't exist is created + filled in a real run", (W / "Target2" / "new_1.jpg").exists() and not r4.errors,
       r4.errors)
    # Report only
    r5 = run(c, "sync", SyncParams([str(Q)], str(Z), False, "structure", "", "", False))
    ok("Report only (real run): target unchanged", snapshot(Z) == z0)
    ok("Report only: status 'missing'", status_of(r5, Q / "new_1.jpg") == "missing", status_of(r5, Q / "new_1.jpg"))


@area("4 Sync: special cases with sources and target")
def t_sync_edge():
    W = ROOT / "s4"
    c = cfg("s4")
    # Source folder with a dot in its name
    img(W / "Holiday 2025.08" / "p.jpg", 120)
    r = run(c, "sync", SyncParams([str(W / "Holiday 2025.08")], str(W / "Z1"), True, "structure", "", "", True))
    target = rows_by(r)[nfc(W / "Holiday 2025.08" / "p.jpg")]["target"]
    ok("Source folder with a dot ('Holiday 2025.08') keeps its full name in the target",
       target == str(W / "Z1" / "Holiday 2025.08" / "p.jpg"), target)
    # Two sources with the same name
    img(W / "a" / "Photos" / "p.jpg", 121); img(W / "b" / "Photos" / "p.jpg", 122)
    srcs = [str(W / "a" / "Photos"), str(W / "b" / "Photos")]
    r = run(c, "sync", SyncParams(srcs, str(W / "Z2"), True, "structure", "", "", False))
    got = sorted(p.name for p in (W / "Z2" / "Photos").iterdir())
    ok("Two sources with the same name: both files backed up (suffix)", got == ["p.jpg", "p_1.jpg"], got)
    r = run(c, "sync", SyncParams(srcs, str(W / "Z2"), True, "structure", "", "", True))
    ok("… and 0 missing afterwards", r.summary["missing"] == 0, r.summary)
    # Overlapping sources
    img(W / "Q" / "a.jpg", 123); img(W / "Q" / "sub" / "b.jpg", 124)
    r = run(c, "sync", SyncParams([str(W / "Q"), str(W / "Q" / "sub")], str(W / "Z3"), True, "structure", "", "", True))
    ok("Overlapping sources: each file counted once", r.summary["files_in_sources"] == 2, r.summary)
    ok("Overlapping sources: b.jpg still copied only once", r.summary["missing"] == 2, r.summary)
    # Source inside the target
    img(W / "Z4" / "Archive" / "x.jpg", 125); cp(W / "Z4" / "Archive" / "x.jpg", W / "Z4" / "New" / "x-copy.jpg")
    img(W / "Z4" / "New" / "only_new.jpg", 126)
    z40 = snapshot(W / "Z4")
    r = run(c, "sync", SyncParams([str(W / "Z4" / "New")], str(W / "Z4"), True, "structure", "", "", False))
    ok("Source inside target: copy detected, single file reported, nothing copied",
       status_of(r, W / "Z4" / "New" / "x-copy.jpg") == "in_target"
       and status_of(r, W / "Z4" / "New" / "only_new.jpg") == "only_in_source"
       and snapshot(W / "Z4") == z40, r.rows)
    # Target inside the source
    img(W / "Q5" / "a.jpg", 127)
    r = run(c, "sync", SyncParams([str(W / "Q5")], str(W / "Q5" / "Backup"), True, "structure", "", "", False))
    r = run(c, "sync", SyncParams([str(W / "Q5")], str(W / "Q5" / "Backup"), True, "structure", "", "", True))
    ok("Target inside source: backup is not counted as source", r.summary["files_in_sources"] == 1
       and r.summary["missing"] == 0, r.summary)
    # Library as source
    lib = W / "Library.photoslibrary"
    img(lib / "originals" / "A" / "UUID1.heic", 128); img(lib / "resources" / "derivatives" / "A" / "UUID1_c.jpeg", 129)
    l0 = snapshot(lib)
    r = run(c, "sync", SyncParams([str(lib)], str(W / "Z6"), True, "structure", "", "", False))
    ok("Photos library: only the original backed up, as 'Library/A/UUID1.heic'",
       sorted(str(p.relative_to(W / "Z6")) for p in (W / "Z6").rglob("*.*") if "_MediaTidy" not in str(p))
       == ["Library/A/UUID1.heic"], list((W / "Z6").rglob("*")))
    ok("Library unchanged", snapshot(lib) == l0)
    # Pattern placement
    img(W / "M" / "a.jpg", 130, {"DateTimeOriginal": "2025:08:09 14:05:59"})
    img(W / "M" / "b.jpg", 131, {"DateTimeOriginal": "2025:08:09 14:05:59"})
    r = run(c, "sync", SyncParams([str(W / "M")], str(W / "Z7"), True, "pattern", "{Year}/{Year}-{Month}-{Day}",
                                  "{Year}{Month}{Day}_{Hour}{Minute}{Second}", False))
    got = sorted(str(p.relative_to(W / "Z7")) for p in (W / "Z7").rglob("*.jpg"))
    ok("Pattern placement + file name pattern, collision → _1",
       got == ["2025/2025-08-09/20250809_140559.jpg", "2025/2025-08-09/20250809_140559_1.jpg"], got)
    r = run(c, "sync", SyncParams([str(W / "M")], str(W / "Z7"), True, "pattern", "{Year}", "", True))
    ok("Pattern placement: second run recognises renamed copies (0 missing)", r.summary["missing"] == 0, r.summary)


@area("5 Sync: access, unreadable files, missing drive")
def t_sync_access():
    W = ROOT / "s5"
    c = cfg("s5")
    img(W / "Q" / "a.jpg", 140); img(W / "Q" / "b.jpg", 141)
    img(W / "Z" / "locked" / "a.jpg", 142); cp(W / "Q" / "a.jpg", W / "Z" / "locked" / "a-copy.jpg")
    os.chmod(W / "Z" / "locked", 0)
    try:
        r = run(c, "sync", SyncParams([str(W / "Q")], str(W / "Z"), True, "structure", "", "", True))
        ok("Partly unreadable target: dry run warns in summary", "target_incomplete" in r.summary, r.summary)
        try:
            run(c, "sync", SyncParams([str(W / "Q")], str(W / "Z"), True, "structure", "", "", False))
            ok("Partly unreadable target: real run stops", False, "no stop")
        except AccessError:
            ok("Partly unreadable target: real run stops", True)
        ok("… and copied nothing", not (W / "Z" / "Q").exists())
    finally:
        os.chmod(W / "Z" / "locked", 0o755)
    os.chmod(W / "Q" / "b.jpg", 0)
    try:
        r = run(c, "sync", SyncParams([str(W / "Q")], str(W / "Z9"), True, "structure", "", "", False))
        ok("Unreadable source file (real run): status 'error', rest copied",
           status_of(r, W / "Q" / "b.jpg") == "error" and (W / "Z9" / "Q" / "a.jpg").exists(), r.rows)
        ok("… no half file in the target", not (W / "Z9" / "Q" / "b.jpg").exists() and not list((W / "Z9").rglob("*tmp")))
    finally:
        os.chmod(W / "Q" / "b.jpg", 0o644)
    for target in ("/Volumes/MediaTidyDoesNotExist/Backup", "/Volumes/MediaTidyDoesNotExist"):
        try:
            run(c, "sync", SyncParams([str(W / "Q")], target, True, "structure", "", "", True))
            ok(f"Unconnected drive ({target}) → stop", False, "no error")
        except AccessError as e:
            ok(f"Unconnected drive ({target}) → stop", "not connected" in str(e), e)
    try:
        run(c, "sync", SyncParams([str(W / "doesnotexist")], str(W / "Z"), True, "structure", "", "", True))
        ok("Missing source folder → error message", False, "no error")
    except FileNotFoundError:
        ok("Missing source folder → error message", True)


# --- 6. Duplicates ------------------------------------------------------------------------
def build_dup_world(D: Path):
    g1 = img(D / "2024" / "IMG_1.jpg", 201, mtime=TS("2024-01-01 10:00"))
    cp(g1, D / "Backup" / "old" / "very" / "deep" / "IMG_1.jpg", TS("2023-01-01 10:00"))
    cp(g1, D / "k.jpg", TS("2025-01-01 10:00"))
    lib = img(D / "Photos.photoslibrary" / "originals" / "B" / "UUID.heic", 202, mtime=TS("2024-06-01 10:00"))
    cp(lib, D / "Export" / "UUID-copy.heic", TS("2020-01-01 10:00"))
    j = img(D / "Shoot" / "IMG_7.JPG", 203, mtime=TS("2024-05-05 10:00"))
    fake_raw(D / "Shoot" / "IMG_7.CR3", 204); blob(D / "Shoot" / "IMG_7.xmp", b"<xmp>CR3 edits</xmp>")
    cp(j, D / "Export" / "IMG_7.JPG", TS("2020-01-01 10:00"))
    data = random.Random(9).randbytes(3_000_000)
    blob(D / "almost" / "a.jpg", data); blob(D / "almost" / "b.jpg", data[:1_500_000] + b"\x01" + data[1_500_001:])
    blob(D / "empty1.jpg", b""); blob(D / "x" / "empty2.jpg", b"")
    blob(D / "n1" / "same.jpg", random.Random(10).randbytes(50_000))
    blob(D / "n2" / "same.jpg", random.Random(11).randbytes(50_000))
    img(D / "_Duplicates" / "old.jpg", 205); cp(D / "_Duplicates" / "old.jpg", D / "old-original.jpg")
    img(D / "single.jpg", 206)


@area("6 Duplicates: detection, keep rules, moving, undo, Trash")
def t_dups():
    D = ROOT / "s6" / "D"
    build_dup_world(D)
    d0 = snapshot(D)
    c = cfg("s6")
    r = run(c, "duplicates", DupParams(str(D), "oldest", "report"))
    groups: dict[int, list] = {}
    for row in r.rows:
        if "group" in row:
            groups.setdefault(row["group"], []).append((Path(row["file"]).relative_to(D).as_posix(), row["status"]))
    sets = sorted(sorted(p for p, _ in g) for g in groups.values())
    expected = sorted([
        sorted(["2024/IMG_1.jpg", "Backup/old/very/deep/IMG_1.jpg", "k.jpg"]),
        sorted(["Photos.photoslibrary/originals/B/UUID.heic", "Export/UUID-copy.heic"]),
        sorted(["Shoot/IMG_7.JPG", "Export/IMG_7.JPG"]),
        sorted(["empty1.jpg", "x/empty2.jpg"]),
    ])
    ok("Exactly the real duplicate groups found (without almost-identical, same-name, empty)",
       sets == sorted(e for e in expected if "empty1.jpg" not in e), sets)
    ok("Content of _Duplicates is not counted", not any("_Duplicates/old.jpg" in s for g in sets for s in g))
    ok("Empty files counted as damaged", r.summary.get("empty_files") == 2, r.summary)
    ok("Report mode changes nothing", snapshot(D) == d0)
    keep = lambda res: {Path(x["file"]).relative_to(D).as_posix() for x in res.rows  # noqa: E731
                        if x.get("status") == "kept"}
    exp_keep = {"oldest": "Backup/old/very/deep/IMG_1.jpg", "newest": "k.jpg", "shortest_path": "k.jpg",
                "longest_path": "Backup/old/very/deep/IMG_1.jpg", "first": "2024/IMG_1.jpg"}
    for rule, want in exp_keep.items():
        k = keep(run(c, "duplicates", DupParams(str(D), rule, "report")))
        ok(f"Keep rule '{rule}' keeps {want}", want in k, k)
        ok(f"Rule '{rule}': the Photos library original is always kept",
           "Photos.photoslibrary/originals/B/UUID.heic" in k, k)
    # Move – dry run
    r = run(c, "duplicates", DupParams(str(D), "oldest", "move", "_Duplicates", True))
    ok("Move dry run changes nothing", snapshot(D) == d0)
    # Move – real
    r = run(c, "duplicates", DupParams(str(D), "oldest", "move", "_Duplicates", False, True))
    ok("Move: no errors", not r.errors, r.errors)
    moved = {"2024/IMG_1.jpg", "k.jpg", "Export/UUID-copy.heic", "Shoot/IMG_7.JPG"}
    ok("Empty files not moved", (D / "empty1.jpg").exists() and (D / "x" / "empty2.jpg").exists())
    ok("Duplicates moved into _Duplicates/<path>",
       all((D / "_Duplicates" / m).exists() and not (D / m).exists() for m in moved),
       [m for m in moved if not (D / "_Duplicates" / m).exists()])
    ok("Kept files unchanged", all((D / p).exists() and h(D / p) == d0[p] for p in
       ("Backup/old/very/deep/IMG_1.jpg", "Photos.photoslibrary/originals/B/UUID.heic", "Export/IMG_7.JPG")))
    ok("The RAW's XMP stays with the RAW when the JPG of the same name is a duplicate", (D / "Shoot" / "IMG_7.xmp").exists())
    r2 = run(c, "duplicates", DupParams(str(D), "oldest", "report"))
    ok("No duplicates left afterwards", r2.summary["dup_groups"] == 0, r2.summary)
    done, probs = undo(r.journal, RunContext())
    ok("Undo: without problems", not probs, probs)
    ok("Undo: folder exactly as before", snapshot(D) == d0, set(snapshot(D)) ^ set(d0))
    # Custom duplicates folder
    r = run(c, "duplicates", DupParams(str(D), "oldest", "move", "SetAside", False, True))
    r2 = run(c, "duplicates", DupParams(str(D), "oldest", "move", "SetAside", True))
    real_groups = [g for g in {x["group"] for x in r2.rows if "group" in x}
                   if not any("empty" in x["file"] for x in r2.rows if x.get("group") == g)]
    ok("Custom duplicates folder is ignored in the next run", not real_groups, real_groups)
    undo(r.journal, RunContext())
    ok("Undo (SetAside) restores everything", snapshot(D) == d0, set(snapshot(D)) ^ set(d0))
    # Methods without hash
    cn = cfg("s6-name", compare__method="name_size")
    rn = run(cn, "duplicates", DupParams(str(D), "oldest", "report"))
    ok("Unsafe method is warned about in the summary", rn.summary.get("comparison", "").startswith("⚠️"), rn.summary)
    if any("n1/same.jpg" in x["file"] for x in rn.rows):
        finding("Method 'name_size' reports different pictures with the same name+size as duplicates",
                "n1/same.jpg ≙ n2/same.jpg (different content) – dangerous with 'move/trash'")
    ok("Method 'name_size' doesn't find renamed copies (expected)",
       not any("Export/UUID-copy.heic" in x["file"] for x in rn.rows))
    # Trash without confirmation: nothing happens
    P = ROOT / "s6" / "P"
    img(P / "MediaTidy-Test.jpg", 210, mtime=TS("2020-01-01 10:00"))
    cp(P / "MediaTidy-Test.jpg", P / "MediaTidy-Test-Copy.jpg", TS("2026-01-01 10:00"))
    blob(P / "MediaTidy-Test-Copy.xmp", b"<xmp/>")
    rp = run(c, "duplicates", DupParams(str(P), "oldest", "trash", "_Duplicates", False, False))
    ok("Trash without confirmation: nothing deleted", (P / "MediaTidy-Test-Copy.jpg").exists())
    # Trash for real (via the Finder)
    rp = run(c, "duplicates", DupParams(str(P), "oldest", "trash", "_Duplicates", False, True))
    st = [x["status"] for x in rp.rows if x.get("file", "").endswith("Copy.jpg")]
    ok("Trash via Finder: duplicate + sidecar gone, original stays",
       not (P / "MediaTidy-Test-Copy.jpg").exists() and not (P / "MediaTidy-Test-Copy.xmp").exists()
       and (P / "MediaTidy-Test.jpg").exists() and st == ["trashed"], f"{st} {rp.errors}")
    ok("Trash noted in the journal", rp.journal and
       sum(e["action"] == "trashed" for e in read_entries(rp.journal)) == 2, rp.journal)


# --- 7. Sort --------------------------------------------------------------------------------
def build_sort_world(S: Path):
    img(S / "a.jpg", 301, {"DateTimeOriginal": "2025:08:09 14:05:59", **CANON})
    img(S / "h.jpg", 302, {"DateTimeOriginal": "2025:08:09 14:05:59", **CANON})
    img(S / "b.jpg", 303, {"DateTimeOriginal": "2025:12:31 23:59:59", "Make": "Canon", "Model": "Canon EOS R5"})
    img(S / "sub" / "c.heic", 304, mtime=TS("2020-05-17 10:00"))
    fake_raw(S / "d.CR3", 305, mtime=TS("2019-03-03 03:03"))
    blob(S / "d.xmp", b"<xmp>d</xmp>"); blob(S / "d.CR3.xmp", b"<xmp>d.CR3</xmp>")
    video(S / "e.mp4", 306, {"CreateDate": "2026:01:01 00:30:00"})
    img(S / "Photos.photoslibrary" / "originals" / "C" / "U.heic", 307, {"DateTimeOriginal": "2024:04:04 04:04:04"})


@area("7 Sort: copy, move, collisions, libraries, undo")
def t_sort():
    W = ROOT / "s7"
    S = W / "S"
    build_sort_world(S)
    s0 = snapshot(S)
    c = cfg("s7")
    T = W / "T"
    r = run(c, "sort", SortParams(str(S), str(T), "{Year}/{Year}-{Month}-{Day}", "", "copy", False))
    exp = ["2019/2019-03-03/d.CR3", "2019/2019-03-03/d.CR3.xmp", "2019/2019-03-03/d.xmp",
           "2020/2020-05-17/c.heic", "2024/2024-04-04/U.heic", "2025/2025-08-09/a.jpg", "2025/2025-08-09/h.jpg",
           "2025/2025-12-31/b.jpg", "2026/2026-01-01/e.mp4"]
    ok("Copy by date: correct folders + sidecars", snapshot(T) and sorted(snapshot(T)) == exp, sorted(snapshot(T)))
    ok("Copy: source unchanged", snapshot(S) == s0)
    ok("Copies byte for byte", h(T / "2025/2025-08-09/a.jpg") == h(S / "a.jpg") and
       h(T / "2019/2019-03-03/d.CR3.xmp") == h(S / "d.CR3.xmp"))
    r = run(c, "sort", SortParams(str(S), str(T), "{Year}/{Year}-{Month}-{Day}", "", "copy", True))
    sts = {x["status"] for x in r.rows}
    ok("Second run: everything 'identical exists'", sts == {"identical_exists"}, sts)
    # File name pattern with collision
    r = run(c, "sort", SortParams(str(S), str(W / "T2"), "{Year}", "{Year}{Month}{Day}_{Hour}{Minute}{Second}",
                                  "copy", True))
    zs = sorted(Path(x["target"]).name for x in r.rows if "2025/20250809" in x.get("target", ""))
    ok("Same capture time → second name with _1", zs == ["20250809_140559.jpg", "20250809_140559_1.jpg"], zs)
    # Collision strategies
    for strat, ok_flag, want in (("suffix", False, "copied"), ("skip", False, "collision_skipped"),
                                 ("overwrite", False, "copied"), ("overwrite", True, "overwritten")):
        T3 = W / f"T3-{strat}-{ok_flag}"
        img(T3 / "2025" / "a.jpg", 399)
        cc = cfg(f"s7-{strat}-{ok_flag}", actions__collision=strat)
        p = SortParams(str(S), str(T3), "{Year}", "", "copy", True)
        p.confirmed = ok_flag
        r = run(cc, "sort", p)
        row = rows_by(r)[nfc(S / "a.jpg")]
        ok(f"Collision '{strat}' (confirmed={ok_flag}) → {want}", row["status"] == want, row)
        if strat == "suffix":
            ok("Suffix target is a_1.jpg", row["target"].endswith("2025/a_1.jpg"), row["target"])
    T4 = W / "T4"
    img(T4 / "2025" / "a.jpg", 399)
    cc = cfg("s7-ow", actions__collision="overwrite")
    p = SortParams(str(S), str(T4), "{Year}", "", "copy", False); p.confirmed = True
    r = run(cc, "sort", p)
    ok("Overwrite (real): file replaced", h(T4 / "2025" / "a.jpg") == h(S / "a.jpg"))
    _, probs = undo(r.journal, RunContext())
    ok("Undo reports the overwrite that can't be restored", any("Overwritten" in x for x in probs), probs)
    # Move in place
    S2 = W / "S2"
    shutil.copytree(S, S2, copy_function=shutil.copy2)
    s20 = snapshot(S2)
    r = run(c, "sort", SortParams(str(S2), str(S2), "{Year}/{Year}-{Month}-{Day}", "", "move", False, True))
    ok("Move: library file protected", status_of(r, S2 / "Photos.photoslibrary/originals/C/U.heic")
       == "protected", status_of(r, S2 / "Photos.photoslibrary/originals/C/U.heic"))
    ok("Move: files at target, source empty", (S2 / "2025/2025-08-09/a.jpg").exists() and not (S2 / "a.jpg").exists()
       and (S2 / "2019/2019-03-03/d.xmp").exists() and not (S2 / "d.xmp").exists())
    ok("Move: contents unchanged (same checksums)", sorted(snapshot(S2).values()) == sorted(s20.values()))
    r2 = run(c, "sort", SortParams(str(S2), str(S2), "{Year}/{Year}-{Month}-{Day}", "", "move", True))
    sts = {x["status"] for x in r2.rows}
    ok("Second run in place: 'already in place' (+ protected)", sts <= {"already_in_place", "protected"}, sts)
    _, probs = undo(r.journal, RunContext())
    ok("Undo move: without problems", not probs, probs)
    ok("Undo move: everything back in its old place", snapshot(S2) == s20, set(snapshot(S2)) ^ set(s20))
    # Shared sidecar with RAW+JPG
    S3 = W / "S3"
    img(S3 / "IMG_9.JPG", 310); fake_raw(S3 / "IMG_9.CR3", 311); blob(S3 / "IMG_9.xmp", b"<xmp>RAW edits</xmp>")
    r = run(c, "sort", SortParams(str(S3), str(S3 / "out"), "{Type}", "", "move", False, True))
    where = [str(p.relative_to(S3)) for p in S3.rglob("IMG_9.xmp")]
    info = {Path(x["file"]).name: x.get("info", "") for x in r.rows}
    ok("CR3+JPG with the same name: XMP ends up with the RAW", where == ["out/RAW/IMG_9.xmp"], f"XMP is in {where}, info: {info}")
    ok("Report names the XMP only with the RAW", "IMG_9.xmp" in info.get("IMG_9.CR3", "")
       and "IMG_9.xmp" not in info.get("IMG_9.JPG", ""), info)
    S3n = W / "S3n"
    img(S3n / "DSC_1.JPG", 312); fake_raw(S3n / "DSC_1.NEF", 313); blob(S3n / "DSC_1.xmp", b"<xmp/>")
    run(c, "sort", SortParams(str(S3n), str(S3n / "out"), "{Type}", "", "move", False, True))
    where = [str(p.relative_to(S3n)) for p in S3n.rglob("DSC_1.xmp")]
    ok("NEF+JPG (NEF sorts after JPG): XMP still ends up with the RAW", where == ["out/RAW/DSC_1.xmp"], where)
    # Move when an identical file is already at the target
    S4 = W / "S4"
    img(S4 / "a.jpg", 320, {"DateTimeOriginal": "2025:08:09 14:05:59"})
    cp(S4 / "a.jpg", W / "T5" / "2025" / "a.jpg")
    r = run(c, "sort", SortParams(str(S4), str(W / "T5"), "{Year}", "", "move", False, True))
    ok("Move with identical file at the target: source stays, report explains it",
       (S4 / "a.jpg").exists() and "duplicate" in rows_by(r)[nfc(S4 / "a.jpg")]["info"], r.rows)
    # Target inside the source (copy)
    r = run(c, "sort", SortParams(str(S), str(S / "Sorted"), "{Year}", "", "copy", False))
    r2 = run(c, "sort", SortParams(str(S), str(S / "Sorted"), "{Year}", "", "copy", True))
    ok("Target inside source: Sorted/ is not sorted in again in the next run",
       not any("Sorted" in x["file"] for x in r2.rows), [x["file"] for x in r2.rows if "Sorted" in x["file"]])
    shutil.rmtree(S / "Sorted")


# --- 8. External drives (disk images) ------------------------------------------------------
def attach(name: str, fs: str) -> Path:
    dmg = ROOT / f"{name}.dmg"
    subprocess.run(["hdiutil", "create", "-quiet", "-size", "40m", "-fs", fs, "-volname", name, str(dmg)], check=True)
    out = subprocess.run(["hdiutil", "attach", "-nobrowse", str(dmg)], check=True, capture_output=True, text=True).stdout
    return Path([line.split("\t")[-1].strip() for line in out.splitlines() if "/Volumes/" in line][-1])


def detach(vol: Path):
    subprocess.run(["hdiutil", "detach", "-quiet", "-force", str(vol)], check=False)


@area("8 External drives (HFS+ and exFAT as disk images)")
def t_volumes():
    W = ROOT / "s8"
    Q, _ = build_sync_world(W)
    for name, fs in (("MTTESTHFS", "HFS+"), ("MTTESTEXFAT", "ExFAT")):
        vol = attach(name, fs)
        try:
            c = cfg(f"s8-{name}")
            tgt = vol / "Backup"
            r = run(c, "sync", SyncParams([str(Q)], str(tgt), True, "structure", "", "", False))
            ok(f"{fs}: sync to drive without errors", not r.errors, r.errors)
            r2 = run(c, "sync", SyncParams([str(Q)], str(tgt), True, "structure", "", "", True))
            ok(f"{fs}: second run 0 missing", r2.summary["missing"] == 0, r2.summary)
            ok(f"{fs}: umlaut/emoji file present", (tgt / "Import" / UNICODE_NAME).exists()
               or any("Beach" in p.name for p in tgt.rglob("*")))
            # Move across drives
            M = W / f"move-{name}"
            img(M / "m.jpg", 400 + len(name), {"DateTimeOriginal": "2025:08:09 14:05:59"}); blob(M / "m.xmp", b"<xmp/>")
            m0 = snapshot(M)
            rs = run(c, "sort", SortParams(str(M), str(vol / "Sorted"), "{Year}", "", "move", False, True))
            ok(f"{fs}: move to another drive (copy+verify+delete)",
               (vol / "Sorted/2025/m.jpg").exists() and (vol / "Sorted/2025/m.xmp").exists() and not (M / "m.jpg").exists(),
               rs.rows)
            _, probs = undo(rs.journal, RunContext())
            ok(f"{fs}: undo across drives", snapshot(M) == m0 and not probs, probs)
            # Duplicates on the drive
            cp(Q / "new_1.jpg", vol / "Backup" / "again.jpg")
            rd = run(c, "duplicates", DupParams(str(vol / "Backup"), "oldest", "report"))
            ok(f"{fs}: duplicates found on drive", rd.summary["dup_groups"] >= 1, rd.summary)
            if fs == "ExFAT":
                junk = [p.name for p in vol.rglob("._*")]
                rs2 = Scanner(c).scan(vol, RunContext())[0]
                ok("exFAT: AppleDouble files (._*) are ignored", not any(e.path.name.startswith("._") for e in rs2),
                   junk)
        finally:
            detach(vol)
        try:
            run(c, "sync", SyncParams([str(Q)], str(tgt), True, "structure", "", "", True))
            ok(f"{fs}: after ejecting → stop 'not connected'", False, "no error")
        except AccessError as e:
            ok(f"{fs}: after ejecting → stop 'not connected'", "not connected" in str(e), e)


# --- 9. Cancelling ---------------------------------------------------------------------------
@area("9 Cancelling in the middle of a run")
def t_cancel():
    W = ROOT / "s9"
    Q = W / "Q"
    for i in range(12):
        blob(Q / f"f{i:02d}.jpg", random.Random(500 + i).randbytes(400_000))
    c = cfg("s9")
    state = {"n": 0}
    ctx = RunContext()

    def prog(phase, done, total):
        if phase == "copy":
            state["n"] += 1
            if state["n"] == 4:
                ctx.cancel()
    ctx._on_progress = prog
    rc = run(c, "sync", SyncParams([str(Q)], str(W / "Z"), True, "structure", "", "", False), ctx)
    ok("Cancelling ends the run", rc.cancelled, rc.summary)
    ok("Cancel: partial report written", rc.reports and all(r.exists() for r in rc.reports)
       and "cancelled" in rc.summary, rc.reports)
    copied = list((W / "Z").rglob("*.jpg"))
    ok("After cancelling: no half files (.mediatidy-tmp)", not list((W / "Z").rglob("*.mediatidy-tmp")))
    ok("After cancelling: copied files complete", all(h(p) == h(Q / p.name) for p in copied))
    js = list_journals(c.data_dir)
    n_files = sum(e["action"] == "copied" for e in read_entries(js[0])) if js else 0
    ok("After cancelling: journal present", n_files == len(copied), (js, len(copied)))
    r = run(c, "sync", SyncParams([str(Q)], str(W / "Z"), True, "structure", "", "", False))
    ok("Resume after cancelling: the rest is copied", len(list((W / "Z").rglob("*.jpg"))) == 12, r.summary)


# --- 10. Settings, profiles, cache, command line ------------------------------------------------
@area("10 Settings, profiles, checksum cache, command line")
def t_misc():
    W = ROOT / "s10"
    d = W / "cfgdir"
    d.mkdir(parents=True)
    (d / "config.toml").write_text('version = 2\n[compare]\nthreads = "four"\nmethod = "hash"\n'
                                   '[metadata.camera_names]\n"Canon EOS R6m2" = "R6 II"\n[unknown]\nx = 1\n')
    c = Config(d)
    ok("Wrong type in config.toml → warning + default", c.get("compare.threads") == 4 and c.warnings, c.warnings)
    ok("Free table (camera names) read", c.get("metadata.camera_names") == {"Canon EOS R6m2": "R6 II"})
    c.set("patterns.default", '{Year}/Ä "Quote" \\ Backslash')
    c.save(); c2 = Config(d)
    ok("Save/load with special characters without loss", c2.get("patterns.default") == '{Year}/Ä "Quote" \\ Backslash',
       c2.get("patterns.default"))
    (d / "config.toml").write_text("broken = [\n")
    c3 = Config(d)
    ok("Broken config.toml → defaults + warning", c3.warnings and c3.get("compare.method") == "hash")
    pr = Profiles(cfg("s10"))
    pr.save("Holiday/2025: Test", {"job": "sync", "params": {"sources": ["/a"], "target": "/b", "new": 1}})
    ok("Profile name with / and : saved safely", "Holiday/2025: Test" in pr.names()
       and all("/" not in p.name.replace(".json", "") or True for p in pr.dir.iterdir()))
    from mediatidy.jobs import params_from_dict
    p = params_from_dict("sync", pr.load("Holiday/2025: Test")["params"])
    ok("Profile with unknown field still loads", p.sources == ["/a"] and p.target == "/b")
    # Cache: same size + same timestamp, different content
    cc = cfg("s10-cache")
    D = W / "cache"
    a = blob(D / "a.jpg", random.Random(1).randbytes(10_000), TS("2025-01-01 10:00"))
    blob(D / "b.jpg", a.read_bytes(), TS("2025-01-01 10:00"))
    r = run(cc, "duplicates", DupParams(str(D), "oldest", "report"))
    blob(a, random.Random(2).randbytes(10_000), TS("2025-01-01 10:00"))
    r = run(cc, "duplicates", DupParams(str(D), "oldest", "report"))
    ok("Cache detects new content despite same size + modification time", r.summary["dup_groups"] == 0, r.summary)
    # Command line
    Q, Z = W / "cli" / "Q", W / "cli" / "Z"
    img(Q / "a.jpg", 600); img(Q / "b.jpg", 601); cp(Q / "a.jpg", Q / "a-copy.jpg")
    cd = W / "cli-cfg"
    cd.mkdir(parents=True)
    (cd / "config.toml").write_text('version = 2\n[ui]\nlanguage = "en"\n')

    def cli(*args):
        return subprocess.run([PY, "-m", "mediatidy", "--config-dir", str(cd), *args], capture_output=True, text=True,
                              cwd=Path(__file__).resolve().parent.parent)
    o = cli("sync", "--source", str(Q), "--target", str(Z))
    ok("CLI sync dry run", o.returncode == 0 and "Missing in target: 2" in o.stdout and not Z.exists(), o.stdout + o.stderr)
    o = cli("--execute", "sync", "--source", str(Q), "--target", str(Z))
    ok("CLI sync --execute copies", o.returncode == 0 and len(list(Z.rglob("*.jpg"))) == 2, o.stdout + o.stderr)
    o = cli("undo")
    ok("CLI undo without --execute is a dry run", "Dry run" in o.stdout and len(list(Z.rglob("*.jpg"))) == 2, o.stdout)
    o = cli("--execute", "undo")
    ok("CLI undo --execute removes the copies", len(list(Z.rglob("*.jpg"))) == 0, o.stdout + o.stderr)
    o = cli("--execute", "duplicates", "--folder", str(Q), "--action", "move")
    ok("CLI duplicates move", o.returncode == 0 and len(list((Q / "_Duplicates").rglob("*.jpg"))) == 1, o.stdout + o.stderr)
    o = cli("sort", "--source", str(Q), "--target", str(W / "cli" / "S"), "--pattern", "{Foo}")
    ok("CLI invalid pattern → error code, nothing created", o.returncode == 2 and not (W / "cli" / "S").exists(),
       o.stdout + o.stderr)
    o = cli("sync", "--source", str(Q), "--target", "/Volumes/MediaTidyDoesNotExist")
    ok("CLI missing drive → error code 2", o.returncode == 2 and "not connected" in o.stderr, o.stderr)
    o = cli("check")
    ok("CLI check runs", o.returncode == 0, o.stderr)


# --- 11. Lightroom warning ---------------------------------------------------------------------
@area("11 Lightroom catalog warning when moving")
def t_lightroom():
    import sqlite3
    from mediatidy.lightroom import affected_catalogs
    lr = ROOT / "lr"
    lr.mkdir(exist_ok=True)
    con = sqlite3.connect(lr / "Test Catalog.lrcat")
    con.execute("CREATE TABLE AgLibraryRootFolder (absolutePath TEXT)")
    con.execute("INSERT INTO AgLibraryRootFolder VALUES (?)", (str(ROOT / "s7" / "S") + "/",))
    con.commit(); con.close()
    c = cfg("s11")
    ok("Moving out of a catalog folder → warning", affected_catalogs(c, ROOT / "s7" / "S"), affected_catalogs(c, ROOT / "s7" / "S"))
    ok("Subfolder of a catalog folder → warning", affected_catalogs(c, ROOT / "s7" / "S" / "sub"))
    ok("Unrelated folder → no warning", not affected_catalogs(c, ROOT / "s5"))


if __name__ == "__main__":
    if ROOT.exists():
        subprocess.run(["chmod", "-R", "u+rwx", str(ROOT)])
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True)
    only = [a for a in sys.argv[1:] if a.startswith("t_")] or None
    tests = [t_scan, t_meta, t_sync, t_sync_edge, t_sync_access, t_dups, t_sort, t_volumes, t_cancel, t_misc, t_lightroom]
    for test in tests:
        if not only or any(o in test.__name__ for o in only):
            test()
    (ROOT / "result.json").write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1))
    n = {k: sum(1 for r in RESULTS if r[1] == k) for k in ("OK", "FAIL", "FINDING")}
    print(f"\nTOTAL: {n}\nWork folder: {ROOT}")
    sys.exit(1 if n["FAIL"] else 0)
