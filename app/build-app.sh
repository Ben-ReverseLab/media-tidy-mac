#!/bin/zsh
# Baut FotoTool.app neu (danach ggf. Festplattenzugriff in den Systemeinstellungen neu setzen).
set -e
cd "$(dirname "$0")/.."
rm -rf FotoTool.app
osacompile -o FotoTool.app app/FotoTool.applescript
P=FotoTool.app/Contents/Info.plist
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier io.github.ben-reverselab.fototool" $P 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string io.github.ben-reverselab.fototool" $P
/usr/libexec/PlistBuddy -c "Add :NSRemovableVolumesUsageDescription string 'FotoTool vergleicht und kopiert Fotos auf externen Festplatten.'" $P
codesign --force --deep -s - FotoTool.app
echo "FotoTool.app gebaut."
