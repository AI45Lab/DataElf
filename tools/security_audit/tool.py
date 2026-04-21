"""
SecurityAuditTool: wraps the security_audit engine as a BaseTool.

Pipeline usage:
    audit = run_tool(
        "security_audit",
        data=data
    )
"""

import os
from typing import Any

from tools.base_tool import BaseTool, ToolContext
from .config import AuditConfig, CheckerConfig, ExecutorConfig, LLMConfig
from .executor import Executor
from .loader import load_samples


_DEFAULT_RISK_WEIGHTS = {
    "harmful":   6,
    "toxicity":  5,
    "bias":      5,
    "pii":       3,
    "secret":    3,
    "factuality_inconsistancy": 4,
    "self_contradiction": 7,
    "instruction_mismatch": 6,
    "label_flipping": 7,
    "backdoor":  8,
    "prompt_injection": 10,
    "jailbreak": 10,
    "sycophancy": 2,
}


def _get_tool_defaults(context_config: dict) -> dict:
    tool_defaults = context_config.get("tool_defaults") or {}
    if not isinstance(tool_defaults, dict):
        return {}
    security_defaults = tool_defaults.get("security_audit") or {}
    return security_defaults if isinstance(security_defaults, dict) else {}


def _resolve_checker_configs(kwargs: dict, tool_defaults: dict) -> list[CheckerConfig]:
    """Resolve CheckerConfig list.

    Priority:
      1. ``checkers`` list in tool_defaults (from default.yaml).
      2. Built-in fallback: [PIIRule].
    """
    names = kwargs.get("checker_names")
    if names:  # non-empty list → explicit override
        return [CheckerConfig(name=n) for n in names]

    raw = tool_defaults.get("checkers")
    if raw and isinstance(raw, list):
        configs = []
        for item in raw:
            if isinstance(item, str):
                configs.append(CheckerConfig(name=item))
            elif isinstance(item, dict) and "name" in item:
                configs.append(CheckerConfig(**item))
        if configs:
            return configs

    return [CheckerConfig(name="PIIRule")]


def _calc_security_score(risk_distribution: dict, risk_weights: dict) -> float:
    """Weighted security score 0.0-1.0. Higher = safer."""
    active = {k: v for k, v in risk_weights.items() if k in risk_distribution}
    if not active:
        return 1.0

    total_weight = sum(active.values())
    penalty = sum(
        (w / total_weight) * (risk_distribution[k]["flagged"] / risk_distribution[k]["total"])
        for k, w in active.items()
        if risk_distribution[k]["total"] > 0
    )
    return round(max(0.0, 1.0 - penalty), 4)


class SecurityAuditTool(BaseTool):

    @property
    def name(self) -> str:
        return "security_audit"

    @property
    def description(self) -> str:
        return (
            "Run a security audit on a dataset using checkers. "
            "Detects risks such as PII leakage, toxicity, harmful content, poisoning, and alignment bypass."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Dataset records to audit (list of dicts).",
                },
                "checker_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "OPTIONAL. Omit this parameter unless the user explicitly names specific checkers to run. "
                        "When omitted, the default checker list from configuration is used automatically."
                    ),
                },
                "max_workers": {
                    "type": "integer",
                    "description": "OPTIONAL. Number of parallel worker threads for the audit executor.",
                    "default": 4,
                },
            },
            "required": ["data"],
        }

    def usage_example(self) -> str:
        return (
            'audit = run_tool(\n'
            '    "security_audit",\n'
            '    data=data,\n'
            ')'
        )

    def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        data: list[dict] = kwargs.get("data", [])
        max_workers: int = kwargs.get("max_workers", 4)

        tool_defaults = _get_tool_defaults(context.config)
        checker_configs = _resolve_checker_configs(kwargs, tool_defaults)
        checker_names = [c.name for c in checker_configs if c.enabled]

        if kwargs.get("checker_names"):
            context.log(f"SecurityAuditTool: using user-specified checkers: {checker_names}")
        if tool_defaults.get("checkers"):
            context.log(
                f"SecurityAuditTool: using checkers from default.yaml: {checker_names}. "
            )
        else:
            context.log(
                f"SecurityAuditTool: no checkers configured, using built-in default: {checker_names}"
            )

        context.log(f"SecurityAuditTool: {len(data)} records, checkers={checker_names}")

        # 1. Convert raw dicts → DataSample
        samples = load_samples(data)

        # 2. Build AuditConfig
        agent_cfg = context.config.get("agent")
        tool_llm_cfg = context.config.get("tool_llm", {})
        llm_name: str = (
            getattr(tool_llm_cfg, "model", "") or getattr(agent_cfg, "model", "")
        )

        task_name = f"security_audit_{context.job_id}"
        output_path = os.path.join("outputs", task_name)

        cfg = AuditConfig(
            task_name=task_name,
            output_path=output_path,
            checkers=checker_configs,
            executor=ExecutorConfig(max_workers=max_workers),
            llm=LLMConfig(model=llm_name) if llm_name else None,
            models=tool_defaults.get("models") or {},
        )

        # 3. Log LLM model info
        if cfg.llm:
            context.log(f"Tool LLM model: {llm_name}", "info")

        # 4. Run the audit engine (writes artifacts to output_path)
        engine = Executor(cfg, logger=context.logger, llm=context.llm)
        engine.setup()
        task_report, _ = engine.run(samples)

        flagged_rate = task_report.flagged_rate()
        total_issues: int = task_report.flagged_samples
        risk_weights: dict = tool_defaults.get("risk_weights")
        if not risk_weights:
            context.log("SecurityAuditTool: risk_weights not configured, using default weights.", "warning")
            risk_weights = _DEFAULT_RISK_WEIGHTS
        security_score = _calc_security_score(task_report.risk_distribution, risk_weights)

        context.log(
            f"Audit complete: {task_report.flagged_samples}/{task_report.total_samples} flagged "
            f"({flagged_rate:.1%}), score={security_score}",
            "info",
        )

        return {
            "result": {
                "security_score": security_score,
                "total_issues": total_issues,
                "flagged_samples": task_report.flagged_samples,
                "safe_samples": task_report.safe_samples,
                "total_samples": task_report.total_samples,
                "flagged_rate": flagged_rate,
                "risk_distribution": task_report.risk_distribution,
                "checker_stats": task_report.checker_stats,
            },
            "metadata": {
                "task_name": task_report.task_name,
                "checker_names": checker_names,
                "create_time": task_report.create_time,
                "finish_time": task_report.finish_time,
            },
            "artifacts": {
                "report_md": os.path.join(output_path, "report.md"),
                "sample_results": os.path.join(output_path, "sample_results.json"),
            },
        }

