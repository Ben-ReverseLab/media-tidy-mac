#!/bin/zsh
# Rebuilds MediaTidy.app (afterwards you may have to grant disk access again in System Settings).
set -e
cd "$(dirname "$0")/.."
rm -rf MediaTidy.app
osacompile -s -o MediaTidy.app app/MediaTidy.applescript  # -s: stays open while MediaTidy runs
P=MediaTidy.app/Contents/Info.plist
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier io.github.ben-reverselab.mediatidy" $P 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string io.github.ben-reverselab.mediatidy" $P
/usr/libexec/PlistBuddy -c "Add :NSRemovableVolumesUsageDescription string 'MediaTidy compares and copies photos on external drives.'" $P
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" $P  # no second Dock icon next to Python
# Permission prompt in German on German systems
R=MediaTidy.app/Contents/Resources
mkdir -p $R/en.lproj $R/de.lproj
print '"NSRemovableVolumesUsageDescription" = "MediaTidy compares and copies photos on external drives.";' > $R/en.lproj/InfoPlist.strings
print '"NSRemovableVolumesUsageDescription" = "MediaTidy vergleicht und kopiert Fotos auf externen Festplatten.";' > $R/de.lproj/InfoPlist.strings
cp packaging/MediaTidy.icns $R/applet.icns  # same icon as the downloadable app
codesign --force --deep -s - MediaTidy.app
echo "MediaTidy.app built."
