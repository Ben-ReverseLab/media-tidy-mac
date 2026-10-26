# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Colour themes: automatic (light or dark like macOS), light, dark, sand, sage, night blue and graphite (`ui.theme`, takes effect after a restart).
- HTML report: filter the table by status (drop-down next to the text filter, with counts).

### Changed
- `mediatidy check` exits with code 1 when it finds problems.

### Fixed
- The shortcut hint at the bottom of the sidebar was cut off.

## [0.2.0] - 2026-10-18

First public release.

### Added
- Sync: copy photos and videos that are missing in a target, compared by content (checksum), keeping the folder structure or sorted by a pattern.
- Duplicates: find files with identical content and report them, move them to a duplicates folder or to the Trash.
- Sort: copy or move files into folders by capture date, camera, lens or type, optionally renaming them.
- Reads the originals in Apple Photos and Lightroom libraries without modifying them.
- XMP/AAE sidecars travel with their photos and are kept up to date in backups.
- Dry run by default, HTML/CSV reports, and undo for every real run.
- English and German user interface (`ui.language`).
- Command line interface (`python3 -m mediatidy`).

[Unreleased]: https://github.com/Ben-ReverseLab/media-tidy-mac/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Ben-ReverseLab/media-tidy-mac/releases/tag/v0.2.0
