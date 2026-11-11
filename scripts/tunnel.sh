#!/usr/bin/env sh
# Keeps a temporary Mini App tunnel in this window. Start the bot with
# `sh scripts/run.sh --tunnel` in another window: it takes this address.
set -eu
export PYTHONUTF8=1
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
if [ ! -f .venv/bin/python ]; then
    printf '%s\n' 'Сначала выполните sh scripts/setup.sh.' >&2
    exit 1
fi
exec .venv/bin/python -m labdefender.tools tunnel
