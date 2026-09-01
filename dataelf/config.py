from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


DEFAULT_CONFIG_FILE = Path("dataelf.local.yaml")
CONFIG_FILE_CANDIDATES = (
    Path("dataelf.local.yaml"), Path("dataelf.local.yml"), Path("dataelf.yaml"), Path("dataelf.yml"),
    Path(".dataelf/config.yaml"), Path(".dataelf/config.yml"), Path(".dataelf/config.json"),
)


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_dir: Path = Field(default_factory=lambda: Path(".dataelf"))
    sqlite_path: Path = Field(default_factory=lambda: Path(".dataelf/dataelf.sqlite"))
    workspaces_dir: Path = Field(default_factory=lambda: Path(".dataelf/workspaces"))
    enable_sqlite: bool = False


class PiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binary: str | None = None
    model: str | None = None
    mode: str = "json"
    cwd: Path | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)
    extra_args: str = ""
    log_mode: Literal["quiet", "summary", "raw"] = "summary"


class ExplorerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["pi"] = "pi"
    pi: PiConfig = Field(default_factory=PiConfig)


class DataElfConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    explorer: ExplorerConfig = Field(default_factory=ExplorerConfig)
    domains: dict[str, dict[str, Any]] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "DataElfConfig":
        values = load_config_file()
        _reject_unknown(values, {"runtime", "explorer", "domains", "env"}, "root")
        runtime_values = _section(values, "runtime")
        explorer_values = _section(values, "explorer")
        pi_values = _section(explorer_values, "pi")
        domains_values = _section(values, "domains")
        _reject_unknown(runtime_values, {"workspace_dir", "sqlite_path", "workspaces_dir", "enable_sqlite"}, "runtime")
        _reject_unknown(explorer_values, {"type", "pi"}, "explorer")
        _reject_unknown(
            pi_values,
            {"binary", "model", "mode", "cwd", "timeout_seconds", "extra_args", "log_mode"},
            "explorer.pi",
        )

        workspace = Path(_env("DATAELF_WORKSPACE", runtime_values.get("workspace_dir", ".dataelf")))
        runtime = RuntimeConfig(
            workspace_dir=workspace,
            sqlite_path=Path(_env("DATAELF_SQLITE_PATH", runtime_values.get("sqlite_path", workspace / "dataelf.sqlite"))),
            workspaces_dir=Path(_env("DATAELF_WORKSPACES_DIR", runtime_values.get("workspaces_dir", workspace / "workspaces"))),
            enable_sqlite=_bool(_env("DATAELF_ENABLE_SQLITE", runtime_values.get("enable_sqlite", False))),
        )
        pi = PiConfig(
            binary=_optional(_env("DATAELF_PI_BINARY", pi_values.get("binary"))),
            model=_optional(_env("DATAELF_PI_MODEL", pi_values.get("model"))),
            mode=str(_env("DATAELF_PI_MODE", pi_values.get("mode", "json"))),
            cwd=_optional_path(_env("DATAELF_PI_CWD", pi_values.get("cwd"))),
            timeout_seconds=_optional_int(_env("DATAELF_PI_TIMEOUT_SECONDS", pi_values.get("timeout_seconds"))),
            extra_args=str(_env("DATAELF_PI_EXTRA_ARGS", pi_values.get("extra_args", ""))),
            log_mode=str(_env("DATAELF_PI_LOG_MODE", pi_values.get("log_mode", "summary"))),
        )
        domains: dict[str, dict[str, Any]] = {}
        for key, value in domains_values.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise ValueError(f"DataElf config section domains.{key} must be a mapping/object")
            domains[key] = dict(value)
        runtime_env = {str(key): str(value) for key, value in _section(values, "env").items() if value is not None}
        for key in _CHILD_ENV_KEYS:
            if os.getenv(key) is not None:
                runtime_env[key] = str(os.environ[key])
        return cls(
            runtime=runtime,
            explorer=ExplorerConfig(type=str(explorer_values.get("type", "pi")), pi=pi),
            domains=domains,
            env=runtime_env,
        )

    def ensure_dirs(self) -> None:
        self.runtime.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.runtime.workspaces_dir.mkdir(parents=True, exist_ok=True)

    def domain_config(self, domain: str) -> dict[str, Any]:
        return dict(self.domains.get(domain, {}))


def load_config_file(path: Path | None = None) -> dict[str, Any]:
    config_path = path or _resolve_config_file()
    if config_path is None:
        return {}
    if not config_path.exists():
        if os.getenv("DATAELF_CONFIG_FILE"):
            raise FileNotFoundError(f"DATAELF_CONFIG_FILE does not exist: {config_path}")
        return {}
    data = json.loads(config_path.read_text(encoding="utf-8")) if config_path.suffix.lower() == ".json" else yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"DataElf config file must contain a mapping/object: {config_path}")
    return data


def write_config_template(path: Path = DEFAULT_CONFIG_FILE, config: DataElfConfig | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    cfg = config or DataElfConfig()
    payload = cfg.model_dump(mode="json")
    path.write_text(
        "# DataElf local config. Environment variables override matching values.\n"
        + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def _resolve_config_file() -> Path | None:
    explicit = os.getenv("DATAELF_CONFIG_FILE")
    if explicit:
        return Path(explicit)
    return next((candidate for candidate in CONFIG_FILE_CANDIDATES if candidate.exists()), None)


def _section(values: dict[str, Any], key: str) -> dict[str, Any]:
    value = values.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"DataElf config section {key!r} must be a mapping/object")
    return value


def _reject_unknown(values: dict[str, Any], allowed: set[str], section: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ValueError(f"Unknown DataElf config keys in {section}: {', '.join(unknown)}")


def _env(name: str, default: Any) -> Any:
    return os.getenv(name, default)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Expected a boolean value, got {value!r}")


def _optional(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _optional_path(value: Any) -> Path | None:
    return None if value in (None, "") else Path(value)


def _optional_int(value: Any) -> int | None:
    return None if value in (None, "") else int(value)


_CHILD_ENV_KEYS = {
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
    "GEMINI_API_KEY", "BRAVE_API_KEY", "PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR", "PI_PACKAGE_DIR",
}


__all__ = ["CONFIG_FILE_CANDIDATES", "DEFAULT_CONFIG_FILE", "DataElfConfig", "ExplorerConfig", "PiConfig", "RuntimeConfig", "load_config_file", "write_config_template"]
