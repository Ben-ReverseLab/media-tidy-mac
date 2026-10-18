#!/bin/zsh
# Starts MediaTidy with the first Python (>= 3.11, with tkinter) that is found.
# Used by MediaTidy.app and MediaTidy.command – changes here don't need an app rebuild.
# Force a specific Python: MEDIATIDY_PYTHON=/path/to/python3
cd "$(dirname "$0")/.."
candidates=(
  $MEDIATIDY_PYTHON
  /opt/homebrew/bin/python3 /opt/homebrew/bin/python3.1[4-9](N) /opt/homebrew/bin/python3.1[1-3](N)
  /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3
  $(whence -p python3)
)
for py in $candidates; do
  [[ -x $py ]] || continue
  if "$py" -c 'import sys, tkinter; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    exec "$py" -m mediatidy "$@"
  fi
done
if defaults read -g AppleLanguages 2>/dev/null | sed -n 2p | grep -q '"de'; then
  echo "Kein passendes Python gefunden (nötig: Python ab 3.11 mit tkinter)." >&2
  echo "Installieren: brew install python-tk@3.14" >&2
else
  echo "No suitable Python found (needed: Python 3.11 or later with tkinter)." >&2
  echo "Install: brew install python-tk@3.14" >&2
fi
exit 1
