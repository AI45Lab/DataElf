#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"
# The deployment launcher may reset PATH and omit the installed Conda base.
# Only use existing interpreters; never install or synchronize dependencies here.
python_command="${DATAELF_PYTHON:-}"
if [[ -z "$python_command" ]]; then
  candidates=()
  conda_command="${CONDA_EXE:-$(command -v conda || true)}"
  if [[ -n "$conda_command" ]]; then
    conda_base="$("$conda_command" info --base 2>/dev/null || true)"
    if [[ -n "$conda_base" ]]; then
      candidates+=("$conda_base/bin/python")
    fi
  fi
  candidates+=(/opt/conda/bin/python "$HOME/miniconda3/bin/python" "$HOME/anaconda3/bin/python" /usr/local/miniconda3/bin/python /usr/local/anaconda3/bin/python)
  for name in python python3; do
    candidate="$(command -v "$name" || true)"
    if [[ -n "$candidate" ]]; then candidates+=("$candidate"); fi
  done
  for candidate in "${candidates[@]}"; do
    if [[ -x "$candidate" ]] && "$candidate" -c 'import sys; assert sys.version_info >= (3, 11); import fastapi, uvicorn' >/dev/null 2>&1; then
      python_command="$candidate"
      break
    fi
  done
  if [[ -z "$python_command" ]]; then
    printf 'No existing Python with FastAPI and Uvicorn found. Set DATAELF_PYTHON to your installed base Python. Checked:\n' >&2
    printf '  %s\n' "${candidates[@]}" >&2
    exit 1
  fi
fi
printf 'Starting DataElf with Python: %s\n' "$python_command" >&2
exec "$python_command" -m dataelf_server --check-environment --config "$repo_root/dataelf.local.yaml" "$@"
