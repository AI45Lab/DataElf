import json

from ...schema import DataSample
from exceptions import LLMConnectionError, LLMResponseError


def _call_llm_json(llm, model: str, prompt: str, key: str, default):
    try:
        raw = llm.generate(model=model, prompt=prompt)
    except Exception as e:
        raise LLMConnectionError(f"[LLM call failed] {e}") from e
    try:
        data, _ = json.JSONDecoder().raw_decode(raw, raw.index("{"))
    except Exception as e:
        raise LLMResponseError(f"[JSON parse failed] {e} | raw response: {raw!r}") from e
    return data.get(key, default)


def call_llm_extract_list(llm, model: str, prompt: str, key: str) -> list:
    """Call LLM, parse JSON, return list value for *key* (defaults to [])."""
    return _call_llm_json(llm, model, prompt, key, [])


def call_llm_extract_string(llm, model: str, prompt: str, key: str) -> str:
    """Call LLM, parse JSON, return string value for *key* (defaults to "")."""
    return _call_llm_json(llm, model, prompt, key, "")


def format_content(sample: DataSample) -> str:
    """Format all text fields of a DataSample into a labelled string."""
    parts = [f"[{field}]\n{text}" for field, text in sample.get_all_text_fields().items()]
    return "\n\n".join(parts)


def extract_response(sample: DataSample) -> str:
    """Extract the assistant's response from a DataSample.

    Priority: response field > chosen_response field > last assistant message.
    """
    if sample.response and sample.response.content:
        return sample.response.content
    if sample.chosen_response and sample.chosen_response.content:
        return sample.chosen_response.content
    for m in reversed(sample.messages):
        if m.role == "assistant":
            texts = m.get_text_parts()
            if texts:
                return " ".join(texts)
    return ""


def is_content_filter_error(err_msg: str) -> bool:
    """Check whether an error message indicates a content filter rejection."""
    return "content_filter" in err_msg or "content management policy" in err_msg