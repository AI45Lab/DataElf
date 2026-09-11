#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"
python_command="${DATAELF_INSTALL_PYTHON:-python3.11}"
"$python_command" -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
npm ci
