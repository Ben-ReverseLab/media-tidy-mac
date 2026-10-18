"""Reads data written by earlier versions (still called FotoTool), which used German names for settings, profiles,
journals and folders. Everything here only translates old names to the current ones."""
from __future__ import annotations

import re
from pathlib import Path

# --- Settings (config version 1 -> 2) ------------------------------------------
SECTIONS = {
    "dateien": "files", "vergleich": "compare", "metadaten": "metadata", "muster": "patterns",
    "aktionen": "actions", "abgleich": "sync", "duplikate": "duplicates", "berichte": "reports",
    "pfade": "paths", "oberflaeche": "ui",
}
KEYS = {
    "files": {"foto": "photo", "aktive_gruppen": "active_groups", "sidecars_mitnehmen": "include_sidecars"},
    "scan": {"ignorieren": "ignore", "versteckte_ignorieren": "skip_hidden", "symlinks_folgen": "follow_symlinks",
             "leere_dateien_als_defekt": "empty_files_as_defect", "bibliotheken": "libraries"},
    "compare": {"methode": "method", "hash_algorithmus": "hash_algorithm", "teilhash_kb": "partial_hash_kb",
                "lesepuffer_kb": "read_buffer_kb", "threads_extern": "threads_external", "voller_hash": "full_hash",
                "cache_speichern_alle": "cache_save_every"},
    "metadata": {"datum_felder": "date_fields", "kamera_felder": "camera_fields", "hersteller_felder": "make_fields",
                 "objektiv_felder": "lens_fields", "unbekannt": "unknown",
                 "hersteller_aus_kamera_entfernen": "strip_make_from_camera", "batch_groesse": "batch_size",
                 "namen_pruefen": "review_names", "kamera_namen": "camera_names", "hersteller_namen": "make_names",
                 "objektiv_namen": "lens_names"},
    "patterns": {"vorlagen": "presets", "standard": "default", "dateiname": "filename", "monatsnamen": "month_names",
                 "ungueltige_zeichen": "invalid_chars", "ersatzzeichen": "replacement_char"},
    "actions": {"probelauf": "dry_run", "kollision": "collision", "suffix_muster": "suffix_pattern",
                "kopie_pruefen": "verify_copy", "zeitstempel_erhalten": "preserve_timestamps", "bestaetigen": "confirm",
                "geschuetzt": "protected"},
    "sync": {"fehlende_ablage": "missing_placement", "quellordner_als_ebene": "source_folder_as_level",
             "sidecars_abgleichen": "sync_sidecars", "sidecar_geaendert": "changed_sidecar"},
    "duplicates": {"behalten": "keep", "aktion": "action", "ordner": "folder"},
    "reports": {"ordner": "folder"},
    "paths": {"datenordner": "data_dir", "lightroom_kataloge": "lightroom_catalogs"},
    "ui": {"schriftgroesse": "font_size", "fenster": "window"},
}
LIBRARY_KEYS = {"endung": "extension", "unterordner": "subfolders"}
VALUES = {
    "compare.method": {"name_groesse": "name_size", "name_groesse_datum": "name_size_date"},
    "actions.collision": {"ueberspringen": "skip", "ueberschreiben": "overwrite"},
    "sync.missing_placement": {"struktur": "structure", "muster": "pattern"},
    "sync.changed_sidecar": {"neuer_ersetzt": "newer_replaces", "nur_bericht": "report_only"},
    "duplicates.keep": {"aeltestes": "oldest", "neuestes": "newest", "kuerzester_pfad": "shortest_path",
                        "laengster_pfad": "longest_path", "erster": "first"},
    "duplicates.action": {"bericht": "report", "verschieben": "move", "papierkorb": "trash"},
}
LIST_VALUES = {
    "files.active_groups": {"foto": "photo"},
    "metadata.date_fields": {"datei_mtime": "file_mtime"},
}
PLACEHOLDERS = {
    "Jahr": "Year", "Monat": "Month", "MonatName": "MonthName", "Tag": "Day", "Quartal": "Quarter", "KW": "Week",
    "KWJahr": "WeekYear", "Wochentag": "Weekday", "Stunde": "Hour", "Sekunde": "Second", "Kamera": "Camera",
    "Hersteller": "Make", "Objektiv": "Lens", "Typ": "Type", "Endung": "Ext", "Ordner": "Folder", "Quelle": "Source",
}
PATTERN_SETTINGS = ("patterns.default", "patterns.filename")


def pattern(p: str) -> str:
    """Old placeholder names in a pattern -> current ones."""
    return re.sub(r"\{(\w+)\}", lambda m: "{%s}" % PLACEHOLDERS.get(m.group(1), m.group(1)), p)


def config_v1_to_v2(cfg: dict) -> dict:
    out: dict = {"version": 2}
    for old_section, value in cfg.items():
        if old_section == "version":
            continue
        section = SECTIONS.get(old_section, old_section)
        if not isinstance(value, dict):
            out[section] = value
            continue
        keys = KEYS.get(section, {})
        new: dict = {}
        for k, v in value.items():
            key = keys.get(k, k)
            path = f"{section}.{key}"
            if path in VALUES and isinstance(v, str):
                v = VALUES[path].get(v, v)
            elif path in LIST_VALUES and isinstance(v, list):
                v = [LIST_VALUES[path].get(x, x) for x in v]
            elif path == "scan.libraries" and isinstance(v, list):
                v = [{LIBRARY_KEYS.get(a, a): b for a, b in item.items()} for item in v if isinstance(item, dict)]
            elif path in PATTERN_SETTINGS and isinstance(v, str):
                v = pattern(v)
            elif path == "patterns.presets" and isinstance(v, list):
                v = [pattern(x) for x in v]
            new[key] = v
        out[section] = new
    return out


# --- Jobs, profiles, last inputs -------------------------------------------------
JOBS = {"abgleich": "sync", "duplikate": "duplicates", "sortieren": "sort"}
PARAMS = {
    "sync": {"quellen": "sources", "ziel": "target", "fehlende_kopieren": "copy_missing", "ablage": "placement",
             "muster": "pattern", "dateiname": "filename", "probelauf": "dry_run"},
    "duplicates": {"ordner": "folder", "behalten": "keep", "aktion": "action", "duplikat_ordner": "dup_folder",
                   "probelauf": "dry_run"},
    "sort": {"quelle": "source", "ziel": "target", "muster": "pattern", "dateiname": "filename", "modus": "mode",
             "probelauf": "dry_run"},
}
PARAM_VALUES = {
    "placement": VALUES["sync.missing_placement"],
    "keep": VALUES["duplicates.keep"],
    "action": VALUES["duplicates.action"],
}


def job_name(job: str) -> str:
    return JOBS.get(job, job)


def params(job: str, d: dict) -> dict:
    job = job_name(job)
    names = PARAMS.get(job, {})
    out = {}
    for k, v in d.items():
        key = names.get(k, k)
        if key in PARAM_VALUES and isinstance(v, str):
            v = PARAM_VALUES[key].get(v, v)
        elif key in ("pattern", "filename") and isinstance(v, str):
            v = pattern(v)
        out[key] = v
    return out


def profile(d: dict) -> dict:
    if "aufgabe" not in d and "parameter" not in d:
        return d
    job = job_name(d.get("aufgabe", "abgleich"))
    return {"name": d.get("name", ""), "version": 2, "job": job, "params": params(job, d.get("parameter", {}))}


def last_inputs(d: dict) -> dict:
    out = {"tab": d.get("tab", 0), "dry_run": d.get("probelauf", True)}
    for old, new in JOBS.items():
        if isinstance(d.get(old), dict):
            out[new] = params(new, d[old])
    return out


# --- Journal ---------------------------------------------------------------------
JOURNAL_KEYS = {"aktion": "action", "von": "src", "nach": "dst", "sicherung": "backup", "rueckgaengig": "undone"}
JOURNAL_ACTIONS = {
    "kopiert": "copied", "verschoben": "moved", "duplikat_verschoben": "duplicate_moved",
    "ueberschrieben": "overwritten", "papierkorb": "trashed", "sidecar_aktualisiert": "sidecar_updated",
    "ordner_angelegt": "folder_created", "lauf_rueckgaengig": "run_undone",
}
UNDONE_SUFFIX = "_RUECKGAENGIG"


def journal_entry(e: dict) -> dict:
    if "aktion" not in e:
        return e
    out = {JOURNAL_KEYS.get(k, k): v for k, v in e.items()}
    out["action"] = JOURNAL_ACTIONS.get(out["action"], out["action"])
    return out


# --- Data folder -------------------------------------------------------------------
DATA_DIRS = {"berichte": "reports", "profile": "profiles"}
LAST_INPUTS_FILE = "zuletzt.json"


def migrate_data_dir(data_dir: Path) -> None:
    """Rename old subfolders once, if the new ones don't exist yet."""
    for old, new in DATA_DIRS.items():
        src, dst = data_dir / old, data_dir / new
        if src.is_dir() and not dst.exists():
            try:
                src.rename(dst)
            except OSError:
                pass
