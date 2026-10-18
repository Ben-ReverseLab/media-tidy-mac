#!/bin/zsh
# Doppelklick startet MediaTidy (Rechte braucht dann das Terminal – besser MediaTidy.app nutzen).
exec "$(dirname "$0")/app/start.sh" "$@"
