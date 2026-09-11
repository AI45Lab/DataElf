"""One completion per extraction, using the explicitly selected internal model."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from .profile import Profile, SERVE_PROFILE
from .prompts import build_prompt_components, render_system_prompt
from .config import IntentModelConfig
from .schema import Intent, Output, OUTPUT_SCHEMA_VERSION
from .layout import parse_sections, has_default_content


class IntentError(RuntimeError):
    """Transport, response or extraction-contract failure; never an empty success."""

    code = "INTENT_MODEL_FAILED"


class IntentRecognizer:
    def __init__(self, profile: Profile = SERVE_PROFILE, *, config: IntentModelConfig | None = None,
                 timeout_seconds: float | None = None):
        self.profile = profile
        self.config = config.resolve() if config is not None else IntentModelConfig.from_env()
        if timeout_seconds is not None:
            self.config = IntentModelConfig.model_validate({**self.config.model_dump(), "timeout_seconds": timeout_seconds})
        self.config.validate_for_run()
        self.timeout_seconds = self.config.timeout_seconds

    def extract(self, query: str, *, reference_time: datetime | None = None,
                timezone: str = "Asia/Shanghai") -> Intent:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a nonblank string")
        zone = ZoneInfo(timezone)
        if reference_time is not None and reference_time.utcoffset() is None:
            raise ValueError("reference_time must include a timezone offset")
        local_time = reference_time.astimezone(zone) if reference_time is not None else datetime.now(zone)
        components = build_prompt_components(self.profile, local_time, timezone)
        sections = parse_sections(query)
        heading_ids = [section["id"] for section in sections[1:]]
        has_heading = bool(heading_ids)
        components["scene_context"]["has_markdown_heading"] = has_heading
        components["scene_context"]["section_headers"] = [{key: section[key] for key in ("id", "title", "level")} for section in sections[1:]]
        schema = self.profile.json_schema()
        if has_heading:
            schema["properties"] = {"_writing_sections": {
                "type": "array", "items": {"type": "string", "enum": heading_ids},
                "description": "先仅按标题语义选择写作/总结等输出要求段落的ID；其他标题不得选，未找到则[]。",
            }, **schema["properties"]}
            schema["required"] = ["_writing_sections", *schema["required"]]
        if not has_heading:
            # Format gate only; heading semantics are still decided by the model.
            schema["$defs"]["Output"]["const"] = Output.unspecified().model_dump()
        payload = {
            "model": self.config.model_name,
            "messages": [
                {"role": "system", "content": render_system_prompt(components)},
                {"role": "user", "content": json.dumps({"input_sections": sections}, ensure_ascii=False) if has_heading else query},
            ],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "dataelf_intent_v" + OUTPUT_SCHEMA_VERSION, "strict": True, "schema": schema,
            }},
            "temperature": 0,
            "max_tokens": self.config.max_tokens,
            "stream": False,
            "thinking": {"type": "disabled"},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        return self._validate_response(self._request(payload), sections=sections)

    def _request(self, payload: dict):
        request = urllib.request.Request(self.config.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + self.config.api_key} if self.config.api_key else {})},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            exc.close()
            raise IntentError(f"Intent model endpoint returned HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError):
            raise IntentError("Model endpoint request failed or timed out") from None
        except (ValueError, UnicodeError):
            raise IntentError("Model endpoint returned invalid JSON") from None
        try:
            return json.loads(raw)
        except ValueError:
            raise IntentError("Model endpoint returned invalid JSON") from None

    def _validate_response(self, envelope, *, sections: list[dict] | None = None) -> Intent:
        try:
            choice = envelope["choices"][0]
            if choice["finish_reason"] != "stop":
                raise IntentError("Model response did not complete normally")
            message = choice["message"]
            if message.get("refusal"):
                raise IntentError("Model refused extraction")
            content = message["content"]
            if not isinstance(content, str):
                raise IntentError("Model response has no JSON text")
            document = json.loads(content)
            writing_ids = []
            if sections and len(sections) > 1:
                document = dict(document)
                writing_ids = document.pop("_writing_sections")
                allowed = {section["id"] for section in sections[1:]}
                if not isinstance(writing_ids, list) or any(not isinstance(item, str) or item not in allowed for item in writing_ids) or len(set(writing_ids)) != len(writing_ids):
                    raise IntentError("Model response violates the section contract")
            result = self.profile.validate(document)
            if sections is not None:
                if not writing_ids:
                    result = result.model_copy(update={"output": Output.unspecified()})
                if not has_default_content(sections, writing_ids):
                    defaults = self.profile.defaults()
                    result = result.model_copy(update={"retrieval": defaults.retrieval, "time_range": defaults.time_range, "domains": defaults.domains})
            return result
        except IntentError:
            raise
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise IntentError("Model response violates the intent contract") from None
