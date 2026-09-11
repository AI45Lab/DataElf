from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ErrorDescriptor:
    """Stable public error contract for asynchronous jobs and HTTP failures."""

    category: str
    reason: str
    message: str
    retryable: bool
    action: str
    stage: str
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "reason": self.reason,
            "message": self.message,
            "retryable": self.retryable,
            "action": self.action,
            "stage": self.stage,
            "details": self.details,
        }


_INTENT_REASONS = {
    "invalid_query": "invalid_instruction",
    "invalid_scope": "invalid_instruction",
    "unsupported_scope": "unsupported_instruction",
    "module_required": "module_required",
    "time_range_not_supported": "time_range_not_supported",
    "invalid_time_expression": "invalid_instruction",
}

_ARTIFACT_REASONS = {
    "invalid_insight_count": "empty_insights",
    "insight_format_invalid": "schema_invalid",
    "quality_review_failed": "quality_review_failed",
    "pi_event_parse_error": "invalid_agent_output",
}

_SERVICE_REASONS = {
    "invalid_pi_config": "configuration_invalid",
    "pi_binary_not_found": "dependency_missing",
    "service_restarted": "service_restarted",
    "service_shutdown": "service_shutdown",
}


def classify_job_error(code: str, message: str, stage: str) -> ErrorDescriptor:
    """Translate internal/legacy errors into the stable public taxonomy.

    Classification intentionally considers both the producer code and message:
    several Pi/Ontology adapters historically collapsed model failures into a
    generic process or stage error.
    """

    raw_code = _normalize(code)
    text = f"{code} {message}".lower()

    if raw_code == "intent_model_failed":
        return ErrorDescriptor("model_error", "intent_recognition_failed",
            "意图识别模型调用失败或返回了无效字段，请重试。", True, "retry", stage)

    # Current core codes are mapped explicitly before legacy prefix heuristics.
    if raw_code in {"ai_index_modeling_raw_acquisition_failed", "ai_index_modeling_raw_empty"}:
        raw_code = "no_source_data" if raw_code.endswith("raw_empty") else "ai_index_acquisition_failed"
    elif raw_code.startswith("ai_index_modeling_") or raw_code == "server_modeling_failed":
        reason = "ontology_review_rejected" if "review" in raw_code else "ontology_build_failed"
        return ErrorDescriptor("analysis_error", reason, _analysis_message(reason),
            reason != "ontology_review_rejected", "retry" if reason != "ontology_review_rejected" else "revise_request", stage)
    raw_code = {
        "output_contract_failed": "invalid_insights",
        "stage_artifact_invalid": "invalid_insights",
        "domain_review_failed": "quality_review_failed",
        "run_cancelled": "service_shutdown",
        "server_configuration_invalid": "invalid_pi_config",
    }.get(raw_code, raw_code)


    if raw_code == "internal_error":
        if stage == "fetching_ai_index" and _has_transport_failure(text):
            reason = "request_timeout" if _has_timeout(text) else "connection_failed"
            return ErrorDescriptor(
                "source_error",
                reason,
                _source_message(reason),
                True,
                "retry",
                stage,
            )
        if stage == "analyzing_with_pi" and _has_transport_failure(text):
            reason = "request_timeout" if _has_timeout(text) else "model_terminated"
            return ErrorDescriptor(
                "model_error",
                reason,
                _model_message(reason),
                True,
                "retry_or_switch_model",
                stage,
            )
        return ErrorDescriptor(
            "service_error",
            "internal_error",
            _service_message("internal_error"),
            True,
            "retry",
            stage,
        )

    if raw_code in _INTENT_REASONS:
        reason = _INTENT_REASONS[raw_code]
        return ErrorDescriptor(
            "intent_error",
            reason,
            _intent_message(reason),
            False,
            "revise_request",
            stage,
        )

    if raw_code in _SERVICE_REASONS:
        reason = _SERVICE_REASONS[raw_code]
        return ErrorDescriptor(
            "service_error",
            reason,
            _service_message(reason),
            reason in {"service_restarted", "service_shutdown", "internal_error"},
            "retry" if reason != "configuration_invalid" else "contact_administrator",
            stage,
        )

    if _is_model_error(raw_code, text):
        reason = _model_reason(raw_code, text)
        return ErrorDescriptor(
            "model_error",
            reason,
            _model_message(reason),
            reason not in {"authentication_failed", "invalid_request"},
            (
                "contact_administrator"
                if reason == "authentication_failed"
                else "retry_or_switch_model"
            ),
            stage,
        )

    if raw_code.startswith("ai_index_") or raw_code in {
        "no_source_data",
        "pi_ontology_raw_acquisition_failed",
        "pi_ontology_raw_empty",
    }:
        reason = _source_reason(raw_code, text)
        return ErrorDescriptor(
            "source_error",
            reason,
            _source_message(reason),
            reason not in {"authentication_failed", "no_data", "invalid_request"},
            _source_action(reason),
            stage,
        )

    if raw_code in {"pi_process_timeout", "pi_process_nonzero_exit"}:
        reason = _analysis_reason(raw_code, text)
        return ErrorDescriptor(
            "analysis_error",
            reason,
            _analysis_message(reason),
            True,
            "retry",
            stage,
        )

    if (
        raw_code == "invalid_insights"
        or raw_code in _ARTIFACT_REASONS
        or _looks_like_artifact_error(text)
    ):
        reason = _ARTIFACT_REASONS.get(raw_code) or _artifact_reason(text)
        return ErrorDescriptor(
            "artifact_error",
            reason,
            _artifact_message(reason),
            reason not in {"schema_invalid", "source_reference_invalid"},
            "retry" if reason != "source_reference_invalid" else "revise_request",
            stage,
        )

    if raw_code.startswith("pi_ontology_") or raw_code.startswith("pi_"):
        reason = _analysis_reason(raw_code, text)
        return ErrorDescriptor(
            "analysis_error",
            reason,
            _analysis_message(reason),
            reason not in {"ontology_review_rejected"},
            "retry" if reason != "ontology_review_rejected" else "revise_request",
            stage,
        )

    return ErrorDescriptor(
        "service_error",
        "internal_error",
        "服务处理任务时发生内部错误，请稍后重试。",
        True,
        "retry",
        stage,
    )


def request_error(
    *, reason: str, message: str, stage: str = "request", retryable: bool = False,
    action: str = "revise_request", details: dict[str, Any] | list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "category": "request_error",
        "reason": reason,
        "message": message,
        "retryable": retryable,
        "action": action,
        "stage": stage,
        "details": details,
    }


def service_error(*, message: str) -> dict[str, Any]:
    return ErrorDescriptor(
        "service_error",
        "internal_error",
        message,
        True,
        "retry",
        "request",
    ).to_dict()


def infer_failure_stage(progress: int, stored_stage: str | None = None) -> str:
    if stored_stage and stored_stage != "failed":
        return stored_stage
    if progress >= 90:
        return "validating_insights"
    if progress >= 40:
        return "analyzing_with_pi"
    if progress >= 30:
        return "source_ready"
    if progress >= 15:
        return "fetching_ai_index"
    if progress >= 5:
        return "preparing_workspace"
    return "queued"


def _normalize(value: str) -> str:
    return value.strip().lower().split(":", 1)[0]


def _has_timeout(text: str) -> bool:
    return "timeout" in text or "timed out" in text


def _has_transport_failure(text: str) -> bool:
    return _has_timeout(text) or any(
        value in text
        for value in ("connection", "reset by peer", "network", "unreachable")
    )


def _is_model_error(code: str, text: str) -> bool:
    if code == "pi_model_error":
        return True
    markers = (
        "pi_model_error",
        "model error",
        "model_error",
        "model request",
        "openai",
        "maximum retry",
        "max retries",
        "max_retries",
    )
    return any(marker in text for marker in markers)


def _model_reason(code: str, text: str) -> str:
    if any(value in text for value in ("401", "403", "unauthorized", "api key", "authentication")):
        return "authentication_failed"
    if "429" in text or "rate limit" in text:
        return "rate_limited"
    if "timeout" in text or "timed out" in text:
        return "request_timeout"
    if "max" in text and "retr" in text:
        return "max_retries_exceeded"
    if any(value in text for value in ("terminated", "connection reset", "connection aborted")):
        return "model_terminated"
    if "invalid" in text or "400" in text:
        return "invalid_request"
    return "model_unavailable"


def _source_reason(code: str, text: str) -> str:
    if code == "ai_index_network_error":
        return "request_timeout" if _has_timeout(text) else "connection_failed"
    if code in {"no_source_data", "pi_ontology_raw_empty"} or "no data" in text or "non-zero total record" in text:
        return "no_data"
    if any(value in text for value in ("invalid_api_key", "401", "403", "unauthorized", "api key format", "authentication")):
        return "authentication_failed"
    if "429" in text or "rate limit" in text:
        return "rate_limited"
    if "timeout" in text or "timed out" in text:
        return "request_timeout"
    if "max" in text and "retr" in text:
        return "max_retries_exceeded"
    if "invalid_response" in code or "invalid response" in text or "non-json" in text:
        return "invalid_response"
    if any(value in text for value in ("network", "connection", "reset by peer", "unreachable")):
        return "connection_failed"
    if re.search(r"http_[45]\d\d", code):
        return "upstream_error"
    return "acquisition_failed"


def _source_action(reason: str) -> str:
    if reason == "authentication_failed":
        return "contact_administrator"
    if reason == "no_data":
        return "revise_request"
    return "retry"


def _looks_like_artifact_error(text: str) -> bool:
    markers = (
        "insight_candidates.json",
        "invalid json",
        "required schema",
        "missing required",
        "contains no insight",
        "quality review",
    )
    return any(marker in text for marker in markers)


def _artifact_reason(text: str) -> str:
    if "contains no insight" in text:
        return "empty_insights"
    if "missing" in text:
        return "output_missing"
    if "invalid json" in text or "not valid json" in text:
        return "invalid_json"
    if "source" in text and ("reference" in text or "url" in text):
        return "source_reference_invalid"
    if "quality review" in text:
        return "quality_review_failed"
    return "schema_invalid"


def _analysis_reason(code: str, text: str) -> str:
    if code == "pi_output_incomplete":
        return "insight_extraction_failed"
    if code == "pi_process_timeout" or "cli timed out" in text:
        return "agent_timeout"
    if "review" in text and any(value in text for value in ("reject", "unusable", "terminal_failed")):
        return "ontology_review_rejected"
    if "stage1" in code or "stage1" in text or "ontology" in code:
        return "ontology_build_failed"
    if "insight" in text:
        return "insight_extraction_failed"
    if code.startswith("pi_process_nonzero_exit"):
        return "agent_process_failed"
    return "agent_analysis_failed"


def _intent_message(reason: str) -> str:
    messages = {
        "invalid_instruction": "指令内容不完整，请重新输入指令。",
        "unsupported_instruction": "无法识别摘要类型，请重新输入受支持的指令。",
        "module_required": "请在指令中指定快讯、观点、开源社区或传播模块。",
        "time_range_not_supported": "暂不支持时间范围，请移除时间范围后重新输入指令。",
    }
    return messages[reason]


def _source_message(reason: str) -> str:
    messages = {
        "authentication_failed": "数据源认证失败，请联系服务管理员检查 AI Index 配置。",
        "request_timeout": "数据源请求超时，请稍后重试。",
        "rate_limited": "数据源请求达到限流，请稍后重试。",
        "invalid_response": "数据源返回了无法识别的响应，请稍后重试。",
        "connection_failed": "暂时无法连接数据源，请稍后重试。",
        "max_retries_exceeded": "数据源请求达到最大重试次数，请稍后重试。",
        "no_data": "AI Index 未返回可用于分析的数据，请调整指令后重试。",
        "upstream_error": "数据源服务返回错误，请稍后重试。",
        "acquisition_failed": "数据获取失败，请稍后重试。",
    }
    return messages[reason]


def _model_message(reason: str) -> str:
    messages = {
        "authentication_failed": "模型认证失败，请联系服务管理员检查模型配置。",
        "request_timeout": "模型调用超时，请重试或切换模型。",
        "rate_limited": "模型调用达到限流，请稍后重试或切换模型。",
        "model_terminated": "模型调用被中止，请重试或切换模型。",
        "max_retries_exceeded": "模型调用达到最大重试次数，请重试或切换模型。",
        "invalid_request": "模型拒绝了当前请求，请联系服务管理员检查模型配置。",
        "model_unavailable": "模型服务暂时不可用，请重试或切换模型。",
    }
    return messages[reason]


def _analysis_message(reason: str) -> str:
    messages = {
        "agent_timeout": "Agent 分析超时，请重试。",
        "agent_process_failed": "Agent 进程异常结束，请重试。",
        "agent_analysis_failed": "Agent 未能完成分析，请重试。",
        "ontology_build_failed": "Ontology 构建未完成，请重试。",
        "ontology_review_rejected": "Ontology 质量检查未通过，请调整指令后重试。",
        "insight_extraction_failed": "Insight 提取未完成，请重试。",
    }
    return messages[reason]


def _artifact_message(reason: str) -> str:
    messages = {
        "output_missing": "分析已运行，但缺少必需的结果文件，请重试。",
        "invalid_json": "分析结果不是有效 JSON，请重试。",
        "schema_invalid": "分析结果格式不符合接口要求，请重试。",
        "source_reference_invalid": "Insight 来源信息不完整，请调整指令后重试。",
        "empty_insights": "分析完成但未生成有效 Insight，请重试。",
        "invalid_agent_output": "Agent 返回内容无法解析，请重试。",
        "quality_review_failed": "Insight 质量检查未通过，请重试。",
    }
    return messages[reason]


def _service_message(reason: str) -> str:
    messages = {
        "internal_error": "服务处理任务时发生内部错误，请稍后重试。",
        "configuration_invalid": "服务配置不完整，请联系服务管理员。",
        "dependency_missing": "服务运行依赖缺失，请联系服务管理员。",
        "service_restarted": "服务重启导致任务中断，请重新提交或重试任务。",
        "service_shutdown": "服务停止导致任务中断，请重新提交或重试任务。",
    }
    return messages[reason]
