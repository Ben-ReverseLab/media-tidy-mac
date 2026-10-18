#!/bin/zsh
# Baut MediaTidy.app neu (danach ggf. Festplattenzugriff in den Systemeinstellungen neu setzen).
set -e
cd "$(dirname "$0")/.."
rm -rf MediaTidy.app
osacompile -s -o MediaTidy.app app/MediaTidy.applescript  # -s: bleibt geöffnet, solange MediaTidy läuft
P=MediaTidy.app/Contents/Info.plist
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier io.github.ben-reverselab.mediatidy" $P 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string io.github.ben-reverselab.mediatidy" $P
/usr/libexec/PlistBuddy -c "Add :NSRemovableVolumesUsageDescription string 'MediaTidy vergleicht und kopiert Fotos auf externen Festplatten.'" $P
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" $P  # kein zweites Dock-Symbol neben Python
codesign --force --deep -s - MediaTidy.app
echo "MediaTidy.app gebaut."
