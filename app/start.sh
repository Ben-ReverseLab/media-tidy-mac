#!/bin/zsh
# Startet FotoTool mit dem ersten Python (>= 3.11, mit tkinter), das gefunden wird.
# Wird von FotoTool.app und FotoTool.command benutzt – Änderungen hier brauchen keinen App-Neubau.
# Eigenes Python erzwingen: FOTOTOOL_PYTHON=/pfad/zu/python3
cd "$(dirname "$0")/.."
candidates=(
  $FOTOTOOL_PYTHON
  /opt/homebrew/bin/python3 /opt/homebrew/bin/python3.1[4-9](N) /opt/homebrew/bin/python3.1[1-3](N)
  /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3
  $(whence -p python3)
)
for py in $candidates; do
  [[ -x $py ]] || continue
  if "$py" -c 'import sys, tkinter; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    exec "$py" -m fototool "$@"
  fi
done
echo "Kein passendes Python gefunden (nötig: Python ab 3.11 mit tkinter)." >&2
echo "Installieren: brew install python-tk@3.14" >&2
exit 1
