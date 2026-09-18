#!/usr/bin/env bash
set -euo pipefail
# For managed deployment prefer systemctl stop dataelf-server.
# This helper accepts the exact foreground/nohup process ID from the operator.
pid="${1:?Usage: stop.sh SERVER_PID}"
[[ "$pid" =~ ^[1-9][0-9]*$ ]] || { echo 'Invalid PID' >&2; exit 2; }
[[ -r "/proc/$pid/cmdline" ]] || { echo 'Process is not running' >&2; exit 1; }
command_line="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
case "$command_line" in
  *'-m dataelf_server '*|*'-m dataelf_server'|*'/dataelf-serve '*) kill -TERM "$pid" ;;
  *) echo 'PID is not a DataElf Server entrypoint' >&2; exit 2 ;;
esac
