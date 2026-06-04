from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class RiskType(str, Enum):
    """Risk categories."""
    HARMFUL               = "harmful"                # 有害内容
    TOXICITY              = "toxicity"               # 毒性内容
    BIAS                  = "bias"                   # 偏见歧视
    PII                   = "pii"                    # PII 泄露
    SECRET                = "secret"                 # 密钥泄露
    LABEL_FLIPPING        = "label_flipping"         # 标签翻转
    FACTUAL_INCONSISTENCY = "factual_inconsistency"  # 违背事实
    SELF_CONTRADICTION    = "self_contradiction"     # 自相矛盾
    INSTRUCTION_MISMATCH  = "instruction_mismatch"   # 指令失配
    BACKDOOR              = "backdoor"               # 后门注入
    PROMPT_INJECTION      = "prompt_injection"       # 提示注入
    JAILBREAK             = "jailbreak"              # 越狱提示
    SYCOPHANCY            = "sycophancy"             # 阿谀奉承


class CheckResult(BaseModel):
    """Result of a single checker on a single sample."""
    checker_name: str
    risk_type: RiskType
    success: bool = False
    score: float = 0.0                         # risk score (0.0-1.0)
    flagged: bool = False
    details: Dict[str, Any] = {}


class SampleReport(BaseModel):
    """Full check report for a single sample."""
    sample_id: str
    flagged: bool = False
    categories: Dict[str, bool] = {}          # per-category flagged status
    category_scores: Dict[str, float] = {}    # per-category continuous score (0.0-1.0)
    results: List[CheckResult] = []

    def compute_category_scores(self, flag_threshold: float = 0.5) -> None:
        """Aggregate checker results into per-category scores and boolean flags.

        Only includes categories that were actually checked (have at least one
        CheckResult). Unchecked categories are omitted from scores and flags.

        Each category score is the max score across all checkers for that category.
        Boolean flags follow each checker's own flagged decision. The
        flag_threshold argument is kept for backward-compatible callers.
        """
        scores: Dict[str, float] = {}
        flags: Dict[str, bool] = {}
        for result in self.results:
            if not result.success:
                continue
            rt = result.risk_type.value
            scores[rt] = max(scores.get(rt, 0.0), result.score)
            flags[rt] = flags.get(rt, False) or result.flagged

        self.category_scores = scores
        self.categories = {rt: flags.get(rt, False) for rt in scores}
        self.flagged = any(self.categories.values())


class TaskReport(BaseModel):
    """Summary report for an entire audit task."""
    task_name: str = ""
    input_path: str = ""
    output_path: str = ""
    create_time: str = ""
    finish_time: str = ""
    total_samples: int = 0
    flagged_samples: int = 0
    safe_samples: int = 0

    risk_distribution: Dict[str, Dict[str, int]] = {}

    checker_stats: Dict[str, Dict[str, int]] = {}

    severity_distribution: Dict[str, int] = {}  # for future use, e.g. {"low": 10, "medium": 5, "high": 2}

    def flagged_rate(self) -> float:
        if self.total_samples == 0:
            return 0.0
        return round(self.flagged_samples / self.total_samples, 4)
