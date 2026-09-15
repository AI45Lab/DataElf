from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ConfigurationError(ValueError):
    """Messages contain field names and fixed reasons only."""


class ModelingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    strategy: str | None = None
    ontology_template: str | None = None


class TrajectoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["tool", "fixture"] = "tool"
    tool_python: str = sys.executable
    skill_path: str | None = str(Path(__file__).resolve().parent / "pi/skills/wt-serving-query/SKILL.md")
    profile: Literal["test"] = "test"
    modeling: ModelingConfig = Field(default_factory=ModelingConfig)

    @classmethod
    def from_mapping(cls, values: dict) -> "TrajectoryConfig":
        values = dict(values)
        if values.get("mode", "tool") == "tool":
            for name, field in {"DATAELF_TRAJECTORY_TOOL_PYTHON": "tool_python", "WT_SDK_PROFILE": "profile"}.items():
                if name in os.environ:
                    values[field] = os.environ[name]
        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            fields = sorted({str(e['loc'][0]) for e in exc.errors() if e['loc']})
            raise ConfigurationError("TRAJECTORY_CONFIG_INVALID: " + ", ".join(fields)) from None

    def tool_environment(self) -> dict[str, str]:
        binary = shutil.which(self.tool_python)
        if not binary or not os.access(binary, os.X_OK):
            raise ConfigurationError("tool_python: executable_required")
        try:
            version = subprocess.run(
                [binary, "-I", "-B", "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"],
                env={}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ConfigurationError("tool_python: python_3_11_required") from None
        if version.returncode:
            raise ConfigurationError("tool_python: python_3_11_required")
        if not self.skill_path or not Path(self.skill_path).is_file():
            raise ConfigurationError("skill_path: file_required")
        if Path(self.skill_path).name != "SKILL.md" or Path(self.skill_path).parent.name != "wt-serving-query":
            raise ConfigurationError("skill_path: wt_serving_query_required")
        env = {"DATAELF_TRAJECTORY_TOOL_PYTHON": str(Path(binary).absolute()), "WT_SDK_PROFILE": self.profile}
        for name in ("WT_SDK_DB_URI", "WT_SDK_S3_ENDPOINT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            value = os.environ.get(name)
            if not value or any(c in value for c in ('\x00', '\n', '\r')):
                raise ConfigurationError(name + ": required")
            env[name] = value
        for name in ("WT_SDK_DB_URI", "WT_SDK_S3_ENDPOINT"):
            try:
                parsed = urlsplit(env[name])
                valid = bool(parsed.scheme and parsed.hostname)
                _ = parsed.port
                if name == "WT_SDK_S3_ENDPOINT":
                    valid = valid and parsed.scheme in {"http", "https"}
            except ValueError:
                valid = False
            if not valid:
                raise ConfigurationError(name + ": invalid_url")
        self._validate_sdk(binary)
        return env

    @staticmethod
    def _validate_sdk(binary: str) -> None:
        # Import only: no GatewayConfig/WTGatewayClient construction or query.
        # Isolated Python ignores cwd/PYTHONPATH; no credentials reach imports.
        try:
            probe = subprocess.run(
                [binary, "-I", "-B", "-c",
                 "from wt_sdk.client import WTGatewayClient; "
                 "from wt_sdk.config import GatewayConfig; "
                 "from dldb.session import InformationSchemaTable"],
                env={}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ConfigurationError("tool_python: wt_sdk_import_required") from None
        if probe.returncode:
            raise ConfigurationError("tool_python: wt_sdk_import_required")
