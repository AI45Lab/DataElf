"""Local startup checks. Never installs packages or calls model endpoints."""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile


def report(message: str) -> None:
    print(f"[environment] {message}", flush=True)


def check_python() -> None:
    report(f"Python: {sys.executable} ({sys.version.split()[0]})")
    report(f"Environment prefix: {sys.prefix}; cwd: {Path.cwd()}")
    if sys.version_info < (3, 11):
        raise RuntimeError("Python >=3.11 is required")
    for name in ("fastapi", "uvicorn", "pydantic", "yaml", "rdflib", "langgraph", "typer", "rich"):
        try:
            importlib.import_module(name)
        except ImportError as exc:
            raise RuntimeError(f"Cannot import {name} with {sys.executable}; select the prepared environment") from exc
        report(f"Import {name}: OK")


def check_storage(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    # Exercise journaling and commit without touching the service database.
    with tempfile.TemporaryDirectory(prefix=".startup-check-", dir=directory) as temporary:
        connection = sqlite3.connect(Path(temporary) / "probe.sqlite")
        try:
            connection.execute("CREATE TABLE probe(value TEXT)")
            connection.execute("INSERT INTO probe VALUES ('ok')")
            connection.commit()
            assert connection.execute("SELECT value FROM probe").fetchone() == ("ok",)
        finally:
            connection.close()
    report(f"State directory SQLite write/commit/read: OK ({directory})")
    report(f"Free space: {shutil.disk_usage(directory).free / 1024**3:.2f} GiB")


def check_runtime(settings) -> None:
    report(f"Config: {Path(os.environ['DATAELF_CONFIG_FILE']).resolve()}")
    report(f"Listen: {settings.server.host}:{settings.server.port}")
    report(f"State directory: {settings.state_dir}")
    settings.validate_runtime()
    # Match the configured subprocess environment, without printing credentials.
    env = {**os.environ, **settings.core.env}
    node = shutil.which("node", path=env.get("PATH"))
    if not node:
        raise RuntimeError("Node.js is absent from the Agent PATH")
    version = subprocess.run([node, "--version"], env=env, capture_output=True,
                             text=True, timeout=10, check=True).stdout.strip()
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (22, 19, 0):
        raise RuntimeError(f"Node.js >=22.19.0 is required; found {version} at {node}")
    report(f"Node: {node} ({version})")
    from dataelf.discovery.pi_cli_explorer import PiCliInsightsExplorer
    runner = PiCliInsightsExplorer(pi_binary=settings.core.explorer.pi.binary)
    binary = runner._resolve_binary()
    if not binary:
        raise RuntimeError("Configured Pi executable cannot be resolved from the service directory")
    result = subprocess.run([binary, "--version"], cwd=settings.project_root, env=env,
                            capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError(f"Pi --version failed (exit {result.returncode}); check Node and npm dependencies")
    # Do not forward arbitrary child output into deployment logs.
    version = re.search(r"\b\d+\.\d+\.\d+\b", result.stdout)
    report(f"Pi: {binary} (version {version.group() if version else 'command OK'})")
    report(f"Intent API key configured: {'yes' if settings.intent_config.api_key else 'no'}")
    check_storage(settings.state_dir)
    report("Checks passed. Model/API connectivity and full analysis are not tested.")
