#!/usr/bin/env sh
set -eu
export PYTHONUTF8=1
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
if [ ! -f .venv/bin/python ]; then
    printf '%s\n' 'Сначала выполните sh scripts/setup.sh.' >&2
    exit 1
fi
.venv/bin/python -m labdefender.tools doctor
exec .venv/bin/python -m labdefender "$@"
