#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
: "${Harness_PYTHON:?Set Harness_PYTHON to the deep-harness Conda Python interpreter}"
export PYTHONDONTWRITEBYTECODE=1
exec "$Harness_PYTHON" -m pytest -c pyproject.toml -p no:cacheprovider "$@"
