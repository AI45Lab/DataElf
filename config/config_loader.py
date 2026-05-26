import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

def _expand_env_placeholders(
    config_data: dict[str, Any],
) -> dict[str, Any]:
    expanded = dict(config_data)
    for key, value in expanded.items():
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            expanded[key] = os.environ.get(value[2:-1])
    return expanded


@dataclass
class DatabaseConfig:
    type: str = "mock"
    path: str = ":memory:"
    table_name: str = "data_pilot_test"
    storage_options: dict[str, Any] = field(default_factory=dict)
    use_memory_queue: bool = False
    flush_every: int = 1000
    connection_params: dict[str, Any] = field(default_factory=dict)

    def to_connection_params(self) -> dict[str, Any]:
        params = {
            "storage_options": self.storage_options,
            "use_memory_queue": self.use_memory_queue,
            "flush_every": self.flush_every,
        }
        # Include any extra connection params
        params.update(self.connection_params)
        return params


@dataclass
class LLMConfig:
    #LLM configuration for both agent and tools.
    model: str = "gpt-4"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    max_retries: int = 3
    retry_delay: float = 1.0


@dataclass
class ExecutionConfig:
    enable_log: bool = True
    log_level: str = "INFO"
    max_retries: int = 3


@dataclass
class RuntimeConfig:
    save_result: bool = True
    timeout_seconds: int = 300


@dataclass
class DeploymentConfig:
    network_mode: str = "online"
    resource_tier: str = "light"


@dataclass
class AgentConfig:
    type: str = "opencode"
    model: str = "gpt-4"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    max_retries: int = 3
    retry_delay: float = 1.0
    include_skill_docs: bool = False
    skill_docs_max_length: int = 2000


@dataclass
class ToolLLMConfig:
    model: str = ""
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    max_retries: int = 3
    retry_delay: float = 1.0

    def is_configured(self) -> bool:
        return bool(self.model and self.api_key)


@dataclass
class LLMTracingConfig:
    enabled: bool = False
    output_dir: str = ".elf/llm_traces"


@dataclass
class Config:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    deployment: DeploymentConfig = field(default_factory=DeploymentConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    tool_llm: ToolLLMConfig = field(default_factory=ToolLLMConfig)
    llm_tracing: LLMTracingConfig = field(default_factory=LLMTracingConfig)
    tool_defaults: dict[str, Any] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)
    skill_paths: list[str] = field(default_factory=list)
    required_scoring_tool: bool = False
    required_select_tool: bool = False
    required_security_audit_tool: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        db_data = data.get("database", {})
        exec_data = data.get("execution", {})
        runtime_data = data.get("runtime", {})
        deployment_data = data.get("deployment", {})
        agent_data = _expand_env_placeholders(data.get("agent", {}))
        tool_llm_data = _expand_env_placeholders(data.get("tool_llm", {}))
        llm_tracing_data = data.get("llm_tracing", {})
        tool_defaults_data = data.get("tool_defaults", {})

        return cls(
            database=DatabaseConfig(**db_data),
            execution=ExecutionConfig(**exec_data),
            runtime=RuntimeConfig(**runtime_data),
            deployment=DeploymentConfig(**deployment_data),
            agent=AgentConfig(**agent_data),
            tool_llm=ToolLLMConfig(**tool_llm_data),
            llm_tracing=LLMTracingConfig(**llm_tracing_data),
            tool_defaults=tool_defaults_data if isinstance(tool_defaults_data, dict) else {},
            skills=data.get("skills", []),
            skill_paths=data.get("skill_paths", []),
            required_scoring_tool=bool(data.get("required_scoring_tool", False)),
            required_select_tool=bool(data.get("required_select_tool", False)),
            required_security_audit_tool=bool(data.get("required_security_audit_tool", False)),
        )

# Load configuration from YAML file accordig to prefix.
def load_config(config_path: Optional[str | Path] = None, prefix: Optional[str] = None) -> Config:
    if config_path is None:
        # Determine config file name based on prefix
        if prefix:
            if any(sep in prefix for sep in ("/", "\\")) or prefix.endswith((".yaml", ".yml")):
                raise ValueError(
                    f"Invalid config prefix: {prefix!r}. "
                    "Use `-c/--config` for a config file path, or pass a bare prefix such as "
                    "`test` to resolve `test-config.yaml`."
                )
            config_file_name = f"{prefix}-config.yaml"
        else:
            config_file_name = "config.yaml"

        # Look for config file in current directory or parent directories
        current_path = Path.cwd()
        for path in [current_path, *current_path.parents]:
            config_file = path / config_file_name
            if config_file.exists():
                config_path = config_file
                break

        if config_path is None:
            config_path = Path(f"./{config_file_name}")

    config_path = Path(config_path)

    if not config_path.exists():
        # Return default config if file doesn't exist
        return Config()

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    config = Config.from_dict(data)
    _load_tool_defaults_if_required(config)
    return config


# Tool-specific default config files, keyed by (flag_name, tool_defaults_key).
_TOOL_DEFAULT_FILES = {
    "required_scoring_tool": ("data_scoring", "scoring/defaults.yaml"),
    "required_select_tool": ("data_select", "select/defaults.yaml"),
    "required_security_audit_tool": ("security_audit", "security_audit/default.yaml"),
}


def _load_tool_defaults_if_required(config: Config) -> None:
    """Load tool default configs from separate YAML files when boolean flags are set."""
    tools_dir = Path(__file__).parent.parent / "tools"

    for flag_attr, (defaults_key, filename) in _TOOL_DEFAULT_FILES.items():
        if not getattr(config, flag_attr, False):
            continue

        defaults_path = tools_dir / filename
        if not defaults_path.exists():
            continue

        with open(defaults_path, "r", encoding="utf-8") as f:
            file_defaults = yaml.safe_load(f) or {}

        if not isinstance(file_defaults, dict):
            continue

        # Merge: file defaults as base, user config overrides on top
        user_overrides = config.tool_defaults.get(defaults_key, {})
        if isinstance(user_overrides, dict) and user_overrides:
            merged = {**file_defaults, **user_overrides}
        else:
            merged = file_defaults

        config.tool_defaults[defaults_key] = merged
