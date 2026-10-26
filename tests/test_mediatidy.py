"""Tests: python3 -m unittest discover -s tests  (in the repository folder)."""
from __future__ import annotations

import json
import os
import shutil
import string
import tempfile
import tomllib
import unittest
from datetime import datetime
from pathlib import Path

from mediatidy import i18n
from mediatidy.config import Config, Profiles, diff, dumps_toml, load_defaults, merge, migrate
from mediatidy.context import RunContext
from mediatidy.jobs import AccessError, DupParams, SortParams, SyncParams, run_job, summary_items
from mediatidy.journal import list_journals, read_entries, undo
from mediatidy.metadata import Meta, parse_exif_date
from mediatidy.patterns import PatternError, PatternRenderer, canonical, localize, validate
from mediatidy.scan import FileEntry, Scanner


def write(p: Path, data: bytes, mtime: float | None = None) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


def statuses(res) -> set[str]:
    return {r["status"] for r in res.rows}


class Base(unittest.TestCase):
    def setUp(self):
        i18n.set_language("en")
        self.tmp = Path(tempfile.mkdtemp(prefix="mediatidy-test-"))
        self.cfg = Config(self.tmp / "cfg")
        self.cfg.set("paths.data_dir", str(self.tmp / "data"))
        self.cfg.set("compare.partial_hash_kb", 1)  # small test files
        self.ctx = RunContext()

    def tearDown(self):
        i18n.set_language("en")
        shutil.rmtree(self.tmp, ignore_errors=True)


class ConfigTests(Base):
    def test_merge_invalid_type_warns_and_keeps_default(self):
        warnings = []
        out = merge({"a": {"b": 1, "c": True}}, {"a": {"b": "x", "c": False}, "new": 5}, warnings=warnings)
        self.assertEqual(out["a"]["b"], 1)
        self.assertFalse(out["a"]["c"])
        self.assertEqual(out["new"], 5)
        self.assertEqual(len(warnings), 1)

    def test_save_roundtrip_only_diff(self):
        self.cfg.set("compare.method", "name_size")
        self.cfg.set("metadata.camera_names", {"Canon EOS R6m2": "R6 II"})
        self.cfg.save()
        data = tomllib.loads(self.cfg.user_file.read_text())
        self.assertEqual(data["compare"]["method"], "name_size")
        self.assertNotIn("threads", data["compare"])
        cfg2 = Config(self.tmp / "cfg")
        self.assertEqual(cfg2.get("compare.method"), "name_size")
        self.assertEqual(cfg2.get("metadata.camera_names"), {"Canon EOS R6m2": "R6 II"})
        self.assertEqual(cfg2.get("compare.threads"), load_defaults()["compare"]["threads"])

    def test_dumps_full_defaults_roundtrip(self):
        d = load_defaults()
        self.assertEqual(tomllib.loads(dumps_toml(d)), d)
        self.assertEqual(diff(d, d), {})

    def test_broken_user_file(self):
        (self.tmp / "cfg").mkdir(exist_ok=True)
        (self.tmp / "cfg" / "config.toml").write_text("broken = [")
        cfg = Config(self.tmp / "cfg")
        self.assertTrue(cfg.warnings)
        self.assertEqual(cfg.get("compare.method"), "hash")

    def test_migrate_sets_version(self):
        self.assertEqual(migrate({})["version"], 2)


class LegacyTests(Base):
    """Data written by earlier versions (German names) keeps working."""

    def test_old_config_is_migrated(self):
        d = self.tmp / "old"
        d.mkdir()
        (d / "config.toml").write_text(
            'version = 1\n[vergleich]\nmethode = "name_groesse"\n[duplikate]\nbehalten = "neuestes"\n'
            '[muster]\nstandard = "{Jahr}/{Kamera}"\n[metadaten]\ndatum_felder = ["datei_mtime"]\n'
            '[metadaten.kamera_namen]\nFC1 = "Drone"\n[[scan.bibliotheken]]\nendung = ".x"\nunterordner = ["a"]\n')
        cfg = Config(d)
        self.assertEqual(cfg.get("compare.method"), "name_size")
        self.assertEqual(cfg.get("duplicates.keep"), "newest")
        self.assertEqual(cfg.get("patterns.default"), "{Year}/{Camera}")
        self.assertEqual(cfg.get("metadata.date_fields"), ["file_mtime"])
        self.assertEqual(cfg.get("metadata.camera_names"), {"FC1": "Drone"})
        self.assertEqual(cfg.get("scan.libraries"), [{"extension": ".x", "subfolders": ["a"]}])
        self.assertFalse(cfg.warnings)
        cfg.save()
        self.assertIn("[compare]", cfg.user_file.read_text())

    def test_old_data_folders_and_profile(self):
        d = self.tmp / "old2"
        (d / "profile").mkdir(parents=True)
        (d / "berichte").mkdir()
        (d / "profile" / "p.json").write_text(json.dumps(
            {"name": "p", "aufgabe": "abgleich", "parameter": {"quellen": ["/a"], "ziel": "/b", "ablage": "muster",
                                                              "muster": "{Jahr}"}}))
        cfg = Config(d)
        self.assertTrue((d / "profiles").is_dir() and (d / "reports").is_dir())
        prof = Profiles(cfg).load("p")
        self.assertEqual(prof["job"], "sync")
        self.assertEqual(prof["params"], {"sources": ["/a"], "target": "/b", "placement": "pattern",
                                          "pattern": "{Year}"})

    def test_old_journal_can_be_undone(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(dst / "a.jpg", b"a")
        write(dst / "moved.jpg", b"m")
        j = self.cfg.data_dir / "journal" / "2026-01-01_10-00-00_abgleich.jsonl"
        j.parent.mkdir(parents=True)
        j.write_text("\n".join(json.dumps(e) for e in (
            {"t": 1, "aktion": "kopiert", "von": str(src / "a.jpg"), "nach": str(dst / "a.jpg")},
            {"t": 2, "aktion": "verschoben", "von": str(src / "moved.jpg"), "nach": str(dst / "moved.jpg")})) + "\n")
        done, problems = undo(j, self.ctx)
        self.assertEqual((done, problems), (2, []))
        self.assertFalse((dst / "a.jpg").exists())
        self.assertTrue((src / "moved.jpg").exists())


class LanguageTests(Base):
    def test_catalogs_have_same_keys_and_fields(self):
        en, de = i18n.catalog("en"), i18n.catalog("de")
        self.assertEqual(set(en), {k for k in de if not k.startswith(("setting.", "help."))})
        fields = lambda s: {f for _, f, _, _ in string.Formatter().parse(s) if f}  # noqa: E731
        for k in en:
            if not k.startswith(("ph_help.", "cli.pattern")):
                self.assertEqual(fields(en[k]), fields(de[k]), k)

    def test_german_placeholders_and_output(self):
        validate("{Jahr}/{Kamera}")
        self.assertEqual(canonical("{Jahr}/{KW}-{Typ}"), "{Year}/{Week}-{Type}")
        i18n.set_language("de")
        self.assertEqual(localize("{Year}/{Camera}"), "{Jahr}/{Kamera}")
        e = FileEntry(Path("/q/IMG_1.jpg"), 1, 0, Path("/q"))
        m = Meta(datetime(2025, 8, 9))
        self.assertEqual(PatternRenderer(self.cfg).folder("{Year}/{MonthName}/{Week}/{Type}/{Camera}", e, m),
                         Path("2025/August/KW32/Foto/Unbekannt"))
        i18n.set_language("en")
        self.assertEqual(PatternRenderer(self.cfg).folder("{Jahr}/{Weekday}/{Week}/{Type}", e, m),
                         Path("2025/Sat/W32/Photo"))

    def test_report_in_german(self):
        i18n.set_language("de")
        src = self.tmp / "src"
        write(src / "a.jpg", b"a" * 100)
        res = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(self.tmp / "dst"), dry_run=True))
        labels = dict(summary_items(res))
        self.assertEqual(labels["Fehlen im Ziel"], 1)
        self.assertIn("Würde kopiert", labels)
        html = next(r for r in res.reports if r.suffix == ".html").read_text()
        self.assertIn('lang="de"', html)
        self.assertIn("Probelauf – es wurde nichts verändert.", html)

    def test_report_status_filter(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "a.jpg", b"a" * 100)
        write(src / "b.jpg", b"b" * 100)
        write(dst / "b.jpg", b"b" * 100)
        res = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(dst), dry_run=True))
        self.assertGreater(len(statuses(res)), 1)
        html = next(r for r in res.reports if r.suffix == ".html").read_text()
        self.assertIn('<select id="s">', html)
        self.assertIn(i18n.t("report.all_statuses", n=len(res.rows)), html)
        self.assertEqual(html.count("<tr data-status="), len(res.rows))


class PatternTests(Base):
    def test_render(self):
        r = PatternRenderer(self.cfg)
        e = FileEntry(Path("/q/a/IMG_1.CR3"), 1, 0, Path("/q"))
        m = Meta(datetime(2025, 8, 9, 14, 5, 59), "x", "Canon EOS R6m2", "Canon", "RF 24/105")
        self.assertEqual(r.folder("{Year}/{Year}-{Month}-{Day}", e, m), Path("2025/2025-08-09"))
        self.assertEqual(r.folder("{Camera}/{Lens}", e, m), Path("Canon EOS R6m2/RF 24_105"))
        self.assertEqual(r.filename("{Year}{Month}{Day}_{Name}", e, m), "20250809_IMG_1.CR3")
        self.assertEqual(r.folder("{Year}/{Camera}", e, Meta()), Path("Unknown/Unknown"))

    def test_validate(self):
        with self.assertRaises(PatternError):
            validate("{Yearr}")
        with self.assertRaises(PatternError):
            validate("../{Year}")
        validate("{Year}/{MonthName}")

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
        self.assertEqual(v.rel, Path("L/v.mp4"))  # without .lrlibrary/abc/originals

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
        write(src / "t2" / "B_copy.CR3", b"BBBB" * 1000)  # duplicate in source
        write(dst / "somewhere" / "renamed.CR3", b"AAAA" * 1000)  # A already there, other name

        dry = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(dst), dry_run=True))
        self.assertEqual(dry.summary["missing"], 1)
        self.assertFalse((dst / "src").exists())

        res = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(dst), dry_run=False))
        copied = list((dst / "src").rglob("*.CR3"))
        self.assertEqual(len(copied), 1)
        self.assertEqual(copied[0].read_bytes(), b"BBBB" * 1000)
        self.assertTrue(res.journal and res.journal.exists())
        self.assertTrue(res.reports)
        # A.CR3 is already in the target (renamed) – its XMP is added next to it
        self.assertEqual((dst / "somewhere" / "renamed.xmp").read_bytes(), b"<xmp/>")

        again = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(dst), dry_run=False))
        self.assertEqual(again.summary["missing"], 0)
        self.assertNotIn("sidecars_todo", again.summary)

        done, problems = undo(res.journal, self.ctx)
        self.assertEqual(done, 2)
        self.assertFalse((dst / "somewhere" / "renamed.xmp").exists())
        self.assertFalse(copied[0].exists())

    def test_sync_pattern_with_sidecar(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "A.CR3", b"A" * 3000, mtime=datetime(2024, 5, 6, 7, 8, 9).timestamp())
        write(src / "A.xmp", b"<xmp/>")
        self.cfg.set("metadata.date_fields", ["file_mtime"])
        run_job(self.cfg, self.ctx, "sync",
                SyncParams([str(src)], str(dst), True, "pattern", "{Year}/{Month}", "", dry_run=False))
        self.assertTrue((dst / "2024" / "05" / "A.CR3").exists())
        self.assertTrue((dst / "2024" / "05" / "A.xmp").exists())

    def test_duplicates_move_and_protected(self):
        root = self.tmp / "photos"
        write(root / "a" / "X.jpg", b"same" * 500, mtime=1_000_000)
        write(root / "b" / "X_copy.jpg", b"same" * 500, mtime=2_000_000)
        write(root / "c" / "other.jpg", b"diff" * 500)
        write(root / "Lib.photoslibrary" / "originals" / "Y.jpg", b"lib!" * 500)
        write(root / "d" / "Y.jpg", b"lib!" * 500, mtime=1)

        rep = run_job(self.cfg, self.ctx, "duplicates", DupParams(str(root), dry_run=False))
        self.assertEqual(rep.summary["dup_groups"], 2)
        self.assertTrue((root / "b" / "X_copy.jpg").exists())  # report only

        run_job(self.cfg, self.ctx, "duplicates",
                DupParams(str(root), "oldest", "move", "", dry_run=False, confirmed=True))
        self.assertTrue((root / "a" / "X.jpg").exists())
        self.assertTrue((root / "_Duplicates" / "b" / "X_copy.jpg").exists())
        # the library file stays, although the other one is older
        self.assertTrue((root / "Lib.photoslibrary" / "originals" / "Y.jpg").exists())
        self.assertTrue((root / "_Duplicates" / "d" / "Y.jpg").exists())

    def test_sort_move_collision_and_undo(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        t = datetime(2023, 1, 2, 3, 4, 5).timestamp()
        write(src / "x" / "IMG.jpg", b"one" * 100, mtime=t)
        write(src / "y" / "IMG.jpg", b"two" * 100, mtime=t)
        self.cfg.set("metadata.date_fields", ["file_mtime"])
        res = run_job(self.cfg, self.ctx, "sort",
                      SortParams(str(src), str(dst), "{Year}/{Year}-{Month}-{Day}", "", "move", dry_run=False))
        day = dst / "2023" / "2023-01-02"
        self.assertEqual(sorted(p.name for p in day.iterdir()), ["IMG.jpg", "IMG_1.jpg"])
        self.assertFalse((src / "x" / "IMG.jpg").exists())
        undo(res.journal, self.ctx)
        self.assertTrue((src / "x" / "IMG.jpg").exists())
        self.assertTrue((src / "y" / "IMG.jpg").exists())
        self.assertEqual(len(list_journals(self.cfg.data_dir)), 1)

    def test_sort_never_moves_out_of_library(self):
        src, dst = self.tmp / "Photos.photoslibrary", self.tmp / "dst"
        write(src / "originals" / "a.jpg", b"a" * 10)
        res = run_job(self.cfg, self.ctx, "sort", SortParams(str(src), str(dst), "{Type}", "", "move", dry_run=False))
        self.assertTrue((src / "originals" / "a.jpg").exists())
        self.assertIn("count.protected", res.summary)


class MatchSyncTests(Base):
    def _setup(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "A.CR3", b"A" * 5000)
        for i in range(4):
            write(dst / f"k{i}" / "A.CR3", b"A" * 5000)
        write(dst / "almost_same.CR3", b"A" * 2500 + b"X" + b"A" * 2499)  # same start/end
        return src, dst

    def test_stops_after_first_target_match(self):
        from mediatidy.hashing import Hasher
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
        self.assertLessEqual(len(reads), 3)  # source + at most 2 targets instead of 5

    def test_full_hash_detects_near_identical(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "A.CR3", b"A" * 5000)
        write(dst / "B.CR3", b"A" * 2500 + b"X" + b"A" * 2499)
        res = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(dst), False, dry_run=True))
        self.assertEqual(res.summary["missing"], 1)
        self.cfg.set("compare.full_hash", False)  # quick mode: counts as identical
        res = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(dst), False, dry_run=True))
        self.assertEqual(res.summary["missing"], 0)


class AccessTests(Base):
    def setUp(self):
        super().setUp()
        self.src, self.dst = self.tmp / "src", self.tmp / "dst"
        write(self.src / "A.jpg", b"A" * 100)
        write(self.dst / "secret" / "A.jpg", b"A" * 100)

    def tearDown(self):
        for d in (self.dst, self.dst / "secret"):
            if d.exists():
                d.chmod(0o755)
        super().tearDown()

    def test_unreadable_target_aborts(self):
        self.dst.chmod(0)
        for dry in (True, False):
            with self.assertRaises(AccessError):
                run_job(self.cfg, self.ctx, "sync", SyncParams([str(self.src)], str(self.dst), dry_run=dry))

    def test_partly_unreadable_target(self):
        (self.dst / "secret").chmod(0)
        res = run_job(self.cfg, self.ctx, "sync", SyncParams([str(self.src)], str(self.dst), dry_run=True))
        self.assertIn("target_incomplete", res.summary)
        with self.assertRaises(AccessError):
            run_job(self.cfg, self.ctx, "sync", SyncParams([str(self.src)], str(self.dst), dry_run=False))
        self.assertEqual(list(self.dst.glob("src")), [])

    def test_unmounted_volume(self):
        with self.assertRaises(AccessError):
            run_job(self.cfg, self.ctx, "sync",
                    SyncParams([str(self.src)], "/Volumes/DoesNotExist123/Photos", dry_run=True))


class RegressionTests(Base):
    """Bugs found by the real-world check of 2026-10-17 (tests/real_world_check.py)."""

    def test_source_folder_with_dot_keeps_full_name(self):
        src = self.tmp / "Holiday 2025.08"
        write(src / "p.jpg", b"p" * 100)
        run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(self.tmp / "dst"), dry_run=False))
        self.assertTrue((self.tmp / "dst" / "Holiday 2025.08" / "p.jpg").exists())

    def test_shared_xmp_belongs_to_raw(self):
        root = self.tmp / "shoot"
        write(root / "IMG_7.JPG", b"j" * 100)
        write(root / "IMG_7.CR3", b"r" * 100)
        write(root / "IMG_7.xmp", b"<xmp/>")
        write(root / "k.jpg", b"k" * 100)
        write(root / "k.jpg.XMP", b"<xmp/>")  # other spelling of the extension
        entries = {e.path.name: [s.name for s in e.sidecars] for e in Scanner(self.cfg).scan(root, self.ctx)[0]}
        self.assertEqual(entries, {"IMG_7.CR3": ["IMG_7.xmp"], "IMG_7.JPG": [], "k.jpg": ["k.jpg.XMP"]})

    def test_dup_jpg_does_not_take_raw_xmp(self):
        root = self.tmp / "photos"
        write(root / "Shoot" / "IMG_7.JPG", b"j" * 100, mtime=2_000_000)
        write(root / "Shoot" / "IMG_7.CR3", b"r" * 100)
        write(root / "Shoot" / "IMG_7.xmp", b"<xmp/>")
        write(root / "Export" / "IMG_7.JPG", b"j" * 100, mtime=1_000_000)
        run_job(self.cfg, self.ctx, "duplicates",
                DupParams(str(root), "oldest", "move", dry_run=False, confirmed=True))
        self.assertTrue((root / "_Duplicates" / "Shoot" / "IMG_7.JPG").exists())
        self.assertTrue((root / "Shoot" / "IMG_7.xmp").exists())

    def test_sort_nef_jpg_xmp_goes_with_raw(self):
        src = self.tmp / "src"
        write(src / "DSC_1.JPG", b"j" * 100)
        write(src / "DSC_1.NEF", b"n" * 100)
        write(src / "DSC_1.xmp", b"<xmp/>")
        run_job(self.cfg, self.ctx, "sort",
                SortParams(str(src), str(self.tmp / "out"), "{Type}", "", "move", dry_run=False, confirmed=True))
        self.assertTrue((self.tmp / "out" / "RAW" / "DSC_1.xmp").exists())
        self.assertFalse((self.tmp / "out" / "Photo" / "DSC_1.xmp").exists())

    def test_custom_dup_folder_is_not_rescanned(self):
        root = self.tmp / "photos"
        write(root / "a.jpg", b"x" * 100, mtime=1_000_000)
        write(root / "b.jpg", b"x" * 100, mtime=2_000_000)
        p = DupParams(str(root), "oldest", "move", "SetAside", dry_run=False, confirmed=True)
        run_job(self.cfg, self.ctx, "duplicates", p)
        again = run_job(self.cfg, self.ctx, "duplicates", DupParams(str(root), "oldest", "move", "SetAside"))
        self.assertEqual(again.summary["dup_groups"], 0)

    def test_empty_files_reported_as_defect(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "empty.jpg", b"")
        write(src / "x" / "empty2.jpg", b"")
        write(dst / "broken.jpg", b"")
        res = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(dst), dry_run=False))
        status = {Path(r["file"]).name: r["status"] for r in res.rows}
        self.assertEqual(status["empty.jpg"], "empty")
        self.assertFalse((dst / "src").exists())
        dup = run_job(self.cfg, self.ctx, "duplicates", DupParams(str(src)))
        self.assertEqual(dup.summary["dup_groups"], 0)
        self.assertEqual(dup.summary["empty_files"], 2)

    def _sidecar_world(self, src_xmp_mtime: float):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "A.CR3", b"A" * 1000)
        write(src / "A.xmp", b"<xmp>new</xmp>", mtime=src_xmp_mtime)
        write(dst / "Backup" / "A.CR3", b"A" * 1000)
        write(dst / "Backup" / "A.xmp", b"<xmp>old</xmp>", mtime=1_500_000_000)
        return src, dst

    def test_changed_sidecar_replaced_when_source_newer_and_undo(self):
        src, dst = self._sidecar_world(1_600_000_000)
        dry = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(dst), dry_run=True))
        self.assertEqual(dry.summary["sidecars_todo"], 1)
        self.assertEqual((dst / "Backup" / "A.xmp").read_bytes(), b"<xmp>old</xmp>")
        res = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(dst), dry_run=False))
        self.assertEqual((dst / "Backup" / "A.xmp").read_bytes(), b"<xmp>new</xmp>")
        self.assertIn("sidecar_updated", statuses(res))
        done, problems = undo(res.journal, self.ctx)
        self.assertEqual(problems, [])
        self.assertEqual((dst / "Backup" / "A.xmp").read_bytes(), b"<xmp>old</xmp>")

    def test_sidecar_newer_in_target_is_kept(self):
        src, dst = self._sidecar_world(1_400_000_000)
        res = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(dst), dry_run=False))
        self.assertEqual((dst / "Backup" / "A.xmp").read_bytes(), b"<xmp>old</xmp>")
        self.assertIn("sidecar_target_newer", statuses(res))

    def test_sidecar_report_only_setting(self):
        src, dst = self._sidecar_world(1_600_000_000)
        self.cfg.set("sync.changed_sidecar", "report_only")
        res = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(dst), dry_run=False))
        self.assertEqual((dst / "Backup" / "A.xmp").read_bytes(), b"<xmp>old</xmp>")
        self.assertIn("sidecar_changed", statuses(res))

    def test_trash_failure_is_reported_not_hidden(self):
        from unittest import mock
        import subprocess
        root = self.tmp / "photos"
        write(root / "a.jpg", b"x" * 100, mtime=1_000_000)
        write(root / "b.jpg", b"x" * 100, mtime=2_000_000)

        def finder_refuses(cmd, **kw):
            self.assertIn("set f to POSIX file (p as text)", cmd)  # the form the Finder understands
            return subprocess.CompletedProcess(cmd, 0, f"{cmd[-1]}\tAccess denied\n", "")
        with mock.patch("mediatidy.actions.subprocess.run", side_effect=finder_refuses):
            res = run_job(self.cfg, self.ctx, "duplicates",
                          DupParams(str(root), "oldest", "trash", dry_run=False, confirmed=True))
        row = next(r for r in res.rows if r.get("file", "").endswith("b.jpg"))
        self.assertEqual(row["status"], "error")
        self.assertIn("Access denied", row["info"])
        self.assertNotIn("count.trashed", res.summary)
        self.assertTrue((root / "b.jpg").exists())

    def test_trash_success_is_journaled(self):
        from unittest import mock
        import subprocess
        root = self.tmp / "photos"
        write(root / "a.jpg", b"x" * 100, mtime=1_000_000)
        write(root / "b.jpg", b"x" * 100, mtime=2_000_000)

        def finder_deletes(cmd, **kw):
            Path(cmd[-1]).unlink()
            return subprocess.CompletedProcess(cmd, 0, "\n", "")
        with mock.patch("mediatidy.actions.subprocess.run", side_effect=finder_deletes):
            res = run_job(self.cfg, self.ctx, "duplicates",
                          DupParams(str(root), "oldest", "trash", dry_run=False, confirmed=True))
        self.assertEqual(res.summary.get("count.trashed"), 1)
        self.assertEqual([e["action"] for e in read_entries(res.journal)], ["trashed"])

    def test_runs_in_same_second_have_own_journal_and_report(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "a.jpg", b"a" * 100)
        first = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(dst), dry_run=False))
        write(src / "b.jpg", b"b" * 100)
        second = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(dst), dry_run=False))
        self.assertNotEqual(first.journal, second.journal)
        self.assertFalse(set(first.reports) & set(second.reports))
        self.assertTrue(all(r.exists() for r in first.reports + second.reports))
        undo(second.journal, self.ctx)
        self.assertTrue((dst / "src" / "a.jpg").exists())
        self.assertFalse((dst / "src" / "b.jpg").exists())


class SmallFindingsTests(Base):
    """Smaller findings of the real-world check (2026-10-17)."""

    def test_overlapping_sources_count_once(self):
        src = self.tmp / "Q"
        write(src / "a.jpg", b"a" * 100)
        write(src / "sub" / "b.jpg", b"b" * 100)
        res = run_job(self.cfg, self.ctx, "sync",
                      SyncParams([str(src), str(src / "sub")], str(self.tmp / "Z"), dry_run=True))
        self.assertEqual(res.summary["files_in_sources"], 2)
        self.assertNotIn("count.duplicate_in_source", res.summary)

    def test_source_inside_target(self):
        z = self.tmp / "Z"
        write(z / "Archive" / "x.jpg", b"x" * 100)
        write(z / "New" / "x-copy.jpg", b"x" * 100)
        write(z / "New" / "only_new.jpg", b"n" * 100)
        res = run_job(self.cfg, self.ctx, "sync", SyncParams([str(z / "New")], str(z), dry_run=False))
        status = {Path(r["file"]).name: r["status"] for r in res.rows}
        self.assertEqual(status["x-copy.jpg"], "in_target")
        self.assertEqual(status["only_new.jpg"], "only_in_source")
        self.assertEqual(res.summary["missing"], 0)
        self.assertEqual(sorted(p.name for p in z.rglob("*.jpg")), ["only_new.jpg", "x-copy.jpg", "x.jpg"])

    def test_sync_adds_into_existing_folder(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "a" / "new.jpg", b"n" * 100)
        write(dst / "src" / "a" / "old.jpg", b"a" * 100)
        write(dst / "src" / "a" / "note.txt", b"hand")
        old = (dst / "src" / "a" / "old.jpg").stat()
        res = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(dst), dry_run=False))
        self.assertTrue((dst / "src" / "a" / "new.jpg").exists())
        self.assertEqual((dst / "src" / "a" / "old.jpg").stat().st_mtime_ns, old.st_mtime_ns)
        self.assertTrue((dst / "src" / "a" / "note.txt").exists())
        self.assertNotIn("folder_created", Path(res.journal).read_text())  # folder existed already
        undo(res.journal, self.ctx)
        self.assertEqual(sorted(p.name for p in (dst / "src" / "a").iterdir()), ["note.txt", "old.jpg"])

    def test_real_run_report_local_plus_copy_in_target(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "x.jpg", b"x" * 100)
        res = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(dst), dry_run=False))
        self.assertEqual(res.reports[0].parent, self.cfg.data_dir / "reports")  # this one is opened
        self.assertEqual({p.parent for p in res.reports}, {self.cfg.data_dir / "reports", dst / "_MediaTidy-Reports"})
        self.assertEqual(len(res.reports), 4)

    def test_undo_removes_created_folders_only(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "a" / "b" / "x.jpg", b"x" * 100)
        (dst / "empty_before").mkdir(parents=True)
        res = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(dst), dry_run=False))
        write(dst / "src" / "a" / ".DS_Store", b"finder")  # the Finder was in the folder
        undo(res.journal, self.ctx)
        self.assertFalse((dst / "src").exists())
        self.assertTrue((dst / "empty_before").is_dir())
        self.assertTrue((dst / "_MediaTidy-Reports").is_dir())  # reports stay

    def test_undo_keeps_folder_with_new_files(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        write(src / "x.jpg", b"x" * 100)
        res = run_job(self.cfg, self.ctx, "sync", SyncParams([str(src)], str(dst), dry_run=False))
        write(dst / "src" / "own.jpg", b"e" * 100)  # added by hand later
        undo(res.journal, self.ctx)
        self.assertTrue((dst / "src" / "own.jpg").exists())
        self.assertFalse((dst / "src" / "x.jpg").exists())

    def test_cancel_writes_partial_report(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        for i in range(5):
            write(src / f"f{i}.jpg", bytes([i]) * 100)
        ctx = RunContext(on_progress=lambda ph, d, n: ctx.cancel() if ph == "copy" and d == 2 else None)
        res = run_job(self.cfg, ctx, "sync", SyncParams([str(src)], str(dst), dry_run=False))
        self.assertTrue(res.cancelled)
        self.assertIn("cancelled", res.summary)
        self.assertTrue(res.reports and all(r.exists() for r in res.reports))
        self.assertEqual(len(list(dst.rglob("*.jpg"))), 2)
        undo(res.journal, self.ctx)
        self.assertFalse(dst.joinpath("src").exists())

    def test_cache_detects_rewrite_with_same_size_and_mtime(self):
        root = self.tmp / "photos"
        a = write(root / "a.jpg", b"1" * 100, mtime=1_000_000)
        write(root / "b.jpg", b"1" * 100, mtime=1_000_000)
        self.assertEqual(run_job(self.cfg, self.ctx, "duplicates", DupParams(str(root))).summary["dup_groups"], 1)
        write(a, b"2" * 100, mtime=1_000_000)  # new content, same size + timestamp as before
        self.assertEqual(run_job(self.cfg, self.ctx, "duplicates", DupParams(str(root))).summary["dup_groups"], 0)

    def test_week_year_placeholder(self):
        e = FileEntry(Path("/q/x.jpg"), 0, 0, Path("/q"))
        m = Meta(datetime(2025, 12, 31, 12, 0))
        self.assertEqual(PatternRenderer(self.cfg).folder("{WeekYear}/{Week}", e, m), Path("2026/W01"))

    def test_unsafe_method_noted_in_summary(self):
        root = self.tmp / "photos"
        write(root / "a.jpg", b"1" * 100)
        self.cfg.set("compare.method", "name_size")
        res = run_job(self.cfg, self.ctx, "duplicates", DupParams(str(root)))
        self.assertTrue(res.summary["comparison"].startswith("⚠️"))

    def test_move_with_identical_target_explains(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        t = datetime(2023, 1, 2).timestamp()
        write(src / "a.jpg", b"a" * 100, mtime=t)
        write(dst / "2023" / "a.jpg", b"a" * 100)
        self.cfg.set("metadata.date_fields", ["file_mtime"])
        res = run_job(self.cfg, self.ctx, "sort", SortParams(str(src), str(dst), "{Year}", "", "move", False, True))
        self.assertIn("duplicate", res.rows[0]["info"])
        self.assertTrue((src / "a.jpg").exists())



class CliTests(Base):
    def test_check_exit_code_reflects_hints(self):
        from unittest import mock
        from mediatidy import __main__ as cli
        args = ["--config-dir", str(self.tmp / "cfg"), "check"]
        with mock.patch.object(cli, "check", return_value=[]), mock.patch("builtins.print"):
            self.assertEqual(cli.main(args), 0)
        with mock.patch.object(cli, "check", return_value=["no exiftool"]), mock.patch("builtins.print"):
            self.assertEqual(cli.main(args), 1)



def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two #RRGGBB colours."""
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class ThemeTests(Base):
    # (foreground, background, minimum ratio): 7 for body text, 4.5 for small/secondary text, 3 for UI parts
    PAIRS = [("text", "paper", 7), ("text", "side", 7), ("text", "field", 7), ("text", "dry_tint", 7),
             ("text", "real_tint", 7), ("muted", "paper", 4.5), ("muted", "side", 4.5), ("muted", "field", 4.5),
             ("muted", "dry_tint", 4.5), ("muted", "real_tint", 4.5), ("on_dry", "dry", 4.5),
             ("on_dry", "dry_dark", 4.5), ("on_real", "real", 4.5), ("on_real", "real_dark", 4.5),
             ("error", "paper", 4.5), ("error", "side", 4.5), ("error", "field", 4.5),
             ("dry", "paper", 3), ("dry", "field", 3), ("thumb", "side", 3)]

    def test_themes_complete_and_readable(self):
        from mediatidy.gui import CHOICES, DARK_THEMES, THEMES
        keys = set(THEMES["light"])
        self.assertEqual(set(CHOICES["ui.theme"]), set(THEMES) | {"system"})
        self.assertLessEqual(DARK_THEMES, set(THEMES))
        for name, pal in THEMES.items():
            self.assertEqual(set(pal), keys, name)
            for fg, bg, need in self.PAIRS:
                self.assertGreaterEqual(contrast(pal[fg], pal[bg]), need, f"{name}: {fg} on {bg}")

    def test_apply_theme_system_and_unknown(self):
        from unittest import mock
        from mediatidy import gui
        try:
            self.assertEqual(gui.apply_theme("sand"), "sand")
            self.assertEqual(gui.P, gui.THEMES["sand"])
            with mock.patch.object(gui, "system_dark", return_value=True):
                self.assertEqual(gui.apply_theme("system"), "dark")
                self.assertEqual(gui.apply_theme("nonsense"), "dark")
            with mock.patch.object(gui, "system_dark", return_value=False):
                self.assertEqual(gui.apply_theme("system"), "light")
        finally:
            gui.apply_theme("light")


if __name__ == "__main__":
    unittest.main()
