import json
import logging
from typing import Dict, List, Optional

from .schema import DataSample, MessageItem, ResponseStruct

_log = logging.getLogger(__name__)


def load_samples(
    data: List[dict],
    dataset_type: str = "sft",
    field_mapping: Optional[Dict[str, str]] = None,
) -> List[DataSample]:
    """Convert a list of plain dicts to DataSample objects.

    Args:
        data: raw records as a list of dicts (any source: file, DB, platform).
        dataset_type: default dataset type if not present in the record.
        field_mapping: optional raw_field → DataSample_field rename map.
    """
    mapping = field_mapping or {}
    samples = []
    for i, rec in enumerate(data):
        if mapping:
            rec = {mapping.get(k, k): v for k, v in rec.items()}
        try:
            sample = _to_sample(rec, i, dataset_type)
        except Exception as e:
            _log.warning(f"Skipping record {i}: {e}")
            continue
        samples.append(sample)
    return samples


def _to_sample(rec: dict, index: int, dataset_type: str) -> DataSample:
    """Best-effort conversion of a single dict to DataSample.

    - Records with standard fields (messages / response / chosen_response /
      rejected_response) are parsed via _normalize.
    - All other records are serialised as a user message so text-based
      checkers can still scan their content.
    """
    if any(k in rec for k in ("messages", "response", "chosen_response", "rejected_response")):
        return _normalize(rec, dataset_type, fallback_id=str(index))

    sample_id = str(rec.get("id", index))
    content = json.dumps(rec, ensure_ascii=False)
    return DataSample(
        id=sample_id,
        dataset_type=dataset_type,
        messages=[MessageItem(role="user", content=content)],
    )


def _normalize(rec: dict, dataset_type: str, fallback_id: str = "0") -> DataSample:
    """Normalise a raw dict (standard DataSample fields) into a DataSample."""
    sample_id = str(rec.get("id", fallback_id))

    messages: List[MessageItem] = []
    if "messages" in rec and isinstance(rec["messages"], list):
        for m in rec["messages"]:
            messages.append(MessageItem(**m) if isinstance(m, dict) else m)

    response          = _to_response(rec.get("response"))
    chosen_response   = _to_response(rec.get("chosen_response"))
    rejected_response = _to_response(rec.get("rejected_response"))

    meta_info = rec.get("meta_info")
    if isinstance(meta_info, str):
        try:
            meta_info = json.loads(meta_info)
        except Exception:
            meta_info = None

    return DataSample(
        id=sample_id,
        dataset_type=rec.get("dataset_type", dataset_type),
        messages=messages,
        response=response,
        chosen_response=chosen_response,
        rejected_response=rejected_response,
        context=rec.get("context"),
        ground_truth_answer=rec.get("ground_truth_answer"),
        reference_answer=rec.get("reference_answer"),
        session_id=rec.get("session_id"),
        step_id=rec.get("step_id"),
        is_terminal=rec.get("is_terminal"),
        created_at=rec.get("created_at"),
        step_reward=rec.get("step_reward"),
        reward=rec.get("reward"),
        meta_info=meta_info,
    )


def _to_response(val) -> ResponseStruct | None:
    if val is None:
        return None
    if isinstance(val, dict):
        return ResponseStruct(**val)
    if isinstance(val, str):
        return ResponseStruct(content=val)
    return None
