from typing import Dict, List, Optional

from pydantic import BaseModel


class LLMConfig(BaseModel):
    """LLM service config for LLM-as-Judge checkers."""
    model: str = ""
    api_key: str = ""
    api_url: str = ""
    temperature: float = 0.0
    max_tokens: int = 2048


class CheckerConfig(BaseModel):
    """Config for a single checker."""
    name: str
    enabled: bool = True
    params: Dict = {}


class ExecutorConfig(BaseModel):
    """Executor engine config."""
    max_workers: int = 4
    batch_size: int = 100
    start_index: int = 0
    end_index: int = -1                        # -1 means process all


class AuditConfig(BaseModel):
    """Top-level audit config."""
    task_name: str = "security_audit"
    output_path: str = "outputs/"
    log_level: str = "INFO"

    executor: ExecutorConfig = ExecutorConfig()
    llm: Optional[LLMConfig] = None        # llm model name (llm-based checkers required)
    models: Dict[str, str] = {}            # model name or path (model-based checkers required)
    checkers: List[CheckerConfig] = []
    checker_tags: List[str] = []
