#!/usr/bin/env sh
set -eu
export PYTHONUTF8=1
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
python_bin=${LABDEFENDER_PYTHON:-python3}
"$python_bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11+ required")'
if [ ! -f .venv/bin/python ]; then
    "$python_bin" -m venv .venv
fi
if [ "${1:-}" = "--dev" ]; then
    .venv/bin/python -m pip install -e '.[dev]'
else
    .venv/bin/python -m pip install -e .
fi
if [ ! -e .env ]; then
    cp .env.example .env
fi
printf '%s\n' 'Готово. Заполните BOT_TOKEN в .env, затем выполните sh scripts/run.sh.'
