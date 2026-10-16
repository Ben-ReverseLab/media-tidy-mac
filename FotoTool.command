#!/bin/zsh
# Doppelklick startet FotoTool (Rechte braucht dann das Terminal – besser FotoTool.app nutzen).
exec "$(dirname "$0")/app/start.sh" "$@"
