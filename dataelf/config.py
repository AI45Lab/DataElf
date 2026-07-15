from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

DEFAULT_AI_INDEX_BASE_URL = "https://index.shlab.org.cn/api/v2"
DEFAULT_AI_INDEX_API_KEY = "ak_0XWHy2OQpSKnaKHL"
DEFAULT_AI_INDEX_MODE = "api"
DEFAULT_ENABLE_SQLITE = False
DEFAULT_INSIGHTS_EXPLORER = "deepagentscode"
DEFAULT_CONFIG_FILE = Path("dataelf.local.yaml")
CONFIG_FILE_CANDIDATES = (
    Path("dataelf.local.yaml"),
    Path("dataelf.local.yml"),
    Path("dataelf.yaml"),
    Path("dataelf.yml"),
    Path(".dataelf/config.yaml"),
    Path(".dataelf/config.yml"),
    Path(".dataelf/config.json"),
)


class DataElfConfig(BaseModel):
    workspace_dir: Path = Field(default_factory=lambda: Path(".dataelf"))
    sqlite_path: Path = Field(default_factory=lambda: Path(".dataelf/dataelf.sqlite"))
    raw_dir: Path = Field(default_factory=lambda: Path(".dataelf/raw"))
    workspaces_dir: Path = Field(default_factory=lambda: Path(".dataelf/workspaces"))
    fixtures_dir: Path = Field(default_factory=lambda: Path("fixtures/ai_index"))
    model: str | None = None
    ai_index_mode: str = DEFAULT_AI_INDEX_MODE
    ai_index_base_url: str = DEFAULT_AI_INDEX_BASE_URL
    ai_index_api_key: str = DEFAULT_AI_INDEX_API_KEY
    enable_sqlite: bool = DEFAULT_ENABLE_SQLITE
    insights_explorer: str = DEFAULT_INSIGHTS_EXPLORER
    dcode_binary: str | None = None
    dcode_shell_allow_list: str | None = None
    dcode_extra_args: str = ""
    dcode_stream_logs: bool | None = None
    pi_binary: str | None = None
    pi_model: str | None = None
    pi_mode: str = "json"
    pi_cwd: Path | None = None
    pi_timeout_seconds: int | None = None
    pi_extra_args: str = ""
    pi_stream_logs: bool | None = None
    pi_log_mode: str | None = None
    runtime_env: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "DataElfConfig":
        file_values = load_config_file()
        workspace = Path(_env_or_config("DATAELF_WORKSPACE", file_values, "workspace_dir", ".dataelf"))
        return cls(
            workspace_dir=workspace,
            sqlite_path=Path(_env_or_config("DATAELF_SQLITE_PATH", file_values, "sqlite_path", workspace / "dataelf.sqlite")),
            raw_dir=Path(_env_or_config("DATAELF_RAW_DIR", file_values, "raw_dir", workspace / "raw")),
            workspaces_dir=Path(_env_or_config("DATAELF_WORKSPACES_DIR", file_values, "workspaces_dir", workspace / "workspaces")),
            fixtures_dir=Path(_env_or_config("DATAELF_FIXTURES_DIR", file_values, "fixtures_dir", "fixtures/ai_index")),
            model=_blank_to_none(_env_or_config("DATAELF_MODEL", file_values, "model", None)),
            ai_index_mode=_env_or_config("DATAELF_AI_INDEX_MODE", file_values, "ai_index_mode", DEFAULT_AI_INDEX_MODE),
            ai_index_base_url=_env_or_config("AI_INDEX_BASE_URL", file_values, "ai_index_base_url", DEFAULT_AI_INDEX_BASE_URL),
            ai_index_api_key=_env_or_config("AI_INDEX_API_KEY", file_values, "ai_index_api_key", DEFAULT_AI_INDEX_API_KEY),
            enable_sqlite=_env_bool("DATAELF_ENABLE_SQLITE", _config_bool(file_values, "enable_sqlite", DEFAULT_ENABLE_SQLITE)),
            insights_explorer=_env_or_config("DATAELF_INSIGHTS_EXPLORER", file_values, "insights_explorer", DEFAULT_INSIGHTS_EXPLORER),
            dcode_binary=_blank_to_none(_env_or_config("DATAELF_DCODE_BINARY", file_values, "dcode_binary", None)),
            dcode_shell_allow_list=_blank_to_none(_env_or_config("DATAELF_DCODE_SHELL_ALLOW_LIST", file_values, "dcode_shell_allow_list", None)),
            dcode_extra_args=_env_or_config("DATAELF_DCODE_EXTRA_ARGS", file_values, "dcode_extra_args", ""),
            dcode_stream_logs=_env_optional_bool("DATAELF_DCODE_STREAM_LOGS", _config_optional_bool(file_values, "dcode_stream_logs")),
            pi_binary=_blank_to_none(_env_or_config("DATAELF_PI_BINARY", file_values, "pi_binary", None)),
            pi_model=_blank_to_none(_env_or_config("DATAELF_PI_MODEL", file_values, "pi_model", None)),
            pi_mode=_env_or_config("DATAELF_PI_MODE", file_values, "pi_mode", "json"),
            pi_cwd=_optional_path(_env_or_config("DATAELF_PI_CWD", file_values, "pi_cwd", None)),
            pi_timeout_seconds=_env_int("DATAELF_PI_TIMEOUT_SECONDS", _config_int(file_values, "pi_timeout_seconds", None)),
            pi_extra_args=_env_or_config("DATAELF_PI_EXTRA_ARGS", file_values, "pi_extra_args", ""),
            pi_stream_logs=_env_optional_bool("DATAELF_PI_STREAM_LOGS", _config_optional_bool(file_values, "pi_stream_logs")),
            pi_log_mode=_blank_to_none(_env_or_config("DATAELF_PI_LOG_MODE", file_values, "pi_log_mode", None)),
            runtime_env=_runtime_env(file_values),
        )

    def ensure_dirs(self) -> None:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)


def load_config_file(path: Path | None = None) -> dict[str, Any]:
    config_path = path or _resolve_config_file()
    if config_path is None:
        return {}
    if not config_path.exists():
        if os.getenv("DATAELF_CONFIG_FILE"):
            raise FileNotFoundError(f"DATAELF_CONFIG_FILE does not exist: {config_path}")
        return {}
    if config_path.suffix.lower() == ".json":
        data = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
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
    path.write_text(
        "\n".join(
            [
                "# DataElf local config. Environment variables with matching names override these values.",
                f"workspace_dir: {cfg.workspace_dir}",
                f"fixtures_dir: {cfg.fixtures_dir}",
                f"model: {cfg.model or ''}",
                f"ai_index_mode: {cfg.ai_index_mode}",
                f"ai_index_base_url: {cfg.ai_index_base_url}",
                f"ai_index_api_key: {cfg.ai_index_api_key}",
                f"enable_sqlite: {str(cfg.enable_sqlite).lower()}",
                f"insights_explorer: {cfg.insights_explorer}",
                f"dcode_binary: {cfg.dcode_binary or 'dcode'}",
                f"dcode_shell_allow_list: {cfg.dcode_shell_allow_list or 'all'}",
                f"dcode_extra_args: {cfg.dcode_extra_args!r}",
                f"dcode_stream_logs: {'' if cfg.dcode_stream_logs is None else str(cfg.dcode_stream_logs).lower()}",
                f"pi_binary: {cfg.pi_binary or './node_modules/.bin/pi'}",
                f"pi_model: {cfg.pi_model or ''}",
                f"pi_mode: {cfg.pi_mode}",
                f"pi_cwd: {cfg.pi_cwd or '.'}",
                f"pi_timeout_seconds: {cfg.pi_timeout_seconds or ''}",
                "# Put official Pi CLI resource flags here, for example: --skill /path/to/brave-search",
                f"pi_extra_args: {cfg.pi_extra_args!r}",
                f"pi_log_mode: {cfg.pi_log_mode or 'summary'}",
                f"pi_stream_logs: {'' if cfg.pi_stream_logs is None else str(cfg.pi_stream_logs).lower()}",
                "# Optional child-process environment. Exported shell variables with the same name win.",
                "env: {}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _resolve_config_file() -> Path | None:
    explicit = os.getenv("DATAELF_CONFIG_FILE")
    if explicit:
        return Path(explicit)
    for candidate in CONFIG_FILE_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def _env_or_config(env_name: str, config: dict[str, Any], key: str, default: Any) -> Any:
    value = os.getenv(env_name)
    if value is not None:
        return value
    return config.get(key, default)


def _config_bool(config: dict[str, Any], key: str, default: bool) -> bool:
    value = config.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _config_optional_bool(config: dict[str, Any], key: str) -> bool | None:
    value = config.get(key)
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _config_int(config: dict[str, Any], key: str, default: int | None) -> int | None:
    value = config.get(key)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_optional_bool(name: str, default: bool | None = None) -> bool | None:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _blank_to_none(value: Any) -> Any:
    if value == "":
        return None
    return value


def _optional_path(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    return Path(value)


def _runtime_env(config: dict[str, Any]) -> dict[str, str]:
    raw_env = config.get("env", {})
    if raw_env is None:
        return {}
    if not isinstance(raw_env, dict):
        raise ValueError("DataElf config key 'env' must be a mapping/object.")
    env: dict[str, str] = {}
    for key, value in raw_env.items():
        key_str = str(key)
        if os.getenv(key_str) is not None:
            env[key_str] = os.environ[key_str]
        elif value is not None:
            env[key_str] = str(value)
    return env
