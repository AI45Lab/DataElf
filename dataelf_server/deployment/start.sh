#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"
exec "${DATAELF_PYTHON:-$repo_root/.venv/bin/python}" -m dataelf_server "$@"
