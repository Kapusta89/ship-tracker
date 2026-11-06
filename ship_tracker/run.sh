#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ] || ! ./.venv/bin/python -c "import sys" >/dev/null 2>&1; then
    echo "Первый запуск на этом компьютере: собираю .venv и ставлю зависимости..."
    rm -rf .venv
    python3 -m venv .venv
    ./.venv/bin/python -m pip install --upgrade pip
    ./.venv/bin/python -m pip install -r requirements.txt
fi

exec ./.venv/bin/python ship_tracker.py "$@"
