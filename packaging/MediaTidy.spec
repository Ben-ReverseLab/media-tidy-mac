# PyInstaller spec for the downloadable MediaTidy.app – run via packaging/build.sh, not directly.
import os
import re
from pathlib import Path

ROOT = Path(SPECPATH).parent
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "mediatidy" / "__init__.py").read_text()).group(1)
EXIFTOOL = Path(os.environ["MEDIATIDY_EXIFTOOL_DIR"])  # unpacked Image-ExifTool distribution (build.sh)
BUNDLE_ID = "io.github.ben-reverselab.mediatidy"
ICON = str(ROOT / "packaging" / "MediaTidy.icns")  # made by packaging/icon/make-icon.swift
REMOVABLE = "MediaTidy compares and copies photos on external drives."

datas = [
    (str(ROOT / "mediatidy" / "defaults.toml"), "mediatidy"),  # comments = English help on the Settings page
    (str(ROOT / "mediatidy" / "locales"), "mediatidy/locales"),
    (str(EXIFTOOL / "exiftool"), "exiftool"),
    (str(EXIFTOOL / "lib"), "exiftool/lib"),
    (str(EXIFTOOL / "README"), "exiftool"),  # contains the ExifTool licence
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "packaging" / "Resources" / "en.lproj"), "en.lproj"),
    (str(ROOT / "packaging" / "Resources" / "de.lproj"), "de.lproj"),
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=["mediatidy.gui"],  # imported inside a function
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="MediaTidy",
    console=False,
    argv_emulation=False,  # breaks Tk windows on macOS
    target_arch="universal2",
    icon=ICON,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="MediaTidy", upx=False)
app = BUNDLE(
    coll,
    name="MediaTidy.app",
    icon=ICON,
    bundle_identifier=BUNDLE_ID,
    version=VERSION,
    info_plist={
        "CFBundleName": "MediaTidy",
        "CFBundleDisplayName": "MediaTidy",
        "CFBundleVersion": VERSION,
        "CFBundleDevelopmentRegion": "en",
        "CFBundleLocalizations": ["en", "de"],
        "LSMinimumSystemVersion": "13.0",
        "LSApplicationCategoryType": "public.app-category.photography",
        "NSRemovableVolumesUsageDescription": REMOVABLE,
        "NSHumanReadableCopyright": "MIT License – github.com/Ben-ReverseLab/media-tidy-mac",
    },
)
