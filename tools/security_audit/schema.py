from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class DataFormat:
    """Valid dataset_type constants."""
    SFT       = "sft"
    RL        = "rl"
    DPO       = "dpo"
    BENCHMARK = "benchmark"


class MessageItem(BaseModel):
    """A single conversation message.

    content supports multimodal: str for plain text, List[dict] for multimodal
    (each dict has a 'type' key: 'text' / 'image_url' / 'image_base64').
    """
    role: str                        # "system" / "user" / "assistant" / "tool"
    content: Optional[Any] = None    # str or List[dict] for multimodal
    tool_calls: Optional[Any] = None
    refusal: Optional[str] = None

    def get_text_parts(self) -> List[str]:
        """Extract all text parts for text-based checkers."""
        if isinstance(self.content, str):
            return [self.content] if self.content else []
        if isinstance(self.content, list):
            return [p["text"] for p in self.content if p.get("type") == "text" and p.get("text")]
        return []

    def get_image_parts(self) -> List[dict]:
        """Extract all image parts for image-based checkers."""
        if isinstance(self.content, list):
            return [p for p in self.content if p.get("type") in ("image_url", "image_base64")]
        return []


class ResponseStruct(BaseModel):
    """Model response structure (response / chosen_response / rejected_response)."""
    role: str = "assistant"
    content: str = ""


class DataSample(BaseModel):
    """Data view for security checks, containing only fields needed by checkers."""
    id: str                                              # unique sample identifier

    # dataset type
    dataset_type: str                                    # sft / rl / dpo / benchmark

    # raw payload fields
    messages: List[MessageItem] = []                     # full conversation history
    response: Optional[ResponseStruct] = None            # model response (SFT/RL)
    chosen_response: Optional[ResponseStruct] = None     # DPO preferred response
    rejected_response: Optional[ResponseStruct] = None   # DPO rejected response
    context: Optional[Any] = None                        # trusted context for factual consistency check (str or List[str])
    ground_truth_answer: Optional[str] = None            # ground truth for rule-based RL
    reference_answer: Optional[str] = None               # reference answer for LLM-judge RL

    # RL trajectory fields
    session_id: Optional[str] = None
    step_id: Optional[int] = None
    is_terminal: Optional[bool] = None
    created_at: Optional[int] = None
    step_reward: Optional[float] = None
    reward: Optional[float] = None
    meta_info: Optional[Dict[str, Any]] = None

    def get_all_text_fields(self) -> Dict[str, str]:
        """Return all non-empty text fields for per-field scanning."""
        fields: Dict[str, str] = {}
        if self.messages:
            fields["messages"] = "\n".join(
                f"[{m.role}] {text}"
                for m in self.messages
                for text in m.get_text_parts()
            )
        for name, struct in [
            ("response", self.response),
            ("chosen_response", self.chosen_response),
            ("rejected_response", self.rejected_response),
        ]:
            if struct and struct.content:
                fields[name] = struct.content
        for name in ["ground_truth_answer", "reference_answer"]:
            val = getattr(self, name)
            if val:
                fields[name] = val
        return fields
