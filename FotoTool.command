#!/bin/zsh
# Doppelklick startet FotoTool.
cd "$(dirname "$0")"
PY=/opt/homebrew/bin/python3
[[ -x $PY ]] || PY=$(command -v python3)
exec "$PY" -m fototool "$@"
