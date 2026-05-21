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

from config import apply_runtime_environment, build_runtime_policy, handle_preflight_issues
from tools.base_tool import BaseTool, ToolContext
from .config import AuditConfig, CheckerConfig, ExecutorConfig, LLMConfig
from .executor import Executor
from .loader import load_samples
from .policy import (
    ResolvedCheckerPlan,
    build_checker_capability_set,
    build_checker_request,
    resolve_checker_plan,
    validate_checker_request,
    validate_resolved_checker_plan,
)


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


def _get_config_value(section: Any, name: str, default: Any = None) -> Any:
    if isinstance(section, dict):
        return section.get(name, default)
    return getattr(section, name, default)


def _apply_runtime_policy_to_checker_configs(
    checker_configs: list[CheckerConfig],
    runtime_policy: Any,
) -> None:
    if runtime_policy.model_policy != "local_only":
        return
    for checker_config in checker_configs:
        checker_config.params["local_files_only"] = True


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

        # Step 1: Load tool defaults and deployment runtime policy.
        tool_defaults = _get_tool_defaults(context.config)
        runtime_policy = build_runtime_policy(context.config)
        apply_runtime_environment(runtime_policy)

        # Step 2: Validate the request, build capabilities, and resolve a checker plan.
        plan = self._build_execution_plan(
            context=context,
            kwargs=kwargs,
            tool_defaults=tool_defaults,
            runtime_policy=runtime_policy,
        )

        # Step 3: Log the resolved single-stage plan.
        checker_configs = plan.checker_configs
        checker_names = [c.name for c in checker_configs if c.enabled]
        self._log_plan(context, plan, checker_names, bool(kwargs.get("checker_names")))
        context.log(f"SecurityAuditTool: {len(data)} records, checkers={checker_names}")

        # TODO:Step 4: Execute the resolved plan.
        return self._execute_single_stage_plan(
            context=context,
            plan=plan,
            data=data,
            max_workers=max_workers,
            tool_defaults=tool_defaults,
            checker_configs=checker_configs,
            checker_names=checker_names,
        )

    def _build_execution_plan(
        self,
        *,
        context: ToolContext,
        kwargs: dict[str, Any],
        tool_defaults: dict,
        runtime_policy: Any,
    ) -> ResolvedCheckerPlan:
        # Step 2.1: Build and validate the raw checker request from tool args.
        request = build_checker_request(kwargs, tool_defaults)
        handle_preflight_issues(
            validate_checker_request(
                request=request,
                runtime_policy=runtime_policy,
                context_config=context.config,
            ),
            strict=runtime_policy.strict_preflight,
            logger=context.logger,
        )

        # Step 2.2: Build the checker capability set under resource/network policy.
        capability_set = build_checker_capability_set(
            tool_defaults=tool_defaults,
            runtime_policy=runtime_policy,
            context_config=context.config,
        )

        # TODO:Step 2.3: Resolve the execution plan within the capability set.
        plan = resolve_checker_plan(
            request=request,
            capability_set=capability_set,
            tool_defaults=tool_defaults,
            resource_tier=runtime_policy.resource_tier,
        )

        # TODO:Step 2.4: Inject runtime params and run final resolved-plan preflight.
        _apply_runtime_policy_to_checker_configs(plan.checker_configs, runtime_policy)
        handle_preflight_issues(
            validate_resolved_checker_plan(
                plan=plan,
                capability_set=capability_set,
                runtime_policy=runtime_policy,
                context_config=context.config,
            ),
            strict=runtime_policy.strict_preflight,
            logger=context.logger,
        )
        return plan

    def _log_plan(
        self,
        context: ToolContext,
        plan: ResolvedCheckerPlan,
        checker_names: list[str],
        explicit_checkers: bool,
    ) -> None:
        if explicit_checkers:
            context.log(f"SecurityAuditTool: using user-specified checkers: {checker_names}")
        elif plan.source == "config":
            context.log(
                f"SecurityAuditTool: using checkers from default.yaml: {checker_names}. "
            )
        else:
            context.log(
                f"SecurityAuditTool: using {plan.strategy} defaults: {checker_names}"
            )

        for skipped in plan.skipped_checkers:
            context.log(
                f"SecurityAuditTool: skipped checker {skipped['name']} ({skipped['reason']}).",
                "warning",
            )

    def _execute_single_stage_plan(
        self,
        *,
        context: ToolContext,
        plan: ResolvedCheckerPlan,
        data: list[dict],
        max_workers: int,
        tool_defaults: dict,
        checker_configs: list[CheckerConfig],
        checker_names: list[str],
    ) -> dict[str, Any]:
        # TODO: (resource_tier) Replace this single-stage executor with a
        # strategy-aware multi-stage executor when funnel plans are implemented.
        samples = load_samples(data)

        agent_cfg = context.config.get("agent")
        tool_llm_cfg = context.config.get("tool_llm", {})
        llm_name: str = (
            _get_config_value(tool_llm_cfg, "model", "")
            or _get_config_value(agent_cfg, "model", "")
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

        if cfg.llm:
            context.log(f"Tool LLM model: {llm_name}", "info")

        engine = Executor(cfg, logger=context.logger, llm=context.llm, job_id=context.job_id, mode=context.mode)
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
                "resolved_plan": {
                    "schema_version": "security_audit.resolved_plan.v1",
                    "strategy": plan.strategy,
                    "source": plan.source,
                    "skipped_checkers": plan.skipped_checkers,
                    "stages": [
                        {
                            "id": "stage_1",
                            "name": "single_stage",
                            "type": "single_stage",
                            "checkers": checker_names,
                            "input_scope": {"type": "all_samples"},
                        }
                    ],
                },
                "create_time": task_report.create_time,
                "finish_time": task_report.finish_time,
            },
            "artifacts": {
                "report_md": os.path.join(output_path, "report.md"),
                "sample_results": os.path.join(output_path, "sample_results.json"),
            },
        }
