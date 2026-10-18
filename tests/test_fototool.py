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
from fototool.jobs import AccessError, DupParams, SortParams, SyncParams, run_job
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
        # A.CR3 liegt schon im Ziel (umbenannt) – seine XMP wird daneben ergänzt
        self.assertEqual((dst / "irgendwo" / "umbenannt.xmp").read_bytes(), b"<xmp/>")

        again = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(src)], str(dst), probelauf=False))
        self.assertEqual(again.summary["Fehlen im Ziel"], 0)
        self.assertNotIn("Sidecars fehlend/geändert", again.summary)

        done, problems = undo(res.journal, self.ctx)
        self.assertEqual(done, 2)
        self.assertFalse((dst / "irgendwo" / "umbenannt.xmp").exists())
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



class MatchSyncTests(Base):
    def _setup(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "A.CR3", b"A" * 5000)
        for i in range(4):
            write(dst / f"k{i}" / "A.CR3", b"A" * 5000)
        write(dst / "fast_gleich.CR3", b"A" * 2500 + b"X" + b"A" * 2499)  # gleicher Anfang/Ende
        return src, dst

    def test_stops_after_first_target_match(self):
        from fototool.hashing import Hasher
        src, dst = self._setup()
        sc, _ = Scanner(self.cfg).scan(src, self.ctx)
        tc, _ = Scanner(self.cfg).scan(dst, self.ctx)
        h = Hasher(self.cfg)
        reads = []
        orig = h.full_hash_file
        h.full_hash_file = lambda p: reads.append(p) or orig(p)
        groups = h.match_sync(sc, tc, self.ctx, [])
        g = next(g for g in groups if sc[0] in g)
        self.assertEqual(len(g), 2)
        self.assertLessEqual(len(reads), 3)  # Quelle + höchstens 2 Ziele statt 5

    def test_full_hash_detects_near_identical(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "A.CR3", b"A" * 5000)
        write(dst / "B.CR3", b"A" * 2500 + b"X" + b"A" * 2499)
        res = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(src)], str(dst), False, probelauf=True))
        self.assertEqual(res.summary["Fehlen im Ziel"], 1)
        self.cfg.set("vergleich.voller_hash", False)  # Schnellmodus: gilt als gleich
        res = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(src)], str(dst), False, probelauf=True))
        self.assertEqual(res.summary["Fehlen im Ziel"], 0)


class AccessTests(Base):
    def setUp(self):
        super().setUp()
        self.src, self.dst = self.tmp / "src", self.tmp / "dst"
        write(self.src / "A.jpg", b"A" * 100)
        write(self.dst / "geheim" / "A.jpg", b"A" * 100)

    def tearDown(self):
        for d in (self.dst, self.dst / "geheim"):
            if d.exists():
                d.chmod(0o755)
        super().tearDown()

    def test_unreadable_target_aborts(self):
        self.dst.chmod(0)
        for dry in (True, False):
            with self.assertRaises(AccessError):
                run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(self.src)], str(self.dst), probelauf=dry))

    def test_partly_unreadable_target(self):
        (self.dst / "geheim").chmod(0)
        res = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(self.src)], str(self.dst), probelauf=True))
        self.assertIn("⚠️ Ziel unvollständig gelesen", res.summary)
        with self.assertRaises(AccessError):
            run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(self.src)], str(self.dst), probelauf=False))
        self.assertEqual(list(self.dst.glob("src")), [])

    def test_unmounted_volume(self):
        with self.assertRaises(AccessError):
            run_job(self.cfg, self.ctx, "abgleich",
                    SyncParams([str(self.src)], "/Volumes/GibtsNicht123/Fotos", probelauf=True))


class RegressionTests(Base):
    """Fehler aus der Praxis-Prüfung vom 17.10.2026 (tests/praxis_pruefung.py)."""

    def test_source_folder_with_dot_keeps_full_name(self):
        src = self.tmp / "Urlaub 2025.08"
        write(src / "p.jpg", b"p" * 100)
        run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(src)], str(self.tmp / "dst"), probelauf=False))
        self.assertTrue((self.tmp / "dst" / "Urlaub 2025.08" / "p.jpg").exists())

    def test_shared_xmp_belongs_to_raw(self):
        root = self.tmp / "shoot"
        write(root / "IMG_7.JPG", b"j" * 100)
        write(root / "IMG_7.CR3", b"r" * 100)
        write(root / "IMG_7.xmp", b"<xmp/>")
        write(root / "k.jpg", b"k" * 100)
        write(root / "k.jpg.XMP", b"<xmp/>")  # andere Schreibweise der Endung
        entries = {e.path.name: [s.name for s in e.sidecars] for e in Scanner(self.cfg).scan(root, self.ctx)[0]}
        self.assertEqual(entries, {"IMG_7.CR3": ["IMG_7.xmp"], "IMG_7.JPG": [], "k.jpg": ["k.jpg.XMP"]})

    def test_dup_jpg_does_not_take_raw_xmp(self):
        root = self.tmp / "fotos"
        write(root / "Shoot" / "IMG_7.JPG", b"j" * 100, mtime=2_000_000)
        write(root / "Shoot" / "IMG_7.CR3", b"r" * 100)
        write(root / "Shoot" / "IMG_7.xmp", b"<xmp/>")
        write(root / "Export" / "IMG_7.JPG", b"j" * 100, mtime=1_000_000)
        run_job(self.cfg, self.ctx, "duplikate",
                DupParams(str(root), "aeltestes", "verschieben", probelauf=False, bestaetigt=True))
        self.assertTrue((root / "_Duplikate" / "Shoot" / "IMG_7.JPG").exists())
        self.assertTrue((root / "Shoot" / "IMG_7.xmp").exists())

    def test_sort_nef_jpg_xmp_goes_with_raw(self):
        src = self.tmp / "src"
        write(src / "DSC_1.JPG", b"j" * 100)
        write(src / "DSC_1.NEF", b"n" * 100)
        write(src / "DSC_1.xmp", b"<xmp/>")
        run_job(self.cfg, self.ctx, "sortieren",
                SortParams(str(src), str(self.tmp / "out"), "{Typ}", "", "move", probelauf=False, bestaetigt=True))
        self.assertTrue((self.tmp / "out" / "RAW" / "DSC_1.xmp").exists())
        self.assertFalse((self.tmp / "out" / "Foto" / "DSC_1.xmp").exists())

    def test_custom_dup_folder_is_not_rescanned(self):
        root = self.tmp / "fotos"
        write(root / "a.jpg", b"x" * 100, mtime=1_000_000)
        write(root / "b.jpg", b"x" * 100, mtime=2_000_000)
        p = DupParams(str(root), "aeltestes", "verschieben", "Aussortiert", probelauf=False, bestaetigt=True)
        run_job(self.cfg, self.ctx, "duplikate", p)
        again = run_job(self.cfg, self.ctx, "duplikate", DupParams(str(root), "aeltestes", "verschieben", "Aussortiert"))
        self.assertEqual(again.summary["Duplikat-Gruppen"], 0)

    def test_empty_files_reported_as_defect(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "leer.jpg", b"")
        write(src / "x" / "leer2.jpg", b"")
        write(dst / "kaputt.jpg", b"")
        res = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(src)], str(dst), probelauf=False))
        status = {Path(r["Datei"]).name: r["Status"] for r in res.rows}
        self.assertEqual(status["leer.jpg"], "Leere Datei (0 Byte) – vermutlich defekt")
        self.assertFalse((dst / "src").exists())
        dup = run_job(self.cfg, self.ctx, "duplikate", DupParams(str(src)))
        self.assertEqual(dup.summary["Duplikat-Gruppen"], 0)
        self.assertEqual(dup.summary["Leere Dateien (defekt)"], 2)

    def _sidecar_world(self, src_xmp_mtime: float):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "A.CR3", b"A" * 1000)
        write(src / "A.xmp", b"<xmp>neu</xmp>", mtime=src_xmp_mtime)
        write(dst / "Backup" / "A.CR3", b"A" * 1000)
        write(dst / "Backup" / "A.xmp", b"<xmp>alt</xmp>", mtime=1_500_000_000)
        return src, dst

    def test_changed_sidecar_replaced_when_source_newer_and_undo(self):
        src, dst = self._sidecar_world(1_600_000_000)
        dry = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(src)], str(dst), probelauf=True))
        self.assertEqual(dry.summary["Sidecars fehlend/geändert"], 1)
        self.assertEqual((dst / "Backup" / "A.xmp").read_bytes(), b"<xmp>alt</xmp>")
        res = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(src)], str(dst), probelauf=False))
        self.assertEqual((dst / "Backup" / "A.xmp").read_bytes(), b"<xmp>neu</xmp>")
        self.assertIn("Sidecar aktualisiert", {r["Status"] for r in res.rows})
        done, problems = undo(res.journal, self.ctx)
        self.assertEqual(problems, [])
        self.assertEqual((dst / "Backup" / "A.xmp").read_bytes(), b"<xmp>alt</xmp>")

    def test_sidecar_newer_in_target_is_kept(self):
        src, dst = self._sidecar_world(1_400_000_000)
        res = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(src)], str(dst), probelauf=False))
        self.assertEqual((dst / "Backup" / "A.xmp").read_bytes(), b"<xmp>alt</xmp>")
        self.assertIn("Sidecar im Ziel neuer – nicht ersetzt", {r["Status"] for r in res.rows})

    def test_sidecar_report_only_setting(self):
        src, dst = self._sidecar_world(1_600_000_000)
        self.cfg.set("abgleich.sidecar_geaendert", "nur_bericht")
        res = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(src)], str(dst), probelauf=False))
        self.assertEqual((dst / "Backup" / "A.xmp").read_bytes(), b"<xmp>alt</xmp>")
        self.assertIn("Sidecar geändert", {r["Status"] for r in res.rows})

    def test_trash_failure_is_reported_not_hidden(self):
        from unittest import mock
        import subprocess
        root = self.tmp / "fotos"
        write(root / "a.jpg", b"x" * 100, mtime=1_000_000)
        write(root / "b.jpg", b"x" * 100, mtime=2_000_000)

        def finder_refuses(cmd, **kw):
            self.assertIn("set f to POSIX file (p as text)", cmd)  # Form, die der Finder versteht
            return subprocess.CompletedProcess(cmd, 0, f"{cmd[-1]}\tZugriff verweigert\n", "")
        with mock.patch("fototool.actions.subprocess.run", side_effect=finder_refuses):
            res = run_job(self.cfg, self.ctx, "duplikate",
                          DupParams(str(root), "aeltestes", "papierkorb", probelauf=False, bestaetigt=True))
        row = next(r for r in res.rows if r.get("Datei", "").endswith("b.jpg"))
        self.assertEqual(row["Status"], "Fehler")
        self.assertIn("Zugriff verweigert", row["Info"])
        self.assertNotIn("In Papierkorb", res.summary)
        self.assertTrue((root / "b.jpg").exists())

    def test_trash_success_is_journaled(self):
        from unittest import mock
        import subprocess
        root = self.tmp / "fotos"
        write(root / "a.jpg", b"x" * 100, mtime=1_000_000)
        write(root / "b.jpg", b"x" * 100, mtime=2_000_000)

        def finder_deletes(cmd, **kw):
            Path(cmd[-1]).unlink()
            return subprocess.CompletedProcess(cmd, 0, "\n", "")
        with mock.patch("fototool.actions.subprocess.run", side_effect=finder_deletes):
            res = run_job(self.cfg, self.ctx, "duplikate",
                          DupParams(str(root), "aeltestes", "papierkorb", probelauf=False, bestaetigt=True))
        self.assertEqual(res.summary.get("In Papierkorb"), 1)
        from fototool.journal import read_entries
        self.assertEqual([e["aktion"] for e in read_entries(res.journal)], ["papierkorb"])

    def test_runs_in_same_second_have_own_journal_and_report(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "a.jpg", b"a" * 100)
        first = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(src)], str(dst), probelauf=False))
        write(src / "b.jpg", b"b" * 100)
        second = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(src)], str(dst), probelauf=False))
        self.assertNotEqual(first.journal, second.journal)
        self.assertFalse(set(first.reports) & set(second.reports))
        self.assertTrue(all(r.exists() for r in first.reports + second.reports))
        undo(second.journal, self.ctx)
        self.assertTrue((dst / "src" / "a.jpg").exists())
        self.assertFalse((dst / "src" / "b.jpg").exists())

class KleineBefundeTests(Base):
    """Kleinere Befunde der Praxis-Prüfung (17.10.2026)."""

    def test_overlapping_sources_count_once(self):
        src = self.tmp / "Q"
        write(src / "a.jpg", b"a" * 100)
        write(src / "sub" / "b.jpg", b"b" * 100)
        res = run_job(self.cfg, self.ctx, "abgleich",
                      SyncParams([str(src), str(src / "sub")], str(self.tmp / "Z"), probelauf=True))
        self.assertEqual(res.summary["Dateien in Quellen"], 2)
        self.assertNotIn("Doppelt in Quelle (nur 1× kopiert)", res.summary)

    def test_source_inside_target(self):
        z = self.tmp / "Z"
        write(z / "Archiv" / "x.jpg", b"x" * 100)
        write(z / "Neu" / "x-kopie.jpg", b"x" * 100)
        write(z / "Neu" / "nur_neu.jpg", b"n" * 100)
        res = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(z / "Neu")], str(z), probelauf=False))
        status = {Path(r["Datei"]).name: r["Status"] for r in res.rows}
        self.assertEqual(status["x-kopie.jpg"], "Bereits im Ziel")
        self.assertEqual(status["nur_neu.jpg"], "Nur im Quellordner (liegt im Ziel, sonst nirgends)")
        self.assertEqual(res.summary["Fehlen im Ziel"], 0)
        self.assertEqual(sorted(p.name for p in z.rglob("*.jpg")), ["nur_neu.jpg", "x-kopie.jpg", "x.jpg"])

    def test_real_run_report_local_plus_copy_in_target(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "x.jpg", b"x" * 100)
        res = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(src)], str(dst), probelauf=False))
        self.assertEqual(res.reports[0].parent, self.cfg.data_dir / "berichte")  # wird geöffnet
        self.assertEqual({p.parent for p in res.reports}, {self.cfg.data_dir / "berichte", dst / "_FotoTool-Berichte"})
        self.assertEqual(len(res.reports), 4)

    def test_undo_removes_created_folders_only(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "a" / "b" / "x.jpg", b"x" * 100)
        (dst / "leer_vorher").mkdir(parents=True)
        res = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(src)], str(dst), probelauf=False))
        write(dst / "src" / "a" / ".DS_Store", b"finder")  # Finder war im Ordner
        undo(res.journal, self.ctx)
        self.assertFalse((dst / "src").exists())
        self.assertTrue((dst / "leer_vorher").is_dir())
        self.assertTrue((dst / "_FotoTool-Berichte").is_dir())  # Berichte bleiben

    def test_undo_keeps_folder_with_new_files(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "x.jpg", b"x" * 100)
        res = run_job(self.cfg, self.ctx, "abgleich", SyncParams([str(src)], str(dst), probelauf=False))
        write(dst / "src" / "eigene.jpg", b"e" * 100)  # später selbst dazugelegt
        undo(res.journal, self.ctx)
        self.assertTrue((dst / "src" / "eigene.jpg").exists())
        self.assertFalse((dst / "src" / "x.jpg").exists())

    def test_cancel_writes_partial_report(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        for i in range(5):
            write(src / f"f{i}.jpg", bytes([i]) * 100)
        ctx = RunContext(on_progress=lambda ph, d, t: ctx.cancel() if ph == "Kopieren" and d == 2 else None)
        res = run_job(self.cfg, ctx, "abgleich", SyncParams([str(src)], str(dst), probelauf=False))
        self.assertTrue(res.cancelled)
        self.assertIn("⚠️ Abgebrochen", res.summary)
        self.assertTrue(res.reports and all(r.exists() for r in res.reports))
        self.assertEqual(len(list(dst.rglob("*.jpg"))), 2)
        undo(res.journal, self.ctx)
        self.assertFalse(dst.joinpath("src").exists())

    def test_cache_detects_rewrite_with_same_size_and_mtime(self):
        root = self.tmp / "fotos"
        a = write(root / "a.jpg", b"1" * 100, mtime=1_000_000)
        write(root / "b.jpg", b"1" * 100, mtime=1_000_000)
        self.assertEqual(run_job(self.cfg, self.ctx, "duplikate", DupParams(str(root))).summary["Duplikat-Gruppen"], 1)
        write(a, b"2" * 100, mtime=1_000_000)  # Inhalt neu, Größe + Zeitstempel wie vorher
        self.assertEqual(run_job(self.cfg, self.ctx, "duplikate", DupParams(str(root))).summary["Duplikat-Gruppen"], 0)

    def test_kw_year_placeholder(self):
        e = FileEntry(Path("/q/x.jpg"), 0, 0, Path("/q"))
        m = Meta(datetime(2025, 12, 31, 12, 0))
        self.assertEqual(PatternRenderer(self.cfg).folder("{KWJahr}/{KW}", e, m), Path("2026/KW01"))

    def test_unsafe_method_noted_in_summary(self):
        root = self.tmp / "fotos"
        write(root / "a.jpg", b"1" * 100)
        self.cfg.set("vergleich.methode", "name_groesse")
        res = run_job(self.cfg, self.ctx, "duplikate", DupParams(str(root)))
        self.assertTrue(res.summary["Vergleich"].startswith("⚠️"))

    def test_move_with_identical_target_explains(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        t = datetime(2023, 1, 2).timestamp()
        write(src / "a.jpg", b"a" * 100, mtime=t)
        write(dst / "2023" / "a.jpg", b"a" * 100)
        self.cfg.set("metadaten.datum_felder", ["datei_mtime"])
        res = run_job(self.cfg, self.ctx, "sortieren", SortParams(str(src), str(dst), "{Jahr}", "", "move", False, True))
        self.assertIn("Duplikat", res.rows[0]["Info"])
        self.assertTrue((src / "a.jpg").exists())

if __name__ == "__main__":
    unittest.main()
