# MediaTidy

A macOS app for photographers: compare photos and videos between folders and drives, find duplicates, and sort files into folders by their metadata (capture date, camera, lens …).

- **Content-based** – files are compared by checksum, not by name.
- **Safe by default** – every run is a dry run unless you explicitly execute it; every real action is logged and can be undone.
- **Library-aware** – reads the originals inside Apple Photos (`.photoslibrary`) and Lightroom (`.lrlibrary`) libraries without ever modifying them, and warns before moving files that a Lightroom Classic catalog uses.
- **Sidecar-aware** – XMP/AAE sidecars travel with their photos and are kept up to date in backups.
- **Configurable** – every option lives in one documented settings file and in the Settings page.

> **Note:** The user interface, settings and command-line options are currently in **German**. This README translates the relevant terms.

> **Warning:** MediaTidy copies, moves and (if you choose) trashes files. Keep a backup of your photos, start with a dry run and read the report before executing. The software is provided without warranty (see [LICENSE](LICENSE)).

## Requirements

- macOS (uses Finder, `/Volumes`, AppleScript)
- Python ≥ 3.11 with tkinter
- [ExifTool](https://exiftool.org) (for date/camera/lens metadata)
- No other Python packages

## Installation

```sh
brew install python-tk@3.14 exiftool          # Homebrew; any Python ≥ 3.11 with tkinter works
git clone https://github.com/Ben-ReverseLab/media-tidy-mac.git
cd media-tidy-mac
app/build-app.sh                              # builds MediaTidy.app (a small launcher, signed locally)
python3 -m mediatidy pruefen                   # checks dependencies and drive access
```

If Homebrew asks for it, run `sudo xcodebuild -license accept` first.

### Permissions

macOS grants file access per app, so grant it to **MediaTidy.app**, not to Terminal:

- **External drives:** macOS asks on first access → click “Allow” (removable volumes).
- **Apple Photos library:** System Settings → Privacy & Security → **Full Disk Access** → `+` → select `MediaTidy.app` from the cloned folder.

After rebuilding the app (`app/build-app.sh`) you may have to grant Full Disk Access again.

## Usage

Double-click **`MediaTidy.app`**. (`MediaTidy.command` also works, but then Terminal needs the permissions.)

Python code changes take effect after restarting the app; rebuilding is only needed after changes to `app/MediaTidy.applescript`.

### Pages

| Page (German UI) | Purpose |
|---|---|
| 1 Abgleich (*Sync*) | Compare several source folders with one target by content. Copy what is missing – keeping the folder structure or sorted by a pattern. Duplicates within the sources are copied only once. Sidecars (XMP/AAE) of already backed-up files are added, or replaced when the source is newer (the old version is kept for undo). |
| 2 Duplikate (*Duplicates*) | Find files with identical content in a folder tree. Choose which copy to keep and what to do with the others: report only / move to `_Duplikate` / move to Trash. A shared XMP (`IMG.xmp` next to `IMG.CR3` and `IMG.JPG`) always belongs to the RAW file. |
| 3 Sortieren (*Sort*) | Copy or move files into a folder structure by capture date, camera, lens, type …, optionally renaming them. |
| Einstellungen (*Settings*) | All options, generated from `defaults.toml` with explanations. |
| Verlauf (*History*) | Every real run is logged and can be undone. |

- **Dry run** (*Probelauf*) is on by default: shows what would happen, as an HTML/CSV report.
- **Empty files** (0 bytes) are reported as “probably damaged” and neither compared nor copied.
- **Profiles**: save a page’s inputs under a name (e.g. “Pictures → Backup drive”) and load them later.
- **Interrupted runs**: just start again – already copied files are recognised. Copies are written under a temporary name, verified by checksum and only then renamed.

## Patterns

`/` separates folders. Placeholders (German names):

| Placeholder | Meaning | Placeholder | Meaning |
|---|---|---|---|
| `{Jahr}` | year (2025) | `{Stunde}` | hour |
| `{Monat}` | month, two digits (08) | `{Minute}` | minute |
| `{MonatName}` | month name | `{Sekunde}` | second |
| `{Tag}` | day, two digits | `{Kamera}` | camera model |
| `{Quartal}` | quarter (Q3) | `{Hersteller}` | camera make |
| `{KW}` | ISO week (KW32) | `{Objektiv}` | lens |
| `{KWJahr}` | year of the ISO week | `{Typ}` | RAW / Foto / Video |
| `{Wochentag}` | weekday | `{Endung}` | file extension |
| `{Name}` | original file name | `{Ordner}` | current folder name |
| `{Quelle}` | source folder name | | |

Examples: `{Jahr}/{Jahr}-{Monat}-{Tag}`, `{Kamera}/{Jahr}`, file name `{Jahr}{Monat}{Tag}_{Stunde}{Minute}{Sekunde}_{Name}`.

Month names can be changed in Settings (`muster.monatsnamen`).

**Name review:** if a pattern uses `{Kamera}`, `{Hersteller}` or `{Objektiv}`, MediaTidy lists all names it has not seen before – after reading the metadata, before changing anything. Example: a drone that writes slightly different model names into its photos and videos. Rename or accept them; the mapping is saved in Settings → Metadaten (`kamera_namen`, `hersteller_namen`, `objektiv_namen`) and not asked again. Turn off with `metadaten.namen_pruefen = false`.

## Command line

```sh
python3 -m mediatidy abgleich --quelle ~/Pictures/2025 --quelle ~/Pictures/2026 --ziel /Volumes/Backup/Photos
python3 -m mediatidy abgleich --profil "Pictures → Backup" --ausfuehren
python3 -m mediatidy duplikate --ordner /Volumes/Backup/Photos --aktion verschieben --ausfuehren
python3 -m mediatidy sortieren --quelle ~/Import --ziel ~/Sorted --muster "{Jahr}/{Kamera}" --verschieben --ausfuehren
python3 -m mediatidy rueckgaengig --ausfuehren
```

| Command / option | Meaning |
|---|---|
| `abgleich` / `duplikate` / `sortieren` | sync / duplicates / sort |
| `rueckgaengig` | undo the last run (`--datei` for a specific journal) |
| `profile` | list saved profiles |
| `pruefen` | check dependencies and drive access |
| `--quelle`, `--ziel`, `--ordner` | source, target, folder |
| `--muster`, `--dateiname` | folder pattern, file name pattern |
| `--ablage struktur\|muster` | keep structure / sort by pattern (sync) |
| `--behalten`, `--aktion` | which copy to keep / what to do with duplicates |
| `--verschieben` | move instead of copy |
| `--profil` | use a saved profile |
| `--ausfuehren` | really execute (without it: always a dry run) |
| `--ja` | confirm trashing/overwriting without asking |
| `--config-dir` | use a different settings folder |

## Files

| Path | Contents |
|---|---|
| `mediatidy/defaults.toml` | Default settings with explanations – don’t edit |
| `~/.mediatidy/config.toml` | Your settings (only values that differ from the defaults) |
| `~/.mediatidy/profile/` | Saved profiles |
| `~/.mediatidy/journal/` | Log of every action (for undo) |
| `~/.mediatidy/berichte/` | All reports (real runs also put a copy into `_MediaTidy-Berichte` in the target) |
| `~/.mediatidy/cache.db` | Checksum cache (makes repeated runs fast; safe to delete) |
| `~/.mediatidy/logs/` | Log files |

## Development

- Python standard library + tkinter only; external tools: exiftool (and Finder via `osascript`).
- New setting: add it with a comment to `defaults.toml` → it appears automatically on the Settings page.
- Incompatible config change: increase `CONFIG_VERSION` in `config.py` and add a function to `MIGRATIONS`.
- Unit tests (under a second): `python3 -m unittest discover -s tests`
- Real-world test with generated photos/videos and disk images: `python3 tests/praxis_pruefung.py` (3–5 min, needs exiftool and ffmpeg). **It moves test files into your real Finder Trash** and mounts temporary disk images.

## License

[MIT](LICENSE)
