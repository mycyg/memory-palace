#!/bin/sh
set -eu
plugin_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python_exec=${EVENTMEM_PYTHON:-"$plugin_dir/.venv/bin/python"}
if [ ! -x "$python_exec" ]; then python_exec=python3; fi
exec "$python_exec" -m eventmem.hooks.bridge "$@"
