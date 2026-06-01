"""
SecurityAuditTool: wraps the security_audit engine as a BaseTool.

Pipeline usage:
    audit = run_tool(
        "security_audit",
        data=data
    )
"""

import json
import os
from datetime import datetime
from typing import Any

from config import apply_runtime_environment, build_runtime_policy, handle_preflight_issues
from tools.base_tool import BaseTool, ToolContext
from .config import AuditConfig, CheckerConfig, ExecutorConfig, LLMConfig
from .checker.registry import CheckerRegistry
from .executor import Executor, _build_report_md
from .loader import load_samples
from .result import CheckResult, SampleReport, TaskReport
from .schema import DataSample
from .policy import (
    ResolvedCheckerPlan,
    build_checker_capability_set,
    build_checker_request,
    capability_set_metadata,
    resolve_checker_plan,
    resolved_plan_metadata,
    checker_request_metadata,
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


_LOCAL_FILES_ONLY_CHECKERS = {
    "HarmfulContentClassifier",
    "JailbreakClassifier",
    "PromptInjectionClassifier",
    "BiasClassifier",
    "GraCeFulBackdoorDefender",
}

_CHECKER_REQUIRED_DATASET_TYPES = {
    "DPOLabelFlipLLMJudge": {"dpo"},
}


def _apply_runtime_policy_to_checker_configs(
    checker_configs: list[CheckerConfig],
    runtime_policy: Any,
) -> None:
    if runtime_policy.model_policy != "local_only":
        return
    for checker_config in checker_configs:
        if checker_config.name in _LOCAL_FILES_ONLY_CHECKERS:
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
                "checker_selection_mode": {
                    "type": "string",
                    "enum": ["explicit", "recommend", "default"],
                    "description": (
                        "OPTIONAL. Checker selection mode. explicit requires checker_names; recommend uses "
                        "audit_intent as the user's full audit intent; default uses the configured default checker set."
                    ),
                },
                "checker_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "OPTIONAL. Specific checker class names. If provided without checker_selection_mode, "
                        "the request is treated as checker_selection_mode='explicit'."
                    ),
                },
                "audit_intent": {
                    "type": "string",
                    "description": (
                        "OPTIONAL. Natural-language audit intent for recommend mode, including target risks, "
                        "audit scope, checker focus, and constraints such as low cost, speed, high accuracy, "
                        "or stronger coverage. Ignored in explicit or default mode."
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

        # Step 3: Log the resolved plan.
        checker_configs = plan.checker_configs
        checker_names = [c.name for c in checker_configs if c.enabled]
        self._log_plan(context, plan, checker_names)
        context.log(f"SecurityAuditTool: {len(data)} records, checkers={checker_names}")

        # Step 4: Execute the resolved plan.
        if plan.strategy == "llm":
            return self._execute_multi_stage_plan(
                context=context,
                plan=plan,
                data=data,
                max_workers=max_workers,
                tool_defaults=tool_defaults,
                checker_names=checker_names,
                runtime_policy=runtime_policy,
            )

        return self._execute_single_stage_plan(
            context=context,
            plan=plan,
            data=data,
            max_workers=max_workers,
            tool_defaults=tool_defaults,
            checker_configs=checker_configs,
            checker_names=checker_names,
            runtime_policy=runtime_policy,
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

        # Step 2.3: Resolve the execution plan within the capability set.
        agent_cfg = context.config.get("agent")
        tool_llm_cfg = context.config.get("tool_llm", {})
        llm_model: str = (
            _get_config_value(tool_llm_cfg, "model", "")
            or _get_config_value(agent_cfg, "model", "")
        )
        plan = resolve_checker_plan(
            request=request,
            capability_set=capability_set,
            tool_defaults=tool_defaults,
            resource_tier=runtime_policy.resource_tier,
            llm=context.llm,
            llm_model=llm_model,
            logger=context.logger,
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
    ) -> None:
        if plan.source == "explicit":
            context.log(f"SecurityAuditTool: using user-specified checkers: {checker_names}")
        elif plan.source == "default":
            context.log(
                f"SecurityAuditTool: using configured default checkers: {checker_names}. "
            )
        elif plan.source == "recommend":
            context.log(
                f"SecurityAuditTool: using recommended checkers ({plan.strategy}): {checker_names}"
            )
        else:
            context.log(
                f"SecurityAuditTool: checker_selection_mode=default but tools/security_audit/default.yaml was not loaded; using resource-tier defaults: {checker_names}. To use your configured defaults, add or update tools/security_audit/default.yaml and set required_security_audit_tool: true in config.yaml.",
                "warning",
            )

        for skipped in plan.skipped_checkers:
            context.log(
                f"SecurityAuditTool: skipped checker {skipped['name']} ({skipped['reason']}).",
                "warning",
            )
        context.log(f"SecurityAuditTool: resolved plan \n {plan.stages}")

    def _execute_multi_stage_plan(
        self,
        *,
        context: ToolContext,
        plan: ResolvedCheckerPlan,
        data: list[dict],
        max_workers: int,
        tool_defaults: dict,
        checker_names: list[str],
        runtime_policy: Any,
    ) -> dict[str, Any]:
        samples = load_samples(data)
        samples_by_id = {sample.id: sample for sample in samples}
        task_name = f"security_audit_{context.job_id}"
        output_path = os.path.join("outputs", task_name)
        stage_output_root = os.path.join(output_path, "stages")

        agent_cfg = context.config.get("agent")
        tool_llm_cfg = context.config.get("tool_llm", {})
        llm_name: str = (
            _get_config_value(tool_llm_cfg, "model", "")
            or _get_config_value(agent_cfg, "model", "")
        )
        if llm_name:
            context.log(f"Tool LLM model: {llm_name}", "info")

        checker_configs_by_name = {
            config.name: config
            for config in plan.checker_configs
            if config.enabled
        }
        merged_reports = {
            sample.id: SampleReport(sample_id=sample.id)
            for sample in samples
        }
        stage_reports_by_id: dict[str, dict[str, SampleReport]] = {}
        stage_execution: list[dict[str, Any]] = []

        for index, stage in enumerate(plan.stages, start=1):
            stage_id = str(stage.get("id") or f"stage_{index}")
            stage_checker_configs = self._checker_configs_for_stage(
                stage=stage,
                checker_configs_by_name=checker_configs_by_name,
            )
            if not stage_checker_configs:
                context.log(f"SecurityAuditTool: stage {stage_id} skipped (no enabled checkers).", "warning")
                stage_execution.append({
                    "stage_id": stage_id,
                    "checkers": [],
                    "input_samples": 0,
                    "flagged_samples": 0,
                    "skipped": True,
                    "reason": "no_enabled_checkers",
                })
                continue

            stage_samples = self._select_stage_samples(
                stage=stage,
                stage_checker_configs=stage_checker_configs,
                all_samples=samples,
                samples_by_id=samples_by_id,
                stage_reports_by_id=stage_reports_by_id,
            )
            if not stage_samples:
                context.log(f"SecurityAuditTool: stage {stage_id} skipped (no routed samples).", "warning")
                stage_execution.append({
                    "stage_id": stage_id,
                    "checkers": [config.name for config in stage_checker_configs],
                    "input_samples": 0,
                    "flagged_samples": 0,
                    "skipped": True,
                    "reason": "no_routed_samples",
                })
                continue

            context.log(
                f"SecurityAuditTool: running stage {stage_id} on {len(stage_samples)} samples, "
                f"checkers={[config.name for config in stage_checker_configs]}",
                "info",
            )
            cfg = AuditConfig(
                task_name=f"{task_name}_{stage_id}",
                output_path=os.path.join(stage_output_root, stage_id),
                checkers=[config.copy(deep=True) for config in stage_checker_configs],
                executor=ExecutorConfig(max_workers=max_workers),
                llm=LLMConfig(model=llm_name) if llm_name else None,
                models=tool_defaults.get("models") or {},
            )
            engine = Executor(cfg, logger=context.logger, llm=context.llm, job_id=context.job_id, mode=context.mode)
            engine.setup()
            stage_report, stage_sample_reports = engine.run(stage_samples)
            stage_reports_by_id[stage_id] = {
                sample_report.sample_id: sample_report
                for sample_report in stage_sample_reports
            }
            self._merge_stage_sample_reports(
                merged_reports=merged_reports,
                stage_sample_reports=stage_sample_reports,
                stage_id=stage_id,
            )
            stage_execution.append({
                "stage_id": stage_id,
                "checkers": [config.name for config in stage_checker_configs],
                "input_samples": len(stage_samples),
                "flagged_samples": stage_report.flagged_samples,
                "skipped": False,
            })

        sample_reports = list(merged_reports.values())
        for sample_report in sample_reports:
            sample_report.compute_category_scores()

        task_report = TaskReport(
            task_name=task_name,
            output_path=output_path,
            create_time=datetime.now().isoformat(),
            total_samples=len(samples),
        )
        self._aggregate_sample_reports(task_report, sample_reports)
        task_report.finish_time = datetime.now().isoformat()
        self._write_multi_stage_artifacts(task_report, sample_reports)

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
                "runtime_policy": {
                    "network_mode": runtime_policy.network_mode,
                    "resource_tier": runtime_policy.resource_tier,
                },
                "request": checker_request_metadata(plan.request) if plan.request else {},
                "capability_set": capability_set_metadata(plan.capability_set) if plan.capability_set else {},
                "resolved_plan": resolved_plan_metadata(plan),
                "execution": {
                    "max_workers": max_workers,
                    "stages": stage_execution,
                },
                "checker_names": checker_names,
                "checker_stats": task_report.checker_stats,
                "create_time": task_report.create_time,
                "finish_time": task_report.finish_time,
            },
            "artifacts": {
                "report_md": os.path.join(output_path, "report.md"),
                "sample_results": os.path.join(output_path, "sample_results.json"),
            },
        }

    def _checker_configs_for_stage(
        self,
        *,
        stage: dict[str, Any],
        checker_configs_by_name: dict[str, CheckerConfig],
    ) -> list[CheckerConfig]:
        configs: list[CheckerConfig] = []
        seen: set[str] = set()
        for checker_name in stage.get("checkers", []):
            if not isinstance(checker_name, str) or checker_name in seen:
                continue
            config = checker_configs_by_name.get(checker_name)
            if not config:
                continue
            seen.add(checker_name)
            configs.append(config)
        return configs

    def _select_stage_samples(
        self,
        *,
        stage: dict[str, Any],
        stage_checker_configs: list[CheckerConfig],
        all_samples: list[DataSample],
        samples_by_id: dict[str, DataSample],
        stage_reports_by_id: dict[str, dict[str, SampleReport]],
    ) -> list[DataSample]:
        routing = stage.get("routing") if isinstance(stage.get("routing"), dict) else {}
        mode = str(routing.get("mode") or "all_samples")

        if mode == "all_samples":
            return list(all_samples)

        if mode == "field_applicable":
            return self._field_applicable_samples(
                samples=list(all_samples),
                checker_configs=stage_checker_configs,
                routing=routing,
            )

        if mode == "sample":
            routed = self._field_applicable_samples(
                samples=list(all_samples),
                checker_configs=stage_checker_configs,
                routing=routing,
            )
            return self._sample_routed_samples(routed, routing)

        if mode == "uncertain":
            return self._samples_by_source_stage_rules(
                routing=routing,
                samples_by_id=samples_by_id,
                stage_reports_by_id=stage_reports_by_id,
                default_rules={"near_threshold", "low_confidence", "conflicting"},
            )

        raise ValueError(f"Unsupported security_audit routing mode: {mode!r}")

    def _field_applicable_samples(
        self,
        *,
        samples: list[DataSample],
        checker_configs: list[CheckerConfig],
        routing: dict[str, Any],
    ) -> list[DataSample]:
        if not samples:
            return []

        required_fields = set(self._routing_string_list(routing, "required_fields", "fields"))
        dataset_types = set(self._routing_string_list(routing, "dataset_types", "dataset_type"))
        applicable_mode = str(routing.get("applicable_mode") or "any_checker")

        if dataset_types:
            samples = [s for s in samples if str(s.dataset_type).lower() in dataset_types]

        if required_fields:
            samples = [s for s in samples if self._sample_has_required_fields(s, required_fields)]

        if not checker_configs or applicable_mode == "none":
            return samples

        if applicable_mode == "all_checkers":
            return [
                sample for sample in samples
                if all(self._checker_applicable_to_sample(config.name, sample) for config in checker_configs)
            ]

        return [
            sample for sample in samples
            if any(self._checker_applicable_to_sample(config.name, sample) for config in checker_configs)
        ]

    def _checker_applicable_to_sample(self, checker_name: str, sample: DataSample) -> bool:
        dataset_types = _CHECKER_REQUIRED_DATASET_TYPES.get(checker_name)
        if dataset_types and str(sample.dataset_type).lower() not in dataset_types:
            return False

        try:
            checker_cls = CheckerRegistry.get(checker_name)
        except KeyError:
            return False

        supported_formats = getattr(checker_cls, "supported_formats", None)
        if supported_formats and sample.dataset_type not in supported_formats:
            return False

        required_fields = self._checker_required_fields(checker_name, checker_cls)
        if required_fields:
            return self._sample_has_required_fields(sample, required_fields)

        return bool(sample.get_all_text_fields())

    def _checker_required_fields(self, checker_name: str, checker_cls: Any) -> set[str]:
        metadata = getattr(checker_cls, "planner_metadata", None)
        if isinstance(metadata, dict) and "required_fields" in metadata:
            return {
                self._normalize_routing_token(field)
                for field in metadata.get("required_fields") or []
                if str(field).strip()
            }
        return set()

    def _sample_has_required_fields(self, sample: DataSample, required_fields: set[str]) -> bool:
        for field in required_fields:
            normalized = str(field).strip().lower()
            if normalized == "messages":
                if not any(message.get_text_parts() for message in sample.messages):
                    return False
            elif normalized == "context":
                if not self._has_text_value(sample.context):
                    return False
            elif normalized in {"response", "chosen_response", "rejected_response"}:
                struct = getattr(sample, normalized, None)
                if not struct or not self._has_text_value(getattr(struct, "content", None)):
                    return False
            else:
                if not self._has_text_value(getattr(sample, normalized, None)):
                    return False
        return True

    def _has_text_value(self, value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, list):
            return any(self._has_text_value(item) for item in value)
        return value is not None

    def _samples_by_source_stage_rules(
        self,
        *,
        routing: dict[str, Any],
        samples_by_id: dict[str, DataSample],
        stage_reports_by_id: dict[str, dict[str, SampleReport]],
        default_rules: set[str],
    ) -> list[DataSample]:
        source_reports = self._source_stage_reports(routing, stage_reports_by_id)
        if not source_reports:
            return []

        rules = set(self._routing_string_list(routing, "rules", "rule")) or default_rules
        risk_types = set(self._routing_string_list(routing, "risk_types", "risk_type"))
        checker_names = set(self._routing_string_list(routing, "checker_names", "checkers"))

        selected: list[DataSample] = []
        for sample_id, sample_report in source_reports.items():
            if sample_id not in samples_by_id:
                continue
            results = [
                result for result in sample_report.results
                if self._result_matches_filters(result, risk_types, checker_names)
            ]
            if self._sample_report_matches_routing_rules(sample_report, results, rules, routing):
                selected.append(samples_by_id[sample_id])
        return selected

    def _source_stage_reports(
        self,
        routing: dict[str, Any],
        stage_reports_by_id: dict[str, dict[str, SampleReport]],
    ) -> dict[str, SampleReport] | None:
        source_stage_id = routing.get("source_stage_id")
        if source_stage_id:
            return stage_reports_by_id.get(str(source_stage_id))
        if stage_reports_by_id:
            latest_stage_id = next(reversed(stage_reports_by_id))
            return stage_reports_by_id[latest_stage_id]
        return None

    def _result_matches_filters(
        self,
        result: CheckResult,
        risk_types: set[str],
        checker_names: set[str],
    ) -> bool:
        risk_value = getattr(result.risk_type, "value", str(result.risk_type))
        if risk_types and str(risk_value).lower() not in risk_types:
            return False
        if checker_names and result.checker_name.lower() not in checker_names:
            return False
        return True

    def _sample_report_matches_routing_rules(
        self,
        sample_report: SampleReport,
        results: list[CheckResult],
        rules: set[str],
        routing: dict[str, Any],
    ) -> bool:
        if not results:
            return False

        if "flagged" in rules and any(result.flagged for result in results):
            return True
        if "unflagged" in rules and any(result.success for result in results) and not any(result.flagged for result in results):
            return True
        if "error" in rules and any(not result.success for result in results):
            return True
        if "content_filter" in rules and any(result.details.get("content_filter_triggered") for result in results):
            return True
        if "near_threshold" in rules and self._has_near_threshold_result(results, routing):
            return True
        if "low_confidence" in rules and self._has_low_confidence_result(results, routing):
            return True
        if "conflicting" in rules and self._has_conflicting_results(results):
            return True
        return False

    def _has_near_threshold_result(
        self,
        results: list[CheckResult],
        routing: dict[str, Any],
    ) -> bool:
        threshold = self._routing_float(routing, "threshold", default=0.5)
        margin = self._routing_float(routing, "threshold_margin", "margin", default=0.1)
        return any(
            result.success and abs(float(result.score) - threshold) <= margin
            for result in results
        )

    def _has_low_confidence_result(
        self,
        results: list[CheckResult],
        routing: dict[str, Any],
    ) -> bool:
        max_confidence = self._routing_float(routing, "max_confidence", "confidence_threshold", default=0.7)
        for result in results:
            confidence = result.details.get("confidence")
            if confidence is None:
                confidence = result.details.get("confidence_score")
            try:
                if float(confidence) <= max_confidence:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _has_conflicting_results(self, results: list[CheckResult]) -> bool:
        by_risk: dict[str, set[bool]] = {}
        for result in results:
            if not result.success:
                continue
            risk_value = getattr(result.risk_type, "value", str(result.risk_type))
            by_risk.setdefault(str(risk_value), set()).add(bool(result.flagged))
        return any(len(flags) > 1 for flags in by_risk.values())

    def _routing_string_list(self, routing: dict[str, Any], *keys: str) -> list[str]:
        for key in keys:
            value = routing.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                return [self._normalize_routing_token(item) for item in value.split(",") if item.strip()]
            if isinstance(value, list):
                return [self._normalize_routing_token(item) for item in value if str(item).strip()]
            return [self._normalize_routing_token(value)]
        return []

    def _normalize_routing_token(self, value: Any) -> str:
        return str(value).strip().lower().replace("-", "_")

    def _routing_float(
        self,
        routing: dict[str, Any],
        *keys: str,
        default: float,
    ) -> float:
        for key in keys:
            if key not in routing:
                continue
            try:
                return float(routing[key])
            except (TypeError, ValueError):
                return default
        return default

    def _sample_routed_samples(
        self,
        samples: list[DataSample],
        routing: dict[str, Any],
    ) -> list[DataSample]:
        if not samples:
            return []
        raw_limit = routing.get("sample_size") or routing.get("max_samples") or routing.get("limit")
        if raw_limit is not None:
            try:
                limit = max(0, int(raw_limit))
            except (TypeError, ValueError):
                limit = len(samples)
            return samples[:limit] if limit else []
        raw_rate = routing.get("sample_rate") or routing.get("rate")
        if raw_rate is not None:
            try:
                rate = float(raw_rate)
            except (TypeError, ValueError):
                rate = 1.0
            if rate <= 0:
                return []
            if rate >= 1:
                return list(samples)
            limit = max(1, int(len(samples) * rate))
            return samples[:limit]
        return list(samples)

    def _merge_stage_sample_reports(
        self,
        *,
        merged_reports: dict[str, SampleReport],
        stage_sample_reports: list[SampleReport],
        stage_id: str,
    ) -> None:
        for stage_sample_report in stage_sample_reports:
            merged_report = merged_reports.setdefault(
                stage_sample_report.sample_id,
                SampleReport(sample_id=stage_sample_report.sample_id),
            )
            for result in stage_sample_report.results:
                result.details = dict(result.details or {})
                result.details.setdefault("stage_id", stage_id)
                merged_report.results.append(result)

    def _aggregate_sample_reports(
        self,
        task_report: TaskReport,
        sample_reports: list[SampleReport],
    ) -> None:
        for sample_report in sample_reports:
            if sample_report.flagged:
                task_report.flagged_samples += 1
            else:
                task_report.safe_samples += 1

            for risk_type, flagged in sample_report.categories.items():
                task_report.risk_distribution.setdefault(risk_type, {"total": 0, "flagged": 0})
                task_report.risk_distribution[risk_type]["total"] += 1
                if flagged:
                    task_report.risk_distribution[risk_type]["flagged"] += 1

            for result in sample_report.results:
                checker_name = result.checker_name
                task_report.checker_stats.setdefault(
                    checker_name,
                    {"total": 0, "flagged": 0, "error": 0, "content_filter": 0},
                )
                if not result.success:
                    task_report.checker_stats[checker_name]["error"] += 1
                    if result.details.get("content_filter_triggered"):
                        task_report.checker_stats[checker_name]["content_filter"] += 1
                else:
                    task_report.checker_stats[checker_name]["total"] += 1
                    if result.flagged:
                        task_report.checker_stats[checker_name]["flagged"] += 1

    def _write_multi_stage_artifacts(
        self,
        task_report: TaskReport,
        sample_reports: list[SampleReport],
    ) -> None:
        if not task_report.output_path:
            return
        os.makedirs(task_report.output_path, exist_ok=True)
        json_path = os.path.join(task_report.output_path, "sample_results.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump([report.model_dump() for report in sample_reports], f, ensure_ascii=False, indent=2)
        md_path = os.path.join(task_report.output_path, "report.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(_build_report_md(task_report, sample_reports))

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
        runtime_policy: Any,
    ) -> dict[str, Any]:
        # Deterministic default and explicit selections run as one execution stage.
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
                "runtime_policy": {
                    "network_mode": runtime_policy.network_mode,
                    "resource_tier": runtime_policy.resource_tier,
                },
                "request": checker_request_metadata(plan.request) if plan.request else {},
                "capability_set": capability_set_metadata(plan.capability_set) if plan.capability_set else {},
                "resolved_plan": resolved_plan_metadata(plan),
                "execution": {
                    "max_workers": max_workers,
                },
                "checker_names": checker_names,
                "create_time": task_report.create_time,
                "finish_time": task_report.finish_time,
            },
            "artifacts": {
                "report_md": os.path.join(output_path, "report.md"),
                "sample_results": os.path.join(output_path, "sample_results.json"),
            },
        }
