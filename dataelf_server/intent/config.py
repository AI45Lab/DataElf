"""Intent-only model configuration; independent of Pi and HTTP lifecycle."""
from __future__ import annotations

import os
from typing import Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IntentModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_name: str | None = None
    base_url: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    timeout_seconds: float = Field(default=90, gt=0)
    max_tokens: int = Field(default=2048, ge=128)

    @field_validator("model_name", "base_url", "api_key")
    @classmethod
    def strip_optional(cls, value):
        return value.strip() or None if value is not None else None

    def resolve(self, env: Mapping[str, str] | None = None) -> "IntentModelConfig":
        values = dict(env or {})
        values.update(os.environ)
        return self.model_copy(update={
            "base_url": self.base_url or values.get("OPENAI_BASE_URL"),
            "api_key": self.api_key or values.get("OPENAI_API_KEY"),
        })

    def validate_for_run(self) -> None:
        if not self.model_name:
            raise ValueError("server.intent.model_name must be specified")
        if not self.base_url:
            raise ValueError("Set server.intent.base_url or OPENAI_BASE_URL")
        parts = urlsplit(self.base_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Intent base_url must be an HTTP(S) API URL without credentials or query parameters")

    @property
    def endpoint(self) -> str:
        self.validate_for_run()
        base = self.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else base + "/chat/completions"

    @classmethod
    def from_env(cls) -> "IntentModelConfig":
        from dataelf.config import DataElfConfig
        core = DataElfConfig.from_env()
        return cls.model_validate(core.server.get("intent") or {}).resolve(core.env)
