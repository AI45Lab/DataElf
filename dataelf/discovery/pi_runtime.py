"""Project-local Pi runtime bootstrap and readiness checks.

Pi is an implementation detail of DataElf's explorer.  This module keeps the
Node/npm lifecycle in one place so callers only need the DataElf ``setup``
command and never have to know the individual Pi package commands.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dataelf.config import DataElfConfig


PI_PACKAGE_SPEC = "npm:@quarkos/pi-fusion"
PI_PACKAGE_NAME = "@quarkos/pi-fusion"
RUNTIME_MANIFEST = Path("runtime") / "pi.json"


class PiRuntimeError(RuntimeError):
    """Raised when DataElf cannot prepare its project-local Pi runtime."""


@dataclass(frozen=True)
class PiRuntimeResult:
    binary: Path
    cwd: Path
    cache_dir: Path
    package_dir: Path
    manifest_path: Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def pi_cwd(config: DataElfConfig) -> Path:
    configured = config.explorer.pi.cwd
    return (configured or project_root()).resolve()


def pi_agent_dir(config: DataElfConfig) -> Path:
    value = config.env.get("PI_CODING_AGENT_DIR") or os.environ.get("PI_CODING_AGENT_DIR")
    path = Path(value) if value else Path(".pi") / "agent"
    return (path if path.is_absolute() else pi_cwd(config) / path).resolve()


def pi_cache_dir(config: DataElfConfig) -> Path:
    value = (
        config.env.get("NPM_CONFIG_CACHE")
        or config.env.get("npm_config_cache")
        or os.environ.get("NPM_CONFIG_CACHE")
        or os.environ.get("npm_config_cache")
    )
    path = Path(value) if value else pi_cwd(config) / ".pi" / "npm-cache"
    return (path if path.is_absolute() else pi_cwd(config) / path).resolve()


def pi_package_dir(config: DataElfConfig) -> Path:
    """Return Pi's project package store used by ``pi install --local``."""
    return pi_agent_dir(config).parent / "npm"


def local_pi_binary() -> Path:
    name = "pi.cmd" if os.name == "nt" else "pi"
    return project_root() / "node_modules" / ".bin" / name


def configured_binary(config: DataElfConfig) -> Path | None:
    value = config.explorer.pi.binary
    if value:
        candidate = Path(value)
        if candidate.is_absolute() or os.sep in value or (os.altsep and os.altsep in value):
            return candidate if candidate.exists() else None
        resolved = shutil.which(value)
        return Path(resolved) if resolved else None
    if local_pi_binary().exists():
        return local_pi_binary()
    resolved = shutil.which("pi.cmd" if os.name == "nt" else "pi")
    return Path(resolved) if resolved else None


def is_managed_binary(binary: Path | None) -> bool:
    if binary is None:
        return True
    try:
        return binary.resolve() == local_pi_binary().resolve()
    except OSError:
        return False


def pi_fusion_package(config: DataElfConfig) -> Path:
    return pi_package_dir(config) / "node_modules" / PI_PACKAGE_NAME / "package.json"


def runtime_ready(config: DataElfConfig, binary: Path | None = None) -> bool:
    """Check readiness without touching the network or changing the workspace."""
    binary = binary or configured_binary(config)
    if binary is None:
        return False
    # A caller-provided binary may be a test double or an externally managed
    # Pi installation.  DataElf only asserts the Fusion package for its own
    # project-local binary.
    return not is_managed_binary(binary) or pi_fusion_package(config).is_file()


def runtime_ready_for_process(binary: str | Path | None, cwd: Path, env: dict[str, str]) -> bool:
    """Readiness check for the explorer, which already has its child env."""
    if binary is None:
        return False
    binary_path = Path(binary)
    if not is_managed_binary(binary_path):
        return True
    configured = env.get("PI_CODING_AGENT_DIR") or str(cwd / ".pi" / "agent")
    agent_dir = Path(configured)
    if not agent_dir.is_absolute():
        agent_dir = cwd / agent_dir
    return (agent_dir.resolve().parent / "npm" / "node_modules" / PI_PACKAGE_NAME / "package.json").is_file()


def setup_pi_runtime(config: DataElfConfig) -> PiRuntimeResult:
    """Install and verify the project-local Pi CLI and DataElf Pi package."""
    root = project_root()
    cwd = pi_cwd(config)
    cwd.mkdir(parents=True, exist_ok=True)
    cache_dir = pi_cache_dir(config)
    cache_dir.mkdir(parents=True, exist_ok=True)
    agent_dir = pi_agent_dir(config)
    agent_dir.mkdir(parents=True, exist_ok=True)
    package_dir = pi_package_dir(config)
    package_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(config.env)
    env.setdefault("PI_CODING_AGENT_DIR", str(agent_dir))
    env.setdefault("NPM_CONFIG_CACHE", str(cache_dir))
    env.setdefault("npm_config_cache", str(cache_dir))
    env.setdefault("PI_SKIP_VERSION_CHECK", "1")
    env.setdefault("PI_TELEMETRY", "0")

    binary = configured_binary(config)
    # With no explicit override, setup always provisions the project-local
    # CLI instead of silently adopting a user's unrelated global Pi binary.
    if not config.explorer.pi.binary and not local_pi_binary().is_file():
        binary = None
    configured_value = config.explorer.pi.binary
    configured_path = Path(configured_value) if configured_value else None
    configured_is_project_local = bool(
        configured_path
        and ("node_modules" in configured_path.parts or "node_modules" in configured_path.as_posix())
    )
    if binary is None and configured_value and configured_path and configured_path.is_absolute() and not configured_is_project_local:
        raise PiRuntimeError("The configured Pi executable was not found.")

    if binary is None:
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        node = shutil.which("node.exe" if os.name == "nt" else "node")
        if not npm or not node:
            raise PiRuntimeError("Node.js and npm are required for DataElf setup.")
        if not (root / "package-lock.json").is_file():
            raise PiRuntimeError("The project Pi dependency lockfile is missing.")
        try:
            subprocess.run(
                [npm, "ci", "--no-audit", "--no-fund"],
                cwd=root,
                env=env,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=900,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise PiRuntimeError("DataElf could not prepare its local runtime dependencies.") from exc
        binary = configured_binary(config) or local_pi_binary()

    if not binary.is_file():
        raise PiRuntimeError("DataElf could not find the project-local Pi executable after setup.")

    package_json = pi_fusion_package(config)
    if not package_json.is_file():
        try:
            subprocess.run(
                [str(binary), "install", PI_PACKAGE_SPEC, "--local", "--approve"],
                cwd=cwd,
                env=env,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=900,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise PiRuntimeError("DataElf could not prepare the Pi analysis package.") from exc
    if not package_json.is_file():
        raise PiRuntimeError("DataElf setup finished without the Pi analysis package.")

    config.ensure_dirs()
    manifest_path = config.runtime.workspace_dir / RUNTIME_MANIFEST
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    package_version = _package_version(package_json)
    manifest_path.write_text(
        json.dumps(
            {
                "managed_by": "dataelf",
                "binary": str(binary.resolve()),
                "cwd": str(cwd),
                "npm_cache": str(cache_dir),
                "pi_package": PI_PACKAGE_NAME,
                "pi_package_version": package_version,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return PiRuntimeResult(binary=binary.resolve(), cwd=cwd, cache_dir=cache_dir, package_dir=package_dir, manifest_path=manifest_path.resolve())


def _package_version(path: Path) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("version")
    except (OSError, json.JSONDecodeError):
        return None
    return str(value) if value is not None else None


__all__ = [
    "PI_PACKAGE_NAME",
    "PI_PACKAGE_SPEC",
    "PiRuntimeError",
    "PiRuntimeResult",
    "configured_binary",
    "is_managed_binary",
    "pi_fusion_package",
    "runtime_ready",
    "runtime_ready_for_process",
    "setup_pi_runtime",
]
