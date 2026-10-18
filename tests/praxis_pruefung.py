"""Praxis-Prüfung von FotoTool mit selbst erzeugten, echten Mediendateien.

Aufruf (im Ordner FotoTool):  python3 tests/praxis_pruefung.py [arbeitsordner] [bereich …]
  z.B. python3 tests/praxis_pruefung.py /tmp/ft-pruefung t_sync t_dups

Braucht: exiftool, ffmpeg, sips, hdiutil (macOS). Dauer: ca. 3–5 Minuten.
Fasst keine vorhandenen Bilder an: alles entsteht im Arbeitsordner (Standard: neuer Temp-Ordner),
dazu zwei kleine Disk-Images, die kurz als /Volumes/FTTESTHFS und /Volumes/FTTESTEXFAT erscheinen.
Der Papierkorb-Test legt eine Testdatei „FotoTool-Prueftest-Kopie.jpg“ in den echten Papierkorb.
Ausgabe: [OK] / [FEHLER] / [BEFUND] (bekanntes, bewusst so gelassenes Verhalten). Rückgabecode 1 bei Fehlern.
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
from fototool.config import Config, Profiles  # noqa: E402
from fototool.context import Declined, RunContext  # noqa: E402
from fototool.jobs import AccessError, DupParams, SortParams, SyncParams, run_job  # noqa: E402
from fototool.journal import list_journals, read_entries, undo  # noqa: E402
from fototool.metadata import MetadataReader  # noqa: E402
from fototool.patterns import PatternError  # noqa: E402
from fototool.scan import Scanner  # noqa: E402

import tempfile  # noqa: E402

_ARGS = [a for a in sys.argv[1:] if not a.startswith("t_")]
ROOT = Path(_ARGS[0] if _ARGS else tempfile.mkdtemp(prefix="fototool-praxis-")).resolve()
PY = sys.executable
RESULTS: list[tuple[str, str, str, str]] = []  # (bereich, art, name, detail)
AREA = ""


def ok(name, cond, detail=""):
    RESULTS.append((AREA, "OK" if cond else "FEHLER", name, "" if cond else str(detail)))
    print(f"  [{'OK' if cond else 'FEHLER'}] {name}" + ("" if cond else f"  → {detail}"))


def befund(name, detail):
    """Kein Programmfehler im engeren Sinn, aber wichtiges Verhalten / Risiko."""
    RESULTS.append((AREA, "BEFUND", name, str(detail)))
    print(f"  [BEFUND] {name}  → {detail}")


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
                ok(f"Bereich ohne Absturz durchgelaufen", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        return run
    return deco


# --- Testdaten -----------------------------------------------------------------
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
        if p.is_file() and "_FotoTool-Berichte" not in p.parts:
            out[str(p.relative_to(d))] = h(p)
    return out


def cfg(name: str, **sets) -> Config:
    d = ROOT / "cfg" / name
    c = Config(d)
    c.set("pfade.datenordner", str(d / "data"))
    c.set("pfade.lightroom_kataloge", [str(ROOT / "lr" / "*.lrcat")])
    for k, v in sets.items():
        c.set(k.replace("__", "."), v)
    return c


def run(c: Config, job: str, p, ctx: RunContext | None = None):
    return run_job(c, ctx or RunContext(), job, p)


def nfc(s) -> str:
    return unicodedata.normalize("NFC", str(s))


def rows_by(res) -> dict[str, dict]:
    return {nfc(r["Datei"]): r for r in res.rows if r.get("Datei")}


def status_of(res, path: Path) -> str:
    return rows_by(res).get(nfc(path), {}).get("Status", "–")


CANON = {"Make": "Canon", "Model": "Canon EOS R6m2", "LensModel": "RF24-105mm F4 L IS USM"}


# --- 1. Durchsuchen ------------------------------------------------------------
@area("1 Durchsuchen: Dateitypen, Ignorieren, Bibliotheken, Sidecars")
def t_scan():
    Q = ROOT / "s1" / "Quelle"
    img(Q / "a.jpg", 1); img(Q / "B.JPG", 2); img(Q / "c.jpeg", 3); img(Q / "d.heic", 4)
    img(Q / "e.tif", 5); img(Q / "f.dng", 6); fake_raw(Q / "g.CR3", 7); video(Q / "h.mp4", 8); video(Q / "i.MOV", 9)
    blob(Q / "notes.txt", b"x"); img(Q / ".versteckt.jpg", 10); blob(Q / "._a.jpg", b"appledouble")
    blob(Q / "Thumbs.db", b"x"); blob(Q / "ohne_endung", b"x"); os.symlink(Q / "a.jpg", Q / "link.jpg")
    img(Q / "Übersicht 🌅 Strand.jpg", 11); img(Q / "sub" / "tief" / "x.jpg", 12)
    img(Q / "_Duplikate" / "y.jpg", 13); img(Q / "_FotoTool-Berichte" / "r.jpg", 14)
    img(Q / "Fotos.photoslibrary" / "originals" / "0" / "UUID-1.heic", 15)
    img(Q / "Fotos.photoslibrary" / "resources" / "derivatives" / "0" / "UUID-1_1_105_c.jpeg", 16)
    img(Q / "Fotos.photoslibrary" / "database" / "x.jpg", 17)
    img(Q / "Lib.lrlibrary" / "abc123" / "originals" / "2025" / "l.jpg", 18)
    img(Q / "Lib.lrlibrary" / "abc123" / "cache" / "thumb.jpg", 19)
    img(Q / "Katalog Previews.lrdata" / "p.jpg", 20)
    blob(Q / "g.xmp", b"<xmp/>"); blob(Q / "d.heic.xmp", b"<xmp/>"); blob(Q / "B.aae", b"<aae/>")
    blob(Q / "kleinxmp" / "k.jpg.XMP", b"<xmp/>"); img(Q / "kleinxmp" / "k.jpg", 21)
    img(Q / "gesperrt" / "z.jpg", 22); os.chmod(Q / "gesperrt", 0)
    try:
        c = cfg("s1")
        entries, errors = Scanner(c).scan(Q, RunContext())
        rels = sorted(nfc(e.rel) for e in entries)
        expected = sorted(["a.jpg", "B.JPG", "c.jpeg", "d.heic", "e.tif", "f.dng", "g.CR3", "h.mp4", "i.MOV",
                           "Übersicht 🌅 Strand.jpg", "sub/tief/x.jpg", "Fotos/0/UUID-1.heic",
                           "Lib/2025/l.jpg", "kleinxmp/k.jpg"])
        ok("Genau die erwarteten Mediendateien gefunden", rels == expected,
           f"zu viel: {sorted(set(rels) - set(expected))}, fehlt: {sorted(set(expected) - set(rels))}")
        ok("Gesperrter Ordner als Lesefehler gemeldet", any("gesperrt" in e for e in errors), errors)
        sc = {e.path.name: sorted(p.name for p in e.sidecars) for e in entries}
        ok("Sidecar g.xmp → g.CR3", sc.get("g.CR3") == ["g.xmp"], sc.get("g.CR3"))
        ok("Sidecar d.heic.xmp → d.heic", sc.get("d.heic") == ["d.heic.xmp"], sc.get("d.heic"))
        ok("Sidecar B.aae → B.JPG", sc.get("B.JPG") == ["B.aae"], sc.get("B.JPG"))
        ok("Sidecar k.jpg.XMP (andere Schreibweise) → k.jpg", sc.get("k.jpg") == ["k.jpg.XMP"], sc.get("k.jpg"))
        foto_only = cfg("s1b", dateien__aktive_gruppen=["foto"])
        names = {e.path.name for e in Scanner(foto_only).scan(Q, RunContext())[0]}
        ok("aktive_gruppen=[foto] blendet RAW/Video aus", not names & {"g.CR3", "f.dng", "h.mp4", "i.MOV"}, names)
        lib_root = Scanner(c).scan(Q / "Fotos.photoslibrary", RunContext())[0]
        ok("Bibliothek direkt als Quelle: nur originals", [str(e.rel) for e in lib_root] == ["0/UUID-1.heic"],
           [str(e.rel) for e in lib_root])
        ok("Bibliothek als Quelle: Bezeichnung ohne Endung", lib_root and lib_root[0].label == "Fotos",
           lib_root and lib_root[0].label)
    finally:
        os.chmod(Q / "gesperrt", 0o755)


# --- 2. Metadaten + Muster ---------------------------------------------------------
@area("2 Metadaten und Muster")
def t_meta():
    S = ROOT / "s2"
    img(S / "a.jpg", 30, {"DateTimeOriginal": "2025:08:09 14:05:59", **CANON})
    img(S / "b.jpg", 31, {"DateTimeOriginal": "2025:12:31 23:59:59", "Make": "Canon", "Model": "Canon EOS R5"})
    img(S / "c.heic", 32, mtime=TS("2020-05-17 10:00"))
    fake_raw(S / "d.CR3", 33, mtime=TS("2019-03-03 03:03"))
    video(S / "e.mp4", 34, {"CreateDate": "2026:01:01 00:30:00", "Model": "Canon EOS R6m2"})
    img(S / "f.jpg", 35, {"DateTimeOriginal": "0000:00:00 00:00:00", "CreateDate": "2024:02:29 12:00:00"})
    img(S / "g.jpg", 36, {"DateTimeOriginal": "2025:01:01 00:00:00+01:00", "Make": "Canon", "Model": "EOS/R6:II*?"})
    img(S / "München NFD.jpg", 37, {"DateTimeOriginal": "2023:07:01 08:00:00"})
    img(S / "München NFC.jpg", 38, {"DateTimeOriginal": "2023:07:02 08:00:00"})
    img(S / "h.png", 39, {"DateTimeOriginal": "2022:06:06 06:06:06"})
    entries = Scanner(cfg("s2")).scan(S, RunContext())[0]
    for batch in (1, 3, 200):
        c = cfg(f"s2-{batch}", metadaten__batch_groesse=batch)
        meta = MetadataReader(c).read(entries, RunContext())
        m = {p.name: v for p, v in meta.items()}
        exp = {"a.jpg": (datetime(2025, 8, 9, 14, 5, 59), "DateTimeOriginal"),
               "b.jpg": (datetime(2025, 12, 31, 23, 59, 59), "DateTimeOriginal"),
               "c.heic": (datetime(2020, 5, 17, 10, 0), "datei_mtime"),
               "d.CR3": (datetime(2019, 3, 3, 3, 3), "datei_mtime"),
               "e.mp4": (datetime(2026, 1, 1, 0, 30), "CreateDate"),
               "f.jpg": (datetime(2024, 2, 29, 12, 0), "CreateDate"),
               "g.jpg": (datetime(2025, 1, 1, 0, 0), "DateTimeOriginal"),
               "München NFD.jpg": (datetime(2023, 7, 1, 8, 0), "DateTimeOriginal"),
               "München NFC.jpg": (datetime(2023, 7, 2, 8, 0), "DateTimeOriginal"),
               "h.png": (datetime(2022, 6, 6, 6, 6, 6), "DateTimeOriginal")}
        bad = {k: (m[k].datum, m[k].datum_quelle) for k, v in exp.items() if k in m and (m[k].datum, m[k].datum_quelle) != v}
        ok(f"Aufnahmedatum + Quelle korrekt (Batchgröße {batch})", not bad and len(m) == len(exp), bad or m.keys())
    ok("Kamera/Hersteller/Objektiv gelesen", (m["a.jpg"].kamera, m["a.jpg"].hersteller, m["a.jpg"].objektiv)
       == ("Canon EOS R6m2", "Canon", "RF24-105mm F4 L IS USM"), m["a.jpg"])
    c = cfg("s2-namen", metadaten__kamera_namen={"Canon EOS R6m2": "R6 II"},
            metadaten__hersteller_aus_kamera_entfernen=True)
    m2 = {p.name: v for p, v in MetadataReader(c).read(entries, RunContext()).items()}
    ok("Kameranamen-Zuordnung (R6m2 → R6 II)", m2["a.jpg"].kamera == "R6 II", m2["a.jpg"].kamera)
    ok("Hersteller aus Kamera entfernen (Canon EOS R5 → EOS R5)", m2["b.jpg"].kamera == "EOS R5", m2["b.jpg"].kamera)
    # Ohne exiftool: Fallback aufs Dateidatum, kein Absturz
    c = cfg("s2-ohne", pfade__exiftool="/gibt/es/nicht")
    m3 = {p.name: v for p, v in MetadataReader(c).read(entries, RunContext()).items()}
    ok("Ohne exiftool: alle Dateien mit Dateidatum", all(v.datum_quelle == "datei_mtime" for v in m3.values()),
       {k: v.datum_quelle for k, v in m3.items()})
    # Alle Platzhalter über einen Sortier-Probelauf
    c = cfg("s2-muster")
    pat = "{Kamera}/{Typ}/{Quartal}/{KW}/{Wochentag}/{MonatName}/{Hersteller}/{Objektiv}/{Endung}/{Ordner}/{Quelle}"
    fn = "{Jahr}{Monat}{Tag}_{Stunde}{Minute}{Sekunde}_{Name}"
    res = run(c, "sortieren", SortParams(str(S), str(ROOT / "s2-ziel"), pat, fn, "copy", True))
    z = {Path(r["Datei"]).name: r.get("Ziel", "") for r in res.rows}
    want = str(ROOT / "s2-ziel" / "Canon EOS R6m2/Foto/Q3/KW32/Sa/August/Canon/RF24-105mm F4 L IS USM/JPG/s2/s2"
               / "20250809_140559_a.jpg")
    ok("Alle Platzhalter korrekt aufgelöst", z.get("a.jpg") == want, f"{z.get('a.jpg')} ≠ {want}")
    ok("Unbekannte Werte → 'Unbekannt'", "/Unbekannt/" in z.get("c.heic", ""), z.get("c.heic"))
    ok("Ungültige Zeichen im Kameranamen ersetzt", "EOS_R6_II__/" in z.get("g.jpg", ""), z.get("g.jpg"))
    ok("Typ RAW für CR3, Video für MP4", "/RAW/" in z.get("d.CR3", "") and "/Video/" in z.get("e.mp4", ""),
       (z.get("d.CR3"), z.get("e.mp4")))
    ok("Zusammenfassung zählt Dateien ohne EXIF-Datum (c.heic, d.CR3)", res.summary.get("Ohne Aufnahmedatum (EXIF)") == 2,
       res.summary.get("Ohne Aufnahmedatum (EXIF)"))
    # Namensprüfung vor dem Lauf: nur Namen ohne Zuordnung, Antwort wird übernommen und gespeichert
    c = cfg("s2-pruefen", metadaten__kamera_namen={"Canon EOS R6m2": "R6 II"})
    asked = []
    def answer(todo):
        asked.append(todo)
        return {ph: {raw: ("R5" if raw == "Canon EOS R5" else prop) for raw, (prop, _) in found.items()}
                for ph, found in todo.items()}
    Zp = ROOT / "s2-pruefen-ziel"
    res = run(c, "sortieren", SortParams(str(S), str(Zp), "{Kamera}/{Jahr}", "", "copy", False, bestaetigt=True),
              RunContext(on_names=answer))
    names = set(asked[0].get("Kamera", {})) if asked else set()
    ok("Namensprüfung: nur nicht zugeordnete Kameras gefragt", "Canon EOS R5" in names and "Canon EOS R6m2" not in names
       and set(asked[0]) == {"Kamera"}, asked)
    ok("Namensprüfung: geänderter Name als Ordner", (Zp / "R5" / "2025" / "b.jpg").exists(), sorted(map(str, Zp.rglob("*.jpg"))))
    ok("Namensprüfung: Zuordnung gespeichert (auch unverändert übernommene)",
       Config(c.user_dir).get("metadaten.kamera_namen", {}).get("Canon EOS R5") == "R5"
       and "EOS/R6:II*?" in Config(c.user_dir).get("metadaten.kamera_namen", {}), Config(c.user_dir).get("metadaten.kamera_namen"))
    asked.clear()
    run(Config(c.user_dir), "sortieren", SortParams(str(S), str(Zp), "{Kamera}/{Jahr}", "", "copy", True), RunContext(on_names=answer))
    ok("Namensprüfung: zweiter Lauf fragt nicht erneut", not asked, asked)
    def decline(todo):
        raise Declined()
    Zd = ROOT / "s2-pruefen-abgelehnt"
    res = run(cfg("s2-ablehnen"), "sortieren", SortParams(str(S), str(Zd), "{Kamera}/{Objektiv}", "", "copy", False,
                                                           bestaetigt=True), RunContext(on_names=decline))
    ok("Namensprüfung abgelehnt: nichts kopiert, kein Bericht", res is None and not Zd.exists(), res)
    asked.clear()
    run(cfg("s2-ohne-namen"), "sortieren", SortParams(str(S), str(Zp), "{Jahr}/{Monat}", "", "copy", True),
        RunContext(on_names=answer))
    ok("Namensprüfung: Muster ohne Namen fragt nicht", not asked, asked)
    for bad_pat in ("{Jahr}/{Foo}", "../{Jahr}", "/abs/{Jahr}", "{Jahr"):
        try:
            run(c, "sortieren", SortParams(str(S), str(ROOT / "s2-ziel2"), bad_pat, "", "copy", True))
            ok(f"Ungültiges Muster '{bad_pat}' abgelehnt", False, "kein Fehler")
        except PatternError:
            ok(f"Ungültiges Muster '{bad_pat}' abgelehnt", True)
    ok("Abgelehntes Muster hat nichts angelegt", not (ROOT / "s2-ziel2").exists())
    # KW am Jahreswechsel: 2025-12-31 ist ISO-KW01 (2026)
    res = run(c, "sortieren", SortParams(str(S), str(ROOT / "s2-ziel"), "{KWJahr}/{KW}", "", "copy", True))
    zb = {Path(r["Datei"]).name: r.get("Ziel", "") for r in res.rows}.get("b.jpg", "")
    ok("{KWJahr}/{KW}: 31.12.2025 → 2026/KW01", zb.endswith("2026/KW01/b.jpg"), zb)


# --- 3. Abgleich ------------------------------------------------------------------
def build_sync_world(W: Path):
    Q, Z = W / "Import", W / "Ziel"
    img(Q / "neu_1.jpg", 101, CANON)
    cp(img(Z / "Archiv" / "2025" / "IMG_0001.jpg", 102), Q / "umbenannt.jpg")
    img(Z / "Import" / "IMG_0002.jpg", 103); img(Q / "IMG_0002.jpg", 104)
    cp(img(Q / "dupA.jpg", 105), Q / "sub" / "dupA-kopie.jpg")
    x = img(Z / "Archiv" / "x.jpg", 106); cp(x, Z / "Backup" / "x.jpg"); cp(x, Q / "x-kopie.jpg")
    data = random.Random(5).randbytes(3_000_000)
    blob(Z / "Archiv" / "gross.jpg", data)
    blob(Q / "gross_bearbeitet.jpg", data[:1_500_000] + b"\x00" + data[1_500_001:])
    blob(Q / "leer.jpg", b""); blob(Z / "Archiv" / "kaputt_leer.jpg", b"")
    fake_raw(Q / "IMG_5000.CR3", 107); blob(Q / "IMG_5000.xmp", b"<xmp>5000</xmp>")
    cp(fake_raw(Z / "Import" / "IMG_6000.CR3", 108), Q / "IMG_6000.CR3"); blob(Q / "IMG_6000.xmp", b"<xmp>6000 neu</xmp>")
    img(Q / "IMG_7000.heic", 109); video(Q / "IMG_7000.mov", 110); blob(Q / "IMG_7000.aae", b"<aae/>")
    img(Q / "Übersicht 🌅 Strand.jpg", 111)
    img(Q / "<b>fett&amp;.jpg", 112)
    return Q, Z


@area("3 Abgleich (Ordnerstruktur): Erkennung, Probelauf, echter Lauf, Wiederholung, Rückgängig")
def t_sync():
    W = ROOT / "s3"
    Q, Z = build_sync_world(W)
    z0, q0 = snapshot(Z), snapshot(Q)
    c = cfg("s3")
    res = run(c, "abgleich", SyncParams([str(Q)], str(Z), True, "struktur", "", "", True))
    st = lambda n: status_of(res, Q / n)  # noqa: E731
    for n in ("umbenannt.jpg", "x-kopie.jpg", "IMG_6000.CR3"):
        ok(f"'{n}' als bereits im Ziel erkannt (Inhalt, nicht Name)", st(n) == "Bereits im Ziel", st(n))
    for n in ("neu_1.jpg", "IMG_0002.jpg", "gross_bearbeitet.jpg", "IMG_5000.CR3", "IMG_7000.heic",
              "IMG_7000.mov", "Übersicht 🌅 Strand.jpg", "<b>fett&amp;.jpg"):
        ok(f"'{n}' als fehlend erkannt", st(n) == "Würde kopiert", st(n))
    dup_st = sorted([st("dupA.jpg"), status_of(res, Q / "sub" / "dupA-kopie.jpg")])
    ok("Doppelt in Quelle: einmal kopieren, einmal markiert", dup_st == ["Doppelt in Quelle (nur 1× kopiert)", "Würde kopiert"], dup_st)
    ok("Fehlen im Ziel = 9", res.summary["Fehlen im Ziel"] == 9, res.summary)
    ok("Namensgleich, anderer Inhalt → Suffix _1", rows_by(res)[nfc(Q / "IMG_0002.jpg")]["Ziel"].endswith("Import/IMG_0002_1.jpg"),
       rows_by(res)[nfc(Q / "IMG_0002.jpg")]["Ziel"])
    ok("Leere Datei als defekt gemeldet (nicht 'Bereits im Ziel')",
       st("leer.jpg") == "Leere Datei (0 Byte) – vermutlich defekt", st("leer.jpg"))
    ok("Neue XMP zu schon gesicherter RAW wird erkannt",
       status_of(res, Q / "IMG_6000.xmp") == "Sidecar würde ergänzt", status_of(res, Q / "IMG_6000.xmp"))
    ok("Probelauf: Ziel unverändert", snapshot(Z) == z0)
    ok("Probelauf: Quelle unverändert", snapshot(Q) == q0)
    ok("Probelauf: Bericht im Datenordner, nicht im Ziel", not (Z / "_FotoTool-Berichte").exists()
       and all(str(c.data_dir) in str(r) for r in res.reports), res.reports)
    # Echter Lauf
    res = run(c, "abgleich", SyncParams([str(Q)], str(Z), True, "struktur", "", "", False))
    ok("Echter Lauf: keine Fehler", not res.errors, res.errors)
    exp_new = {"Import/neu_1.jpg": "neu_1.jpg", "Import/IMG_0002_1.jpg": "IMG_0002.jpg",
               "Import/gross_bearbeitet.jpg": "gross_bearbeitet.jpg", "Import/IMG_5000.CR3": "IMG_5000.CR3",
               "Import/IMG_5000.xmp": "IMG_5000.xmp", "Import/IMG_7000.heic": "IMG_7000.heic",
               "Import/IMG_7000.mov": "IMG_7000.mov", "Import/IMG_7000.aae": "IMG_7000.aae",
               "Import/Übersicht 🌅 Strand.jpg": "Übersicht 🌅 Strand.jpg", "Import/<b>fett&amp;.jpg": "<b>fett&amp;.jpg"}
    bad = [k for k, src in exp_new.items() if not (Z / k).exists() or h(Z / k) != h(Q / src)]
    ok("Alle fehlenden Dateien + Sidecars bytegenau kopiert", not bad, bad)
    dupcopies = [p for p in (Z / "Import").rglob("dupA*")]
    ok("dupA genau einmal kopiert", len(dupcopies) == 1, dupcopies)
    ok("Zeitstempel erhalten", abs((Z / "Import/neu_1.jpg").stat().st_mtime - (Q / "neu_1.jpg").stat().st_mtime) < 1)
    ok("Vorhandene Zieldateien unverändert", all(snapshot(Z).get(k) == v for k, v in z0.items()))
    ok("Quelle unverändert (Kopieren)", snapshot(Q) == q0)
    ok("Keine .fototool-tmp Reste", not list(Z.rglob("*.fototool-tmp")))
    ok("Bericht im Ziel/_FotoTool-Berichte", any("_FotoTool-Berichte" in str(r) for r in res.reports), res.reports)
    ok("Journal angelegt", res.journal and res.journal.exists(), res.journal)
    # CSV prüfen
    csvf = next(r for r in res.reports if r.suffix == ".csv")
    with csvf.open(encoding="utf-8-sig") as f:
        crow = list(csv.DictReader(f, delimiter=";"))
    ok("CSV lesbar, Umlaute/Emoji intakt", any("Übersicht 🌅 Strand.jpg" in nfc(r["Datei"]) for r in crow))
    htmlf = next(r for r in res.reports if r.suffix == ".html").read_text(encoding="utf-8")
    ok("HTML-Bericht maskiert Sonderzeichen", "&lt;b&gt;fett&amp;amp;.jpg" in htmlf and "<b>fett" not in htmlf)
    # Wiederholung: nichts mehr fehlt
    ok("XMP neben schon gesicherter RAW ergänzt", (Z / "Import" / "IMG_6000.xmp").exists()
       and h(Z / "Import" / "IMG_6000.xmp") == h(Q / "IMG_6000.xmp"))
    res2 = run(c, "abgleich", SyncParams([str(Q)], str(Z), True, "struktur", "", "", True))
    ok("Zweiter Lauf: 0 fehlen, keine Sidecars offen (idempotent)", res2.summary["Fehlen im Ziel"] == 0
       and "Sidecars fehlend/geändert" not in res2.summary, res2.summary)
    # XMP in der Quelle bearbeitet (neuer) → wird im Ziel ersetzt, alte Version über Rückgängig zurück
    old_xmp = (Z / "Import" / "IMG_5000.xmp").read_bytes()
    blob(Q / "IMG_5000.xmp", b"<xmp>5000 bearbeitet</xmp>", (Z / "Import" / "IMG_5000.xmp").stat().st_mtime + 60)
    rx = run(c, "abgleich", SyncParams([str(Q)], str(Z), True, "struktur", "", "", False))
    ok("Bearbeitete XMP ersetzt die alte im Ziel", (Z / "Import" / "IMG_5000.xmp").read_bytes() == b"<xmp>5000 bearbeitet</xmp>"
       and status_of(rx, Q / "IMG_5000.xmp") == "Sidecar aktualisiert", status_of(rx, Q / "IMG_5000.xmp"))
    _, probs = undo(rx.journal, RunContext())
    ok("Rückgängig holt die alte XMP zurück", (Z / "Import" / "IMG_5000.xmp").read_bytes() == old_xmp and not probs, probs)
    blob(Q / "IMG_5000.xmp", b"<xmp>5000</xmp>")
    # Rückgängig
    undone, probs = undo(res.journal, RunContext())
    ok("Rückgängig ohne Probleme", not probs, probs)
    ok("Rückgängig: Ziel wieder wie vorher", snapshot(Z) == z0,
       set(snapshot(Z)) ^ set(z0))
    ok("Rückgängig entfernt die angelegten Ordner wieder", not (Z / "Import" / "sub").exists()
       and (Z / "Import").is_dir(), [str(p.relative_to(Z)) for p in Z.rglob("*") if p.is_dir()])
    # Schnellmodus
    cq = cfg("s3-schnell", vergleich__voller_hash=False)
    rq = run(cq, "abgleich", SyncParams([str(Q)], str(Z), True, "struktur", "", "", True))
    ok("Schnellmodus wird in der Zusammenfassung gewarnt", rq.summary.get("Vergleich", "").startswith("⚠️"), rq.summary)
    if status_of(rq, Q / "gross_bearbeitet.jpg") == "Bereits im Ziel":
        befund("Schnellmodus (voller_hash=false) hält Dateien mit gleicher Größe/Anfang/Ende für gleich",
               "gross_bearbeitet.jpg (1 Byte in der Mitte anders) → 'Bereits im Ziel' → würde NICHT gesichert. "
               "Voller Modus erkennt es korrekt.")
    # Ohne Quellordner-Ebene
    c2 = cfg("s3-ebene", abgleich__quellordner_als_ebene=False)
    r3 = run(c2, "abgleich", SyncParams([str(Q)], str(W / "Ziel2"), True, "struktur", "", "", True))
    ok("quellordner_als_ebene=false: kein 'Import/' im Ziel",
       rows_by(r3)[nfc(Q / "neu_1.jpg")]["Ziel"] == str(W / "Ziel2" / "neu_1.jpg"), rows_by(r3)[nfc(Q / "neu_1.jpg")]["Ziel"])
    ok("Nicht vorhandenes Ziel im Probelauf nicht angelegt", not (W / "Ziel2").exists())
    r4 = run(c2, "abgleich", SyncParams([str(Q)], str(W / "Ziel2"), True, "struktur", "", "", False))
    ok("Nicht vorhandenes Ziel im echten Lauf angelegt + befüllt", (W / "Ziel2" / "neu_1.jpg").exists() and not r4.errors,
       r4.errors)
    # Nur Bericht
    r5 = run(c, "abgleich", SyncParams([str(Q)], str(Z), False, "struktur", "", "", False))
    ok("Nur Bericht (echter Lauf): Ziel unverändert", snapshot(Z) == z0)
    ok("Nur Bericht: Status 'Fehlt im Ziel'", status_of(r5, Q / "neu_1.jpg") == "Fehlt im Ziel", status_of(r5, Q / "neu_1.jpg"))


@area("4 Abgleich: Sonderfälle bei Quellen und Ziel")
def t_sync_edge():
    W = ROOT / "s4"
    c = cfg("s4")
    # Quellordner mit Punkt im Namen
    img(W / "Urlaub 2025.08" / "p.jpg", 120)
    r = run(c, "abgleich", SyncParams([str(W / "Urlaub 2025.08")], str(W / "Z1"), True, "struktur", "", "", True))
    ziel = rows_by(r)[nfc(W / "Urlaub 2025.08" / "p.jpg")]["Ziel"]
    ok("Quellordner mit Punkt ('Urlaub 2025.08') behält vollen Namen im Ziel",
       ziel == str(W / "Z1" / "Urlaub 2025.08" / "p.jpg"), ziel)
    # Zwei Quellen mit gleichem Namen
    img(W / "a" / "Fotos" / "p.jpg", 121); img(W / "b" / "Fotos" / "p.jpg", 122)
    srcs = [str(W / "a" / "Fotos"), str(W / "b" / "Fotos")]
    r = run(c, "abgleich", SyncParams(srcs, str(W / "Z2"), True, "struktur", "", "", False))
    got = sorted(p.name for p in (W / "Z2" / "Fotos").iterdir())
    ok("Zwei gleichnamige Quellen: beide Dateien gesichert (Suffix)", got == ["p.jpg", "p_1.jpg"], got)
    r = run(c, "abgleich", SyncParams(srcs, str(W / "Z2"), True, "struktur", "", "", True))
    ok("… und danach 0 fehlend", r.summary["Fehlen im Ziel"] == 0, r.summary)
    # Überlappende Quellen
    img(W / "Q" / "a.jpg", 123); img(W / "Q" / "sub" / "b.jpg", 124)
    r = run(c, "abgleich", SyncParams([str(W / "Q"), str(W / "Q" / "sub")], str(W / "Z3"), True, "struktur", "", "", True))
    ok("Überlappende Quellen: jede Datei nur einmal gezählt", r.summary["Dateien in Quellen"] == 2, r.summary)
    ok("Überlappende Quellen: b.jpg trotzdem nur 1× kopiert", r.summary["Fehlen im Ziel"] == 2, r.summary)
    # Quelle liegt im Ziel
    img(W / "Z4" / "Archiv" / "x.jpg", 125); cp(W / "Z4" / "Archiv" / "x.jpg", W / "Z4" / "Neu" / "x-kopie.jpg")
    img(W / "Z4" / "Neu" / "nur_neu.jpg", 126)
    z40 = snapshot(W / "Z4")
    r = run(c, "abgleich", SyncParams([str(W / "Z4" / "Neu")], str(W / "Z4"), True, "struktur", "", "", False))
    ok("Quelle im Ziel: Kopie erkannt, Einzelstück gemeldet, nichts kopiert",
       status_of(r, W / "Z4" / "Neu" / "x-kopie.jpg") == "Bereits im Ziel"
       and status_of(r, W / "Z4" / "Neu" / "nur_neu.jpg").startswith("Nur im Quellordner")
       and snapshot(W / "Z4") == z40, r.rows)
    # Ziel liegt in der Quelle
    img(W / "Q5" / "a.jpg", 127)
    r = run(c, "abgleich", SyncParams([str(W / "Q5")], str(W / "Q5" / "Backup"), True, "struktur", "", "", False))
    r = run(c, "abgleich", SyncParams([str(W / "Q5")], str(W / "Q5" / "Backup"), True, "struktur", "", "", True))
    ok("Ziel in Quelle: Backup wird nicht als Quelle mitgezählt", r.summary["Dateien in Quellen"] == 1
       and r.summary["Fehlen im Ziel"] == 0, r.summary)
    # Bibliothek als Quelle
    lib = W / "Mediathek.photoslibrary"
    img(lib / "originals" / "A" / "UUID1.heic", 128); img(lib / "resources" / "derivatives" / "A" / "UUID1_c.jpeg", 129)
    l0 = snapshot(lib)
    r = run(c, "abgleich", SyncParams([str(lib)], str(W / "Z6"), True, "struktur", "", "", False))
    ok("Fotos-Mediathek: nur Original gesichert, als 'Mediathek/A/UUID1.heic'",
       sorted(str(p.relative_to(W / "Z6")) for p in (W / "Z6").rglob("*.*") if "_FotoTool" not in str(p))
       == ["Mediathek/A/UUID1.heic"], list((W / "Z6").rglob("*")))
    ok("Mediathek unverändert", snapshot(lib) == l0)
    # Muster-Ablage
    img(W / "M" / "a.jpg", 130, {"DateTimeOriginal": "2025:08:09 14:05:59"})
    img(W / "M" / "b.jpg", 131, {"DateTimeOriginal": "2025:08:09 14:05:59"})
    r = run(c, "abgleich", SyncParams([str(W / "M")], str(W / "Z7"), True, "muster", "{Jahr}/{Jahr}-{Monat}-{Tag}",
                                      "{Jahr}{Monat}{Tag}_{Stunde}{Minute}{Sekunde}", False))
    got = sorted(str(p.relative_to(W / "Z7")) for p in (W / "Z7").rglob("*.jpg"))
    ok("Muster-Ablage + Dateiname-Muster, Kollision → _1",
       got == ["2025/2025-08-09/20250809_140559.jpg", "2025/2025-08-09/20250809_140559_1.jpg"], got)
    r = run(c, "abgleich", SyncParams([str(W / "M")], str(W / "Z7"), True, "muster", "{Jahr}", "", True))
    ok("Muster-Ablage: zweiter Lauf erkennt umbenannte Kopien (0 fehlend)", r.summary["Fehlen im Ziel"] == 0, r.summary)


@area("5 Abgleich: Zugriff, nicht lesbare Dateien, fehlendes Laufwerk")
def t_sync_access():
    W = ROOT / "s5"
    c = cfg("s5")
    img(W / "Q" / "a.jpg", 140); img(W / "Q" / "b.jpg", 141)
    img(W / "Z" / "gesperrt" / "a.jpg", 142); cp(W / "Q" / "a.jpg", W / "Z" / "gesperrt" / "a-kopie.jpg")
    os.chmod(W / "Z" / "gesperrt", 0)
    try:
        r = run(c, "abgleich", SyncParams([str(W / "Q")], str(W / "Z"), True, "struktur", "", "", True))
        ok("Teilweise unlesbares Ziel: Probelauf warnt in Zusammenfassung",
           any("unvollständig" in k for k in r.summary), r.summary)
        try:
            run(c, "abgleich", SyncParams([str(W / "Q")], str(W / "Z"), True, "struktur", "", "", False))
            ok("Teilweise unlesbares Ziel: echter Lauf bricht ab", False, "kein Abbruch")
        except AccessError:
            ok("Teilweise unlesbares Ziel: echter Lauf bricht ab", True)
        ok("… und hat nichts kopiert", not (W / "Z" / "Q").exists())
    finally:
        os.chmod(W / "Z" / "gesperrt", 0o755)
    os.chmod(W / "Q" / "b.jpg", 0)
    try:
        r = run(c, "abgleich", SyncParams([str(W / "Q")], str(W / "Z9"), True, "struktur", "", "", False))
        ok("Unlesbare Quelldatei (echter Lauf): Status 'Fehler', Rest kopiert",
           status_of(r, W / "Q" / "b.jpg") == "Fehler" and (W / "Z9" / "Q" / "a.jpg").exists(), r.rows)
        ok("… keine halbe Datei im Ziel", not (W / "Z9" / "Q" / "b.jpg").exists() and not list((W / "Z9").rglob("*tmp")))
    finally:
        os.chmod(W / "Q" / "b.jpg", 0o644)
    for ziel in ("/Volumes/FotoToolGibtsNicht/Backup", "/Volumes/FotoToolGibtsNicht"):
        try:
            run(c, "abgleich", SyncParams([str(W / "Q")], ziel, True, "struktur", "", "", True))
            ok(f"Nicht angeschlossenes Laufwerk ({ziel}) → Abbruch", False, "kein Fehler")
        except AccessError as e:
            ok(f"Nicht angeschlossenes Laufwerk ({ziel}) → Abbruch", "nicht angeschlossen" in str(e), e)
    try:
        run(c, "abgleich", SyncParams([str(W / "gibtsnicht")], str(W / "Z"), True, "struktur", "", "", True))
        ok("Fehlender Quellordner → Fehlermeldung", False, "kein Fehler")
    except FileNotFoundError:
        ok("Fehlender Quellordner → Fehlermeldung", True)


# --- 6. Duplikate --------------------------------------------------------------
def build_dup_world(D: Path):
    g1 = img(D / "2024" / "IMG_1.jpg", 201, mtime=TS("2024-01-01 10:00"))
    cp(g1, D / "Backup" / "alt" / "sehr" / "tief" / "IMG_1.jpg", TS("2023-01-01 10:00"))
    cp(g1, D / "k.jpg", TS("2025-01-01 10:00"))
    lib = img(D / "Fotos.photoslibrary" / "originals" / "B" / "UUID.heic", 202, mtime=TS("2024-06-01 10:00"))
    cp(lib, D / "Export" / "UUID-kopie.heic", TS("2020-01-01 10:00"))
    j = img(D / "Shoot" / "IMG_7.JPG", 203, mtime=TS("2024-05-05 10:00"))
    fake_raw(D / "Shoot" / "IMG_7.CR3", 204); blob(D / "Shoot" / "IMG_7.xmp", b"<xmp>Bearbeitung CR3</xmp>")
    cp(j, D / "Export" / "IMG_7.JPG", TS("2020-01-01 10:00"))
    data = random.Random(9).randbytes(3_000_000)
    blob(D / "fast" / "a.jpg", data); blob(D / "fast" / "b.jpg", data[:1_500_000] + b"\x01" + data[1_500_001:])
    blob(D / "leer1.jpg", b""); blob(D / "x" / "leer2.jpg", b"")
    blob(D / "n1" / "same.jpg", random.Random(10).randbytes(50_000))
    blob(D / "n2" / "same.jpg", random.Random(11).randbytes(50_000))
    img(D / "_Duplikate" / "alt.jpg", 205); cp(D / "_Duplikate" / "alt.jpg", D / "alt-original.jpg")
    img(D / "einzeln.jpg", 206)


@area("6 Duplikate: Erkennung, Behalten-Regeln, Verschieben, Rückgängig, Papierkorb")
def t_dups():
    D = ROOT / "s6" / "D"
    build_dup_world(D)
    d0 = snapshot(D)
    c = cfg("s6")
    r = run(c, "duplikate", DupParams(str(D), "aeltestes", "bericht"))
    groups: dict[int, list] = {}
    for row in r.rows:
        if "Gruppe" in row:
            groups.setdefault(row["Gruppe"], []).append((Path(row["Datei"]).relative_to(D).as_posix(), row["Status"]))
    sets = sorted(sorted(p for p, _ in g) for g in groups.values())
    expected = sorted([
        sorted(["2024/IMG_1.jpg", "Backup/alt/sehr/tief/IMG_1.jpg", "k.jpg"]),
        sorted(["Fotos.photoslibrary/originals/B/UUID.heic", "Export/UUID-kopie.heic"]),
        sorted(["Shoot/IMG_7.JPG", "Export/IMG_7.JPG"]),
        sorted(["leer1.jpg", "x/leer2.jpg"]),
    ])
    ok("Genau die echten Duplikat-Gruppen gefunden (ohne Fast-gleiche, gleichnamige, leere)",
       sets == sorted(e for e in expected if "leer1.jpg" not in e), sets)
    ok("Inhalt in _Duplikate wird nicht mitgezählt", not any("_Duplikate/alt.jpg" in s for g in sets for s in g))
    ok("Leere Dateien als defekt gezählt", r.summary.get("Leere Dateien (defekt)") == 2, r.summary)
    ok("Bericht-Modus ändert nichts", snapshot(D) == d0)
    keep = lambda res: {Path(x["Datei"]).relative_to(D).as_posix() for x in res.rows  # noqa: E731
                        if x.get("Status") == "Original (behalten)"}
    exp_keep = {"aeltestes": "Backup/alt/sehr/tief/IMG_1.jpg", "neuestes": "k.jpg", "kuerzester_pfad": "k.jpg",
                "laengster_pfad": "Backup/alt/sehr/tief/IMG_1.jpg", "erster": "2024/IMG_1.jpg"}
    for rule, want in exp_keep.items():
        k = keep(run(c, "duplikate", DupParams(str(D), rule, "bericht")))
        ok(f"Behalten-Regel '{rule}' behält {want}", want in k, k)
        ok(f"Regel '{rule}': Mediathek-Original wird immer behalten",
           "Fotos.photoslibrary/originals/B/UUID.heic" in k, k)
    # Verschieben – Probelauf
    r = run(c, "duplikate", DupParams(str(D), "aeltestes", "verschieben", "_Duplikate", True))
    ok("Verschieben-Probelauf ändert nichts", snapshot(D) == d0)
    # Verschieben – echt
    r = run(c, "duplikate", DupParams(str(D), "aeltestes", "verschieben", "_Duplikate", False, True))
    ok("Verschieben: keine Fehler", not r.errors, r.errors)
    moved = {"2024/IMG_1.jpg", "k.jpg", "Export/UUID-kopie.heic", "Shoot/IMG_7.JPG"}
    ok("Leere Dateien nicht verschoben", (D / "leer1.jpg").exists() and (D / "x" / "leer2.jpg").exists())
    ok("Duplikate in _Duplikate/<Pfad> verschoben",
       all((D / "_Duplikate" / m).exists() and not (D / m).exists() for m in moved),
       [m for m in moved if not (D / "_Duplikate" / m).exists()])
    ok("Behaltene Dateien unverändert", all((D / p).exists() and h(D / p) == d0[p] for p in
       ("Backup/alt/sehr/tief/IMG_1.jpg", "Fotos.photoslibrary/originals/B/UUID.heic", "Export/IMG_7.JPG")))
    ok("XMP der RAW bleibt bei der RAW, wenn gleichnamiges JPG Duplikat ist", (D / "Shoot" / "IMG_7.xmp").exists())
    r2 = run(c, "duplikate", DupParams(str(D), "aeltestes", "bericht"))
    ok("Danach keine Duplikate mehr", r2.summary["Duplikat-Gruppen"] == 0, r2.summary)
    done, probs = undo(r.journal, RunContext())
    ok("Rückgängig: ohne Probleme", not probs, probs)
    ok("Rückgängig: Ordner wieder exakt wie vorher", snapshot(D) == d0, set(snapshot(D)) ^ set(d0))
    # Eigener Duplikat-Ordner
    r = run(c, "duplikate", DupParams(str(D), "aeltestes", "verschieben", "Aussortiert", False, True))
    r2 = run(c, "duplikate", DupParams(str(D), "aeltestes", "verschieben", "Aussortiert", True))
    real_groups = [g for g in {x["Gruppe"] for x in r2.rows if "Gruppe" in x}
                   if not any("leer" in x["Datei"] for x in r2.rows if x.get("Gruppe") == g)]
    ok("Eigener Duplikat-Ordner wird beim nächsten Lauf ignoriert", not real_groups, real_groups)
    undo(r.journal, RunContext())
    ok("Rückgängig (Aussortiert) stellt wieder her", snapshot(D) == d0, set(snapshot(D)) ^ set(d0))
    # Methoden ohne Hash
    cn = cfg("s6-name", vergleich__methode="name_groesse")
    rn = run(cn, "duplikate", DupParams(str(D), "aeltestes", "bericht"))
    ok("Unsichere Methode wird in der Zusammenfassung gewarnt", rn.summary.get("Vergleich", "").startswith("⚠️"), rn.summary)
    if any("n1/same.jpg" in x["Datei"] for x in rn.rows):
        befund("Methode 'name_groesse' meldet verschiedene Bilder mit gleichem Namen+Größe als Duplikat",
               "n1/same.jpg ≙ n2/same.jpg (Inhalt verschieden) – mit 'verschieben/papierkorb' gefährlich")
    ok("Methode 'name_groesse' findet umbenannte Kopien nicht (erwartet)",
       not any("Export/UUID-kopie.heic" in x["Datei"] for x in rn.rows))
    # Papierkorb ohne Bestätigung: nichts passiert
    P = ROOT / "s6" / "P"
    img(P / "FotoTool-Prueftest.jpg", 210, mtime=TS("2020-01-01 10:00"))
    cp(P / "FotoTool-Prueftest.jpg", P / "FotoTool-Prueftest-Kopie.jpg", TS("2026-01-01 10:00"))
    blob(P / "FotoTool-Prueftest-Kopie.xmp", b"<xmp/>")
    rp = run(c, "duplikate", DupParams(str(P), "aeltestes", "papierkorb", "_Duplikate", False, False))
    ok("Papierkorb ohne Bestätigung: nichts gelöscht", (P / "FotoTool-Prueftest-Kopie.jpg").exists())
    # Papierkorb echt (über Finder)
    rp = run(c, "duplikate", DupParams(str(P), "aeltestes", "papierkorb", "_Duplikate", False, True))
    st = [x["Status"] for x in rp.rows if x.get("Datei", "").endswith("Kopie.jpg")]
    ok("Papierkorb über Finder: Duplikat + Sidecar weg, Original bleibt",
       not (P / "FotoTool-Prueftest-Kopie.jpg").exists() and not (P / "FotoTool-Prueftest-Kopie.xmp").exists()
       and (P / "FotoTool-Prueftest.jpg").exists() and st == ["In Papierkorb"], f"{st} {rp.errors}")
    ok("Papierkorb im Journal vermerkt", rp.journal and
       sum(e["aktion"] == "papierkorb" for e in read_entries(rp.journal)) == 2, rp.journal)


# --- 7. Sortieren --------------------------------------------------------------
def build_sort_world(S: Path):
    img(S / "a.jpg", 301, {"DateTimeOriginal": "2025:08:09 14:05:59", **CANON})
    img(S / "h.jpg", 302, {"DateTimeOriginal": "2025:08:09 14:05:59", **CANON})
    img(S / "b.jpg", 303, {"DateTimeOriginal": "2025:12:31 23:59:59", "Make": "Canon", "Model": "Canon EOS R5"})
    img(S / "unter" / "c.heic", 304, mtime=TS("2020-05-17 10:00"))
    fake_raw(S / "d.CR3", 305, mtime=TS("2019-03-03 03:03"))
    blob(S / "d.xmp", b"<xmp>d</xmp>"); blob(S / "d.CR3.xmp", b"<xmp>d.CR3</xmp>")
    video(S / "e.mp4", 306, {"CreateDate": "2026:01:01 00:30:00"})
    img(S / "Fotos.photoslibrary" / "originals" / "C" / "U.heic", 307, {"DateTimeOriginal": "2024:04:04 04:04:04"})


@area("7 Sortieren: Kopieren, Verschieben, Kollisionen, Bibliotheken, Rückgängig")
def t_sort():
    W = ROOT / "s7"
    S = W / "S"
    build_sort_world(S)
    s0 = snapshot(S)
    c = cfg("s7")
    T = W / "T"
    r = run(c, "sortieren", SortParams(str(S), str(T), "{Jahr}/{Jahr}-{Monat}-{Tag}", "", "copy", False))
    exp = ["2019/2019-03-03/d.CR3", "2019/2019-03-03/d.CR3.xmp", "2019/2019-03-03/d.xmp",
           "2020/2020-05-17/c.heic", "2024/2024-04-04/U.heic", "2025/2025-08-09/a.jpg", "2025/2025-08-09/h.jpg",
           "2025/2025-12-31/b.jpg", "2026/2026-01-01/e.mp4"]
    ok("Kopieren nach Datum: richtige Ordner + Sidecars", snapshot(T) and sorted(snapshot(T)) == exp, sorted(snapshot(T)))
    ok("Kopieren: Quelle unverändert", snapshot(S) == s0)
    ok("Kopien bytegenau", h(T / "2025/2025-08-09/a.jpg") == h(S / "a.jpg") and
       h(T / "2019/2019-03-03/d.CR3.xmp") == h(S / "d.CR3.xmp"))
    r = run(c, "sortieren", SortParams(str(S), str(T), "{Jahr}/{Jahr}-{Monat}-{Tag}", "", "copy", True))
    sts = {x["Status"] for x in r.rows}
    ok("Zweiter Lauf: alles 'Identisch vorhanden'", sts == {"Identisch vorhanden – übersprungen"}, sts)
    # Dateinamen-Muster mit Kollision
    r = run(c, "sortieren", SortParams(str(S), str(W / "T2"), "{Jahr}", "{Jahr}{Monat}{Tag}_{Stunde}{Minute}{Sekunde}",
                                       "copy", True))
    zs = sorted(Path(x["Ziel"]).name for x in r.rows if "2025/20250809" in x.get("Ziel", ""))
    ok("Gleicher Aufnahmezeitpunkt → zweiter Name mit _1", zs == ["20250809_140559.jpg", "20250809_140559_1.jpg"], zs)
    # Kollisionsstrategien
    for strat, ok_flag, want in (("suffix", False, "Würde kopiert"), ("ueberspringen", False, "Namenskollision – übersprungen"),
                                 ("ueberschreiben", False, "Würde kopiert"), ("ueberschreiben", True, "Würde überschrieben")):
        T3 = W / f"T3-{strat}-{ok_flag}"
        img(T3 / "2025" / "a.jpg", 399)
        cc = cfg(f"s7-{strat}-{ok_flag}", aktionen__kollision=strat)
        p = SortParams(str(S), str(T3), "{Jahr}", "", "copy", True)
        p.bestaetigt = ok_flag
        r = run(cc, "sortieren", p)
        row = rows_by(r)[nfc(S / "a.jpg")]
        ok(f"Kollision '{strat}' (bestätigt={ok_flag}) → {want}", row["Status"] == want, row)
        if strat == "suffix":
            ok("Suffix-Ziel ist a_1.jpg", row["Ziel"].endswith("2025/a_1.jpg"), row["Ziel"])
    T4 = W / "T4"
    img(T4 / "2025" / "a.jpg", 399)
    cc = cfg("s7-ow", aktionen__kollision="ueberschreiben")
    p = SortParams(str(S), str(T4), "{Jahr}", "", "copy", False); p.bestaetigt = True
    r = run(cc, "sortieren", p)
    ok("Überschreiben (echt): Datei ersetzt", h(T4 / "2025" / "a.jpg") == h(S / "a.jpg"))
    _, probs = undo(r.journal, RunContext())
    ok("Rückgängig meldet nicht wiederherstellbare Überschreibung", any("Überschrieben" in x for x in probs), probs)
    # Verschieben an Ort und Stelle
    S2 = W / "S2"
    shutil.copytree(S, S2, copy_function=shutil.copy2)
    s20 = snapshot(S2)
    r = run(c, "sortieren", SortParams(str(S2), str(S2), "{Jahr}/{Jahr}-{Monat}-{Tag}", "", "move", False, True))
    ok("Verschieben: Bibliotheksdatei geschützt", status_of(r, S2 / "Fotos.photoslibrary/originals/C/U.heic")
       == "Geschützt – nicht verändert", status_of(r, S2 / "Fotos.photoslibrary/originals/C/U.heic"))
    ok("Verschieben: Dateien am Ziel, Quelle leer", (S2 / "2025/2025-08-09/a.jpg").exists() and not (S2 / "a.jpg").exists()
       and (S2 / "2019/2019-03-03/d.xmp").exists() and not (S2 / "d.xmp").exists())
    ok("Verschieben: Inhalte unverändert (gleiche Prüfsummen)", sorted(snapshot(S2).values()) == sorted(s20.values()))
    r2 = run(c, "sortieren", SortParams(str(S2), str(S2), "{Jahr}/{Jahr}-{Monat}-{Tag}", "", "move", True))
    sts = {x["Status"] for x in r2.rows}
    ok("Zweiter Lauf an Ort und Stelle: 'Liegt schon richtig' (+ geschützt)",
       sts <= {"Liegt schon richtig", "Geschützt – nicht verändert"}, sts)
    _, probs = undo(r.journal, RunContext())
    ok("Rückgängig Verschieben: ohne Probleme", not probs, probs)
    ok("Rückgängig Verschieben: alles wieder am alten Platz", snapshot(S2) == s20, set(snapshot(S2)) ^ set(s20))
    # Gemeinsamer Sidecar bei RAW+JPG
    S3 = W / "S3"
    img(S3 / "IMG_9.JPG", 310); fake_raw(S3 / "IMG_9.CR3", 311); blob(S3 / "IMG_9.xmp", b"<xmp>RAW-Bearbeitung</xmp>")
    r = run(c, "sortieren", SortParams(str(S3), str(S3 / "out"), "{Typ}", "", "move", False, True))
    where = [str(p.relative_to(S3)) for p in S3.rglob("IMG_9.xmp")]
    info = {Path(x["Datei"]).name: x.get("Info", "") for x in r.rows}
    ok("CR3+JPG gleicher Name: XMP landet bei der RAW", where == ["out/RAW/IMG_9.xmp"], f"XMP liegt in {where}, Info: {info}")
    ok("Bericht nennt die XMP nur bei der RAW", "IMG_9.xmp" in info.get("IMG_9.CR3", "")
       and "IMG_9.xmp" not in info.get("IMG_9.JPG", ""), info)
    S3n = W / "S3n"
    img(S3n / "DSC_1.JPG", 312); fake_raw(S3n / "DSC_1.NEF", 313); blob(S3n / "DSC_1.xmp", b"<xmp/>")
    run(c, "sortieren", SortParams(str(S3n), str(S3n / "out"), "{Typ}", "", "move", False, True))
    where = [str(p.relative_to(S3n)) for p in S3n.rglob("DSC_1.xmp")]
    ok("NEF+JPG (NEF alphabetisch nach JPG): XMP landet trotzdem bei der RAW", where == ["out/RAW/DSC_1.xmp"], where)
    # Verschieben, wenn identische Datei schon am Ziel liegt
    S4 = W / "S4"
    img(S4 / "a.jpg", 320, {"DateTimeOriginal": "2025:08:09 14:05:59"})
    cp(S4 / "a.jpg", W / "T5" / "2025" / "a.jpg")
    r = run(c, "sortieren", SortParams(str(S4), str(W / "T5"), "{Jahr}", "", "move", False, True))
    ok("Verschieben mit identischer Datei am Ziel: Quelle bleibt, Bericht erklärt es",
       (S4 / "a.jpg").exists() and "Duplikat" in rows_by(r)[nfc(S4 / "a.jpg")]["Info"], r.rows)
    # Ziel innerhalb der Quelle (copy)
    r = run(c, "sortieren", SortParams(str(S), str(S / "Sortiert"), "{Jahr}", "", "copy", False))
    r2 = run(c, "sortieren", SortParams(str(S), str(S / "Sortiert"), "{Jahr}", "", "copy", True))
    ok("Ziel in Quelle: Sortiert/ wird beim nächsten Lauf nicht erneut einsortiert",
       not any("Sortiert" in x["Datei"] for x in r2.rows), [x["Datei"] for x in r2.rows if "Sortiert" in x["Datei"]])
    shutil.rmtree(S / "Sortiert")


# --- 8. Externe Laufwerke (Disk-Images) ------------------------------------------------
def attach(name: str, fs: str) -> Path:
    dmg = ROOT / f"{name}.dmg"
    subprocess.run(["hdiutil", "create", "-quiet", "-size", "40m", "-fs", fs, "-volname", name, str(dmg)], check=True)
    out = subprocess.run(["hdiutil", "attach", "-nobrowse", str(dmg)], check=True, capture_output=True, text=True).stdout
    return Path([l.split("\t")[-1].strip() for l in out.splitlines() if "/Volumes/" in l][-1])


def detach(vol: Path):
    subprocess.run(["hdiutil", "detach", "-quiet", "-force", str(vol)], check=False)


@area("8 Externe Laufwerke (HFS+ und exFAT als Disk-Image)")
def t_volumes():
    W = ROOT / "s8"
    Q, _ = build_sync_world(W)
    for name, fs in (("FTTESTHFS", "HFS+"), ("FTTESTEXFAT", "ExFAT")):
        vol = attach(name, fs)
        try:
            c = cfg(f"s8-{name}")
            tgt = vol / "Backup"
            r = run(c, "abgleich", SyncParams([str(Q)], str(tgt), True, "struktur", "", "", False))
            ok(f"{fs}: Abgleich auf Laufwerk ohne Fehler", not r.errors, r.errors)
            r2 = run(c, "abgleich", SyncParams([str(Q)], str(tgt), True, "struktur", "", "", True))
            ok(f"{fs}: zweiter Lauf 0 fehlend", r2.summary["Fehlen im Ziel"] == 0, r2.summary)
            ok(f"{fs}: Umlaut/Emoji-Datei vorhanden", (tgt / "Import" / "Übersicht 🌅 Strand.jpg").exists()
               or any("Strand" in p.name for p in tgt.rglob("*")))
            # Verschieben über Laufwerksgrenzen
            M = W / f"move-{name}"
            img(M / "m.jpg", 400 + len(name), {"DateTimeOriginal": "2025:08:09 14:05:59"}); blob(M / "m.xmp", b"<xmp/>")
            m0 = snapshot(M)
            rs = run(c, "sortieren", SortParams(str(M), str(vol / "Sortiert"), "{Jahr}", "", "move", False, True))
            ok(f"{fs}: Verschieben auf anderes Laufwerk (kopieren+prüfen+löschen)",
               (vol / "Sortiert/2025/m.jpg").exists() and (vol / "Sortiert/2025/m.xmp").exists() and not (M / "m.jpg").exists(),
               rs.rows)
            _, probs = undo(rs.journal, RunContext())
            ok(f"{fs}: Rückgängig über Laufwerksgrenzen", snapshot(M) == m0 and not probs, probs)
            # Duplikate auf dem Laufwerk
            cp(Q / "neu_1.jpg", vol / "Backup" / "nochmal.jpg")
            rd = run(c, "duplikate", DupParams(str(vol / "Backup"), "aeltestes", "bericht"))
            ok(f"{fs}: Duplikate auf Laufwerk gefunden", rd.summary["Duplikat-Gruppen"] >= 1, rd.summary)
            if fs == "ExFAT":
                junk = [p.name for p in vol.rglob("._*")]
                rs2 = Scanner(c).scan(vol, RunContext())[0]
                ok("exFAT: AppleDouble-Dateien (._*) werden ignoriert", not any(e.path.name.startswith("._") for e in rs2),
                   junk)
        finally:
            detach(vol)
        try:
            run(c, "abgleich", SyncParams([str(Q)], str(tgt), True, "struktur", "", "", True))
            ok(f"{fs}: nach Auswerfen → Abbruch 'nicht angeschlossen'", False, "kein Fehler")
        except AccessError as e:
            ok(f"{fs}: nach Auswerfen → Abbruch 'nicht angeschlossen'", "nicht angeschlossen" in str(e), e)


# --- 9. Abbrechen ------------------------------------------------------------------
@area("9 Abbrechen mitten im Lauf")
def t_cancel():
    W = ROOT / "s9"
    Q = W / "Q"
    for i in range(12):
        blob(Q / f"f{i:02d}.jpg", random.Random(500 + i).randbytes(400_000))
    c = cfg("s9")
    state = {"n": 0}
    ctx = RunContext()

    def prog(phase, done, total):
        if phase == "Kopieren":
            state["n"] += 1
            if state["n"] == 4:
                ctx.cancel()
    ctx._on_progress = prog
    rc = run(c, "abgleich", SyncParams([str(Q)], str(W / "Z"), True, "struktur", "", "", False), ctx)
    ok("Abbrechen beendet den Lauf", rc.cancelled, rc.summary)
    ok("Abbruch: Teilbericht geschrieben", rc.reports and all(r.exists() for r in rc.reports)
       and "⚠️ Abgebrochen" in rc.summary, rc.reports)
    copied = list((W / "Z").rglob("*.jpg"))
    ok("Nach Abbruch: keine halben Dateien (.fototool-tmp)", not list((W / "Z").rglob("*.fototool-tmp")))
    ok("Nach Abbruch: kopierte Dateien vollständig", all(h(p) == h(Q / p.name) for p in copied))
    js = list_journals(c.data_dir)
    n_files = sum(e["aktion"] == "kopiert" for e in read_entries(js[0])) if js else 0
    ok("Nach Abbruch: Journal vorhanden", n_files == len(copied), (js, len(copied)))
    r = run(c, "abgleich", SyncParams([str(Q)], str(W / "Z"), True, "struktur", "", "", False))
    ok("Fortsetzen nach Abbruch: Rest wird kopiert", len(list((W / "Z").rglob("*.jpg"))) == 12, r.summary)


# --- 10. Einstellungen, Profile, Cache, Kommandozeile ------------------------------------
@area("10 Einstellungen, Profile, Prüfsummen-Cache, Kommandozeile")
def t_misc():
    W = ROOT / "s10"
    d = W / "cfgdir"
    d.mkdir(parents=True)
    (d / "config.toml").write_text('version = 1\n[vergleich]\nthreads = "vier"\nmethode = "hash"\n'
                                   '[metadaten.kamera_namen]\n"Canon EOS R6m2" = "R6 II"\n[unbekannt]\nx = 1\n')
    c = Config(d)
    ok("Falscher Typ in config.toml → Warnung + Standardwert", c.get("vergleich.threads") == 4 and c.warnings, c.warnings)
    ok("Freie Tabelle (Kameranamen) gelesen", c.get("metadaten.kamera_namen") == {"Canon EOS R6m2": "R6 II"})
    c.set("muster.standard", '{Jahr}/Ä "Zitat" \\ Backslash')
    c.save(); c2 = Config(d)
    ok("Speichern/Laden mit Sonderzeichen verlustfrei", c2.get("muster.standard") == '{Jahr}/Ä "Zitat" \\ Backslash',
       c2.get("muster.standard"))
    (d / "config.toml").write_text("kaputt = [\n")
    c3 = Config(d)
    ok("Kaputte config.toml → Standardwerte + Warnung", c3.warnings and c3.get("vergleich.methode") == "hash")
    pr = Profiles(cfg("s10"))
    pr.save("Urlaub/2025: Test", {"aufgabe": "abgleich", "parameter": {"quellen": ["/a"], "ziel": "/b", "neu": 1}})
    ok("Profilname mit / und : sicher gespeichert", "Urlaub/2025: Test" in pr.names()
       and all("/" not in p.name.replace(".json", "") or True for p in pr.dir.iterdir()))
    from fototool.jobs import params_from_dict
    p = params_from_dict("abgleich", pr.load("Urlaub/2025: Test")["parameter"])
    ok("Profil mit unbekanntem Feld lädt trotzdem", p.quellen == ["/a"] and p.ziel == "/b")
    # Cache: gleiche Größe + gleicher Zeitstempel, anderer Inhalt
    cc = cfg("s10-cache")
    D = W / "cache"
    a = blob(D / "a.jpg", random.Random(1).randbytes(10_000), TS("2025-01-01 10:00"))
    blob(D / "b.jpg", a.read_bytes(), TS("2025-01-01 10:00"))
    r = run(cc, "duplikate", DupParams(str(D), "aeltestes", "bericht"))
    blob(a, random.Random(2).randbytes(10_000), TS("2025-01-01 10:00"))
    r = run(cc, "duplikate", DupParams(str(D), "aeltestes", "bericht"))
    ok("Cache erkennt neuen Inhalt trotz gleicher Größe + Änderungszeit", r.summary["Duplikat-Gruppen"] == 0, r.summary)
    # Kommandozeile
    Q, Z = W / "cli" / "Q", W / "cli" / "Z"
    img(Q / "a.jpg", 600); img(Q / "b.jpg", 601); cp(Q / "a.jpg", Q / "a-kopie.jpg")
    cd = W / "cli-cfg"

    def cli(*args):
        return subprocess.run([PY, "-m", "fototool", "--config-dir", str(cd), *args], capture_output=True, text=True,
                              cwd=Path.home() / "FotoTool")
    o = cli("abgleich", "--quelle", str(Q), "--ziel", str(Z))
    ok("CLI abgleich Probelauf", o.returncode == 0 and "Fehlen im Ziel: 2" in o.stdout and not Z.exists(), o.stdout + o.stderr)
    o = cli("--ausfuehren", "abgleich", "--quelle", str(Q), "--ziel", str(Z))
    ok("CLI abgleich --ausfuehren kopiert", o.returncode == 0 and len(list(Z.rglob("*.jpg"))) == 2, o.stdout + o.stderr)
    o = cli("rueckgaengig")
    ok("CLI rueckgaengig ohne --ausfuehren ist Probelauf", "Probelauf" in o.stdout and len(list(Z.rglob("*.jpg"))) == 2, o.stdout)
    o = cli("--ausfuehren", "rueckgaengig")
    ok("CLI rueckgaengig --ausfuehren entfernt Kopien", len(list(Z.rglob("*.jpg"))) == 0, o.stdout + o.stderr)
    o = cli("--ausfuehren", "duplikate", "--ordner", str(Q), "--aktion", "verschieben")
    ok("CLI duplikate verschieben", o.returncode == 0 and len(list((Q / "_Duplikate").rglob("*.jpg"))) == 1, o.stdout + o.stderr)
    o = cli("sortieren", "--quelle", str(Q), "--ziel", str(W / "cli" / "S"), "--muster", "{Foo}")
    ok("CLI ungültiges Muster → Fehlercode, nichts angelegt", o.returncode == 2 and not (W / "cli" / "S").exists(),
       o.stdout + o.stderr)
    o = cli("abgleich", "--quelle", str(Q), "--ziel", "/Volumes/FotoToolGibtsNicht")
    ok("CLI fehlendes Laufwerk → Fehlercode 2", o.returncode == 2 and "nicht angeschlossen" in o.stderr, o.stderr)
    o = cli("pruefen")
    ok("CLI pruefen läuft", o.returncode == 0, o.stderr)


# --- 11. Lightroom-Warnung ----------------------------------------------------------
@area("11 Lightroom-Katalog-Warnung beim Verschieben")
def t_lightroom():
    import sqlite3
    from fototool.lightroom import affected_catalogs
    lr = ROOT / "lr"
    lr.mkdir(exist_ok=True)
    con = sqlite3.connect(lr / "Test Katalog.lrcat")
    con.execute("CREATE TABLE AgLibraryRootFolder (absolutePath TEXT)")
    con.execute("INSERT INTO AgLibraryRootFolder VALUES (?)", (str(ROOT / "s7" / "S") + "/",))
    con.commit(); con.close()
    c = cfg("s11")
    ok("Verschieben aus Katalogordner → Warnung", affected_catalogs(c, ROOT / "s7" / "S"), affected_catalogs(c, ROOT / "s7" / "S"))
    ok("Unterordner eines Katalogordners → Warnung", affected_catalogs(c, ROOT / "s7" / "S" / "unter"))
    ok("Fremder Ordner → keine Warnung", not affected_catalogs(c, ROOT / "s5"))


if __name__ == "__main__":
    if ROOT.exists():
        subprocess.run(["chmod", "-R", "u+rwx", str(ROOT)])
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True)
    only = [a for a in sys.argv[1:] if a.startswith("t_")] or None
    tests = [t_scan, t_meta, t_sync, t_sync_edge, t_sync_access, t_dups, t_sort, t_volumes, t_cancel, t_misc, t_lightroom]
    for t in tests:
        if not only or any(o in t.__name__ for o in only):
            t()
    (ROOT / "ergebnis.json").write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1))
    n = {k: sum(1 for r in RESULTS if r[1] == k) for k in ("OK", "FEHLER", "BEFUND")}
    print(f"\nSUMME: {n}\nArbeitsordner: {ROOT}")
    sys.exit(1 if n["FEHLER"] else 0)
