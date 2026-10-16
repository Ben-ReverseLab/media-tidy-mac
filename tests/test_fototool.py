"""Tests: python3 -m unittest discover -s tests  (im Ordner FotoTool)."""
from __future__ import annotations

import os
import shutil
import tempfile
import tomllib
import unittest
from datetime import datetime
from pathlib import Path

from fototool.config import Config, diff, dumps_toml, load_defaults, merge, migrate
from fototool.context import RunContext
from fototool.jobs import DupParams, SortParams, SyncParams, run_job
from fototool.journal import list_journals, undo
from fototool.metadata import Meta, parse_exif_date
from fototool.patterns import PatternError, PatternRenderer, validate
from fototool.scan import FileEntry, Scanner


def write(p: Path, data: bytes, mtime: float | None = None) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fototool-test-"))
        self.cfg = Config(self.tmp / "cfg")
        self.cfg.set("pfade.datenordner", str(self.tmp / "data"))
        self.cfg.set("vergleich.teilhash_kb", 1)  # kleine Testdateien
        self.ctx = RunContext()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class ConfigTests(Base):
    def test_merge_invalid_type_warns_and_keeps_default(self):
        warnings = []
        out = merge({"a": {"b": 1, "c": True}}, {"a": {"b": "x", "c": False}, "neu": 5}, warnings=warnings)
        self.assertEqual(out["a"]["b"], 1)
        self.assertFalse(out["a"]["c"])
        self.assertEqual(out["neu"], 5)
        self.assertEqual(len(warnings), 1)

    def test_save_roundtrip_only_diff(self):
        self.cfg.set("vergleich.methode", "name_groesse")
        self.cfg.set("metadaten.kamera_namen", {"Canon EOS R6m2": "R6 II"})
        self.cfg.save()
        data = tomllib.loads(self.cfg.user_file.read_text())
        self.assertEqual(data["vergleich"]["methode"], "name_groesse")
        self.assertNotIn("threads", data["vergleich"])
        cfg2 = Config(self.tmp / "cfg")
        self.assertEqual(cfg2.get("vergleich.methode"), "name_groesse")
        self.assertEqual(cfg2.get("metadaten.kamera_namen"), {"Canon EOS R6m2": "R6 II"})
        self.assertEqual(cfg2.get("vergleich.threads"), load_defaults()["vergleich"]["threads"])

    def test_dumps_full_defaults_roundtrip(self):
        d = load_defaults()
        self.assertEqual(tomllib.loads(dumps_toml(d)), d)
        self.assertEqual(diff(d, d), {})

    def test_broken_user_file(self):
        (self.tmp / "cfg").mkdir(exist_ok=True)
        (self.tmp / "cfg" / "config.toml").write_text("kaputt = [")
        cfg = Config(self.tmp / "cfg")
        self.assertTrue(cfg.warnings)
        self.assertEqual(cfg.get("vergleich.methode"), "hash")

    def test_migrate_sets_version(self):
        self.assertEqual(migrate({})["version"], 1)


class PatternTests(Base):
    def test_render(self):
        r = PatternRenderer(self.cfg)
        e = FileEntry(Path("/q/a/IMG_1.CR3"), 1, 0, Path("/q"))
        m = Meta(datetime(2025, 8, 9, 14, 5, 59), "x", "Canon EOS R6m2", "Canon", "RF 24/105")
        self.assertEqual(r.folder("{Jahr}/{Jahr}-{Monat}-{Tag}", e, m), Path("2025/2025-08-09"))
        self.assertEqual(r.folder("{Kamera}/{Objektiv}", e, m), Path("Canon EOS R6m2/RF 24_105"))
        self.assertEqual(r.filename("{Jahr}{Monat}{Tag}_{Name}", e, m), "20250809_IMG_1.CR3")
        self.assertEqual(r.folder("{Jahr}/{Kamera}", e, Meta()), Path("Unbekannt/Unbekannt"))

    def test_validate(self):
        with self.assertRaises(PatternError):
            validate("{Jahrr}")
        with self.assertRaises(PatternError):
            validate("../{Jahr}")
        validate("{Jahr}/{MonatName}")

    def test_exif_date(self):
        self.assertEqual(parse_exif_date("2025:08:09 14:05:59+02:00"), datetime(2025, 8, 9, 14, 5, 59))
        self.assertIsNone(parse_exif_date("0000:00:00 00:00:00"))


class ScanTests(Base):
    def test_ignores_and_libraries(self):
        root = self.tmp / "src"
        write(root / "a.CR3", b"1")
        write(root / "a.xmp", b"x")
        write(root / ".DS_Store", b"")
        write(root / "notes.txt", b"t")
        write(root / "X.photoslibrary" / "originals" / "0" / "p.heic", b"2")
        write(root / "X.photoslibrary" / "resources" / "derivatives" / "p.jpg", b"3")
        write(root / "L.lrlibrary" / "abc" / "originals" / "v.mp4", b"4")
        write(root / "L.lrlibrary" / "FolderSourcePreviews" / "q.jpg", b"5")
        entries, errors = Scanner(self.cfg).scan(root, self.ctx)
        names = sorted(e.path.name for e in entries)
        self.assertEqual(names, ["a.CR3", "p.heic", "v.mp4"])
        a = next(e for e in entries if e.path.name == "a.CR3")
        self.assertEqual([s.name for s in a.sidecars], ["a.xmp"])
        v = next(e for e in entries if e.path.name == "v.mp4")
        self.assertEqual(v.rel, Path("L/v.mp4"))  # ohne .lrlibrary/abc/originals

    def test_library_as_source_root(self):
        lib = self.tmp / "Lightroom Library.lrlibrary"
        write(lib / "abc" / "originals" / "2025" / "x.CR3", b"1")
        entries, _ = Scanner(self.cfg).scan(lib, self.ctx)
        self.assertEqual(entries[0].rel, Path("2025/x.CR3"))
        self.assertEqual(entries[0].label, "Lightroom Library")


class JobTests(Base):
    def test_sync_copies_missing_once_and_undo(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "t1" / "A.CR3", b"AAAA" * 1000)
        write(src / "t1" / "A.xmp", b"<xmp/>")
        write(src / "t2" / "B.CR3", b"BBBB" * 1000)
        write(src / "t2" / "B_kopie.CR3", b"BBBB" * 1000)  # doppelt in Quelle
        write(dst / "irgendwo" / "umbenannt.CR3", b"AAAA" * 1000)  # A schon da, anderer Name

        dry = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(src)], str(dst), probelauf=True))
        self.assertEqual(dry.summary["Fehlen im Ziel"], 1)
        self.assertFalse((dst / "src").exists())

        res = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(src)], str(dst), probelauf=False))
        copied = list((dst / "src").rglob("*.CR3"))
        self.assertEqual(len(copied), 1)
        self.assertEqual(copied[0].read_bytes(), b"BBBB" * 1000)
        self.assertTrue(res.journal and res.journal.exists())
        self.assertTrue(res.reports)

        again = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(src)], str(dst), probelauf=False))
        self.assertEqual(again.summary["Fehlen im Ziel"], 0)

        done, problems = undo(res.journal, self.ctx)
        self.assertEqual(done, 1)
        self.assertFalse(copied[0].exists())

    def test_sync_pattern_with_sidecar(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "A.CR3", b"A" * 3000, mtime=datetime(2024, 5, 6, 7, 8, 9).timestamp())
        write(src / "A.xmp", b"<xmp/>")
        self.cfg.set("metadaten.datum_felder", ["datei_mtime"])
        run_job(self.cfg, self.ctx, "abgleich",
                SyncParams([str(src)], str(dst), True, "muster", "{Jahr}/{Monat}", "", probelauf=False))
        self.assertTrue((dst / "2024" / "05" / "A.CR3").exists())
        self.assertTrue((dst / "2024" / "05" / "A.xmp").exists())

    def test_duplicates_move_and_protected(self):
        root = self.tmp / "fotos"
        write(root / "a" / "X.jpg", b"same" * 500, mtime=1_000_000)
        write(root / "b" / "X_copy.jpg", b"same" * 500, mtime=2_000_000)
        write(root / "c" / "other.jpg", b"diff" * 500)
        write(root / "Lib.photoslibrary" / "originals" / "Y.jpg", b"lib!" * 500)
        write(root / "d" / "Y.jpg", b"lib!" * 500, mtime=1)

        rep = run_job(self.cfg, self.ctx, "duplikate", DupParams(str(root), probelauf=False))
        self.assertEqual(rep.summary["Duplikat-Gruppen"], 2)
        self.assertTrue((root / "b" / "X_copy.jpg").exists())  # nur Bericht

        run_job(self.cfg, self.ctx, "duplikate",
                DupParams(str(root), "aeltestes", "verschieben", "_Duplikate", probelauf=False, bestaetigt=True))
        self.assertTrue((root / "a" / "X.jpg").exists())
        self.assertTrue((root / "_Duplikate" / "b" / "X_copy.jpg").exists())
        # Bibliotheksdatei bleibt, obwohl die andere älter ist
        self.assertTrue((root / "Lib.photoslibrary" / "originals" / "Y.jpg").exists())
        self.assertTrue((root / "_Duplikate" / "d" / "Y.jpg").exists())

    def test_sort_move_collision_and_undo(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        t = datetime(2023, 1, 2, 3, 4, 5).timestamp()
        write(src / "x" / "IMG.jpg", b"one" * 100, mtime=t)
        write(src / "y" / "IMG.jpg", b"two" * 100, mtime=t)
        self.cfg.set("metadaten.datum_felder", ["datei_mtime"])
        res = run_job(self.cfg, self.ctx, "sortieren",
                      SortParams(str(src), str(dst), "{Jahr}/{Jahr}-{Monat}-{Tag}", "", "move", probelauf=False))
        day = dst / "2023" / "2023-01-02"
        self.assertEqual(sorted(p.name for p in day.iterdir()), ["IMG.jpg", "IMG_1.jpg"])
        self.assertFalse((src / "x" / "IMG.jpg").exists())
        undo(res.journal, self.ctx)
        self.assertTrue((src / "x" / "IMG.jpg").exists())
        self.assertTrue((src / "y" / "IMG.jpg").exists())
        self.assertEqual(len(list_journals(self.cfg.data_dir)), 1)

    def test_sort_never_moves_out_of_library(self):
        src, dst = self.tmp / "Fotos.photoslibrary", self.tmp / "dst"
        write(src / "originals" / "a.jpg", b"a" * 10)
        res = run_job(self.cfg, self.ctx, "sortieren",
                      SortParams(str(src), str(dst), "{Typ}", "", "move", probelauf=False))
        self.assertTrue((src / "originals" / "a.jpg").exists())
        self.assertIn("Geschützt – nicht verändert", res.summary)


if __name__ == "__main__":
    unittest.main()
