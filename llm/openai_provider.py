import json
import time
from typing import Any

from .provider import LLMProvider
from exceptions import LLMConnectionError, LLMResponseError


class OpenAIProvider(LLMProvider):
    _json_response_format_support_cache: dict[tuple[str, str], bool] = {}

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ):
        try:
            import openai
        except ImportError:
            raise ImportError("openai package is required. Install with: pip install openai")

        self._client = openai.OpenAI(api_key=api_key, base_url=base_url)
        self.base_url = base_url
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    def _call_with_retry(self, model: str, messages: list[dict], **kwargs: Any) -> str:
        #Call LLM API with retry logic.
        import openai

        last_error = None

        for attempt in range(self.max_retries):
            try:
                response = self._client.chat.completions.create(
                    model=model,
                    messages=messages,
                    **kwargs,
                )

                if not response.choices:
                    raise LLMResponseError("LLM returned empty response")

                content = response.choices[0].message.content
                if content is None:
                    raise LLMResponseError("LLM returned null content")

                return content

            except openai.APIConnectionError as e:
                last_error = LLMConnectionError(f"Failed to connect to LLM API: {e}")
            except openai.RateLimitError as e:
                last_error = LLMConnectionError(f"Rate limit exceeded: {e}")
            except openai.APIStatusError as e:
                last_error = LLMConnectionError(f"LLM API error (status {e.status_code}): {e}")
            except LLMResponseError:
                raise
            except Exception as e:
                last_error = LLMConnectionError(f"Unexpected LLM error: {e}")

            # Wait before retry (except last attempt)
            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay * (attempt + 1))

        raise last_error

    def generate_from_messages(self, model: str, messages: list[dict], **kwargs: Any) -> str:
        return self._call_with_retry(model=model, messages=messages, **kwargs)

    def generate(
        self,
        model: str,
        prompt: str,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return self.generate_from_messages(model=model, messages=messages, **kwargs)

    def generate_json(self, model: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": "You must respond with valid JSON only."},
            {"role": "user", "content": prompt},
        ]

        cache_key = self._json_response_format_cache_key(model)
        supports_response_format = self._json_response_format_support_cache.get(cache_key, True)

        if supports_response_format:
            try:
                content = self.generate_from_messages(
                    model=model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    **kwargs,
                )
                self._json_response_format_support_cache[cache_key] = True
                return self.load_json_content(content)
            except Exception as e:
                if self._looks_like_response_format_incompatibility(e):
                    self._json_response_format_support_cache[cache_key] = False
                # Some OpenAI-compatible backends reject response_format=json_object.
                # Fall back to plain text generation with the same JSON-only contract.

        content = self.generate_from_messages(
            model=model,
            messages=messages,
            **kwargs,
        )

        return self.load_json_content(content)

    def _json_response_format_cache_key(self, model: str) -> tuple[str, str]:
        return (self.base_url or "", model)

    def _looks_like_response_format_incompatibility(self, error: Exception) -> bool:
        message = str(error).lower()
        incompatibility_markers = [
            "response_format",
            "json_object",
            "unsupported",
            "not support",
            "not supported",
            "invalid parameter",
            "unknown parameter",
            "unknown field",
            "unrecognized request argument",
            "extra inputs are not permitted",
        ]
        return any(marker in message for marker in incompatibility_markers)

    def load_json_content(self, content: str) -> dict[str, Any]:
        cleaned = content.strip()

        if cleaned.startswith("```json"):
            cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
        elif cleaned.startswith("```"):
            cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise LLMResponseError(f"LLM returned invalid JSON: {e}")
