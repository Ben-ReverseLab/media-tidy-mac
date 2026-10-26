#!/bin/zsh
# Builds MediaTidy.app (in $MEDIATIDY_BUILD_DIR, default $TMPDIR/mediatidy-build) and
# dist/MediaTidy-<version>.dmg (+ .sha256) – locally and in
# .github/workflows/release.yml. Needs a universal2 Python ≥ 3.11 from python.org with the build tools:
#   python3 -m venv .venv-build
#   .venv-build/bin/pip install --require-hashes -r packaging/requirements-build.txt
#   PYTHON=.venv-build/bin/python packaging/build.sh
# MEDIATIDY_SIGN_IDENTITY = SHA-1 of the code signing certificate (packaging/signing-certificate.txt),
# MEDIATIDY_SIGN_KEYCHAIN = keychain that holds it (optional);
# without it the app is only ad-hoc signed (then macOS forgets Full Disk Access after every update).
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON=${PYTHON:-python3}
EXIFTOOL_VERSION=13.55
EXIFTOOL_SHA256=5f4c81d34ad406538c2871ad72dbfceb5d9b412b2f16cbbeb4d712d270846667
EXIFTOOL_URL="https://sourceforge.net/projects/exiftool/files/Image-ExifTool-$EXIFTOOL_VERSION.tar.gz/download"
PINNED_CERT=$(sed -n 's/^SHA-1: *//p' packaging/signing-certificate.txt 2>/dev/null || true)

VERSION=$("$PYTHON" -c 'import mediatidy; print(mediatidy.__version__)')
# Build outside the repo: in an iCloud-synced folder macOS adds attributes that break the signature
OUT=${MEDIATIDY_BUILD_DIR:-${TMPDIR:-/tmp}/mediatidy-build}
APP=$OUT/dist/MediaTidy.app
DMG=dist/MediaTidy-$VERSION.dmg
fail() { print -u2 "ERROR: $*"; exit 1; }

# 1. ExifTool, checked against the published checksum
mkdir -p "$OUT" dist
TARBALL=$OUT/Image-ExifTool-$EXIFTOOL_VERSION.tar.gz
[[ -f $TARBALL ]] || curl -fsSL --retry 3 -o "$TARBALL" "$EXIFTOOL_URL"
echo "$EXIFTOOL_SHA256  $TARBALL" | shasum -a 256 -c - >/dev/null || fail "ExifTool checksum does not match"
rm -rf "$OUT/exiftool" && mkdir -p "$OUT/exiftool"
tar -xzf "$TARBALL" -C "$OUT/exiftool" --strip-components 1
export MEDIATIDY_EXIFTOOL_DIR=$OUT/exiftool

# 2. App bundle
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 11)' || fail "Python 3.11 or later needed"
rm -rf "$OUT/dist" "$DMG" "$DMG.sha256"
"$PYTHON" -m PyInstaller --noconfirm --clean --log-level WARN \
  --distpath "$OUT/dist" --workpath "$OUT/work" packaging/MediaTidy.spec

# 3. Signature: own certificate (keeps privacy permissions across updates) or ad-hoc.
#    No hardened runtime: with a self-signed certificate it stops the bundled libraries from loading.
#    Extended attributes (quarantine of a downloaded Python, iCloud/Finder info) make codesign fail.
xattr -cr "$APP"
if [[ -n ${MEDIATIDY_SIGN_IDENTITY:-} ]]; then
  [[ -z $PINNED_CERT || $MEDIATIDY_SIGN_IDENTITY == "$PINNED_CERT" ]] \
    || fail "MEDIATIDY_SIGN_IDENTITY is not the certificate in packaging/signing-certificate.txt"
  keychain=()
  [[ -n ${MEDIATIDY_SIGN_KEYCHAIN:-} ]] && keychain=(--keychain "$MEDIATIDY_SIGN_KEYCHAIN")
  codesign --force --deep --timestamp=none --sign "$MEDIATIDY_SIGN_IDENTITY" "${keychain[@]}" "$APP"
  requirement=$(codesign -d -r- "$APP" 2>&1)
  [[ ${requirement:l} == *"certificate leaf = h\"${MEDIATIDY_SIGN_IDENTITY:l}\""* ]] \
    || fail "signature does not use the pinned certificate"
else
  print "Note: no MEDIATIDY_SIGN_IDENTITY – app is only ad-hoc signed."
  codesign --force --deep --timestamp=none --sign - "$APP"
fi
codesign --verify --deep --strict "$APP" || fail "signature check failed"

# 4. Checks: both processor types, version, bundled ExifTool, command line
ARCHS=$(lipo -archs "$APP/Contents/MacOS/MediaTidy")
[[ $ARCHS == *arm64* && $ARCHS == *x86_64* ]] || fail "not universal2 (archs: $ARCHS)"
[[ $("$APP/Contents/MacOS/MediaTidy" --version) == "$VERSION" ]] || fail "--version does not match $VERSION"
[[ $(/usr/bin/perl "$APP/Contents/Resources/exiftool/exiftool" -ver) == "$EXIFTOOL_VERSION" ]] \
  || fail "bundled ExifTool does not run"
CFG=$(mktemp -d)
if ! "$APP/Contents/MacOS/MediaTidy" --config-dir "$CFG" check; then
  [[ -n ${CI:-} ]] && fail "'MediaTidy check' reported problems"
  print "Note: 'check' reported problems on this Mac (e.g. a drive without access) – see above."
fi
rm -rf "$CFG"

# 5. Disk image with a link to Applications
rm -rf "$OUT/dmg" && mkdir -p "$OUT/dmg"
cp -R "$APP" "$OUT/dmg/"  # -R keeps the symlinks inside the bundle
ln -s /Applications "$OUT/dmg/Applications"
hdiutil create -quiet -volname "MediaTidy $VERSION" -srcfolder "$OUT/dmg" -fs HFS+ -format UDZO -ov "$DMG"
(cd dist && shasum -a 256 "${DMG:t}" > "${DMG:t}.sha256")
print "Built $APP ($ARCHS) and $DMG"
