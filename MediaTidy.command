#!/bin/zsh
# Double-click starts MediaTidy (then Terminal needs the permissions – better use MediaTidy.app).
exec "$(dirname "$0")/app/start.sh" "$@"
