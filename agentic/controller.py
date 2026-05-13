from __future__ import annotations

import ast
from difflib import get_close_matches
import hashlib
import inspect
import json
import re
from time import perf_counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from agent import create_agent_adapter, load_tool_readme_entries, resolve_tool_readme_path
from llm.provider import LLMProvider
from llm.tracing import llm_trace_context
from runtime import JobManager, JobStatus, RuntimeExecutor
from tools import ToolRegistry


# Scorer aliases that map to data_scoring / data_select tools.
# Used by _check_tool_availability to detect when a scorer name in the task
# implies a missing tool dependency.
_SCORER_ALIASES: dict[str, list[str]] = {
    "data_scoring": [
        "ppl", "perplexity", "norm_loss", "ifd", "instruction_following",
        "deita", "deita_quality", "deita_complexity", "deberta", "fineweb",
        "textbook", "llm_judge", "ask_llm", "oda",
    ],
    "data_select": [
        "diversity", "cluster", "kmeans", "topk", "select",
    ],
}


@dataclass
class CoordinationContext:
    task: str
    dataset_schemas: dict[str, list[str]]
    tool_schemas: list[dict[str, Any]]
    model: str


class RunCoordinator:
    def __init__(
        self,
        config: Any,
        job_manager: JobManager,
        executor: RuntimeExecutor,
        registry: ToolRegistry,
        llm_provider: LLMProvider | None = None,
    ) -> None:
        self.config = config
        self.job_manager = job_manager
        self.executor = executor
        self.registry = registry
        self.llm_provider = llm_provider

    def maybe_request_clarification(
        self,
        task: str,
        dataset_schemas: dict[str, list[str]],
        ask_user: bool,
        max_rounds: int = 5,
        job_id: str | None = None,
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if self.llm_provider is None or not ask_user:
            return {
                "status": "not_requested",
                "clarification_turns": 0,
                "clarification_transcript": [],
                "resolved_task": task,
                "resolved_slots": {},
            }

        import click

        def decision_provider(
            current_task: str,
            transcript: list[dict[str, Any]],
            messages: list[dict[str, str]],
            turn: int,
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            self._emit(event_handler, {
                "type": "stage_started",
                "mode": "run",
                "job_id": job_id,
                "stage": "clarification_llm",
                "turn": turn,
                "model": self.config.agent.model,
            })
            decision, llm_meta = self._clarification_decision(
                task=task,
                current_task=current_task,
                transcript=transcript,
                messages=messages,
                dataset_schemas=dataset_schemas,
                job_id=job_id,
            )
            self._emit(event_handler, {
                "type": "stage_completed",
                "mode": "run",
                "job_id": job_id,
                "stage": "clarification_llm",
                "turn": turn,
                "model": llm_meta.get("model"),
                "llm": llm_meta,
                "status": decision.get("status"),
                "missing_items": decision.get("missing_items", []),
            })
            return decision, llm_meta

        def response_provider(
            checkpoint_payload: dict[str, Any],
            turn: int,
            _llm_meta: dict[str, Any],
            _current_task: str,
        ) -> dict[str, Any]:
            click.echo(f"Clarification turn {turn}/{max_rounds}")
            click.echo(checkpoint_payload["prompt"])
            click.echo("> ", nl=False)
            return {"decision": "answer", "answer": (input() or "").strip()}

        clarification = _run_shared_clarification_loop(
            self,
            task=task,
            dataset_schemas=dataset_schemas,
            max_rounds=max_rounds,
            decision_provider=decision_provider,
            response_provider=response_provider,
            ready_status="ready",
            not_requested_status="not_requested",
            paused_status=None,
            exhausted_status="escalate_to_pilot",
            exhausted_reason="Clarification exceeded 5 turns without converging. Switch to `elf pilot`.",
            allow_escalation=True,
        )
        return {
            "status": clarification["status"],
            "clarification_turns": clarification["turns"],
            "clarification_transcript": clarification["transcript"],
            "resolved_task": clarification["resolved_task"],
            "resolved_slots": clarification["resolved_slots"],
            **({"handoff_reason": clarification.get("handoff_reason")} if clarification.get("handoff_reason") else {}),
        }

    def execute(
        self,
        task: str,
        dataset_schemas: dict[str, list[str]],
        ask_user: bool,
        verbose: bool,
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        job = self.job_manager.create_job(task, mode="run")

        # Fast-fail: check if any tool mentioned in the task is not registered.
        # Do this before any LLM call to avoid wasting API quota and give a clear error.
        tool_gap = self._check_tool_availability(task)
        if tool_gap:
            capability_gap = {
                "type": "tool_not_available",
                "reason": tool_gap["reason"],
                "missing_tools": tool_gap["missing_tools"],
                "fix_hint": tool_gap["fix_hint"],
            }
            self.job_manager.update_capability_gap(job.job_id, capability_gap)
            self.job_manager.update_result(job.job_id, {"metadata": {"tool_availability_check": tool_gap}})
            self.job_manager.update_error(job.job_id, tool_gap["reason"])
            return {
                "job_id": job.job_id,
                "status": "needs_pilot",
                "pipeline": "",
                "llm_metadata": {},
                "execution": {"success": False, "result": None, "error": tool_gap["reason"]},
                "clarification": {
                    "status": "escalate_to_pilot",
                    "clarification_turns": 0,
                    "clarification_transcript": [],
                    "resolved_task": task,
                    "resolved_slots": {},
                    "handoff_reason": tool_gap["reason"],
                },
                "capability_gap": capability_gap,
                "verbose": verbose,
            }

        self._emit(event_handler, {
            "type": "stage_started",
            "mode": "run",
            "job_id": job.job_id,
            "stage": "clarification",
            "interactive": ask_user,
        })
        clarification = self.maybe_request_clarification(
            task=task,
            dataset_schemas=dataset_schemas,
            ask_user=ask_user,
            job_id=job.job_id,
            event_handler=event_handler,
        )
        self._emit(event_handler, {
            "type": "stage_completed",
            "mode": "run",
            "job_id": job.job_id,
            "stage": "clarification",
            "status": clarification.get("status"),
            "turns": clarification.get("clarification_turns", 0),
        })
        effective_task = clarification["resolved_task"]
        self.job_manager.update_clarification(
            job.job_id,
            status=clarification["status"],
            turns=clarification["clarification_turns"],
            transcript=clarification["clarification_transcript"],
            resolved_task=clarification["resolved_task"],
            resolved_slots=clarification["resolved_slots"],
        )

        if clarification["status"] == "escalate_to_pilot":
            capability_gap = {
                "type": "clarification_needs_pilot",
                "reason": clarification.get("handoff_reason", "Clarification did not converge."),
                "recommended_command": "elf pilot",
            }
            self.job_manager.update_capability_gap(job.job_id, capability_gap)
            self.job_manager.update_result(job.job_id, {
                "metadata": {
                    "clarification_status": clarification["status"],
                    "clarification_turns": clarification["clarification_turns"],
                    "clarification_transcript": clarification["clarification_transcript"],
                    "resolved_task": clarification["resolved_task"],
                    "resolved_slots": clarification["resolved_slots"],
                }
            })
            self.job_manager.update_error(job.job_id, capability_gap["reason"])
            return {
                "job_id": job.job_id,
                "status": "needs_pilot",
                "pipeline": "",
                "llm_metadata": {},
                "execution": {"success": False, "result": None, "error": capability_gap["reason"]},
                "clarification": clarification,
                "capability_gap": capability_gap,
                "verbose": verbose,
            }

        agent = create_agent_adapter(
            self.config,
            _visible_tool_schemas(self.config, self.registry),
            dataset_schemas,
            llm_provider=self.llm_provider,
        )
        self._emit(event_handler, {
            "type": "stage_started",
            "mode": "run",
            "job_id": job.job_id,
            "stage": "pipeline_generation",
            "model": self.config.agent.model,
        })
        with llm_trace_context(job_id=job.job_id, mode="run", scope="core", caller="pipeline_generator"):
            pipeline, llm_metadata = agent.generate_pipeline(effective_task)
        pipeline = self._stabilize_run_pipeline(effective_task, pipeline)
        self._emit(event_handler, {
            "type": "stage_completed",
            "mode": "run",
            "job_id": job.job_id,
            "stage": "pipeline_generation",
            "model": llm_metadata.get("model"),
            "llm": llm_metadata,
        })
        self.job_manager.update_pipeline(job.job_id, pipeline)

        write_approval = self._maybe_request_write_approval(
            job_id=job.job_id,
            pipeline=pipeline,
            ask_user=ask_user,
            event_handler=event_handler,
        )
        if write_approval.get("decision") == "answer" and write_approval.get("answer"):
            revised = _apply_revised_write_targets_to_pipeline(
                pipeline,
                write_approval.get("paths", []),
                write_approval["answer"],
            )
            if revised is not None:
                pipeline, revised_paths = revised
                self.job_manager.update_pipeline(job.job_id, pipeline)
                write_approval = {"decision": "allow", "paths": revised_paths}
        if write_approval.get("decision") != "allow":
            capability_gap = {
                "type": "write_approval_required" if write_approval.get("decision") == "defer" else "write_denied",
                "reason": write_approval.get("reason", "External write requires approval."),
                "requested_paths": write_approval.get("paths", []),
                "recommended_command": "Re-run interactively and approve the write operation.",
            }
            self.job_manager.update_capability_gap(job.job_id, capability_gap)
            self.job_manager.update_result(job.job_id, {
                "metadata": {
                    "clarification_status": clarification["status"],
                    "clarification_turns": clarification["clarification_turns"],
                    "clarification_transcript": clarification["clarification_transcript"],
                    "resolved_task": clarification["resolved_task"],
                    "resolved_slots": clarification["resolved_slots"],
                    "write_approval": write_approval,
                }
            })
            self.job_manager.update_error(job.job_id, capability_gap["reason"])
            return {
                "job_id": job.job_id,
                "status": "failed",
                "pipeline": pipeline,
                "llm_metadata": llm_metadata,
                "execution": {"success": False, "result": None, "error": capability_gap["reason"]},
                "clarification": clarification,
                "capability_gap": capability_gap,
                "verbose": verbose,
            }

        self._emit(event_handler, {
            "type": "stage_started",
            "mode": "run",
            "job_id": job.job_id,
            "stage": "execution",
        })
        result = self.executor.execute(job.job_id, pipeline)
        self._emit(event_handler, {
            "type": "stage_completed",
            "mode": "run",
            "job_id": job.job_id,
            "stage": "execution",
            "success": result.get("success"),
            "error": result.get("error"),
        })
        capability_gap: dict[str, Any] = {}
        if not result["success"]:
            self._emit(event_handler, {
                "type": "stage_started",
                "mode": "run",
                "job_id": job.job_id,
                "stage": "capability_gap_judge",
                "model": self.config.agent.model,
            })
            with llm_trace_context(job_id=job.job_id, mode="run", scope="core", caller="capability_gap_judge"):
                capability_gap = self._judge_capability_gap(effective_task, pipeline, result)
            self._emit(event_handler, {
                "type": "stage_completed",
                "mode": "run",
                "job_id": job.job_id,
                "stage": "capability_gap_judge",
                "capability_gap": capability_gap,
            })
            if capability_gap:
                self.job_manager.update_capability_gap(job.job_id, capability_gap)

        existing_result = self.job_manager.get_job(job.job_id).result if self.job_manager.get_job(job.job_id) else {}
        merged_result = existing_result.copy() if isinstance(existing_result, dict) else {}
        merged_metadata = dict(merged_result.get("metadata", {}))
        merged_metadata.update({
            "clarification_status": clarification["status"],
            "clarification_turns": clarification["clarification_turns"],
            "clarification_transcript": clarification["clarification_transcript"],
            "resolved_task": clarification["resolved_task"],
            "resolved_slots": clarification["resolved_slots"],
        })
        merged_result["metadata"] = merged_metadata
        self.job_manager.update_result(job.job_id, merged_result)

        return {
            "job_id": job.job_id,
            "status": "completed" if result["success"] else "failed",
            "pipeline": pipeline,
            "llm_metadata": llm_metadata,
            "execution": result,
            "clarification": clarification,
            "capability_gap": capability_gap,
            "verbose": verbose,
        }

    def _maybe_request_write_approval(
        self,
        job_id: str,
        pipeline: str,
        ask_user: bool,
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        paths = _extract_external_write_targets(pipeline)
        if not paths:
            return {"decision": "allow", "paths": []}

        prompt = _build_write_approval_prompt(paths)
        payload = {"prompt": prompt, "paths": paths}
        self._emit(event_handler, {
            "type": "checkpoint_paused",
            "mode": "run",
            "job_id": job_id,
            "checkpoint_type": "write_approval",
            "payload": payload,
        })

        if not ask_user:
            return {
                "decision": "defer",
                "paths": paths,
                "reason": "External write requires interactive approval before execution.",
            }

        import click

        click.echo("")
        click.echo(prompt)
        raw_answer = click.prompt("> ", prompt_suffix="", default="deny", show_default=False).strip()
        normalized_answer = raw_answer.lower()
        if normalized_answer in {"allow", "yes", "y", "approve"}:
            decision = "allow"
        elif normalized_answer in {"deny", "no", "n"}:
            decision = "deny"
        elif raw_answer:
            decision = "answer"
        else:
            decision = "deny"
        self._emit(event_handler, {
            "type": "checkpoint_resolved",
            "mode": "run",
            "job_id": job_id,
            "checkpoint_type": "write_approval",
            "response": {"decision": decision, "paths": paths, "answer": raw_answer},
        })
        if decision == "allow":
            return {"decision": "allow", "paths": paths}
        if decision == "answer":
            return {"decision": "answer", "paths": paths, "answer": raw_answer}
        return {
            "decision": "deny",
            "paths": paths,
            "reason": "User denied external write approval.",
        }

    def _stabilize_run_pipeline(self, task: str, pipeline: str) -> str:
        pipeline = _stabilize_pipeline_logging(pipeline)
        if _is_broad_security_audit_task(task):
            pipeline = _stabilize_security_audit_result_logging(pipeline)
        return pipeline

    def _clarification_decision(
        self,
        task: str,
        current_task: str,
        transcript: list[dict[str, Any]],
        messages: list[dict[str, str]],
        dataset_schemas: dict[str, list[str]],
        job_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        fallback = {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": current_task,
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        }
        fallback_llm = {
            "stage": "clarification",
            "status": "disabled" if self.llm_provider is None else "fallback",
            "model": self.config.agent.model,
            "elapsed_seconds": None,
            "error": None,
        }
        if self.llm_provider is None:
            return fallback, fallback_llm

        prompt = _build_clarification_prompt(
            task=task,
            current_task=current_task,
            transcript=transcript,
            messages=messages,
            dataset_schemas=dataset_schemas,
            tool_schemas=_visible_tool_schemas(self.config, self.registry),
            tool_shortlist=_select_relevant_tool_schemas(
                task,
                self.registry,
                allowed_tool_names=_configured_tool_names(self.config),
            ),
            security_hints=_build_security_audit_hints(
                task,
                self.registry,
            ),
            tool_readmes=self._clarification_tool_readmes(task),
        )
        start_time = perf_counter()
        try:
            with llm_trace_context(job_id=job_id, mode="run", scope="core", caller="clarification"):
                decision = self.llm_provider.generate_json(self.config.agent.model, prompt)
        except Exception as e:
            fallback_llm["elapsed_seconds"] = round(perf_counter() - start_time, 2)
            fallback_llm["error"] = f"{type(e).__name__}: {e}"
            return fallback, fallback_llm

        status = _coerce_text_block(decision.get("status")) or "clarifying"
        assistant_message = _coerce_text_block(decision.get("assistant_message"))
        response_mode = _coerce_text_block(decision.get("response_mode")) or "ask_user"
        if response_mode not in {"ask_user", "answer_then_ask"}:
            response_mode = "ask_user"
        if status not in {"clarifying", "ready", "escalate_to_pilot"}:
            status = "clarifying"

        parsed = {
            "status": status,
            "assistant_message": assistant_message,
            "ready_to_execute": bool(decision.get("ready_to_execute", status == "ready")),
            "resolved_task": _coerce_text_block(decision.get("resolved_task")) or current_task,
            "resolved_slots": decision.get("resolved_slots", {}) if isinstance(decision.get("resolved_slots", {}), dict) else {},
            "missing_items": decision.get("missing_items", []),
            "suggested_defaults": decision.get("suggested_defaults", {}),
            "response_mode": response_mode,
            "handoff_reason": _coerce_text_block(decision.get("handoff_reason")),
        }
        if parsed["ready_to_execute"]:
            parsed["status"] = "ready"
        return parsed, {
            "stage": "clarification",
            "status": "success",
            "model": self.config.agent.model,
            "elapsed_seconds": round(perf_counter() - start_time, 2),
            "error": None,
        }

    def _clarification_tool_readmes(self, task: str) -> list[dict[str, str]]:
        if not getattr(self.config.agent, "include_tool_readmes", False):
            return []
        shortlist = _select_relevant_tool_schemas(
            task,
            self.registry,
            allowed_tool_names=_configured_tool_names(self.config),
        )
        tool_names = [schema.get("name", "") for schema in shortlist if schema.get("name")]
        max_len = getattr(self.config.agent, "tool_readmes_max_length", 2000)
        return load_tool_readme_entries(tool_names, max_len=max_len)

    def _judge_capability_gap(self, task: str, pipeline: str, execution: dict[str, Any]) -> dict[str, Any]:
        fallback = {
            "type": "tooling_or_pipeline_gap",
            "reason": execution.get("error", "Unknown failure"),
            "recommended_command": "elf pilot",
        }
        if self.llm_provider is None:
            return fallback

        prompt = _build_capability_gap_prompt(task, pipeline, execution)
        try:
            with llm_trace_context(scope="core", caller="capability_gap_judge"):
                response = self.llm_provider.generate_json(self.config.agent.model, prompt)
        except Exception:
            return fallback

        response.setdefault("recommended_command", "elf pilot")
        return response

    def _emit(
        self,
        event_handler: Callable[[dict[str, Any]], None] | None,
        event: dict[str, Any],
    ) -> None:
        if event_handler is not None:
            event_handler(event)

    def _check_tool_availability(self, task: str) -> dict[str, Any] | None:
        """Fast-fail check: if the task references a tool that is not registered, return gap info."""
        # All known built-in tool identifiers (registry name -> class name)
        _BUILTIN_TOOLS: dict[str, str] = {
            "security_audit": "SecurityAuditTool",
            "data_scoring": "DataScoringTool",
            "data_select": "DataSelectTool",
            "enzyme_acquire": "EnzymeAcquireTool",
            "protein_analyzer": "ProteinAnalyzerTool",
            "skillrl_skill_extraction": "SkillRLSkillExtractionTool",
        }

        registered = set(self.registry.list_tools())
        task_lower = task.lower()
        mentioned_tools: list[str] = []

        for tool_name, class_name in _BUILTIN_TOOLS.items():
            if tool_name in task_lower or class_name.lower() in task_lower:
                if tool_name not in registered:
                    mentioned_tools.append(f"{tool_name} ({class_name})")
            # Only flag scorer aliases when the parent tool itself is NOT registered.
            # If the tool is registered, its scorers are available at runtime.
            if tool_name not in registered and tool_name in ("data_scoring", "data_select"):
                for alias in _SCORER_ALIASES.get(tool_name, []):
                    if alias in task_lower:
                        mentioned_tools.append(f"{alias} (scorer inside {tool_name})")

        if not mentioned_tools:
            return None

        fix_parts: list[str] = []
        needs_scoring = any("data_scoring" in t or "data_select" in t for t in mentioned_tools)
        needs_scitools = any("enzyme_acquire" in t or "protein_analyzer" in t for t in mentioned_tools)
        if needs_scoring:
            fix_parts.append('pip install -e ".[scoring]"  # data_scoring + data_select tools')
        if needs_scitools:
            fix_parts.append('pip install -e ".[scitools]"  # enzyme_acquire + protein_analyzer tools')

        fix_hint = (
            "Install the missing dependency group(s):\n  "
            + "\n  ".join(fix_parts)
            if fix_parts
            else "Remove the unavailable tool from your config or install its dependencies."
        )

        missing_summary = ", ".join(mentioned_tools)
        reason = (
            f"The following tool(s) are referenced in your task but are not available: "
            f"{missing_summary}. This usually means the required dependencies are not installed."
        )

        return {
            "missing_tools": mentioned_tools,
            "reason": reason,
            "fix_hint": fix_hint,
        }

    def _default_slots_for_missing_items(
        self,
        task: str,
        missing_items: list[str],
    ) -> dict[str, Any]:
        defaults: dict[str, Any] = {}
        if "checker_names" in missing_items and _task_targets_security_checker_selection(task):
            security_hints = _build_security_audit_hints(
                task,
                self.registry,
            ) or {}
            baseline = security_hints.get("quick_baseline_checkers")
            if baseline:
                defaults["checker_names"] = baseline
        defaults |= _default_slots_from_required_specs(
            _schema_required_slot_specs(task, self.registry, {}),
            missing_items,
        )
        return defaults

    def _guard_clarification_ready(
        self,
        task: str,
        decision: dict[str, Any],
        resolved_slots: dict[str, Any],
        outstanding_missing_items: list[str],
        last_user_reply: str,
        dataset_schemas: dict[str, list[str]],
        required_slot_specs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        if (
            not outstanding_missing_items
            and _slot_value_from_resolved_slots("dataset_name", decision.get("resolved_slots", {})) in (None, "", [], {})
            and _slot_value_from_resolved_slots("dataset_name", resolved_slots) in (None, "", [], {})
            and _requires_dataset_clarification(task, dataset_schemas)
        ):
            return {
                "allow_ready": False,
                "followup_message": _build_dataset_options_message(dataset_schemas),
                "missing_items": ["dataset_name"],
                "suggested_defaults": {},
            }

        if (
            _looks_like_option_request(last_user_reply)
            and "checker_names" in outstanding_missing_items
            and _task_targets_security_checker_selection(task)
        ):
            default_slots = self._default_slots_for_missing_items(task, ["checker_names"])
            return {
                "allow_ready": False,
                "followup_message": self._build_security_checker_recommendation_message(task, last_user_reply),
                "missing_items": ["checker_names"],
                "suggested_defaults": default_slots,
            }

        if (
            not outstanding_missing_items
            and _slot_value_from_resolved_slots("checker_names", decision.get("resolved_slots", {})) in (None, "", [], {})
            and _slot_value_from_resolved_slots("checker_names", resolved_slots) in (None, "", [], {})
            and _requires_security_checker_clarification(task)
        ):
            default_slots = self._default_slots_for_missing_items(task, ["checker_names"])
            return {
                "allow_ready": False,
                "followup_message": (
                    "Please specify the custom checker_names you would like to use for the security audit."
                ),
                "missing_items": ["checker_names"],
                "suggested_defaults": default_slots,
            }

        field_issue = _find_dataset_field_reference_issue(
            task_text=decision.get("resolved_task", task) or task,
            last_user_reply=last_user_reply,
            resolved_slots=resolved_slots | _filter_resolved_slots_by_missing_items(
                decision.get("resolved_slots", {}),
                decision.get("missing_items", []),
            ),
            dataset_schemas=dataset_schemas,
        )
        if field_issue:
            return {
                "allow_ready": False,
                "followup_message": field_issue["message"],
                "missing_items": ["filter_field", "filter_value"],
                "suggested_defaults": {},
            }

        missing_after_decision = _find_unresolved_missing_items(
            outstanding_missing_items,
            resolved_slots,
            {},
            weak_reply=False,
        )
        if not missing_after_decision:
            return {"allow_ready": True, "followup_message": ""}

        if "dataset_name" in missing_after_decision:
            return {
                "allow_ready": False,
                "followup_message": _build_dataset_options_message(dataset_schemas)
                if _looks_like_dataset_option_request(last_user_reply)
                else "I still need the dataset name. You can ask for available datasets or provide one exact dataset name.",
                "missing_items": missing_after_decision,
                "suggested_defaults": {},
            }

        if "checker_names" in missing_after_decision and _task_targets_security_checker_selection(task):
            if _looks_like_recommendation_request(last_user_reply):
                return {
                    "allow_ready": False,
                    "followup_message": self._build_security_checker_recommendation_message(task, last_user_reply),
                    "missing_items": missing_after_decision,
                    "suggested_defaults": self._default_slots_for_missing_items(task, missing_after_decision),
                }
            return {
                "allow_ready": False,
                "followup_message": (
                    "I still need the exact checker_names. "
                    "Use defaults or provide names like PIIRule, SecretRule, "
                    "HarmfulContentLLMJudge, ToxicityLLMJudge, or PIILLMJudge."
                ),
                "missing_items": missing_after_decision,
                "suggested_defaults": self._default_slots_for_missing_items(task, missing_after_decision),
            }

        return {
            "allow_ready": False,
            "followup_message": _build_missing_slot_followup_message(
                missing_after_decision,
                required_slot_specs,
                dataset_schemas,
            ),
            "missing_items": missing_after_decision,
            "suggested_defaults": self._default_slots_for_missing_items(task, missing_after_decision),
        }

    def _guard_clarification_escalation(
        self,
        task: str,
        decision: dict[str, Any],
        outstanding_missing_items: list[str],
        last_user_reply: str,
        turn: int,
        max_rounds: int,
        dataset_schemas: dict[str, list[str]],
        required_slot_specs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        if decision.get("status") != "escalate_to_pilot":
            return {"continue_clarifying": False}
        if turn >= max_rounds:
            return {"continue_clarifying": False}

        missing_items = outstanding_missing_items or decision.get("missing_items", [])
        normalized_missing = _normalize_missing_items(task, missing_items)
        if "dataset_name" in normalized_missing:
            return {
                "continue_clarifying": True,
                "followup_message": _build_dataset_options_message(dataset_schemas),
                "missing_items": ["dataset_name"],
                "suggested_defaults": {},
            }
        if "checker_names" in normalized_missing and _task_targets_security_checker_selection(task):
            default_slots = self._default_slots_for_missing_items(task, normalized_missing)
            if _looks_like_option_request(last_user_reply):
                return {
                    "continue_clarifying": True,
                    "followup_message": self._build_security_checker_recommendation_message(task, last_user_reply),
                    "missing_items": ["checker_names"],
                    "suggested_defaults": default_slots,
                }
            return {
                "continue_clarifying": True,
                "followup_message": (
                    "I still need the exact checker_names. "
                    "Use defaults or provide names like PIIRule, SecretRule, "
                    "HarmfulContentLLMJudge, ToxicityLLMJudge, or PIILLMJudge."
                ),
                "missing_items": ["checker_names"],
                "suggested_defaults": default_slots,
            }

        if normalized_missing:
            return {
                "continue_clarifying": True,
                "followup_message": _build_missing_slot_followup_message(
                    normalized_missing,
                    required_slot_specs,
                    dataset_schemas,
                ),
                "missing_items": normalized_missing,
                "suggested_defaults": self._default_slots_for_missing_items(task, normalized_missing),
            }

        return {"continue_clarifying": False}

    def _build_security_checker_recommendation_message(self, task: str, user_reply: str) -> str:
        recommendation_sets = self._security_checker_recommendation_sets(task)
        baseline = recommendation_sets["cheap_baseline"]
        balanced_plus = recommendation_sets["balanced"]
        stronger = recommendation_sets["stronger"]

        if _looks_like_accuracy_request(user_reply):
            return (
                "If accuracy and semantic coverage matter more, I recommend the stronger set "
                f"{', '.join(stronger)}. "
                "This is usually slower and more expensive than a rule-based baseline. "
                "You can reply with `use stronger recommendation`, `use balanced recommendation`, "
                "`use cheap baseline`, or provide exact checker names."
            )

        if _looks_like_cost_speed_request(user_reply):
            return (
                "For a cost/speed balance, I recommend the balanced set "
                f"{', '.join(balanced_plus)}. "
                f"The cheaper baseline is {', '.join(baseline)}. "
                "The balanced option keeps the cheap rules and adds one semantic LLM judge. "
                "You can reply with `use balanced recommendation`, `use cheap baseline`, "
                "`use stronger recommendation`, or provide exact checker names."
            )

        return (
            f"Cheap baseline: {', '.join(baseline)}. "
            f"Balanced recommendation: {', '.join(balanced_plus)}. "
            f"Stronger recommendation: {', '.join(stronger)}. "
            "You can reply with `use cheap baseline`, `use balanced recommendation`, "
            "`use stronger recommendation`, `use defaults`, or provide exact checker names."
        )

    def _security_checker_recommendation_sets(self, task: str) -> dict[str, list[str]]:
        hints = _build_security_audit_hints(
            task,
            self.registry,
        ) or {}
        baseline = hints.get("quick_baseline_checkers", ["PIIRule", "SecretRule"])
        llm_required = hints.get("llm_required_checkers", [])
        preferred_order = [
            "HarmfulContentLLMJudge",
            "ToxicityLLMJudge",
            "PIILLMJudge",
            "BiasLLMJudge",
            "SycophancyLLMJudge",
        ]
        preferred_llm = [name for name in preferred_order if name in llm_required]
        if not preferred_llm:
            preferred_llm = llm_required[:5]
        balanced = baseline + ([preferred_llm[0]] if preferred_llm else [])
        stronger = preferred_llm[:5] if preferred_llm else balanced
        return {
            "cheap_baseline": baseline,
            "balanced": _dedupe_preserve_order(balanced),
            "stronger": _dedupe_preserve_order(stronger),
        }

    def _resolve_security_checker_reply(
        self,
        task: str,
        missing_items: list[str],
        user_reply: str,
    ) -> tuple[list[str], str | None]:
        if "checker_names" not in missing_items or not _task_targets_security_checker_selection(task):
            return [], None
        normalized = re.sub(r"\s+", " ", user_reply.strip().lower())
        recommendation_sets = self._security_checker_recommendation_sets(task)
        if any(phrase in normalized for phrase in [
            "use balanced recommendation",
            "use the balanced recommendation",
            "use balanced option",
            "use the balanced option",
            "balanced recommendation",
            "balanced option",
        ]):
            return recommendation_sets["balanced"], "balanced_recommendation"
        if any(phrase in normalized for phrase in [
            "use stronger recommendation",
            "use the stronger recommendation",
            "use stronger option",
            "use the stronger option",
            "use the accurate option",
            "use the more accurate option",
        ]):
            return recommendation_sets["stronger"], "stronger_recommendation"
        if any(phrase in normalized for phrase in [
            "use cheap baseline",
            "use the cheap baseline",
            "use cheaper baseline",
            "use the cheaper baseline",
            "use cheap option",
            "use the cheap option",
            "use cheaper option",
            "use the cheaper option",
        ]):
            return recommendation_sets["cheap_baseline"], "cheap_baseline"
        return [], None


class PilotController:
    def __init__(
        self,
        config: Any,
        job_manager: JobManager,
        executor: RuntimeExecutor,
        registry: ToolRegistry,
        asset_manager: Any,
        llm_provider: LLMProvider | None = None,
    ) -> None:
        self.config = config
        self.job_manager = job_manager
        self.executor = executor
        self.registry = registry
        self.asset_manager = asset_manager
        self.llm_provider = llm_provider

    def execute(
        self,
        task: str,
        dataset_schemas: dict[str, list[str]],
        budget_steps: int,
        allow_experimental_tools: bool,
        ask_user: bool = False,
        checkpoint_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        job = self.job_manager.create_job(task, mode="pilot")
        best_score = float("-inf")
        best_attempt: dict[str, Any] | None = None
        previous_attempts: list[dict[str, Any]] = []
        current_task = task
        approved_asset_ids: list[str] = []
        latest_pipeline_candidate_id: str | None = None

        self._emit(event_handler, {
            "type": "stage_started",
            "mode": "pilot",
            "job_id": job.job_id,
            "stage": "goal_clarification",
            "model": self.config.agent.model if self.llm_provider is not None else None,
        })
        goal_clarification = self._maybe_request_goal_clarification(
            job_id=job.job_id,
            task=task,
            dataset_schemas=dataset_schemas,
            allow_experimental_tools=allow_experimental_tools,
            ask_user=ask_user,
            checkpoint_handler=checkpoint_handler,
            event_handler=event_handler,
        )
        self._emit(event_handler, {
            "type": "stage_completed",
            "mode": "pilot",
            "job_id": job.job_id,
            "stage": "goal_clarification",
            "status": goal_clarification.get("status"),
            "turns": goal_clarification.get("turns", 0),
        })
        current_task = goal_clarification["resolved_task"]
        self._emit(event_handler, {
            "type": "stage_started",
            "mode": "pilot",
            "job_id": job.job_id,
            "stage": "security_checker_clarification",
        })
        security_checker_clarification = self._maybe_request_security_checker_clarification(
            job_id=job.job_id,
            task=task,
            current_task=current_task,
            resolved_slots=goal_clarification.get("resolved_slots", {}),
            ask_user=ask_user,
            checkpoint_handler=checkpoint_handler,
            event_handler=event_handler,
        )
        self._emit(event_handler, {
            "type": "stage_completed",
            "mode": "pilot",
            "job_id": job.job_id,
            "stage": "security_checker_clarification",
            "status": security_checker_clarification.get("status"),
            "turns": security_checker_clarification.get("turns", 0),
        })
        if security_checker_clarification["turns"]:
            goal_clarification["turns"] += security_checker_clarification["turns"]
            goal_clarification["transcript"].extend(security_checker_clarification["transcript"])
            goal_clarification["resolved_slots"] = (
                goal_clarification.get("resolved_slots", {})
                | security_checker_clarification.get("resolved_slots", {})
            )
            goal_clarification["status"] = security_checker_clarification["status"]
            current_task = security_checker_clarification["resolved_task"]
        self.job_manager.update_clarification(
            job.job_id,
            status=goal_clarification["status"],
            turns=goal_clarification["turns"],
            transcript=goal_clarification["transcript"],
            resolved_task=current_task,
            resolved_slots=goal_clarification["resolved_slots"],
        )
        if goal_clarification["status"] == "paused":
            self.job_manager.update_capability_gap(job.job_id, {
                "type": "needs_user_input",
                "reason": goal_clarification.get("reason", "Pilot paused for goal clarification."),
            })
            return {
                "job_id": job.job_id,
                "attempts": previous_attempts,
                "best_attempt": None,
                "approved_asset_ids": approved_asset_ids,
                "pipeline_candidate_id": None,
                "status": "paused",
                "goal_clarification": goal_clarification,
                "pilot_summary": {},
            }

        for index in range(1, budget_steps + 1):
            attempt_id = f"attempt_{index:02d}"
            attempt_started_at = perf_counter()
            self._emit(event_handler, {
                "type": "attempt_started",
                "attempt_id": attempt_id,
                "index": index,
                "budget_steps": budget_steps,
                "job_id": job.job_id,
            })

            while True:
                self._emit(event_handler, {
                    "type": "stage_started",
                    "mode": "pilot",
                    "job_id": job.job_id,
                    "attempt_id": attempt_id,
                    "stage": "planner",
                    "model": self.config.agent.model if self.llm_provider is not None else None,
                })
                with llm_trace_context(job_id=job.job_id, mode="pilot", attempt_id=attempt_id):
                    action, planner_llm = self._plan_action(
                        task=current_task,
                        dataset_schemas=dataset_schemas,
                        previous_attempts=previous_attempts,
                        allow_experimental_tools=allow_experimental_tools,
                    )
                action = self._stabilize_planner_action(
                    current_task,
                    previous_attempts,
                    action,
                    allow_experimental_tools,
                )
                self._emit(event_handler, {
                    "type": "planner",
                    "attempt_id": attempt_id,
                    "job_id": job.job_id,
                    "action": action,
                    "llm": planner_llm,
                })

                if action.get("action_type") != "request_user_input":
                    break

                checkpoint_payload = {
                    "prompt": action.get("instructions")
                    or action.get("reason")
                    or "Additional clarification required before continuing pilot mode.",
                    "attempt_id": attempt_id,
                    "suggested_defaults": action.get("suggested_defaults", {}),
                    "missing_items": _normalize_missing_items(
                        current_task,
                        action.get("missing_items", []),
                    ),
                }
                decision = self._request_checkpoint_decision(
                    job_id=job.job_id,
                    checkpoint_type="goal_clarification",
                    checkpoint_payload=checkpoint_payload,
                    ask_user=ask_user,
                    checkpoint_handler=checkpoint_handler,
                    event_handler=event_handler,
                    fallback_decision="defer",
                )
                if decision.get("decision") == "answer" and decision.get("answer"):
                    current_task = _merge_clarification_into_task(
                        current_task=current_task,
                        assistant_message=checkpoint_payload["prompt"],
                        user_reply=decision["answer"],
                        suggested_defaults=checkpoint_payload["suggested_defaults"],
                    )
                    continue

                self.job_manager.update_capability_gap(job.job_id, {
                    "type": "needs_user_input",
                    "reason": checkpoint_payload["prompt"],
                    "recommended_command": "elf pilot",
                })
                if decision.get("decision") == "defer":
                    return {
                        "job_id": job.job_id,
                        "attempts": previous_attempts,
                        "best_attempt": best_attempt,
                        "approved_asset_ids": approved_asset_ids,
                        "pipeline_candidate_id": None,
                        "goal_clarification": goal_clarification,
                        "pilot_summary": self._build_pilot_summary(previous_attempts),
                        "status": "paused",
                    }
                action = {
                    "action_type": "stop_failed",
                    "reason": checkpoint_payload["prompt"],
                }
                planner_llm = {
                    "stage": "planner",
                    "status": "skipped",
                    "model": None,
                    "elapsed_seconds": None,
                    "error": None,
                }
                break

            if action.get("action_type") == "stop_failed":
                pipeline = ""
                pipeline_llm = {
                    "stage": "pipeline_generator",
                    "status": "skipped",
                    "model": None,
                    "elapsed_seconds": None,
                    "error": None,
                }
                execution = {
                    "success": False,
                    "result": None,
                    "artifacts": {},
                    "metadata": {},
                    "error": action.get("reason", "Planner requested stop_failed."),
                }
                judge = {
                    "goal_satisfied": False,
                    "score": 0.0,
                    "failure_type": "planner_stop",
                    "capability_gap": {},
                    "recommended_next_action": "stop_failed",
                    "reason": action.get("reason", "Planner requested stop_failed."),
                }
                judge_llm = {
                    "stage": "judge",
                    "status": "skipped",
                    "model": None,
                    "elapsed_seconds": None,
                    "error": None,
                }
                self._emit(event_handler, {
                    "type": "pipeline",
                    "attempt_id": attempt_id,
                    "job_id": job.job_id,
                    "llm": pipeline_llm,
                    "pipeline": pipeline,
                })
                self._emit(event_handler, {
                    "type": "judge",
                    "attempt_id": attempt_id,
                    "job_id": job.job_id,
                    "judge": judge,
                    "llm": judge_llm,
                })
                attempt_record = {
                    "attempt_id": attempt_id,
                    "job_id": job.job_id,
                    "action": action,
                    "planner_llm": planner_llm,
                    "pipeline": pipeline,
                    "pipeline_llm": pipeline_llm,
                    "execution": execution,
                    "execution_log_excerpt": execution.get("log_excerpt", []),
                    "execution_log_ref": execution.get("log_ref"),
                    "judge": judge,
                    "judge_llm": judge_llm,
                }
                attempt_record["attempt_metrics"] = self._derive_attempt_metrics(
                    attempt_id=attempt_id,
                    pipeline=pipeline,
                    planner_llm=planner_llm,
                    pipeline_llm=pipeline_llm,
                    execution=execution,
                    execution_latency_s=0.0,
                    judge=judge,
                    total_attempt_latency_s=round(perf_counter() - attempt_started_at, 2),
                    derived_candidate_count=0,
                )
                self.asset_manager.save_attempt(job.job_id, attempt_id, attempt_record)
                previous_attempts.append(attempt_record)
                self.job_manager.update_attempts(job.job_id, len(previous_attempts), final_score=max(best_score, 0.0))
                pilot_summary = self._build_pilot_summary(previous_attempts)
                if best_attempt is not None:
                    self.job_manager.update_result(job.job_id, {
                        "result": best_attempt["execution"].get("result"),
                        "artifacts": best_attempt["execution"].get("artifacts", {}),
                        "metadata": {
                            **best_attempt["execution"].get("metadata", {}),
                            "best_attempt_id": best_attempt["attempt_id"],
                            "pilot_summary": pilot_summary,
                            "goal_clarification": goal_clarification,
                            "approved_asset_ids": approved_asset_ids,
                        },
                    })
                self.job_manager.update_error(job.job_id, action.get("reason", "Pilot stopped after planner requested failure."))
                return {
                    "job_id": job.job_id,
                    "attempts": previous_attempts,
                    "best_attempt": best_attempt,
                    "approved_asset_ids": approved_asset_ids,
                    "goal_clarification": goal_clarification,
                    "pilot_summary": pilot_summary,
                    "status": "failed",
                }

            prepared_python_candidate: dict[str, Any] | None = None
            prepared_python_validation: dict[str, Any] | None = None
            prepared_python_error: str | None = None
            if action.get("action_type") == "derive_python_tool_draft" and allow_experimental_tools:
                try:
                    prepared_python_candidate, prepared_python_validation = self._prepare_python_tool_candidate_for_attempt(
                        job_id=job.job_id,
                        task=current_task,
                        attempt_id=attempt_id,
                        previous_attempts=previous_attempts,
                        dataset_schemas=dataset_schemas,
                        allow_experimental_tools=allow_experimental_tools,
                    )
                except Exception as e:
                    prepared_python_error = f"{type(e).__name__}: {e}"

            try:
                self._emit(event_handler, {
                    "type": "stage_started",
                    "mode": "pilot",
                    "job_id": job.job_id,
                    "attempt_id": attempt_id,
                    "stage": "pipeline_generation",
                    "model": self.config.agent.model if self.llm_provider is not None else None,
                })
                with llm_trace_context(job_id=job.job_id, mode="pilot", attempt_id=attempt_id):
                    pipeline, pipeline_llm = self._materialize_pipeline(
                        task=current_task,
                        action=action,
                        dataset_schemas=dataset_schemas,
                        previous_attempts=previous_attempts,
                        attempt_id=attempt_id,
                    )
            except Exception as e:
                pipeline = ""
                pipeline_llm = {
                    "stage": "pipeline_generator",
                    "status": "error",
                    "model": self.config.agent.model,
                    "error": f"{type(e).__name__}: {e}",
                }

            self._emit(event_handler, {
                "type": "pipeline",
                "attempt_id": attempt_id,
                "job_id": job.job_id,
                "llm": pipeline_llm,
                "pipeline": pipeline,
            })
            self.job_manager.update_pipeline(job.job_id, pipeline)
            write_approval = self._maybe_request_pilot_write_approval(
                job_id=job.job_id,
                attempt_id=attempt_id,
                pipeline=pipeline,
                ask_user=ask_user,
                checkpoint_handler=checkpoint_handler,
                event_handler=event_handler,
            )
            if write_approval.get("decision") == "answer" and write_approval.get("answer"):
                revised = _apply_revised_write_targets_to_pipeline(
                    pipeline,
                    write_approval.get("paths", []),
                    write_approval["answer"],
                )
                if revised is not None:
                    pipeline, revised_paths = revised
                    self.job_manager.update_pipeline(job.job_id, pipeline)
                    write_approval = {"decision": "allow", "paths": revised_paths}
                else:
                    current_task = _merge_clarification_into_task(
                        current_task=current_task,
                        assistant_message=_build_write_approval_prompt(write_approval.get("paths", [])),
                        user_reply=write_approval["answer"],
                        suggested_defaults={},
                    )
                    continue
            if write_approval.get("decision") == "defer":
                self.job_manager.update_capability_gap(job.job_id, {
                    "type": "write_approval_required",
                    "reason": write_approval.get("reason", "External write requires approval."),
                    "requested_paths": write_approval.get("paths", []),
                })
                return {
                    "job_id": job.job_id,
                    "attempts": previous_attempts,
                    "best_attempt": best_attempt,
                    "approved_asset_ids": approved_asset_ids,
                    "pipeline_candidate_id": None,
                    "goal_clarification": goal_clarification,
                    "pilot_summary": self._build_pilot_summary(previous_attempts),
                    "status": "paused",
                }
            if write_approval.get("decision") == "deny":
                self.job_manager.update_capability_gap(job.job_id, {
                    "type": "write_denied",
                    "reason": write_approval.get("reason", "External write approval denied."),
                    "requested_paths": write_approval.get("paths", []),
                })
                self.job_manager.update_error(job.job_id, write_approval.get("reason", "External write approval denied."))
                return {
                    "job_id": job.job_id,
                    "attempts": previous_attempts,
                    "best_attempt": best_attempt,
                    "approved_asset_ids": approved_asset_ids,
                    "pipeline_candidate_id": None,
                    "goal_clarification": goal_clarification,
                    "pilot_summary": self._build_pilot_summary(previous_attempts),
                    "status": "failed",
                }
            if pipeline_llm["status"] == "error":
                execution = {
                    "success": False,
                    "result": None,
                    "artifacts": {},
                    "metadata": {},
                    "error": f"Pipeline generation failed: {pipeline_llm['error']}",
                }
                execution_latency_s = 0.0
            else:
                execution_started_at = perf_counter()
                self._emit(event_handler, {
                    "type": "stage_started",
                    "mode": "pilot",
                    "job_id": job.job_id,
                    "attempt_id": attempt_id,
                    "stage": "execution",
                })
                execution = self.executor.execute(job.job_id, pipeline)
                execution = _attach_execution_signals(execution)
                execution_latency_s = round(perf_counter() - execution_started_at, 2)
                execution["elapsed_seconds"] = execution_latency_s
                self._emit(event_handler, {
                    "type": "stage_completed",
                    "mode": "pilot",
                    "job_id": job.job_id,
                    "attempt_id": attempt_id,
                    "stage": "execution",
                    "elapsed_seconds": execution_latency_s,
                    "success": execution.get("success"),
                    "error": execution.get("error"),
                })
            self._emit(event_handler, {
                "type": "stage_started",
                "mode": "pilot",
                "job_id": job.job_id,
                "attempt_id": attempt_id,
                "stage": "judge",
                "model": self.config.agent.model if self.llm_provider is not None else None,
            })
            with llm_trace_context(job_id=job.job_id, mode="pilot", attempt_id=attempt_id):
                judge, judge_llm = self._judge_attempt(
                    current_task,
                    pipeline,
                    execution,
                    previous_attempts,
                    execution_latency_s=execution_latency_s,
                )
            self._emit(event_handler, {
                "type": "judge",
                "attempt_id": attempt_id,
                "job_id": job.job_id,
                "judge": judge,
                "llm": judge_llm,
            })

            attempt_record = {
                "attempt_id": attempt_id,
                "job_id": job.job_id,
                "action": action,
                "planner_llm": planner_llm,
                "pipeline": pipeline,
                "pipeline_llm": pipeline_llm,
                "execution": execution,
                "execution_log_excerpt": execution.get("log_excerpt", []),
                "execution_log_ref": execution.get("log_ref"),
                "judge": judge,
                "judge_llm": judge_llm,
                "candidates": [],
            }

            derived_candidates = 0
            continue_optimization_reasons: list[str] = []

            if action["action_type"] == "derive_composite_tool":
                try:
                    with llm_trace_context(job_id=job.job_id, mode="pilot", attempt_id=attempt_id):
                        candidate = self._derive_composite_tool(
                            task=current_task,
                            pipeline=pipeline,
                            source_attempts=[attempt_id],
                            dataset_schemas=dataset_schemas,
                        )
                    self.asset_manager.save_candidate(candidate)
                    self.job_manager.add_candidate_asset(job.job_id, candidate["candidate_id"])
                    validation = self._validate_candidate(
                        candidate=candidate,
                        pipeline=pipeline,
                        dataset_schemas=dataset_schemas,
                        allow_experimental_tools=allow_experimental_tools,
                    )
                    candidate.update(validation)
                    self.asset_manager.update_candidate(candidate["candidate_id"], **validation)
                    derived_candidates += 1
                    attempt_record["candidate"] = candidate
                    attempt_record["candidates"].append(candidate)
                    self._emit(event_handler, {
                        "type": "candidate_saved",
                        "attempt_id": attempt_id,
                        "job_id": job.job_id,
                        "candidate": candidate,
                    })
                    self._emit(event_handler, {
                        "type": "candidate_validated",
                        "attempt_id": attempt_id,
                        "job_id": job.job_id,
                        "candidate": candidate,
                        "validation": validation,
                    })
                    decision = self._handle_candidate_checkpoint(
                        job_id=job.job_id,
                        attempt_id=attempt_id,
                        candidate=candidate,
                        validation=validation,
                        judge=judge,
                        ask_user=ask_user,
                        checkpoint_handler=checkpoint_handler,
                        event_handler=event_handler,
                    )
                    if decision.get("registered_candidate"):
                        self.asset_manager.register_candidate_tools(self.registry, allow_experimental=False)
                    approved_asset_id = decision.get("approved_asset_id")
                    if approved_asset_id:
                        candidate["status"] = "approved"
                        candidate["asset_id"] = approved_asset_id
                        approved_asset_ids.append(approved_asset_id)
                    if validation.get("validation_status") == "smoke_failed":
                        continue_optimization_reasons.append("composite_tool_smoke_failed")
                except Exception as e:
                    continue_optimization_reasons.append("composite_tool_derivation_error")
                    attempt_record.setdefault("candidate_errors", []).append({
                        "candidate_type": "composite_tool",
                        "error": f"{type(e).__name__}: {e}",
                    })
                    self._emit(event_handler, {
                        "type": "candidate_error",
                        "attempt_id": attempt_id,
                        "job_id": job.job_id,
                        "candidate_type": "composite_tool",
                        "error": f"{type(e).__name__}: {e}",
                    })
            elif action["action_type"] == "derive_python_tool_draft" and allow_experimental_tools:
                try:
                    if prepared_python_candidate is not None:
                        candidate = prepared_python_candidate
                        validation = prepared_python_validation or {
                            "validation_status": candidate.get("validation_status"),
                            "validation_summary": candidate.get("validation_summary"),
                            "smoke_test_result": candidate.get("smoke_test_result"),
                            "benchmark_result": candidate.get("benchmark_result"),
                            "status": candidate.get("status"),
                        }
                    else:
                        with llm_trace_context(job_id=job.job_id, mode="pilot", attempt_id=attempt_id):
                            candidate, code = self._derive_python_tool(
                                task=current_task,
                                pipeline=pipeline,
                                source_attempts=[attempt_id],
                                execution=execution,
                                judge=judge,
                            )
                        self.asset_manager.save_candidate(candidate, python_code=code)
                        self.job_manager.add_candidate_asset(job.job_id, candidate["candidate_id"])
                        validation = self._validate_candidate(
                            candidate=candidate,
                            pipeline=pipeline,
                            dataset_schemas=dataset_schemas,
                            allow_experimental_tools=allow_experimental_tools,
                            task=current_task,
                        )
                        candidate.update(validation)
                        self.asset_manager.update_candidate(candidate["candidate_id"], **validation)
                    derived_candidates += 1
                    attempt_record["candidate"] = candidate
                    attempt_record["candidates"].append(candidate)
                    self._emit(event_handler, {
                        "type": "candidate_saved",
                        "attempt_id": attempt_id,
                        "job_id": job.job_id,
                        "candidate": candidate,
                    })
                    self._emit(event_handler, {
                        "type": "candidate_validated",
                        "attempt_id": attempt_id,
                        "job_id": job.job_id,
                        "candidate": candidate,
                        "validation": validation,
                    })
                    decision = self._handle_candidate_checkpoint(
                        job_id=job.job_id,
                        attempt_id=attempt_id,
                        candidate=candidate,
                        validation=validation,
                        judge=judge,
                        ask_user=ask_user,
                        checkpoint_handler=checkpoint_handler,
                        event_handler=event_handler,
                    )
                    if decision.get("registered_candidate"):
                        self.asset_manager.register_candidate_tools(self.registry, allow_experimental=True)
                    approved_asset_id = decision.get("approved_asset_id")
                    if approved_asset_id:
                        candidate["status"] = "approved"
                        candidate["asset_id"] = approved_asset_id
                        approved_asset_ids.append(approved_asset_id)
                    if validation.get("validation_status") == "smoke_failed":
                        continue_optimization_reasons.append("experimental_tool_smoke_failed")
                except Exception as e:
                    continue_optimization_reasons.append("experimental_tool_derivation_error")
                    attempt_record.setdefault("candidate_errors", []).append({
                        "candidate_type": "experimental_python_tool",
                        "error": f"{type(e).__name__}: {e}",
                    })
                    self._emit(event_handler, {
                        "type": "candidate_error",
                        "attempt_id": attempt_id,
                        "job_id": job.job_id,
                        "candidate_type": "experimental_python_tool",
                        "error": f"{type(e).__name__}: {e}",
                    })
            if prepared_python_error:
                continue_optimization_reasons.append("experimental_tool_derivation_error")
                attempt_record.setdefault("candidate_errors", []).append({
                    "candidate_type": "experimental_python_tool",
                    "error": prepared_python_error,
                })
                self._emit(event_handler, {
                    "type": "candidate_error",
                    "attempt_id": attempt_id,
                    "job_id": job.job_id,
                    "candidate_type": "experimental_python_tool",
                    "error": prepared_python_error,
                })

            attempt_metrics = self._derive_attempt_metrics(
                attempt_id=attempt_id,
                pipeline=pipeline,
                planner_llm=planner_llm,
                pipeline_llm=pipeline_llm,
                execution=execution,
                execution_latency_s=execution_latency_s,
                judge=judge,
                total_attempt_latency_s=round(perf_counter() - attempt_started_at, 2),
                derived_candidate_count=derived_candidates,
            )
            attempt_record["attempt_metrics"] = attempt_metrics

            self.asset_manager.save_attempt(job.job_id, attempt_id, attempt_record)
            previous_attempts.append(attempt_record)

            score = float(judge.get("score", 0.0))
            if score > best_score:
                best_score = score
                best_attempt = attempt_record

            self.job_manager.update_attempts(job.job_id, len(previous_attempts), final_score=max(best_score, 0.0))

            if execution["success"] and judge.get("goal_satisfied", execution["success"]):
                pipeline_candidate = self._derive_pipeline_candidate(
                    job_id=job.job_id,
                    task=current_task,
                    pipeline=pipeline,
                    source_attempts=[attempt_id],
                    judge=judge,
                )
                pipeline_validation = {
                    "validation_status": "smoke_passed",
                    "validation_summary": "Pipeline candidate validated by successful execution in this attempt.",
                    "smoke_test_result": {
                        "status": "from_successful_attempt",
                        "attempt_id": attempt_id,
                    },
                    "benchmark_result": {"status": "not_configured"},
                    "status": "awaiting_approval",
                }
                pipeline_candidate.update(pipeline_validation)
                self.asset_manager.save_candidate(pipeline_candidate)
                self.job_manager.add_candidate_asset(job.job_id, pipeline_candidate["candidate_id"])
                latest_pipeline_candidate_id = pipeline_candidate["candidate_id"]
                attempt_record["candidate"] = pipeline_candidate
                attempt_record["candidates"].append(pipeline_candidate)
                attempt_record["attempt_metrics"]["derived_candidate_count"] = (
                    attempt_record["attempt_metrics"].get("derived_candidate_count", 0) + 1
                )
                self.asset_manager.save_attempt(job.job_id, attempt_id, attempt_record)
                self._emit(event_handler, {
                    "type": "candidate_saved",
                    "attempt_id": attempt_id,
                    "job_id": job.job_id,
                    "candidate": pipeline_candidate,
                })
                self._emit(event_handler, {
                    "type": "candidate_validated",
                    "attempt_id": attempt_id,
                    "job_id": job.job_id,
                    "candidate": pipeline_candidate,
                    "validation": pipeline_validation,
                })
                decision = self._handle_candidate_checkpoint(
                    job_id=job.job_id,
                    attempt_id=attempt_id,
                    candidate=pipeline_candidate,
                    validation=pipeline_validation,
                    judge=judge,
                    ask_user=ask_user,
                    checkpoint_handler=checkpoint_handler,
                    event_handler=event_handler,
                )
                approved_asset_id = decision.get("approved_asset_id")
                if approved_asset_id:
                    pipeline_candidate["status"] = "approved"
                    pipeline_candidate["asset_id"] = approved_asset_id
                    approved_asset_ids.append(approved_asset_id)
                if decision.get("decision") == "reject" and index < budget_steps:
                    continue_optimization_reasons.append("pipeline_candidate_rejected")
                elif decision.get("decision") == "continue" and index < budget_steps:
                    continue_optimization_reasons.append("pipeline_candidate_not_approved")
                if continue_optimization_reasons and index < budget_steps and decision.get("decision") != "approve":
                    attempt_record["continue_optimization"] = {
                        "enabled": True,
                        "reasons": _dedupe_preserve_order(continue_optimization_reasons),
                        "last_candidate_decision": decision.get("decision"),
                        "message": (
                            "The user goal was satisfied, but pilot still has remaining budget and unresolved "
                            "optimization opportunities. Continue iterating."
                        ),
                    }
                    self.asset_manager.save_attempt(job.job_id, attempt_id, attempt_record)
                    continue
                pilot_summary = self._build_pilot_summary(previous_attempts)
                self.job_manager.update_result(job.job_id, {
                    "result": execution["result"],
                    "artifacts": execution.get("artifacts", {}),
                    "metadata": {
                        **execution.get("metadata", {}),
                        "judge": judge,
                        "attempt_count": len(previous_attempts),
                        "pipeline_candidate_id": pipeline_candidate["candidate_id"],
                        "pilot_summary": pilot_summary,
                        "goal_clarification": goal_clarification,
                        "approved_asset_ids": approved_asset_ids,
                    },
                })
                self.job_manager.update_status(job.job_id, JobStatus.COMPLETED)
                return {
                    "job_id": job.job_id,
                    "attempts": previous_attempts,
                    "best_attempt": attempt_record,
                    "pipeline_candidate_id": pipeline_candidate["candidate_id"],
                    "approved_asset_ids": approved_asset_ids,
                    "goal_clarification": goal_clarification,
                    "pilot_summary": pilot_summary,
                    "status": "success",
                }

            next_action = judge.get("recommended_next_action", "mutate_pipeline")
            if next_action == "request_user_input":
                checkpoint_payload = {
                    "prompt": judge.get("reason", "Additional clarification required."),
                    "attempt_id": attempt_id,
                    "suggested_defaults": judge.get("capability_gap", {}),
                }
                decision = self._request_checkpoint_decision(
                    job_id=job.job_id,
                    checkpoint_type="goal_clarification",
                    checkpoint_payload=checkpoint_payload,
                    ask_user=ask_user,
                    checkpoint_handler=checkpoint_handler,
                    event_handler=event_handler,
                    fallback_decision="defer",
                )
                if decision.get("decision") == "answer" and decision.get("answer"):
                    current_task = _merge_clarification_into_task(
                        current_task=current_task,
                        assistant_message=checkpoint_payload["prompt"],
                        user_reply=decision["answer"],
                        suggested_defaults={},
                    )
                    continue
                self.job_manager.update_capability_gap(job.job_id, {
                    "type": "needs_user_input",
                    "reason": judge.get("reason", "Additional clarification required."),
                    "recommended_command": "elf pilot",
                })
                if decision.get("decision") == "defer":
                    return {
                        "job_id": job.job_id,
                        "attempts": previous_attempts,
                        "best_attempt": best_attempt,
                        "approved_asset_ids": approved_asset_ids,
                        "pipeline_candidate_id": None,
                        "goal_clarification": goal_clarification,
                        "pilot_summary": self._build_pilot_summary(previous_attempts),
                        "status": "paused",
                    }
                break

        pilot_summary = self._build_pilot_summary(previous_attempts)
        best_attempt_satisfied_goal = bool(
            best_attempt
            and best_attempt.get("execution", {}).get("success")
            and best_attempt.get("judge", {}).get(
                "goal_satisfied",
                best_attempt.get("execution", {}).get("success"),
            )
        )
        if best_attempt is not None:
            self.job_manager.update_result(job.job_id, {
                "result": best_attempt["execution"].get("result"),
                "artifacts": best_attempt["execution"].get("artifacts", {}),
                "metadata": {
                    **best_attempt["execution"].get("metadata", {}),
                    "best_attempt_id": best_attempt["attempt_id"],
                    "budget_exhausted": True,
                    "goal_satisfied_before_budget_end": best_attempt_satisfied_goal,
                    "pipeline_candidate_id": latest_pipeline_candidate_id,
                    "pilot_summary": pilot_summary,
                    "goal_clarification": goal_clarification,
                    "approved_asset_ids": approved_asset_ids,
                },
            })
        if best_attempt_satisfied_goal:
            self.job_manager.update_status(job.job_id, JobStatus.COMPLETED)
            return {
                "job_id": job.job_id,
                "attempts": previous_attempts,
                "best_attempt": best_attempt,
                "approved_asset_ids": approved_asset_ids,
                "pipeline_candidate_id": latest_pipeline_candidate_id,
                "goal_clarification": goal_clarification,
                "pilot_summary": pilot_summary,
                "status": "success",
            }
        self.job_manager.update_error(job.job_id, "Pilot budget exhausted before reaching a satisfactory result.")
        return {
            "job_id": job.job_id,
            "attempts": previous_attempts,
            "best_attempt": best_attempt,
            "approved_asset_ids": approved_asset_ids,
            "pipeline_candidate_id": latest_pipeline_candidate_id,
            "goal_clarification": goal_clarification,
            "pilot_summary": pilot_summary,
            "status": "budget_exhausted",
        }

    def _maybe_request_pilot_write_approval(
        self,
        job_id: str,
        attempt_id: str,
        pipeline: str,
        ask_user: bool,
        checkpoint_handler: Callable[[dict[str, Any]], dict[str, Any]] | None,
        event_handler: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        paths = _extract_external_write_targets(pipeline)
        if not paths:
            return {"decision": "allow", "paths": []}

        checkpoint_payload = {
            "attempt_id": attempt_id,
            "prompt": _build_write_approval_prompt(paths),
            "paths": paths,
            "options": ["allow", "deny"],
        }
        if not ask_user or checkpoint_handler is None:
            self.job_manager.update_checkpoint(
                job_id,
                checkpoint_type="write_approval",
                checkpoint_state="awaiting_input",
                checkpoint_payload=checkpoint_payload,
                status=JobStatus.PAUSED,
            )
            self._emit(event_handler, {
                "type": "checkpoint_paused",
                "job_id": job_id,
                "attempt_id": attempt_id,
                "checkpoint_type": "write_approval",
                "payload": checkpoint_payload,
            })
            return {
                "decision": "defer",
                "paths": paths,
                "reason": "External write requires interactive approval before execution.",
            }

        response = self._request_checkpoint_decision(
            job_id=job_id,
            checkpoint_type="write_approval",
            checkpoint_payload=checkpoint_payload,
            ask_user=ask_user,
            checkpoint_handler=checkpoint_handler,
            event_handler=event_handler,
            fallback_decision="deny",
        )
        decision = str(response.get("decision", "deny")).lower()
        if decision == "answer" and _coerce_text_block(response.get("answer")):
            return {
                "decision": "answer",
                "answer": _coerce_text_block(response.get("answer")),
                "paths": paths,
                "reason": "User requested write-target changes before approval.",
            }
        if decision in {"allow", "approve", "yes", "y"}:
            return {"decision": "allow", "paths": paths}
        if decision == "defer":
            return {
                "decision": "defer",
                "paths": paths,
                "reason": "External write requires approval before execution.",
            }
        return {
            "decision": "deny",
            "paths": paths,
            "reason": "User denied external write approval.",
        }

    def _maybe_request_goal_clarification(
        self,
        job_id: str,
        task: str,
        dataset_schemas: dict[str, list[str]],
        allow_experimental_tools: bool,
        ask_user: bool,
        checkpoint_handler: Callable[[dict[str, Any]], dict[str, Any]] | None,
        event_handler: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        if self.llm_provider is None or not ask_user or checkpoint_handler is None:
            return {
                "status": "not_requested",
                "turns": 0,
                "transcript": [],
                "resolved_task": task,
                "resolved_slots": {},
            }
        def decision_provider(
            current_task: str,
            transcript: list[dict[str, Any]],
            messages: list[dict[str, str]],
            _turn: int,
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            relevant_tools = _select_relevant_tool_schemas(
                current_task,
                self.registry,
                allowed_tool_names=_configured_tool_names(self.config),
            )
            prompt = _build_pilot_goal_clarification_prompt(
                task=task,
                current_task=current_task,
                transcript=transcript,
                dataset_schemas=dataset_schemas,
                relevant_tools=relevant_tools,
                allow_experimental_tools=allow_experimental_tools,
                tool_readmes=self._pilot_tool_readmes(relevant_tools),
            )
            start_time = perf_counter()
            fallback_llm = {
                "stage": "pilot_goal_clarification",
                "status": "fallback",
                "model": self.config.agent.model,
                "elapsed_seconds": None,
                "error": None,
            }
            try:
                with llm_trace_context(job_id=job_id, mode="pilot", scope="core", caller="clarification"):
                    decision = self.llm_provider.generate_json(self.config.agent.model, prompt)
            except Exception as e:
                fallback_llm["elapsed_seconds"] = round(perf_counter() - start_time, 2)
                fallback_llm["error"] = f"{type(e).__name__}: {e}"
                return {
                    "status": "ready",
                    "assistant_message": "",
                    "ready_to_execute": True,
                    "resolved_task": current_task,
                    "resolved_slots": {},
                    "missing_items": [],
                    "suggested_defaults": {},
                }, fallback_llm
            if not isinstance(decision, dict) or "status" not in decision:
                decision = {
                    "status": "ready",
                    "assistant_message": "",
                    "ready_to_execute": True,
                    "resolved_task": current_task,
                    "resolved_slots": {},
                    "missing_items": [],
                    "suggested_defaults": {},
                }
            return decision, {
                "stage": "pilot_goal_clarification",
                "status": "success",
                "model": self.config.agent.model,
                "elapsed_seconds": round(perf_counter() - start_time, 2),
                "error": None,
            }

        def response_provider(
            checkpoint_payload: dict[str, Any],
            _turn: int,
            _llm_meta: dict[str, Any],
            _current_task: str,
        ) -> dict[str, Any]:
            return self._request_checkpoint_decision(
                job_id=job_id,
                checkpoint_type="goal_clarification",
                checkpoint_payload=checkpoint_payload,
                ask_user=ask_user,
                checkpoint_handler=checkpoint_handler,
                event_handler=event_handler,
                fallback_decision="defer",
            )

        return _run_shared_clarification_loop(
            self,
            task=task,
            dataset_schemas=dataset_schemas,
            max_rounds=2,
            decision_provider=decision_provider,
            response_provider=response_provider,
            ready_status="resolved",
            not_requested_status="not_requested",
            paused_status="paused",
            exhausted_status="resolved",
            exhausted_reason="Pilot goal clarification exceeded 2 turns; continue with best-effort resolved goal.",
            allow_escalation=False,
        )

    def _maybe_request_security_checker_clarification(
        self,
        job_id: str,
        task: str,
        current_task: str,
        resolved_slots: dict[str, Any] | None,
        ask_user: bool,
        checkpoint_handler: Callable[[dict[str, Any]], dict[str, Any]] | None,
        event_handler: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        if (
            not ask_user
            or checkpoint_handler is None
            or not _task_targets_security_checker_selection(current_task)
            or (resolved_slots or {}).get("checker_names")
            or _extract_security_checker_names(task, ["checker_names"], task)
        ):
            return {
                "status": "not_requested",
                "turns": 0,
                "transcript": [],
                "resolved_task": current_task,
                "resolved_slots": {},
            }

        hints = _build_security_audit_hints(
            current_task,
            self.registry,
        ) or {}
        available = hints.get("checker_names_available", [])
        suggested_defaults = self._default_slots_for_missing_items(current_task, ["checker_names"])
        current = current_task
        transcript: list[dict[str, Any]] = []
        _accumulated_slots: dict[str, Any] = dict(resolved_slots or {})
        forced_prompt = ""

        for turn in range(1, 3):
            if forced_prompt:
                prompt = forced_prompt
                forced_prompt = ""
            else:
                rule_based = hints.get("rule_based_checkers", [])
                llm_required = hints.get("llm_required_checkers", [])
                model_based = hints.get("model_based_checkers", [])
                baseline = suggested_defaults.get("checker_names", ["PIIRule", "SecretRule"])
                if turn == 1:
                    prompt = (
                        "Please specify which security_audit checker_names you want to use. "
                        f"Rule-based options: {', '.join(rule_based[:6])}. "
                        f"LLM-based options: {', '.join(llm_required[:6]) or 'none'}. "
                        f"Model-based options: {', '.join(model_based[:6]) or 'none'}. "
                        "Rule-based checkers are usually the cheapest baseline."
                    )
                else:
                    prompt = self._build_security_checker_recommendation_message(current_task, "recommendation")

            response = self._request_checkpoint_decision(
                job_id=job_id,
                checkpoint_type="goal_clarification",
                checkpoint_payload={
                    "prompt": prompt,
                    "turn": turn,
                    "suggested_defaults": suggested_defaults,
                    "missing_items": ["checker_names"],
                },
                ask_user=ask_user,
                checkpoint_handler=checkpoint_handler,
                event_handler=event_handler,
                fallback_decision="defer",
            )
            if response.get("decision") == "defer":
                return {
                    "status": "paused",
                    "turns": len(transcript),
                    "transcript": transcript,
                    "resolved_task": current,
                    "resolved_slots": _accumulated_slots,
                    "reason": prompt,
                }

            user_reply = str(response.get("answer", "")).strip()
            transcript.append({
                "turn": turn,
                "assistant_message": prompt,
                "user_reply": user_reply,
                "missing_items": ["checker_names"],
                "suggested_defaults": suggested_defaults,
            })

            if _looks_like_default_reply(user_reply):
                _accumulated_slots["checker_names"] = suggested_defaults.get("checker_names", ["PIIRule", "SecretRule"])
                current = _merge_clarification_into_task(
                    current_task=current,
                    assistant_message=prompt,
                    user_reply=user_reply,
                    suggested_defaults=suggested_defaults,
                )
                return {
                    "status": "resolved",
                    "turns": len(transcript),
                    "transcript": transcript,
                    "resolved_task": current,
                    "resolved_slots": _accumulated_slots,
                }

            recommended_checker_names, recommendation_mode = self._resolve_security_checker_reply(
                current_task,
                ["checker_names"],
                user_reply,
            )
            if recommended_checker_names:
                _accumulated_slots["checker_names"] = recommended_checker_names
                _accumulated_slots["selection_mode"] = recommendation_mode or "recommended"
                current = _merge_clarification_into_task(
                    current_task=current,
                    assistant_message=prompt,
                    user_reply=user_reply,
                    suggested_defaults=suggested_defaults,
                )
                return {
                    "status": "resolved",
                    "turns": len(transcript),
                    "transcript": transcript,
                    "resolved_task": current,
                    "resolved_slots": _accumulated_slots,
                }

            extracted = _extract_security_checker_names(current_task, ["checker_names"], user_reply)
            if extracted:
                _accumulated_slots["checker_names"] = extracted
                current = _merge_clarification_into_task(
                    current_task=current,
                    assistant_message=prompt,
                    user_reply=user_reply,
                    suggested_defaults=suggested_defaults,
                )
                return {
                    "status": "resolved",
                    "turns": len(transcript),
                    "transcript": transcript,
                    "resolved_task": current,
                    "resolved_slots": _accumulated_slots,
                }

            if _looks_like_recommendation_request(user_reply):
                forced_prompt = self._build_security_checker_recommendation_message(current_task, user_reply)
                continue

            if not (_looks_like_option_request(user_reply) or _looks_like_weak_reply(user_reply)):
                break

        return {
            "status": "resolved",
            "turns": len(transcript),
            "transcript": transcript,
            "resolved_task": current,
            "resolved_slots": _accumulated_slots,
        }

    def _pilot_tool_readmes(self, relevant_tools: list[dict[str, Any]]) -> list[dict[str, str]]:
        if not getattr(self.config.agent, "include_tool_readmes", False):
            return []
        tool_names = [schema.get("name", "") for schema in relevant_tools if schema.get("name")]
        max_len = getattr(self.config.agent, "tool_readmes_max_length", 2000)
        return load_tool_readme_entries(tool_names, max_len=max_len)

    def _plan_action(
        self,
        task: str,
        dataset_schemas: dict[str, list[str]],
        previous_attempts: list[dict[str, Any]],
        allow_experimental_tools: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        fallback_type = "propose_pipeline" if not previous_attempts else "mutate_pipeline"
        fallback = {
            "action_type": fallback_type,
            "reason": "Default planning fallback.",
        }
        fallback_llm = {
            "stage": "planner",
            "status": "disabled" if self.llm_provider is None else "fallback",
            "model": self.config.agent.model,
            "elapsed_seconds": None,
            "error": None,
        }
        if self.llm_provider is None:
            if len(previous_attempts) == 1:
                fallback["action_type"] = "derive_composite_tool"
            elif allow_experimental_tools and len(previous_attempts) >= 2:
                fallback["action_type"] = "derive_python_tool_draft"
            return fallback, fallback_llm

        prompt = _build_planner_prompt(
            task=task,
            dataset_schemas=dataset_schemas,
            tool_schemas=_visible_tool_schemas(self.config, self.registry),
            previous_attempts=previous_attempts,
            allow_experimental_tools=allow_experimental_tools,
        )
        start_time = perf_counter()
        try:
            with llm_trace_context(scope="core", caller="planner"):
                action = self.llm_provider.generate_json(self.config.agent.model, prompt)
        except Exception as e:
            fallback["reason"] = f"Default planning fallback. LLM planner error: {type(e).__name__}: {e}"
            fallback_llm["elapsed_seconds"] = round(perf_counter() - start_time, 2)
            fallback_llm["error"] = f"{type(e).__name__}: {e}"
            return fallback, fallback_llm

        normalized_action = _normalize_planner_action(action, fallback_type)
        return normalized_action, {
            "stage": "planner",
            "status": "success",
            "model": self.config.agent.model,
            "elapsed_seconds": round(perf_counter() - start_time, 2),
            "error": None,
        }

    def _stabilize_planner_action(
        self,
        task: str,
        previous_attempts: list[dict[str, Any]],
        action: dict[str, Any],
        allow_experimental_tools: bool = False,
    ) -> dict[str, Any]:
        action = _normalize_planner_action(action, "mutate_pipeline" if previous_attempts else "propose_pipeline")
        if not previous_attempts:
            return action
        last_attempt = previous_attempts[-1]
        last_execution = last_attempt.get("execution", {})
        last_judge = last_attempt.get("judge", {})
        failed_experimental_candidate = _latest_failed_experimental_candidate(previous_attempts)
        if (
            allow_experimental_tools
            and failed_experimental_candidate
            and action.get("action_type") in {
                "propose_pipeline",
                "mutate_pipeline",
                "stop_failed",
            }
        ):
            source_tool_names = failed_experimental_candidate.get("source_tool_names", [])
            instructions = (
                f"{failed_experimental_candidate.get('reason', '').strip()} "
                "Derive a repaired experimental Python tool draft. "
                f"Focus on these source tools: {source_tool_names or ['current candidate implementation']}. "
                "Preserve the user goal, fix the validation/runtime issue, and keep the tool compatible with DataElf's tool contract."
            ).strip()
            return {
                **action,
                "action_type": "derive_python_tool_draft",
                "reason": f"{action.get('reason', '').strip()} {instructions}".strip(),
                "instructions": f"{action.get('instructions', '').strip()}\n{instructions}".strip(),
            }
        experimental_gap = _latest_experimental_tool_gap(previous_attempts)
        if (
            allow_experimental_tools
            and experimental_gap
            and action.get("action_type") in {
                "propose_pipeline",
                "mutate_pipeline",
                "stop_failed",
            }
        ):
            source_tool_names = experimental_gap.get("source_tool_names", [])
            instructions = (
                f"{experimental_gap.get('reason', '').strip()} "
                "Derive an experimental Python tool draft instead of another plain pipeline mutate. "
                f"Focus on these source tools: {source_tool_names or ['current called tools']}. "
                "Review the current tool implementation, preserve compatible inputs where possible, "
                "and improve robustness, fallback behavior, and result/report handling."
            ).strip()
            return {
                **action,
                "action_type": "derive_python_tool_draft",
                "reason": f"{action.get('reason', '').strip()} {instructions}".strip(),
                "instructions": f"{action.get('instructions', '').strip()}\n{instructions}".strip(),
            }
        checker_failure_gap = _latest_llm_checker_failure_gap(previous_attempts)
        if checker_failure_gap and action.get("action_type") in {
            "propose_pipeline",
            "mutate_pipeline",
            "stop_failed",
        }:
            avoid_checkers = checker_failure_gap.get("avoid_checkers", [])
            recommended = checker_failure_gap.get("recommended_checker_names", [])
            failure_type = checker_failure_gap.get("type")
            failure_summary = (
                "Previous attempts show LLM checker content filter failures."
                if failure_type == "llm_checker_content_filter"
                else "Previous attempts show LLM checker execution failures."
            )
            instructions = (
                f"{failure_summary} "
                f"Do not use these checkers: {avoid_checkers}. "
                f"Use this safe fallback checker set instead: {recommended}. "
                "If semantic harmful-content coverage is still needed, prefer rule-based "
                "HarmfulKeywordRule/ToxicityKeywordRule in this run; do not retry the same "
                "failed LLM judge in the next attempt."
            )
            return {
                **action,
                "action_type": "mutate_pipeline",
                "reason": f"{action.get('reason', '').strip()} {instructions}".strip(),
                "instructions": f"{action.get('instructions', '').strip()}\n{instructions}".strip(),
            }
        if (
            last_execution.get("success")
            and not last_judge.get("goal_satisfied", False)
            and action.get("action_type") == "stop_failed"
        ):
            return {
                **action,
                "action_type": "mutate_pipeline",
                "reason": (
                    f"{action.get('reason', '').strip()} Previous attempt executed successfully "
                    "but did not satisfy the goal, so mutate the pipeline before stopping."
                ).strip(),
            }
        if (
            last_attempt.get("continue_optimization", {}).get("enabled")
            and action.get("action_type") == "stop_failed"
        ):
            return {
                **action,
                "action_type": "mutate_pipeline",
                "reason": (
                    f"{action.get('reason', '').strip()} Previous attempt already satisfied the user goal, "
                    "but pilot still has remaining optimization work after candidate review, so keep iterating."
                ).strip(),
            }
        quality_gap = _latest_security_quality_gap(previous_attempts)
        if quality_gap and action.get("action_type") in {
            "propose_pipeline",
            "mutate_pipeline",
            "stop_failed",
        }:
            required_categories = quality_gap.get("required_risk_categories", [])
            covered = quality_gap.get("covered_checkers", [])
            instructions = (
                "Previous attempt already satisfied baseline coverage but failed the quality target. "
                f"Keep coverage for these risk categories intact: {required_categories}. "
                f"Current covered checkers were: {covered}. "
                "Do not remove already covered baseline checkers. Make one targeted improvement only, "
                "such as adding one stronger stable checker, adjusting max_workers, or improving report quality."
            )
            return {
                **action,
                "action_type": "mutate_pipeline",
                "reason": f"{action.get('reason', '').strip()} {instructions}".strip(),
                "instructions": f"{action.get('instructions', '').strip()}\n{instructions}".strip(),
            }
        return action

    def _default_slots_for_missing_items(
        self,
        task: str,
        missing_items: list[str],
    ) -> dict[str, Any]:
        defaults: dict[str, Any] = {}
        if "checker_names" in missing_items and _task_targets_security_checker_selection(task):
            security_hints = _build_security_audit_hints(
                task,
                self.registry,
            ) or {}
            baseline = security_hints.get("quick_baseline_checkers")
            if baseline:
                defaults["checker_names"] = baseline
        defaults |= _default_slots_from_required_specs(
            _schema_required_slot_specs(task, self.registry, {}),
            missing_items,
        )
        return defaults

    def _materialize_pipeline(
        self,
        task: str,
        action: dict[str, Any],
        dataset_schemas: dict[str, list[str]],
        previous_attempts: list[dict[str, Any]] | None = None,
        attempt_id: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        original_action_type = action.get("action_type", "propose_pipeline")
        action_type = original_action_type
        if action_type in {"derive_composite_tool", "derive_python_tool_draft"}:
            action_type = "mutate_pipeline"

        if action_type == "stop_failed":
            return (
                'log_step("Stopping pilot after judge requested failure")\nsave_result({"stopped": True})',
                {
                    "stage": "pipeline_generator",
                    "status": "skipped",
                    "model": None,
                    "error": None,
                },
            )

        adapted_task = task
        if action.get("reason"):
            adapted_task = f"{task}\n\nPlanner note: {action['reason']}"
        if action.get("instructions"):
            adapted_task = f"{adapted_task}\n\nRevision instructions:\n{action['instructions']}"
        if original_action_type == "derive_python_tool_draft":
            adapted_task = (
                f"{adapted_task}\n\nExperimental tool derivation note:\n"
                "- This attempt may use an experimental Python tool draft if it has already been derived, validated, "
                "and appears in Available Tools for this attempt.\n"
                "- Do not call a speculative new tool name unless it already exists in Available Tools.\n"
                "- Use existing DSL primitives and already-registered tools to gather repair and optimization evidence.\n"
                "- If prior derived/experimental tools failed, you may bypass them and solve the task directly in DSL."
            )
        elif original_action_type == "derive_composite_tool":
            adapted_task = (
                f"{adapted_task}\n\nComposite tool derivation note:\n"
                "- Do not call a speculative composite tool name in this attempt unless it already exists in Available Tools.\n"
                "- Use existing registered tools and DSL steps; the composite tool draft will be derived from this attempt's evidence."
            )
        strategy_guidance = _build_pilot_strategy_guidance(task)
        if strategy_guidance:
            adapted_task = f"{adapted_task}\n\nPilot strategy guidance:\n{strategy_guidance}"
        optimization_diversity_note = _build_optimization_diversity_note(previous_attempts or [])
        if optimization_diversity_note:
            adapted_task = f"{adapted_task}\n\nOptimization diversity note:\n{optimization_diversity_note}"
        repair_context = _latest_attempt_repair_context(previous_attempts or [])
        if repair_context and previous_attempts:
            adapted_task = (
                f"{adapted_task}\n\nRepair context:\n{repair_context}\n"
                "Treat the latest failure evidence as concrete repair targets for this next attempt. "
                "You may repair by changing DSL steps, switching tools, reusing a derived tool, or deriving a new "
                "experimental tool when planner instructions point that way. All tool arguments must match schema exactly."
            )
        if attempt_id:
            attempt_suffix = _attempt_suffix(attempt_id)
            adapted_task = (
                f"{adapted_task}\n\nPilot attempt note:\n"
                f"- This is pilot attempt `{attempt_id}`.\n"
                f"- If you write external files, use attempt-distinguishable filenames such as `*_"
                f"{attempt_suffix}` before the extension to avoid overwriting previous attempt outputs."
            )

        agent = create_agent_adapter(
            self.config,
            _visible_tool_schemas(self.config, self.registry),
            dataset_schemas,
            llm_provider=self.llm_provider,
        )
        with llm_trace_context(scope="core", caller="pipeline_generator", attempt_id=attempt_id):
            pipeline, metadata = agent.generate_pipeline(adapted_task)
        if _should_retry_duplicate_optimization_pipeline(previous_attempts or [], pipeline):
            retry_task = (
                f"{adapted_task}\n\nImportant regeneration instruction:\n"
                "- The draft you just produced is too close to the previous successful attempt.\n"
                "- Regenerate a materially different optimization attempt.\n"
                "- Do not only change filenames, variable names, log wording, or lightly wrap the same tool result.\n"
                "- Change the DSL structure, the primary tool path, the result contract, the robustness strategy, "
                "or the performance/throughput approach."
            )
            with llm_trace_context(scope="core", caller="pipeline_generator", attempt_id=attempt_id):
                pipeline, metadata = agent.generate_pipeline(retry_task)
        elif _should_retry_duplicate_failure_pipeline(previous_attempts or [], pipeline):
            retry_task = (
                f"{adapted_task}\n\nImportant regeneration instruction:\n"
                "- Recent failed attempts repeated the same primary tool path and the new draft is still too similar.\n"
                "- Regenerate a materially different repair attempt.\n"
                "- Do not keep wrapping or post-processing the same failing tool output in slightly different ways.\n"
                "- Prefer a different main strategy such as direct DSL filtering, load_dataset filters, a different "
                "tool path, or deriving/fixing tool code explicitly."
            )
            with llm_trace_context(scope="core", caller="pipeline_generator", attempt_id=attempt_id):
                pipeline, metadata = agent.generate_pipeline(retry_task)
        if attempt_id:
            pipeline = _stabilize_attempt_write_targets(pipeline, attempt_id)
        pipeline = _stabilize_pipeline_logging(pipeline)
        if _is_broad_security_audit_task(task):
            pipeline = _stabilize_security_audit_result_logging(pipeline)
            pipeline = _stabilize_security_checker_failover(pipeline, previous_attempts or [])
        return pipeline, {
            "stage": "pipeline_generator",
            "status": "success",
            "model": metadata.get("model", self.config.agent.model),
            "elapsed_seconds": metadata.get("elapsed_seconds"),
            "error": None,
        }

    def _build_security_checker_recommendation_message(self, task: str, user_reply: str) -> str:
        return RunCoordinator._build_security_checker_recommendation_message(self, task, user_reply)

    def _security_checker_recommendation_sets(self, task: str) -> dict[str, list[str]]:
        return RunCoordinator._security_checker_recommendation_sets(self, task)

    def _resolve_security_checker_reply(
        self,
        task: str,
        missing_items: list[str],
        user_reply: str,
    ) -> tuple[list[str], str | None]:
        return RunCoordinator._resolve_security_checker_reply(self, task, missing_items, user_reply)

    def _judge_attempt(
        self,
        task: str,
        pipeline: str,
        execution: dict[str, Any],
        previous_attempts: list[dict[str, Any]],
        execution_latency_s: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        fallback = {
            "goal_satisfied": execution["success"],
            "score": 1.0 if execution["success"] else 0.0,
            "failure_type": "none" if execution["success"] else "execution_failure",
            "capability_gap": {} if execution["success"] else {"reason": execution.get("error", "")},
            "recommended_next_action": "stop_success" if execution["success"] else "mutate_pipeline",
            "reason": "Heuristic judge fallback.",
        }
        fallback_llm = {
            "stage": "judge",
            "status": "disabled" if self.llm_provider is None else "fallback",
            "model": self.config.agent.model,
            "elapsed_seconds": None,
            "error": None,
        }
        if self.llm_provider is None:
            fallback = _apply_execution_signal_judge_policy(task, execution, fallback)
            fallback = _attach_execution_domain_metrics(fallback, execution)
            return fallback, fallback_llm

        prompt = _build_judge_prompt(
            task,
            pipeline,
            execution,
            previous_attempts,
            execution_latency_s=execution_latency_s,
        )
        start_time = perf_counter()
        try:
            with llm_trace_context(scope="core", caller="judge"):
                result = self.llm_provider.generate_json(self.config.agent.model, prompt)
        except Exception as e:
            fallback["reason"] = f"Heuristic judge fallback. LLM judge error: {type(e).__name__}: {e}"
            fallback_llm["elapsed_seconds"] = round(perf_counter() - start_time, 2)
            fallback_llm["error"] = f"{type(e).__name__}: {e}"
            result_payload = execution.get("result", {}) if isinstance(execution.get("result"), dict) else {}
            if _looks_like_security_audit_result(result_payload):
                fallback = self._apply_security_audit_judge_rubric(task, pipeline, execution, fallback)
            fallback = _apply_execution_signal_judge_policy(task, execution, fallback)
            fallback = _attach_execution_domain_metrics(fallback, execution)
            fallback = _apply_pipeline_intent_judge_guard(task, pipeline, execution, fallback)
            fallback = _attach_execution_efficiency_metrics(fallback, execution_latency_s)
            return fallback, fallback_llm

        for key, value in fallback.items():
            result.setdefault(key, value)
        result = _normalize_judge_result(result, fallback)
        result["score"] = _normalize_judge_score(result.get("score", fallback["score"]))
        result = self._apply_security_audit_judge_rubric(task, pipeline, execution, result)
        result = _apply_execution_signal_judge_policy(task, execution, result)
        result = _attach_execution_domain_metrics(result, execution)
        result = _apply_pipeline_intent_judge_guard(task, pipeline, execution, result)
        result = _attach_execution_efficiency_metrics(result, execution_latency_s)
        return result, {
            "stage": "judge",
            "status": "success",
            "model": self.config.agent.model,
            "elapsed_seconds": round(perf_counter() - start_time, 2),
            "error": None,
        }

    def _apply_security_audit_judge_rubric(
        self,
        task: str,
        pipeline: str,
        execution: dict[str, Any],
        judge: dict[str, Any],
    ) -> dict[str, Any]:
        if not _is_broad_security_audit_task(task):
            return judge
        if not execution.get("success"):
            return judge

        result_payload = execution.get("result", {}) if isinstance(execution.get("result"), dict) else {}
        checker_names = _extract_checker_names_from_pipeline(pipeline)
        required_risk_categories = ["pii", "secret", "toxicity", "harmful"]
        checker_risk_categories = _extract_risk_categories_from_checker_names(checker_names)
        covered_risk_categories = [
            category for category in required_risk_categories
            if category in checker_risk_categories
        ]
        coverage_ratio = len(covered_risk_categories) / len(required_risk_categories)

        if coverage_ratio < 1.0:
            security_score = float(result_payload.get("security_score", 0.0))
            flagged_rate = float(result_payload.get("flagged_rate", 1.0))
            judge_score = round(min(0.39, 0.1 + 0.29 * coverage_ratio), 2)
            missing_categories = [
                category for category in required_risk_categories
                if category not in checker_risk_categories
            ]
            return {
                **judge,
                "goal_satisfied": False,
                "score": judge_score,
                "failure_type": "insufficient_security_coverage",
                "recommended_next_action": "mutate_pipeline",
                "reason": (
                    "The pipeline executed successfully, but this broad security audit did not cover "
                    "enough required risk categories. "
                    f"Covered categories: {covered_risk_categories or checker_risk_categories or ['none']}; "
                    f"missing categories: {missing_categories}; "
                    f"required categories: {required_risk_categories}."
                ),
                "capability_gap": {
                    "type": "insufficient_security_risk_coverage",
                    "required_risk_categories": required_risk_categories,
                    "covered_risk_categories": covered_risk_categories,
                    "missing_risk_categories": missing_categories,
                    "covered_checkers": checker_names,
                    "coverage_ratio": round(coverage_ratio, 2),
                },
                "domain_metrics": {
                    "security_score": security_score,
                    "flagged_rate": flagged_rate,
                    "flagged_samples": result_payload.get("flagged_samples"),
                    "total_samples": result_payload.get("total_samples"),
                    "coverage_ratio": round(coverage_ratio, 2),
                    "covered_risk_categories": covered_risk_categories,
                    "required_risk_categories": required_risk_categories,
                    "checker_risk_categories": checker_risk_categories,
                },
            }

        if not _looks_like_security_audit_result(result_payload):
            return judge

        security_score = float(result_payload.get("security_score", 0.0))
        flagged_rate = float(result_payload.get("flagged_rate", 1.0))
        passed = bool(result_payload.get("passed", False))
        security_component = max(0.0, min(1.0, security_score / 100.0))
        flagged_component = max(0.0, min(1.0, 1.0 - flagged_rate))
        pass_component = 1.0 if passed else 0.0
        domain_component = (0.5 * security_component) + (0.3 * flagged_component) + (0.2 * pass_component)
        judge_score = round(min(1.0, 0.4 + (0.6 * domain_component)), 2)
        goal_satisfied = coverage_ratio == 1.0 and passed and security_score >= 80.0

        updated = {
            **judge,
            "goal_satisfied": goal_satisfied,
            "score": judge_score,
            "failure_type": "none" if goal_satisfied else "insufficient_security_quality",
            "recommended_next_action": "stop_success" if goal_satisfied else "mutate_pipeline",
            "reason": (
                "Broad security audit baseline coverage is satisfied."
                if goal_satisfied
                else (
                    "Broad security audit baseline coverage is satisfied, "
                    "but the audit quality metrics are still below the target threshold."
                )
            ),
            "domain_metrics": {
                "security_score": security_score,
                "flagged_rate": flagged_rate,
                "flagged_samples": result_payload.get("flagged_samples"),
                "total_samples": result_payload.get("total_samples"),
                "coverage_ratio": round(coverage_ratio, 2),
                "covered_risk_categories": covered_risk_categories,
                "required_risk_categories": required_risk_categories,
                "checker_risk_categories": checker_risk_categories,
                "domain_component": round(domain_component, 2),
            },
        }
        if not goal_satisfied:
            updated["capability_gap"] = {
                "type": "insufficient_security_quality",
                "required_risk_categories": required_risk_categories,
                "covered_risk_categories": covered_risk_categories,
                "covered_checkers": checker_names,
                "security_score": security_score,
                "flagged_rate": flagged_rate,
            }
        return updated

    def _emit(
        self,
        event_handler: Callable[[dict[str, Any]], None] | None,
        event: dict[str, Any],
    ) -> None:
        if event_handler is not None:
            event_handler(event)

    def _request_checkpoint_decision(
        self,
        job_id: str,
        checkpoint_type: str,
        checkpoint_payload: dict[str, Any],
        ask_user: bool,
        checkpoint_handler: Callable[[dict[str, Any]], dict[str, Any]] | None,
        event_handler: Callable[[dict[str, Any]], None] | None,
        fallback_decision: str = "continue",
    ) -> dict[str, Any]:
        if not ask_user or checkpoint_handler is None:
            self.job_manager.update_checkpoint(
                job_id,
                checkpoint_type=checkpoint_type,
                checkpoint_state="resolved",
                checkpoint_payload={**checkpoint_payload, "response": {"decision": fallback_decision}},
                status=JobStatus.RUNNING,
            )
            return {"decision": fallback_decision}

        self.job_manager.update_checkpoint(
            job_id,
            checkpoint_type=checkpoint_type,
            checkpoint_state="awaiting_input",
            checkpoint_payload=checkpoint_payload,
            status=JobStatus.PAUSED,
        )
        self._emit(event_handler, {
            "type": "checkpoint_paused",
            "job_id": job_id,
            "checkpoint_type": checkpoint_type,
            "payload": checkpoint_payload,
        })

        response = checkpoint_handler({
            "checkpoint_type": checkpoint_type,
            "payload": checkpoint_payload,
            "job_id": job_id,
        }) or {"decision": "continue"}

        self.job_manager.update_checkpoint(
            job_id,
            checkpoint_type=checkpoint_type,
            checkpoint_state="resolved",
            checkpoint_payload={**checkpoint_payload, "response": response},
            status=JobStatus.RUNNING,
        )
        self._emit(event_handler, {
            "type": "checkpoint_resolved",
            "job_id": job_id,
            "checkpoint_type": checkpoint_type,
            "response": response,
        })
        return response

    def _handle_candidate_checkpoint(
        self,
        job_id: str,
        attempt_id: str,
        candidate: dict[str, Any],
        validation: dict[str, Any],
        judge: dict[str, Any],
        ask_user: bool,
        checkpoint_handler: Callable[[dict[str, Any]], dict[str, Any]] | None,
        event_handler: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        if validation.get("validation_status") == "smoke_failed":
            self.job_manager.update_approval_state(job_id, "validation_failed")
            return {"decision": "skip", "registered_candidate": False}

        self.job_manager.update_approval_state(job_id, "pending_review")
        checkpoint_payload = {
            "candidate_id": candidate["candidate_id"],
            "candidate_type": candidate["candidate_type"],
            "candidate_name": candidate.get("name"),
            "attempt_id": attempt_id,
            "validation_summary": validation.get("validation_summary"),
            "validation_status": validation.get("validation_status"),
            "judge_score": judge.get("score"),
            "attempt_metrics": {
                "judge_score": judge.get("score"),
            },
            "options": ["approve", "reject", "continue"],
        }
        response = self._request_checkpoint_decision(
            job_id=job_id,
            checkpoint_type="candidate_approval",
            checkpoint_payload=checkpoint_payload,
            ask_user=ask_user,
            checkpoint_handler=checkpoint_handler,
            event_handler=event_handler,
            fallback_decision="continue",
        )
        decision = str(response.get("decision", "continue")).lower()
        if decision == "defer":
            self.asset_manager.update_candidate(candidate["candidate_id"], status="awaiting_approval")
            return {"decision": "defer", "registered_candidate": False}
        if decision == "approve":
            asset = self.asset_manager.approve_candidate(candidate["candidate_id"])
            self.job_manager.update_approval_state(job_id, "approved")
            if asset.get("asset_type") == "tool":
                self.asset_manager.register_stable_tools(self.registry)
            return {
                "decision": "approve",
                "approved_asset_id": asset["asset_id"],
                "registered_candidate": False,
            }
        if decision == "reject":
            self.asset_manager.reject_candidate(
                candidate["candidate_id"],
                reason=response.get("reason", "Rejected during pilot checkpoint."),
            )
            self.job_manager.update_approval_state(job_id, "rejected")
            return {"decision": "reject", "registered_candidate": False}

        self.asset_manager.update_candidate(candidate["candidate_id"], status="awaiting_approval")
        return {"decision": "continue", "registered_candidate": True}

    def _validate_candidate(
        self,
        candidate: dict[str, Any],
        pipeline: str,
        dataset_schemas: dict[str, list[str]],
        allow_experimental_tools: bool,
        task: str | None = None,
    ) -> dict[str, Any]:
        candidate_type = candidate.get("candidate_type")
        if candidate_type == "pipeline":
            return {
                "validation_status": "smoke_passed",
                "validation_summary": "Validated by successful pipeline execution.",
                "smoke_test_result": {"status": "from_successful_attempt"},
                "benchmark_result": {"status": "not_configured"},
                "status": "awaiting_approval",
            }

        if candidate_type == "experimental_python_tool" and not allow_experimental_tools:
            return {
                "validation_status": "smoke_failed",
                "validation_summary": "Experimental tools are disabled for this pilot run.",
                "smoke_test_result": {"status": "skipped", "reason": "experimental_disabled"},
                "benchmark_result": {"status": "not_configured"},
                "status": "smoke_failed",
            }

        try:
            if candidate_type == "experimental_python_tool":
                compile(Path(candidate["code_path"]).read_text(encoding="utf-8"), candidate["code_path"], "exec")
                tool = self.asset_manager._load_python_tool(candidate["code_path"])
            else:
                from .composite_tool import CompositeDerivedTool

                tool = CompositeDerivedTool(candidate)
            registry_loaded = tool is not None
        except Exception as e:
            return {
                "validation_status": "smoke_failed",
                "validation_summary": f"Tool load failed: {type(e).__name__}: {e}",
                "smoke_test_result": {"status": "load_failed", "error": f"{type(e).__name__}: {e}"},
                "benchmark_result": {"status": "not_configured"},
                "status": "smoke_failed",
            }

        sample_data = self._sample_data_for_pipeline(pipeline, dataset_schemas, task=task)
        smoke_kwargs = self._build_smoke_kwargs(tool.parameters, sample_data)
        sample_dataset_name = (
            _extract_dataset_name_from_pipeline(pipeline)
            or _extract_dataset_name_from_user_reply(["dataset_name"], task or "", dataset_schemas)
            or "sample_data"
        )
        try:
            from tools.base_tool import ToolContext

            class _SmokeLogger:
                def info(self, *_args, **_kwargs): pass
                def warning(self, *_args, **_kwargs): pass
                def error(self, *_args, **_kwargs): pass

            smoke_output = tool.run(
                ToolContext(
                    job_id="smoke_test",
                    logger=_SmokeLogger(),
                    config=self.config.__dict__ if hasattr(self.config, "__dict__") else {},
                    llm=getattr(self.executor, "tool_llm_provider", None) or getattr(self.executor, "llm_provider", None),
                    datasets={
                        "sample_data": sample_data,
                        sample_dataset_name: sample_data,
                    },
                ),
                **smoke_kwargs,
            )
        except Exception as e:
            return {
                "validation_status": "smoke_failed",
                "validation_summary": f"Smoke test failed: {type(e).__name__}: {e}",
                "smoke_test_result": {
                    "status": "execution_failed",
                    "error": f"{type(e).__name__}: {e}",
                    "registry_loaded": registry_loaded,
                },
                "benchmark_result": {"status": "not_configured"},
                "status": "smoke_failed",
            }

        return {
            "validation_status": "smoke_passed",
            "validation_summary": "Candidate loaded and passed a smoke run on a small sample.",
            "smoke_test_result": {
                "status": "passed",
                "registry_loaded": registry_loaded,
                "sample_size": len(sample_data),
                "output_preview": smoke_output.get("result") if isinstance(smoke_output, dict) else smoke_output,
            },
            "benchmark_result": {"status": "not_configured"},
            "status": "awaiting_approval",
        }

    def _sample_data_for_pipeline(
        self,
        pipeline: str,
        dataset_schemas: dict[str, list[str]],
        task: str | None = None,
    ) -> list[dict[str, Any]]:
        dataset_name = (
            _extract_dataset_name_from_pipeline(pipeline)
            or _extract_dataset_name_from_user_reply(["dataset_name"], task or "", dataset_schemas)
        )
        if dataset_name and getattr(self.executor, "database", None) is not None:
            try:
                sample = self.executor.database.read_table(dataset_name, limit=1)
                if sample:
                    return sample
            except Exception:
                pass
        if dataset_name and dataset_name in dataset_schemas:
            return [{column: None for column in dataset_schemas[dataset_name]}]
        return []

    def _build_smoke_kwargs(self, parameters: dict[str, Any], sample_data: list[dict[str, Any]]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        properties = parameters.get("properties", {})
        required = parameters.get("required", [])
        for name in required:
            spec = properties.get(name, {})
            if name == "data":
                kwargs[name] = sample_data
            elif "default" in spec:
                kwargs[name] = spec["default"]
            elif spec.get("type") == "array":
                kwargs[name] = []
            elif spec.get("type") == "object":
                kwargs[name] = {}
            elif spec.get("type") == "integer":
                kwargs[name] = 0
            elif spec.get("type") == "number":
                kwargs[name] = 0
            elif spec.get("type") == "boolean":
                kwargs[name] = False
            else:
                kwargs[name] = ""
        return kwargs

    def _derive_attempt_metrics(
        self,
        attempt_id: str,
        pipeline: str,
        planner_llm: dict[str, Any],
        pipeline_llm: dict[str, Any],
        execution: dict[str, Any],
        execution_latency_s: float,
        judge: dict[str, Any],
        total_attempt_latency_s: float,
        derived_candidate_count: int,
    ) -> dict[str, Any]:
        metrics = {
            "attempt_id": attempt_id,
            "planning_latency_s": planner_llm.get("elapsed_seconds"),
            "pipeline_generation_latency_s": pipeline_llm.get("elapsed_seconds"),
            "execution_latency_s": execution_latency_s,
            "total_attempt_latency_s": total_attempt_latency_s,
            "pipeline_execution_latency_s": execution_latency_s,
            "attempt_total_latency_s": total_attempt_latency_s,
            "success": execution.get("success", False),
            "judge_score": judge.get("score", 0.0),
            "tool_count": _count_run_tool_calls(pipeline),
            "derived_candidate_count": derived_candidate_count,
        }
        if isinstance(execution.get("result"), dict):
            for key in ["security_score", "flagged_rate", "flagged_samples", "total_samples"]:
                if key in execution["result"]:
                    metrics[key] = execution["result"][key]
        return metrics

    def _prepare_python_tool_candidate_for_attempt(
        self,
        *,
        job_id: str,
        task: str,
        attempt_id: str,
        previous_attempts: list[dict[str, Any]],
        dataset_schemas: dict[str, list[str]],
        allow_experimental_tools: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        seed_attempt = previous_attempts[-1] if previous_attempts else {}
        seed_pipeline = str(seed_attempt.get("pipeline", "") or "")
        seed_execution = seed_attempt.get("execution") if isinstance(seed_attempt.get("execution"), dict) else None
        seed_judge = seed_attempt.get("judge") if isinstance(seed_attempt.get("judge"), dict) else None

        with llm_trace_context(job_id=job_id, mode="pilot", attempt_id=attempt_id):
            candidate, code = self._derive_python_tool(
                task=task,
                pipeline=seed_pipeline,
                source_attempts=[attempt_id],
                execution=seed_execution,
                judge=seed_judge,
            )
        self.asset_manager.save_candidate(candidate, python_code=code)
        self.job_manager.add_candidate_asset(job_id, candidate["candidate_id"])
        validation_seed_pipeline = seed_pipeline or _build_candidate_validation_seed_pipeline(
            task,
            candidate,
            dataset_schemas,
        )
        validation = self._validate_candidate(
            candidate=candidate,
            pipeline=validation_seed_pipeline,
            dataset_schemas=dataset_schemas,
            allow_experimental_tools=allow_experimental_tools,
            task=task,
        )
        candidate.update(validation)
        self.asset_manager.update_candidate(candidate["candidate_id"], **validation)
        if validation.get("validation_status") == "smoke_passed":
            self.asset_manager.register_candidate_tools(self.registry, allow_experimental=True)
        return candidate, validation

    def _build_pilot_summary(self, attempts: list[dict[str, Any]]) -> dict[str, Any]:
        if not attempts:
            return {}
        metrics = [attempt.get("attempt_metrics", {}) for attempt in attempts]
        best_attempt = max(attempts, key=lambda attempt: float(attempt.get("judge", {}).get("score", 0.0)))
        deltas: list[dict[str, Any]] = []
        interesting_keys = [
            "judge_score",
            "attempt_total_latency_s",
            "pipeline_execution_latency_s",
            "total_attempt_latency_s",
            "execution_latency_s",
            "security_score",
            "flagged_rate",
            "flagged_samples",
        ]
        for previous, current in zip(metrics, metrics[1:]):
            delta = {"from_attempt": previous.get("attempt_id"), "to_attempt": current.get("attempt_id"), "changes": {}}
            for key in interesting_keys:
                if previous.get(key) is None or current.get(key) is None:
                    continue
                try:
                    delta["changes"][key] = round(float(current[key]) - float(previous[key]), 4)
                except Exception:
                    continue
            deltas.append(delta)

        return {
            "attempt_count": len(attempts),
            "first_attempt_id": attempts[0]["attempt_id"],
            "best_attempt_id": best_attempt["attempt_id"],
            "final_attempt_id": attempts[-1]["attempt_id"],
            "candidate_ids": _dedupe_preserve_order([
                candidate.get("candidate_id")
                for attempt in attempts
                for candidate in _attempt_candidates(attempt)
                if candidate.get("candidate_id")
            ]),
            "attempt_deltas": deltas,
            "best_vs_first": {
                "judge_score_delta": round(
                    float(best_attempt.get("judge", {}).get("score", 0.0))
                    - float(attempts[0].get("judge", {}).get("score", 0.0)),
                    4,
                ),
                "security_score_delta": _metric_delta(best_attempt.get("attempt_metrics", {}), attempts[0].get("attempt_metrics", {}), "security_score"),
                "attempt_total_latency_delta": _metric_delta(best_attempt.get("attempt_metrics", {}), attempts[0].get("attempt_metrics", {}), "attempt_total_latency_s"),
                "pipeline_execution_latency_delta": _metric_delta(best_attempt.get("attempt_metrics", {}), attempts[0].get("attempt_metrics", {}), "pipeline_execution_latency_s"),
                "latency_delta": _metric_delta(best_attempt.get("attempt_metrics", {}), attempts[0].get("attempt_metrics", {}), "total_attempt_latency_s"),
            },
            "best_vs_final": {
                "judge_score_delta": round(
                    float(best_attempt.get("judge", {}).get("score", 0.0))
                    - float(attempts[-1].get("judge", {}).get("score", 0.0)),
                    4,
                ),
                "security_score_delta": _metric_delta(best_attempt.get("attempt_metrics", {}), attempts[-1].get("attempt_metrics", {}), "security_score"),
                "attempt_total_latency_delta": _metric_delta(best_attempt.get("attempt_metrics", {}), attempts[-1].get("attempt_metrics", {}), "attempt_total_latency_s"),
                "pipeline_execution_latency_delta": _metric_delta(best_attempt.get("attempt_metrics", {}), attempts[-1].get("attempt_metrics", {}), "pipeline_execution_latency_s"),
                "latency_delta": _metric_delta(best_attempt.get("attempt_metrics", {}), attempts[-1].get("attempt_metrics", {}), "total_attempt_latency_s"),
            },
        }

    def _derive_composite_tool(
        self,
        task: str,
        pipeline: str,
        source_attempts: list[str],
        dataset_schemas: dict[str, list[str]],
    ) -> dict[str, Any]:
        candidate_id = _stable_id("cand_comp", task + pipeline + json.dumps(source_attempts))
        source_tool_contexts = _collect_tool_contexts_from_pipeline(pipeline, self.registry, include_code=False)
        source_tool_names = [item["tool_name"] for item in source_tool_contexts]
        fallback = {
            "candidate_id": candidate_id,
            "candidate_type": "composite_tool",
            "name": _normalize_candidate_tool_name(
                "",
                task=task,
                pipeline=pipeline,
                candidate_type="composite_tool",
                source_tool_names=source_tool_names,
            ),
            "description": "Composite tool candidate derived during pilot mode.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "data": {
                        "type": "array",
                        "items": {"type": "object"},
                    }
                },
                "required": ["data"],
            },
            "steps": [
                {
                    "type": "run_tool",
                    "tool_name": "security_audit",
                    "kwargs": {"data": "$input.data"},
                    "output": "audit",
                }
            ],
            "result": {"audit": "$audit"},
            "validation_criteria": ["Runs successfully on benchmark data."],
            "source_attempts": source_attempts,
            "status": "draft",
            "pipeline_template": pipeline,
            "tool_domains": ["security_audit_tools", "data_selection_tools", "data_scoring_tools"],
            "source_tool_names": source_tool_names,
        }
        if self.llm_provider is None:
            return fallback

        prompt = _build_composite_tool_prompt(
            task,
            pipeline,
            source_attempts,
            dataset_schemas,
            source_tool_contexts,
        )
        try:
            with llm_trace_context(scope="core", caller="toolsmith"):
                candidate = self.llm_provider.generate_json(self.config.agent.model, prompt)
        except Exception:
            return fallback

        for key, value in fallback.items():
            candidate.setdefault(key, value)
        candidate["name"] = _normalize_candidate_tool_name(
            candidate.get("name", ""),
            task=task,
            pipeline=pipeline,
            candidate_type="composite_tool",
            source_tool_names=source_tool_names,
        )
        candidate["candidate_type"] = "composite_tool"
        return candidate

    def _derive_python_tool(
        self,
        task: str,
        pipeline: str,
        source_attempts: list[str],
        execution: dict[str, Any] | None = None,
        judge: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        candidate_id = _stable_id("cand_py", task + pipeline + json.dumps(source_attempts))
        source_tool_contexts = _collect_tool_contexts_from_pipeline(pipeline, self.registry, include_code=True)
        source_tool_names = [item["tool_name"] for item in source_tool_contexts]
        fallback_name = _normalize_candidate_tool_name(
            "",
            task=task,
            pipeline=pipeline,
            candidate_type="experimental_python_tool",
            source_tool_names=source_tool_names,
        )
        fallback_candidate = {
            "candidate_id": candidate_id,
            "candidate_type": "experimental_python_tool",
            "name": fallback_name,
            "description": "Experimental Python tool draft derived during pilot mode.",
            "validation_criteria": ["Module compiles successfully.", "Manual review required before promotion."],
            "source_attempts": source_attempts,
            "status": "draft",
            "tool_domains": ["security_audit_tools", "data_selection_tools", "data_scoring_tools"],
            "pipeline_template": pipeline,
            "source_tool_names": source_tool_names,
            "review_comments": [],
            "enhancement_rationale": "",
            "behavior_changes": [],
            "compatibility_notes": "",
        }
        fallback_code = _default_python_tool_code(fallback_candidate["name"], fallback_candidate["description"])
        if self.llm_provider is None:
            return fallback_candidate, fallback_code

        prompt = _build_python_tool_prompt(
            task,
            pipeline,
            source_attempts,
            source_tool_contexts=source_tool_contexts,
            execution_summary=_toolsmith_execution_summary(execution),
            judge_summary=_toolsmith_judge_summary(judge),
        )
        try:
            with llm_trace_context(scope="core", caller="toolsmith"):
                response = self.llm_provider.generate_json(self.config.agent.model, prompt)
        except Exception:
            return fallback_candidate, fallback_code

        candidate = fallback_candidate | {
            "name": _normalize_candidate_tool_name(
                response.get("name", fallback_candidate["name"]),
                task=task,
                pipeline=pipeline,
                candidate_type="experimental_python_tool",
                source_tool_names=source_tool_names,
            ),
            "description": response.get("description", fallback_candidate["description"]),
            "validation_criteria": response.get("validation_criteria", fallback_candidate["validation_criteria"]),
            "review_comments": response.get("review_comments", fallback_candidate["review_comments"]),
            "enhancement_rationale": response.get("enhancement_rationale", fallback_candidate["enhancement_rationale"]),
            "behavior_changes": response.get("behavior_changes", fallback_candidate["behavior_changes"]),
            "compatibility_notes": response.get("compatibility_notes", fallback_candidate["compatibility_notes"]),
        }
        code = _force_python_tool_name(
            response.get("code", fallback_code),
            candidate["name"],
        )
        compile(code, f"{candidate_id}.py", "exec")
        return candidate, code

    def _derive_pipeline_candidate(
        self,
        job_id: str,
        task: str,
        pipeline: str,
        source_attempts: list[str],
        judge: dict[str, Any],
    ) -> dict[str, Any]:
        candidate_id = _stable_id("cand_pipe", job_id + pipeline + json.dumps(source_attempts))
        primary_attempt = source_attempts[0] if source_attempts else "attempt"
        return {
            "candidate_id": candidate_id,
            "candidate_type": "pipeline",
            "name": f"pipeline_{job_id}_{primary_attempt}",
            "description": f"Pipeline candidate derived from pilot job {job_id}.",
            "pipeline": pipeline,
            "source_attempts": source_attempts,
            "status": "draft",
            "validation_criteria": ["Manual review required before submit.", "Judge score should be acceptable."],
            "tool_domains": ["security_audit_tools", "data_selection_tools", "data_scoring_tools"],
            "metadata": {
                "job_id": job_id,
                "task": task,
                "judge": judge,
            },
        }


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _run_shared_clarification_loop(
    owner: Any,
    *,
    task: str,
    dataset_schemas: dict[str, list[str]],
    max_rounds: int,
    decision_provider: Callable[[str, list[dict[str, Any]], list[dict[str, str]], int], tuple[dict[str, Any], dict[str, Any]]],
    response_provider: Callable[[dict[str, Any], int, dict[str, Any], str], dict[str, Any]],
    ready_status: str,
    not_requested_status: str,
    paused_status: str | None = None,
    exhausted_status: str,
    exhausted_reason: str,
    allow_escalation: bool,
) -> dict[str, Any]:
    current_task = task
    transcript: list[dict[str, Any]] = []
    messages: list[dict[str, str]] = [{"role": "user", "content": task}]
    resolved_slots: dict[str, Any] = {}
    initial_dataset_name = _extract_dataset_name_from_user_reply(
        ["dataset_name"],
        task,
        dataset_schemas,
    )
    if initial_dataset_name:
        resolved_slots["dataset_name"] = initial_dataset_name
    resolved_slots |= _extract_schema_required_slots_from_text(
        task,
        owner.registry,
        dataset_schemas,
    )
    outstanding_missing_items: list[str] = []
    missing_item_retry_counts: dict[str, int] = {}
    last_user_reply = ""
    forced_followup_message = ""
    forced_followup_missing_items: list[str] = []
    forced_followup_suggested_defaults: dict[str, Any] = {}

    for turn in range(1, max_rounds + 1):
        if forced_followup_message:
            decision = {
                "status": "clarifying",
                "assistant_message": forced_followup_message,
                "ready_to_execute": False,
                "resolved_task": current_task,
                "resolved_slots": {},
                "missing_items": forced_followup_missing_items or (
                    ["checker_names"] if _task_targets_security_checker_selection(current_task) else outstanding_missing_items
                ),
                "suggested_defaults": forced_followup_suggested_defaults
                or owner._default_slots_for_missing_items(
                    current_task,
                    forced_followup_missing_items or outstanding_missing_items,
                ),
                "response_mode": "answer_then_ask",
            }
            llm_meta = {
                "stage": "clarification",
                "status": "programmatic_followup",
                "model": None,
                "elapsed_seconds": 0.0,
                "error": None,
            }
            forced_followup_message = ""
            forced_followup_missing_items = []
            forced_followup_suggested_defaults = {}
        else:
            decision, llm_meta = decision_provider(current_task, transcript, messages, turn)
            decision["missing_items"] = _normalize_missing_items(current_task, decision.get("missing_items", []))

        required_slot_specs = _schema_required_slot_specs(current_task, owner.registry, dataset_schemas)
        decision["missing_items"] = _merge_missing_items(
            decision.get("missing_items", []),
            _missing_required_slots(required_slot_specs, resolved_slots),
            resolved_slots,
        )
        outstanding_missing_items = _merge_missing_items(
            outstanding_missing_items,
            decision.get("missing_items", []),
            resolved_slots,
        )
        ready_guard = RunCoordinator._guard_clarification_ready(
            owner,
            task=current_task,
            decision=decision,
            resolved_slots=resolved_slots,
            outstanding_missing_items=outstanding_missing_items,
            last_user_reply=last_user_reply,
            dataset_schemas=dataset_schemas,
            required_slot_specs=required_slot_specs,
        )
        escalation_guard = RunCoordinator._guard_clarification_escalation(
            owner,
            task=current_task,
            decision=decision,
            outstanding_missing_items=outstanding_missing_items,
            last_user_reply=last_user_reply,
            turn=turn,
            max_rounds=max_rounds,
            dataset_schemas=dataset_schemas,
            required_slot_specs=required_slot_specs,
        )

        if (
            decision["status"] == "clarifying"
            and not outstanding_missing_items
            and _asks_only_optional_execution_detail(decision)
        ):
            return {
                "status": ready_status,
                "turns": len(transcript),
                "transcript": transcript,
                "resolved_task": current_task,
                "resolved_slots": resolved_slots | decision.get("resolved_slots", {}),
            }

        if decision["status"] == "ready" and ready_guard["allow_ready"]:
            trusted_decision_slots = _filter_resolved_slots_by_missing_items(
                decision.get("resolved_slots", {}),
                outstanding_missing_items or decision.get("missing_items", []),
            )
            if turn == 1 and not transcript and not decision.get("assistant_message"):
                final_status = not_requested_status
            else:
                final_status = ready_status
            return {
                "status": final_status,
                "turns": len(transcript),
                "transcript": transcript,
                "resolved_task": decision.get("resolved_task", current_task),
                "resolved_slots": resolved_slots | trusted_decision_slots,
            }
        elif decision["status"] == "ready":
            decision = {
                **decision,
                "status": "clarifying",
                "assistant_message": ready_guard["followup_message"] or decision.get("assistant_message", ""),
                "resolved_slots": {},
                "missing_items": ready_guard.get("missing_items", decision.get("missing_items", [])),
                "suggested_defaults": ready_guard.get("suggested_defaults", decision.get("suggested_defaults", {})),
            }

        if allow_escalation and decision["status"] == "escalate_to_pilot":
            if escalation_guard["continue_clarifying"]:
                decision = {
                    **decision,
                    "status": "clarifying",
                    "assistant_message": escalation_guard["followup_message"],
                    "resolved_slots": {},
                    "missing_items": escalation_guard.get("missing_items", decision.get("missing_items", [])),
                    "suggested_defaults": escalation_guard.get("suggested_defaults", decision.get("suggested_defaults", {})),
                }
            else:
                return {
                    "status": exhausted_status,
                    "turns": len(transcript),
                    "transcript": transcript,
                    "resolved_task": current_task,
                    "resolved_slots": resolved_slots,
                    "handoff_reason": decision.get("handoff_reason", exhausted_reason),
                }

        checkpoint_payload = {
            "prompt": decision.get("assistant_message", "Please clarify the task."),
            "turn": turn,
            "suggested_defaults": decision.get("suggested_defaults", {}),
            "missing_items": decision.get("missing_items", []),
            "response_mode": decision.get("response_mode", "ask_user"),
        }
        response = response_provider(checkpoint_payload, turn, llm_meta, current_task)
        if paused_status is not None and response.get("decision") == "defer":
            return {
                "status": paused_status,
                "turns": len(transcript),
                "transcript": transcript,
                "resolved_task": current_task,
                "resolved_slots": resolved_slots,
                "reason": checkpoint_payload["prompt"],
            }

        user_reply = _coerce_text_block(response.get("answer", ""))
        last_user_reply = user_reply
        turn_record = {
            "turn": turn,
            "assistant_message": checkpoint_payload["prompt"],
            "response_mode": checkpoint_payload["response_mode"],
            "missing_items": checkpoint_payload["missing_items"],
            "suggested_defaults": checkpoint_payload["suggested_defaults"],
            "llm": llm_meta,
            "user_reply": user_reply,
        }
        transcript.append(turn_record)

        messages.append({"role": "assistant", "content": checkpoint_payload["prompt"]})
        messages.append({"role": "user", "content": user_reply})

        resolved_slots_delta: dict[str, Any] = {}
        dataset_name = _extract_dataset_name_from_user_reply(
            missing_items=["dataset_name"],
            user_reply=user_reply,
            dataset_schemas=dataset_schemas,
        )
        if dataset_name:
            resolved_slots_delta["dataset_name"] = dataset_name
        security_reply_missing_items = outstanding_missing_items or decision.get("missing_items", [])
        if (
            _task_targets_security_checker_selection(current_task)
            and "checker" in checkpoint_payload["prompt"].lower()
        ):
            security_reply_missing_items = ["checker_names"]
        resolved_slots_delta |= _extract_generic_slot_values_from_text(
            required_slot_specs,
            outstanding_missing_items or decision.get("missing_items", []),
            user_reply,
            allow_freeform_single_value=True,
            dataset_schemas=dataset_schemas,
            preferred_dataset_name=_slot_value_from_resolved_slots(
                "dataset_name",
                resolved_slots | resolved_slots_delta,
            ),
        )
        recommended_checker_names, recommendation_mode = owner._resolve_security_checker_reply(
            task=current_task,
            missing_items=security_reply_missing_items,
            user_reply=user_reply,
        )
        if recommended_checker_names:
            resolved_slots_delta["checker_names"] = recommended_checker_names
            resolved_slots_delta["selection_mode"] = recommendation_mode or "recommended"
        checker_names = _extract_security_checker_names(
            task=current_task,
            missing_items=security_reply_missing_items,
            user_reply=user_reply,
        )
        if checker_names:
            resolved_slots_delta["checker_names"] = checker_names

        weak_reply = _looks_like_weak_reply(user_reply)
        option_request = _looks_like_option_request(user_reply)
        if _looks_like_default_reply(user_reply):
            computed_defaults = owner._default_slots_for_missing_items(
                task=current_task,
                missing_items=outstanding_missing_items or decision.get("missing_items", []),
            )
            default_slots = computed_defaults or decision.get("suggested_defaults", {})
            resolved_slots_delta |= default_slots
            unresolved_after_default = _find_unresolved_missing_items(
                outstanding_missing_items or decision.get("missing_items", []),
                resolved_slots,
                resolved_slots_delta,
                weak_reply=False,
            )
            turn_record["resolved_slots_delta"] = resolved_slots_delta
            turn_record["guard_forced_continue"] = bool(unresolved_after_default)
            turn_record["guard_reason"] = (
                "Missing required clarification values." if unresolved_after_default else ""
            )
            turn_record["weak_reply"] = False
            resolved_slots = resolved_slots | resolved_slots_delta | {
                "selection_mode": "defaults",
            }
            current_task = _merge_clarification_into_task(
                current_task=current_task,
                assistant_message=checkpoint_payload["prompt"],
                user_reply=user_reply,
                suggested_defaults=decision.get("suggested_defaults", {}),
            )
            current_task = _append_resolved_slots_to_task(current_task, resolved_slots_delta)
            if not unresolved_after_default:
                return {
                    "status": ready_status,
                    "turns": len(transcript),
                    "transcript": transcript,
                    "resolved_task": current_task,
                    "resolved_slots": resolved_slots,
                }
            outstanding_missing_items = unresolved_after_default
            forced_followup_message = _build_missing_slot_followup_message(
                outstanding_missing_items,
                required_slot_specs,
                dataset_schemas,
            )
            forced_followup_missing_items = outstanding_missing_items
            forced_followup_suggested_defaults = owner._default_slots_for_missing_items(
                current_task,
                outstanding_missing_items,
            )
            continue

        resolved_slots_delta |= _filter_resolved_slots_by_missing_items(
            decision.get("resolved_slots", {}),
            outstanding_missing_items or decision.get("missing_items", []),
        )
        unresolved_after_reply = _find_unresolved_missing_items(
            outstanding_missing_items or decision.get("missing_items", []),
            resolved_slots,
            resolved_slots_delta,
            weak_reply=weak_reply or option_request,
        )
        turn_record["resolved_slots_delta"] = resolved_slots_delta
        turn_record["guard_forced_continue"] = bool(unresolved_after_reply)
        turn_record["guard_reason"] = (
            "Missing required clarification values." if unresolved_after_reply else ""
        )
        turn_record["weak_reply"] = weak_reply
        turn_record["option_request"] = option_request

        resolved_slots = resolved_slots | resolved_slots_delta
        outstanding_missing_items = unresolved_after_reply
        current_task = _merge_clarification_into_task(
            current_task=current_task,
            assistant_message=checkpoint_payload["prompt"],
            user_reply=user_reply,
            suggested_defaults=decision.get("suggested_defaults", {}),
        )
        current_task = _append_resolved_slots_to_task(current_task, resolved_slots_delta)
        field_issue = _find_dataset_field_reference_issue(
            task_text=current_task,
            last_user_reply=user_reply,
            resolved_slots=resolved_slots,
            dataset_schemas=dataset_schemas,
        )
        _update_missing_item_retry_counts(
            missing_item_retry_counts,
            outstanding_missing_items,
        )
        if not outstanding_missing_items and resolved_slots_delta:
            if field_issue:
                turn_record["guard_forced_continue"] = True
                turn_record["guard_reason"] = field_issue["message"]
                forced_followup_message = field_issue["message"]
                forced_followup_missing_items = ["filter_field", "filter_value"]
                forced_followup_suggested_defaults = {}
                continue
            return {
                "status": ready_status,
                "turns": len(transcript),
                "transcript": transcript,
                "resolved_task": current_task,
                "resolved_slots": resolved_slots,
            }
        if _should_trust_semantic_user_reply_for_missing_items(
            user_reply=user_reply,
            unresolved_missing_items=outstanding_missing_items,
            retry_counts=missing_item_retry_counts,
        ):
            turn_record["guard_trusted_semantic_reply"] = True
            return {
                "status": ready_status,
                "turns": len(transcript),
                "transcript": transcript,
                "resolved_task": current_task,
                "resolved_slots": resolved_slots,
            }
        if (
            outstanding_missing_items
            and "checker_names" in outstanding_missing_items
            and _task_targets_security_checker_selection(task)
            and _looks_like_recommendation_request(user_reply)
            and not resolved_slots_delta.get("checker_names")
        ):
            forced_followup_message = owner._build_security_checker_recommendation_message(task, user_reply)
            forced_followup_missing_items = ["checker_names"]
            forced_followup_suggested_defaults = owner._default_slots_for_missing_items(task, ["checker_names"])
        if (
            outstanding_missing_items
            and "dataset_name" in outstanding_missing_items
            and _looks_like_dataset_option_request(user_reply)
            and not resolved_slots_delta.get("dataset_name")
        ):
            forced_followup_message = _build_dataset_options_message(dataset_schemas)
            forced_followup_missing_items = ["dataset_name"]
            forced_followup_suggested_defaults = {}
        elif (
            outstanding_missing_items
            and any(item not in {"dataset_name", "checker_names"} for item in outstanding_missing_items)
            and _should_force_programmatic_missing_slot_followup(
                outstanding_missing_items,
                required_slot_specs,
            )
        ):
            forced_followup_message = _build_missing_slot_followup_message(
                outstanding_missing_items,
                required_slot_specs,
                dataset_schemas,
            )
            forced_followup_missing_items = outstanding_missing_items
            forced_followup_suggested_defaults = owner._default_slots_for_missing_items(
                current_task,
                outstanding_missing_items,
            )

    return {
        "status": exhausted_status,
        "turns": len(transcript),
        "transcript": transcript,
        "resolved_task": current_task,
        "resolved_slots": resolved_slots,
        "handoff_reason": exhausted_reason,
    }


def _build_clarification_prompt(
    task: str,
    current_task: str,
    transcript: list[dict[str, Any]],
    messages: list[dict[str, str]],
    dataset_schemas: dict[str, list[str]],
    tool_schemas: list[dict[str, Any]],
    tool_shortlist: list[dict[str, Any]],
    security_hints: dict[str, Any] | None,
    tool_readmes: list[dict[str, str]],
) -> str:
    return (
        "You are the Clarification Agent for DataElf `elf run`.\n"
        "Your job is to help the user finish a task definition in at most 5 clarification turns, then stop.\n"
        "You are not the pilot loop. Do not retry, optimize, derive tools, or do open-ended consulting.\n"
        "You may only clarify task parameters, output destinations, and tool choices.\n"
        "If the user asks what options/defaults are available, answer them and try to explain the difference among the available options.\n"
        "If the task is already clear enough, set status=ready with no assistant_message.\n"
        "If the task is too open-ended for single-shot mode, set status=escalate_to_pilot.\n\n"
        "Return JSON with keys:\n"
        "- status: clarifying | ready | escalate_to_pilot\n"
        "- assistant_message: string\n"
        "- ready_to_execute: bool\n"
        "- resolved_task: string\n"
        "- resolved_slots: object\n"
        "- missing_items: array of strings\n"
        "- suggested_defaults: object\n"
        "- response_mode: ask_user | answer_then_ask\n"
        "- handoff_reason: string\n\n"
        "Rules:\n"
        "- Keep assistant_message concise and concrete.\n"
        "- Prefer listing specific tool choices/defaults over vague questions.\n"
        "- If a tool requires `data`, resolve it as a dataset name from Available datasets; do not ask the user to paste raw records.\n"
        "- If the user asks what datasets are available, list the available dataset names and ask them to choose one.\n"
        "- Do not ask the user for implementation knobs such as max_workers, concurrency, timeout, batch_size, or retries; choose sensible defaults.\n"
        "- For security_audit or checker-related tasks, explain available checker choices and recommended defaults.\n"
        "- If the user already said something like 'use defaults' or 'as you recommended', you should help user to decide and mark ready_to_execute=true.\n"
        "- For structured data filtering/extraction/export tasks, prefer user-facing slot names like `dataset_name`, `filter_field`, `filter_value`, `output_format`, `output_filename`.\n"
        "- Do not infer a security_audit intent from a dataset name like `security_audit_samples` alone.\n"
        "- Do not invent internal-looking slot names such as `flag_field_name` unless that exact field name truly exists in the dataset schema or tool schema.\n"
        "- missing_items must be a plain list of slot names like ['dataset_name', 'checker_names']. Never include null, None, or empty strings. If all slots are resolved, return [].\n"
        "- resolved_slots keys must match the slot names used in missing_items exactly (e.g. 'dataset_name', not 'dataset' or 'data'). If a slot is not yet resolved, omit it from resolved_slots rather than setting it to null or empty string.\n"
        "- response_mode: use 'answer_then_ask' when you need to answer a user question AND ask a clarification question in the same turn; otherwise use 'ask_user'.\n\n"
        f"Original task:\n{task}\n\n"
        f"Current resolved task draft:\n{current_task}\n\n"
        f"Conversation transcript:\n{json.dumps(transcript, ensure_ascii=False)}\n\n"
        f"Conversation messages:\n{json.dumps(messages, ensure_ascii=False)}\n\n"
        f"Available datasets:\n{json.dumps(dataset_schemas, ensure_ascii=False)}\n\n"
        f"Relevant tool shortlist:\n{json.dumps(tool_shortlist, ensure_ascii=False)}\n\n"
        f"All tool schemas:\n{json.dumps(tool_schemas, ensure_ascii=False)}\n\n"
        f"Security audit hints:\n{json.dumps(security_hints or {}, ensure_ascii=False)}\n\n"
        f"Relevant tool documentation excerpts:\n{json.dumps(tool_readmes, ensure_ascii=False)}"
    )


def _build_capability_gap_prompt(task: str, pipeline: str, execution: dict[str, Any]) -> str:
    return (
        "Classify why a single-shot workflow failed. Return JSON with keys: "
        "type, reason, missing_capability, recommended_command.\n\n"
        f"Task:\n{task}\n\nPipeline:\n{pipeline}\n\nExecution:\n{json.dumps(execution, ensure_ascii=False)}"
    )


def _build_candidate_validation_seed_pipeline(
    task: str,
    candidate: dict[str, Any],
    dataset_schemas: dict[str, list[str]],
) -> str:
    dataset_name = _extract_dataset_name_from_user_reply(["dataset_name"], task, dataset_schemas)
    tool_name = str(candidate.get("name", "candidate_tool") or "candidate_tool")
    lines: list[str] = []
    if dataset_name:
        lines.append(f'data = load_dataset("{dataset_name}")')
        lines.append(f'result = run_tool("{tool_name}", data=data)')
    else:
        lines.append(f'result = run_tool("{tool_name}")')
    lines.append("save_result(result)")
    return "\n".join(lines)


def _build_pilot_strategy_guidance(task: str, allow_experimental_tools: bool | None = None) -> str:
    normalized = _task_text_for_analysis(task).lower()
    lines: list[str] = []

    experimental_markers = [
        "prefer experimental",
        "prioritize experimental",
        "experimental python tool",
        "experimental python code",
        "derive python tool",
        "优先派生",
        "优先 experimental",
        "优先用 experimental",
        "优先 python code",
    ]
    derived_tool_markers = [
        "prefer derived tool",
        "prefer candidate tool",
        "reuse derived tool",
        "reuse candidate tool",
        "优先调用派生",
        "优先调用 derived tool",
        "优先复用派生",
    ]
    free_exploration_markers = [
        "freely explore",
        "free exploration",
        "don't stop on failure",
        "do not stop on failure",
        "keep trying",
        "autonomously repair",
        "自主尝试",
        "自由尝试",
        "不要因为失败就停止",
        "失败后继续修复",
    ]
    performance_markers = [
        "optimize tool",
        "optimize code",
        "improve performance",
        "improve throughput",
        "reduce latency",
        "make it faster",
        "parallel",
        "parallelize",
        "concurrency",
        "并发",
        "并行",
        "优化代码",
        "优化 tool",
        "提高性能",
        "提速",
        "更高效",
    ]

    if any(marker in normalized for marker in experimental_markers):
        if allow_experimental_tools is False:
            lines.append("The user prefers experimental Python tool derivation, but experimental tools are disabled for this run.")
        else:
            lines.append("The user prefers experimental Python tool derivation when it helps.")
    if any(marker in normalized for marker in derived_tool_markers):
        lines.append("The user prefers reusing or prioritizing derived/candidate tools when they are relevant.")
    if any(marker in normalized for marker in free_exploration_markers):
        lines.append("The user prefers freer pilot exploration and wants failed attempts to feed the next repair step.")
    if any(marker in normalized for marker in performance_markers):
        lines.append(
            "The user explicitly cares about code/tool efficiency, so latency, throughput, and safe concurrency "
            "improvements are valid optimization targets."
        )

    if not lines:
        lines.append(
            "No explicit strategy preference was stated. You may freely choose among pipeline DSL repair, derived-tool reuse, "
            "composite tool derivation, and experimental tool derivation based on the evidence."
        )
    return "\n".join(f"- {line}" for line in lines)


def _latest_attempt_repair_context(previous_attempts: list[dict[str, Any]]) -> str:
    if not previous_attempts:
        return "None."
    last_attempt = previous_attempts[-1] or {}
    action = last_attempt.get("action", {}) or {}
    execution = last_attempt.get("execution", {}) or {}
    judge = last_attempt.get("judge", {}) or {}
    candidate_errors = last_attempt.get("candidate_errors", []) or []

    lines: list[str] = []
    if action.get("action_type"):
        lines.append(f"Last action_type: {action.get('action_type')}.")
    if execution.get("success") is False:
        lines.append(f"Last execution error: {execution.get('error') or 'unknown execution failure'}.")
    if execution.get("elapsed_seconds") is not None:
        lines.append(f"Last execution latency seconds: {execution.get('elapsed_seconds')}.")
    raw_signals = execution.get("signals", []) or []
    if raw_signals:
        if isinstance(raw_signals, list):
            signal_excerpt: Any = raw_signals[:3]
        elif isinstance(raw_signals, dict):
            signal_excerpt = raw_signals
        else:
            signal_excerpt = str(raw_signals)
        lines.append(f"Execution signals: {json.dumps(signal_excerpt, ensure_ascii=False) if not isinstance(signal_excerpt, str) else signal_excerpt}.")
    if judge.get("failure_type"):
        lines.append(f"Judge failure_type: {judge.get('failure_type')}.")
    if judge.get("recommended_next_action"):
        lines.append(f"Judge recommended_next_action: {judge.get('recommended_next_action')}.")
    capability_gap = judge.get("capability_gap")
    if capability_gap:
        lines.append(f"Judge capability_gap: {json.dumps(capability_gap, ensure_ascii=False)}.")
    if judge.get("reason"):
        lines.append(f"Judge reason: {judge.get('reason')}.")
    if candidate_errors:
        candidate_excerpt = candidate_errors[-2:] if isinstance(candidate_errors, list) else candidate_errors
        lines.append(f"Candidate derivation/validation errors: {json.dumps(candidate_excerpt, ensure_ascii=False)}.")
    repeated_tool_failure = _repeated_tool_failure_summary(previous_attempts)
    if repeated_tool_failure:
        lines.append(repeated_tool_failure)
    if not lines:
        return "None."
    return "\n".join(f"- {line}" for line in lines)


def _build_planner_prompt(
    task: str,
    dataset_schemas: dict[str, list[str]],
    tool_schemas: list[dict[str, Any]],
    previous_attempts: list[dict[str, Any]],
    allow_experimental_tools: bool,
) -> str:
    prompt_attempts = _compact_attempts_for_prompt(previous_attempts)
    recent_logs = _previous_attempt_log_summary(previous_attempts)
    strategy_guidance = _build_pilot_strategy_guidance(task, allow_experimental_tools)
    repair_context = _latest_attempt_repair_context(previous_attempts)
    return (
        "You are the Planner role for DataElf pilot mode.\n"
        "Return JSON with keys: action_type, reason, instructions.\n"
        "Allowed action_type values: propose_pipeline, mutate_pipeline, derive_composite_tool, "
        "derive_python_tool_draft, request_user_input, stop_failed.\n"
        "Pilot freedom policy:\n"
        "- You may choose among direct pipeline DSL repair, derived-tool reuse, composite tool derivation, "
        "and experimental Python tool derivation.\n"
        "- Treat failed attempts as evidence for the next repair, not as a reason to stop by default.\n"
        "- When a previous attempt failed, prefer the most concrete repair path suggested by execution errors, "
        "schema mismatches, candidate validation results, and judge capability gaps.\n"
        "- After repeated derived/experimental tool failures such as tool_not_found, tool_parameter_error, "
        "or tool_runtime_error, consider bypassing that tool path and solving the task directly in DSL if feasible.\n"
        "- Respect any explicit user preference in the task text about prioritizing experimental tools, "
        "derived tools, or freer exploration.\n"
        "- If an existing built-in tool can already complete the task but appears slow, repetitive, or operationally inefficient, "
        "you may choose derive_python_tool_draft to create an enhanced implementation focused on performance, "
        "throughput, or safer bounded concurrency.\n"
        "- When tool source code is available, inspect it for independent work units such as per-category, per-checker, "
        "per-sample, per-partition, or per-query substeps that could be parallelized without changing semantics.\n"
        "If previous attempts contain execution.signals or capability_gap entries, treat them as hard constraints.\n"
        "For llm_checker_content_filter gaps, do not retry affected LLM judge checkers; switch to safe rule-based "
        "fallback checkers or derive a non-LLM fallback if experimental tools are allowed.\n"
        "If experimental tools are allowed and repeated attempts indicate tool-level robustness issues or "
        "a repeated quality plateau, prefer derive_python_tool_draft over another blind mutate.\n"
        "If a previous attempt already satisfied the user goal but a candidate was rejected/not approved, "
        "or an experimental tool candidate failed validation, continue optimizing within the remaining budget "
        "instead of stopping immediately.\n"
        "When continuing optimization after a successful attempt, make one materially different improvement. "
        "Do not merely rename files, rewrite log messages, rename variables, or lightly wrap the same tool output.\n"
        "In that post-success optimization scenario, prefer a different improvement axis such as a different DSL structure, "
        "a different primary tool path, improved output structure, stronger robustness checks, or a meaningful "
        "performance/throughput improvement.\n"
        "If previous_attempts include continue_optimization.enabled=true, do not choose stop_failed unless a new "
        "hard blocker makes further iteration impossible.\n"
        "Do not repeat the same checker_names after a provider content_filter failure.\n"
        f"Experimental Python tools allowed: {allow_experimental_tools}\n\n"
        f"Task:\n{task}\n\n"
        f"User strategy guidance:\n{strategy_guidance}\n\n"
        f"Available datasets:\n{json.dumps(dataset_schemas, ensure_ascii=False)}\n\n"
        f"Available tools:\n{json.dumps(tool_schemas, ensure_ascii=False)}\n\n"
        f"Latest repair context:\n{repair_context}\n\n"
        f"Previous attempts:\n{json.dumps(prompt_attempts, ensure_ascii=False)}\n\n"
        f"Recent execution warning/error excerpts:\n{json.dumps(recent_logs, ensure_ascii=False)}"
    )


def _build_judge_prompt(
    task: str,
    pipeline: str,
    execution: dict[str, Any],
    previous_attempts: list[dict[str, Any]],
    execution_latency_s: float | None = None,
) -> str:
    prompt_attempts = _compact_attempts_for_prompt(previous_attempts)
    recent_logs = _previous_attempt_log_summary(previous_attempts)
    latency_block = ""
    if execution_latency_s is not None:
        latency_block = f"Execution latency seconds:\n{execution_latency_s}\n\n"
    return (
        "You are the Judge role for DataElf pilot mode.\n"
        "Return JSON with keys: goal_satisfied, score, failure_type, capability_gap, "
        "recommended_next_action, reason.\n"
        "Use execution success and goal fit as the primary signal. "
        "Only apply security-audit style reasoning when the user explicitly asked for a security audit.\n"
        "If the pipeline materially diverges from the user's intent, mark goal_satisfied=false and explain the mismatch.\n"
        "Execution latency is only a secondary tiebreaker between otherwise similar successful attempts.\n\n"
        f"Task:\n{task}\n\n"
        f"Pipeline:\n{pipeline}\n\n"
        f"Execution:\n{json.dumps(execution, ensure_ascii=False)}\n\n"
        f"{latency_block}"
        f"Previous attempts:\n{json.dumps(prompt_attempts, ensure_ascii=False)}\n\n"
        f"Recent execution warning/error excerpts:\n{json.dumps(recent_logs, ensure_ascii=False)}"
    )


def _build_composite_tool_prompt(
    task: str,
    pipeline: str,
    source_attempts: list[str],
    dataset_schemas: dict[str, list[str]],
    source_tool_contexts: list[dict[str, Any]],
) -> str:
    return (
        "You are the Toolsmith role for DataElf.\n"
        "Return JSON for a composite tool candidate with keys: "
        "candidate_id, name, description, input_schema, steps, result, validation_criteria, "
        "source_attempts, status, pipeline_template, tool_domains.\n"
        "Composite steps must only use run_tool semantics and string references like $input.data or $audit.\n\n"
        "Repair policy:\n"
        "- Honor explicit user preferences in the task text about prioritizing derived tools or freer exploration.\n"
        "- Use the current pipeline and its observed failures as repair evidence for this composite tool draft.\n"
        "- Keep the composite tool's input contract explicit and aligned with the steps it wraps.\n\n"
        "Naming rules:\n"
        "- `name` must be a semantic snake_case tool name.\n"
        "- The name should reflect the tool's domain and behavior.\n"
        "- Do not use UUIDs, hashes, timestamps, or names like derived_xxx / candidate_xxx / tool_tmp.\n\n"
        f"Task:\n{task}\n\nPipeline:\n{pipeline}\n\n"
        f"Source attempts:\n{json.dumps(source_attempts)}\n\n"
        f"Datasets:\n{json.dumps(dataset_schemas, ensure_ascii=False)}\n\n"
        f"Relevant called tools:\n{json.dumps(source_tool_contexts, ensure_ascii=False)}"
    )


def _build_python_tool_prompt(
    task: str,
    pipeline: str,
    source_attempts: list[str],
    *,
    source_tool_contexts: list[dict[str, Any]],
    execution_summary: dict[str, Any],
    judge_summary: dict[str, Any],
) -> str:
    return (
        "You are the Toolsmith role for DataElf experimental Python tool generation.\n"
        "You may either derive a brand-new experimental tool draft or derive an enhanced draft based on "
        "existing called tools and their observed failures.\n"
        "Do not modify the original tool implementation in place; always produce a new experimental tool draft.\n"
        "Honor explicit user preferences in the task text about prioritizing experimental Python tools or "
        "reusing/repairing derived tools.\n"
        "Use the execution summary and judge summary as concrete repair targets; if a prior attempt failed because "
        "of a parameter mismatch or runtime error, make that fix visible in the tool contract and implementation.\n"
        "If an existing tool already solves the task semantically but looks slow or repetitive, you may derive a more "
        "efficient version that preserves the user-visible behavior while improving throughput or latency.\n"
        "Return JSON with keys: name, description, validation_criteria, review_comments, "
        "enhancement_rationale, behavior_changes, compatibility_notes, code.\n"
        "The code must define a build_tool() function that returns a BaseTool instance.\n"
        "Experimental tools must use DataElf's native tool API so they can be loaded by the platform.\n\n"
        "Required code contract:\n"
        "- Import `BaseTool` and `ToolContext` from `tools.base_tool`.\n"
        "- Implement `name`, `description`, and `parameters` as properties on a `BaseTool` subclass.\n"
        "- Implement `run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]`.\n"
        "- Return a dict with keys like `result`, optional `artifacts`, and optional `metadata`.\n"
        "- Define `build_tool()` that returns an instance of your tool class.\n"
        "- Use `context.log(...)` for logging inside the tool.\n"
        "- The tool should receive its working inputs through parameters or through existing tool composition patterns supported by DataElf.\n"
        "- Do NOT assume ToolContext exposes runtime DSL primitives such as load_dataset(), write_file(), write_db(), or save_result().\n"
        "- If the tool needs records, accept them through explicit parameters such as `data`; if it needs output persistence, use normal Python filesystem code.\n"
        "- ToolContext reliably provides logging plus contextual fields like datasets, artifacts, metadata, and config; treat those as read-only context rather than DSL helpers.\n\n"
        "Optimization guidance:\n"
        "- Read the provided source tool code before proposing an enhancement.\n"
        "- Preserve the semantic contract unless you intentionally document a behavior change.\n"
        "- If you see independent sub-work such as per-category, per-checker, per-sample, per-bucket, per-query, or per-partition processing, "
        "you may parallelize it with bounded concurrency when that does not change correctness.\n"
        "- Prefer concurrency for independent I/O-bound or LLM-bound work. Avoid parallelizing shared mutable state, order-dependent logic, "
        "or tiny workloads where coordination overhead outweighs the benefit.\n"
        "- If you introduce concurrency, keep it conservative and reviewable: use bounded worker counts, collect results deterministically, "
        "surface errors clearly, and fall back cleanly when a subtask fails.\n"
        "- You may add an optional tuning parameter such as `max_workers` only when it materially helps. Otherwise choose a sensible default internally.\n\n"
        "You may import additional packages, use standard library modules, or depend on available third-party libraries if that helps. "
        "Validation will check whether the generated tool actually loads and runs in the current environment.\n\n"
        "Naming rules:\n"
        "- `name` must be a semantic snake_case tool name.\n"
        "- The name should reflect the tool's actual behavior or enhancement intent.\n"
        "- Do not use UUIDs, hashes, timestamps, or names like experimental_xxx / candidate_xxx / tool_tmp.\n"
        "- If you are enhancing an existing tool, prefer names like <source_tool>_enhanced, "
        "<source_tool>_robust, or <source_tool>_failover when appropriate.\n\n"
        "If relevant called tool contexts are provided, inspect their schema, README, source file, and source code. "
        "Use them to produce review_comments and a refined experimental draft when that is helpful.\n\n"
        f"Task:\n{task}\n\n"
        f"Pipeline:\n{pipeline}\n\n"
        f"Source attempts:\n{json.dumps(source_attempts)}\n\n"
        f"Relevant called tool contexts:\n{json.dumps(source_tool_contexts, ensure_ascii=False)}\n\n"
        f"Execution summary:\n{json.dumps(execution_summary, ensure_ascii=False)}\n\n"
        f"Judge summary:\n{json.dumps(judge_summary, ensure_ascii=False)}"
    )


def _latest_successful_optimization_attempt(previous_attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not previous_attempts:
        return None
    last_attempt = previous_attempts[-1] or {}
    if not last_attempt.get("continue_optimization", {}).get("enabled"):
        return None
    execution = last_attempt.get("execution", {}) or {}
    judge = last_attempt.get("judge", {}) or {}
    if execution.get("success") and judge.get("goal_satisfied", execution.get("success")):
        return last_attempt
    return None


def _build_optimization_diversity_note(previous_attempts: list[dict[str, Any]]) -> str:
    reference_attempt = _latest_successful_optimization_attempt(previous_attempts)
    if reference_attempt is None:
        return ""
    prior_tools = _extract_tool_names_from_pipeline(reference_attempt.get("pipeline", ""))
    prior_tool_text = ", ".join(prior_tools) if prior_tools else "no tool calls"
    return (
        f"The previous attempt `{reference_attempt.get('attempt_id', 'previous_attempt')}` already satisfied the user goal. "
        f"It used this primary tool path: {prior_tool_text}. "
        "Because pilot is continuing to optimize rather than recover from a failure, this next attempt must make one "
        "materially different improvement. Do not just change filenames, log wording, variable names, or lightly wrap "
        "the same tool result. Prefer a different improvement axis such as a different DSL structure, a different "
        "primary tool path, a more structured result contract, stronger robustness checks, or a meaningful "
        "performance/throughput improvement."
    )


def _repeated_tool_failure_summary(previous_attempts: list[dict[str, Any]]) -> str:
    if len(previous_attempts) < 2:
        return ""
    recent_attempts = previous_attempts[-3:]
    failed_attempts = [
        attempt for attempt in recent_attempts
        if not (attempt.get("execution", {}) or {}).get("success")
    ]
    if len(failed_attempts) < 2:
        return ""
    tool_paths = [
        _extract_tool_names_from_pipeline(str(attempt.get("pipeline", "") or ""))
        for attempt in failed_attempts
    ]
    if not tool_paths or any(not path for path in tool_paths):
        return ""
    primary_path = tool_paths[-1]
    repeated_count = sum(1 for path in tool_paths if path == primary_path)
    if repeated_count < 2:
        return ""
    primary_tool = primary_path[0]
    return (
        f"Recent attempts have repeatedly failed on the same primary tool path {primary_path}. "
        f"In particular, `{primary_tool}` has already been retried {repeated_count} times. "
        "Do not keep patching the same path with superficial wrappers unless you are explicitly changing the tool contract "
        "or generated tool code itself. Prefer a materially different approach such as direct DSL filtering, a different "
        "tool path, or a new derived tool."
    )


def _build_pilot_goal_clarification_prompt(
    task: str,
    current_task: str,
    transcript: list[dict[str, Any]],
    dataset_schemas: dict[str, list[str]],
    relevant_tools: list[dict[str, Any]],
    allow_experimental_tools: bool,
    tool_readmes: list[dict[str, str]],
) -> str:
    return (
        "You are the Goal Clarification role for DataElf pilot mode.\n"
        "You may ask at most one concise question per turn, and only for goal/constraint clarification.\n"
        "Return JSON with keys: status, assistant_message, ready_to_execute, resolved_task, "
        "resolved_slots, missing_items, suggested_defaults.\n"
        "Allowed status values: clarifying, ready.\n"
        "Do not ask about tool internals unless they materially affect success criteria.\n"
        "Prefer defaults when the user did not specify a hard constraint.\n"
        "Use user-facing slot names such as dataset_name, filter_field, filter_value, output_format, "
        "and output_filename for structured data tasks.\n"
        "Do not infer a security_audit intent from a dataset name like `security_audit_samples` alone.\n"
        "Do not invent internal-looking slot names unless they exactly match a real dataset field or tool parameter.\n"
        "If a dataset is available, ask for the dataset name instead of asking the user to paste raw records.\n"
        "Do not ask for execution knobs such as max_workers, concurrency, timeout, batch_size, or retries.\n"
        "If the task is already clear enough, set status=ready without an unnecessary follow-up question.\n\n"
        f"Original task:\n{task}\n\n"
        f"Current resolved task:\n{current_task}\n\n"
        f"Transcript:\n{json.dumps(transcript, ensure_ascii=False)}\n\n"
        f"Available datasets:\n{json.dumps(dataset_schemas, ensure_ascii=False)}\n\n"
        f"Relevant tools:\n{json.dumps(relevant_tools, ensure_ascii=False)}\n\n"
        f"Relevant tool documentation excerpts:\n{json.dumps(tool_readmes, ensure_ascii=False)}\n\n"
        f"Experimental Python tools allowed: {allow_experimental_tools}\n"
    )
 


def _default_python_tool_code(tool_name: str, description: str) -> str:
    class_name = "".join(part.capitalize() for part in tool_name.split("_")) + "Tool"
    return f'''from typing import Any

from tools.base_tool import BaseTool, ToolContext


class {class_name}(BaseTool):
    @property
    def name(self) -> str:
        return "{tool_name}"

    @property
    def description(self) -> str:
        return "{description}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {{
            "type": "object",
            "properties": {{
                "data": {{
                    "type": "array",
                    "items": {{"type": "object"}},
                    "description": "Input data records.",
                }}
            }},
            "required": ["data"],
        }}

    def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        data = kwargs.get("data", [])
        context.log(f"Experimental tool processed {{len(data)}} records", "info")
        return {{
            "result": {{
                "records": len(data),
                "status": "draft_tool_executed",
            }},
            "metadata": {{
                "experimental": True,
            }},
        }}


def build_tool():
    return {class_name}()
'''


def _merge_clarification_into_task(
    current_task: str,
    assistant_message: str,
    user_reply: str,
    suggested_defaults: dict[str, Any],
) -> str:
    lines = [current_task.strip(), "", "Clarification update:"]
    if user_reply:
        lines.append(f"- User replied: {user_reply}")
    if _looks_like_default_reply(user_reply) and suggested_defaults:
        lines.append(f"- Accepted defaults: {json.dumps(suggested_defaults, ensure_ascii=False)}")
    return "\n".join(line for line in lines if line is not None).strip()


def _append_resolved_slots_to_task(current_task: str, resolved_slots_delta: dict[str, Any]) -> str:
    if not resolved_slots_delta:
        return current_task
    material_slots = {
        key: value
        for key, value in resolved_slots_delta.items()
        if value not in (None, "", [], {}) and key != "selection_mode"
    }
    if not material_slots:
        return current_task
    return (
        current_task.strip()
        + "\n"
        + f"- Resolved slots: {json.dumps(material_slots, ensure_ascii=False)}"
    )


def _looks_like_default_reply(user_reply: str) -> bool:
    normalized = user_reply.strip().lower()
    return normalized in {
        "default",
        "defaults",
        "use default",
        "use defaults",
        "just use default settings",
        "use default settings",
        "as you suggest",
        "as suggested",
        "use suggested",
        "use your suggestion",
        "use your recommendation",
        "go with your suggestion",
        "go with suggested",
    }


def _looks_like_weak_reply(user_reply: str) -> bool:
    normalized = re.sub(r"\s+", " ", user_reply.strip().lower())
    return normalized in {
        "not sure",
        "still not sure",
        "i don't know",
        "idk",
        "you decide",
        "whatever",
        "anything",
        "no idea",
    }


def _looks_like_option_request(user_reply: str) -> bool:
    normalized = re.sub(r"\s+", " ", user_reply.strip().lower())
    prompts = [
        "what choices",
        "what options",
        "what datasets",
        "available choices",
        "available options",
        "available datasets",
        "list datasets",
        "what are available",
        "recommend",
        "recommendation",
        "recommendations",
        "do you have recommendations",
        "what do you recommend",
        "which do you recommend",
        "any recommendation",
        "any recommendations",
    ]
    return any(phrase in normalized for phrase in prompts)


def _looks_like_dataset_option_request(user_reply: str) -> bool:
    normalized = re.sub(r"\s+", " ", user_reply.strip().lower())
    prompts = [
        "what datasets",
        "which datasets",
        "available datasets",
        "list datasets",
        "list all datasets",
        "tell me all the available datasets",
        "show datasets",
        "dataset options",
    ]
    return any(phrase in normalized for phrase in prompts)


def _looks_like_cost_speed_request(user_reply: str) -> bool:
    normalized = re.sub(r"\s+", " ", user_reply.strip().lower())
    return (
        "cost" in normalized
        or "cheap" in normalized
        or "cheaper" in normalized
        or "expensive" in normalized
        or "speed" in normalized
        or "fast" in normalized
        or "faster" in normalized
    )


def _looks_like_accuracy_request(user_reply: str) -> bool:
    normalized = re.sub(r"\s+", " ", user_reply.strip().lower())
    return (
        "accuracy" in normalized
        or "accurate" in normalized
        or "stronger" in normalized
        or "best coverage" in normalized
        or "semantic" in normalized
    )


def _looks_like_recommendation_request(user_reply: str) -> bool:
    return (
        _looks_like_option_request(user_reply)
        or _looks_like_cost_speed_request(user_reply)
        or _looks_like_accuracy_request(user_reply)
    )


def _merge_missing_items(
    previous_missing_items: list[str],
    current_missing_items: list[str],
    resolved_slots: dict[str, Any],
) -> list[str]:
    merged: list[str] = []
    for item in [*previous_missing_items, *current_missing_items]:
        if item not in merged and item not in resolved_slots:
            merged.append(item)
    return merged


def _normalize_missing_items(task: str, missing_items: list[str]) -> list[str]:
    normalized: list[str] = []
    for item in missing_items:
        canonical = item
        item_lower = item.lower().strip()
        if _is_optional_execution_detail(item_lower):
            continue
        if _task_targets_security_checker_selection(task) and (
            "checker" in item_lower or "checker set" in item_lower or "custom checker" in item_lower
        ):
            canonical = "checker_names"
        elif (
            item_lower in {
                "data",
                "input data",
                "records",
                "record data",
                "dataset",
                "datasets",
                "dataset name",
                "dataset_name",
                "table",
                "table name",
            }
            or "dataset" in item_lower
        ):
            canonical = "dataset_name"
        elif _looks_like_filter_field_slot_name(item_lower):
            canonical = "filter_field"
        elif _looks_like_filter_value_slot_name(item_lower):
            canonical = "filter_value"
        elif _looks_like_output_format_slot_name(item_lower):
            canonical = "output_format"
        elif _looks_like_output_filename_slot_name(item_lower):
            canonical = "output_filename"
        if canonical not in normalized:
            normalized.append(canonical)
    return normalized


def _looks_like_filter_field_slot_name(item: str) -> bool:
    normalized = re.sub(r"[^a-z0-9_]+", "_", item.lower()).strip("_")
    if any(token in normalized for token in ["field", "column", "attribute", "key"]):
        return any(token in normalized for token in ["filter", "match", "where", "flag", "condition"])
    return False


def _looks_like_filter_value_slot_name(item: str) -> bool:
    normalized = re.sub(r"[^a-z0-9_]+", "_", item.lower()).strip("_")
    if "value" in normalized:
        return any(token in normalized for token in ["filter", "match", "flag", "condition"])
    return normalized in {"match_value", "field_value"}


def _looks_like_output_format_slot_name(item: str) -> bool:
    normalized = re.sub(r"[^a-z0-9_]+", "_", item.lower()).strip("_")
    return normalized in {
        "output_format",
        "file_format",
        "export_format",
        "format",
    }


def _looks_like_output_filename_slot_name(item: str) -> bool:
    normalized = re.sub(r"[^a-z0-9_]+", "_", item.lower()).strip("_")
    return normalized in {
        "output_filename",
        "file_name",
        "filename",
        "output_file",
        "file_path",
        "output_path",
        "path",
    }


def _is_optional_execution_detail(item: str) -> bool:
    normalized = re.sub(r"[^a-z0-9_]+", "_", item.lower()).strip("_")
    optional_names = {
        "max_workers",
        "workers",
        "num_workers",
        "concurrency",
        "parallelism",
        "batch_size",
        "timeout",
        "timeout_seconds",
        "retries",
        "max_retries",
        "retry_count",
    }
    return normalized in optional_names


def _asks_only_optional_execution_detail(decision: dict[str, Any]) -> bool:
    raw_missing = decision.get("missing_items", [])
    if raw_missing:
        return all(_is_optional_execution_detail(str(item)) for item in raw_missing)
    message = str(decision.get("assistant_message", "")).lower()
    if not message:
        return False
    optional_markers = [
        "max_workers",
        "workers",
        "concurrency",
        "parallelism",
        "batch size",
        "batch_size",
        "timeout",
        "retries",
    ]
    return any(marker in message for marker in optional_markers)


def _find_unresolved_missing_items(
    missing_items: list[str],
    resolved_slots: dict[str, Any],
    resolved_slots_delta: dict[str, Any],
    *,
    weak_reply: bool,
) -> list[str]:
    if weak_reply:
        return list(missing_items)

    unresolved: list[str] = []
    merged_slots = resolved_slots | resolved_slots_delta
    for item in missing_items:
        if item == "dataset_name":
            value = (
                merged_slots.get("dataset_name")
                or merged_slots.get("dataset")
                or merged_slots.get("data")
            )
        else:
            value = merged_slots.get(item)
        if value in (None, "", [], {}):
            unresolved.append(item)
    return unresolved


def _requires_dataset_clarification(task: str, dataset_schemas: dict[str, list[str]]) -> bool:
    if not dataset_schemas:
        return False
    normalized = task.lower()
    if not any(marker in normalized for marker in ["audit", "security", "dataset", "data", "score", "filter", "analyze"]):
        return False
    return _extract_dataset_name_from_user_reply(
        missing_items=["dataset_name"],
        user_reply=task,
        dataset_schemas=dataset_schemas,
    ) is None


def _build_dataset_options_message(dataset_schemas: dict[str, list[str]]) -> str:
    dataset_names = sorted(dataset_schemas)
    if not dataset_names:
        return "I still need the dataset name, but I do not see configured datasets in the current profile."
    return (
        "Available datasets: "
        + ", ".join(dataset_names)
        + ". Please reply with one dataset name, for example `security_audit_samples`."
    )


def _extract_dataset_name_from_user_reply(
    missing_items: list[str],
    user_reply: str,
    dataset_schemas: dict[str, list[str]],
) -> str | None:
    if "dataset_name" not in missing_items or not dataset_schemas:
        return None
    if _looks_like_weak_reply(user_reply) or _looks_like_dataset_option_request(user_reply):
        return None

    available = list(dataset_schemas)
    normalized_reply = _normalize_dataset_text(user_reply)
    if not normalized_reply:
        return None

    normalized_to_dataset = {_normalize_dataset_text(name): name for name in available}
    reply_tokens = {
        _normalize_dataset_text(token)
        for token in re.split(r"[\s,;:，。]+", user_reply)
        if _normalize_dataset_text(token)
    }

    for normalized_name, dataset_name in normalized_to_dataset.items():
        if normalized_reply == normalized_name or normalized_name in reply_tokens:
            return dataset_name
        if re.search(rf"(?<![a-z0-9]){re.escape(normalized_name)}(?![a-z0-9])", normalized_reply):
            return dataset_name

    close_matches = get_close_matches(normalized_reply, list(normalized_to_dataset), n=1, cutoff=0.86)
    if close_matches:
        return normalized_to_dataset[close_matches[0]]
    for token in reply_tokens:
        close_matches = get_close_matches(token, list(normalized_to_dataset), n=1, cutoff=0.86)
        if close_matches:
            return normalized_to_dataset[close_matches[0]]
    return None


def _normalize_dataset_text(text: str) -> str:
    value = text.strip().strip("\"'`")
    value = re.sub(r"\.(jsonl?|csv|parquet)$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"[^a-zA-Z0-9]+", "_", value.lower()).strip("_")
    return value


def _task_targets_security_checker_selection(task: str) -> bool:
    return _mentions_security_audit_intent(task)


def _requires_security_checker_clarification(task: str) -> bool:
    if not _task_targets_security_checker_selection(task):
        return False
    normalized = task.lower()
    if _looks_like_default_reply(normalized):
        return False
    if _extract_security_checker_names(task, ["checker_names"], task):
        return False
    ambiguous_markers = [
        "custom checker",
        "custom checker set",
        "customized checker",
        "with checker set",
        "with a checker set",
        "which checker",
    ]
    return any(marker in normalized for marker in ambiguous_markers)


def _extract_security_checker_names(
    task: str,
    missing_items: list[str],
    user_reply: str,
) -> list[str]:
    if "checker_names" not in missing_items or not _task_targets_security_checker_selection(task):
        return []
    if _looks_like_default_reply(user_reply) or _looks_like_weak_reply(user_reply):
        return []

    supported = {
        "piirule": "PIIRule",
        "secretrule": "SecretRule",
        "toxicitykeywordrule": "ToxicityKeywordRule",
        "toxickeywordrule": "ToxicityKeywordRule",
        "harmfulkeywordrule": "HarmfulKeywordRule",
        "biaskeywordrule": "BiasKeywordRule",
        "alignmentrefusalbypassrule": "AlignmentRefusalBypassRule",
        "harmfulcontentllmjudge": "HarmfulContentLLMJudge",
        "biasllmjudge": "BiasLLMJudge",
        "toxicityllmjudge": "ToxicityLLMJudge",
        "piillmjudge": "PIILLMJudge",
        "sycophancyllmjudge": "SycophancyLLMJudge",
        "jailbreakllmjudge": "JailbreakLLMJudge",
        "promptinjectionllmjudge": "PromptInjectionLLMJudge",
        "selfcontradictionllmjudge": "SelfContradictionLLMJudge",
        "instructionmismatchllmjudge": "InstructionMismatchLLMJudge",
        "factualinconsistancyllmjudge": "FactualInconsistancyLLMJudge",
        "dpolabelflipllmjudge": "DPOLabelFlipLLMJudge",
        "harmfulcontentclassifier": "HarmfulContentClassifier",
        "toxicityclassifier": "ToxicityClassifier",
        "biasclassifier": "BiasClassifier",
        "piinerdetector": "PIINERDetector",
        "jailbreakclassifier": "JailbreakClassifier",
        "promptinjectionclassifier": "PromptInjectionClassifier",
    }

    normalized_reply = re.sub(r"[^a-z0-9_]+", " ", user_reply.lower())
    compact_reply = re.sub(r"[^a-z0-9]+", "", user_reply.lower())
    found: list[str] = []
    for normalized_name, canonical_name in supported.items():
        if normalized_name in compact_reply or normalized_name in normalized_reply.replace("_", ""):
            if canonical_name not in found:
                found.append(canonical_name)
    return found


def _extract_checker_names_from_pipeline(pipeline: str) -> list[str]:
    match = re.search(r"checker_names\s*=\s*(\[[^\]]*\])", pipeline, flags=re.DOTALL)
    if not match:
        return []
    try:
        parsed = ast.literal_eval(match.group(1))
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _extract_tool_names_from_pipeline(pipeline: str) -> list[str]:
    matches = re.findall(r'run_tool\(\s*["\']([^"\']+)["\']', pipeline)
    return _dedupe_preserve_order([str(name) for name in matches if str(name).strip()])


class _PipelineSimilarityNormalizer(ast.NodeTransformer):
    _preserved_names = {
        "load_dataset",
        "run_tool",
        "save_result",
        "write_file",
        "write_db",
        "log_step",
        "len",
        "str",
        "int",
        "float",
        "list",
        "dict",
        "True",
        "False",
        "None",
    }

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in self._preserved_names:
            return node
        return ast.copy_location(ast.Name(id="_VAR", ctx=node.ctx), node)

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        value = node.value
        if isinstance(value, str):
            return ast.copy_location(ast.Constant(value="__STR__"), node)
        if isinstance(value, (int, float)):
            return ast.copy_location(ast.Constant(value=0), node)
        return node


def _canonicalize_pipeline_for_similarity(pipeline: str) -> str:
    if not isinstance(pipeline, str):
        return ""
    try:
        tree = ast.parse(pipeline)
        normalized = _PipelineSimilarityNormalizer().visit(tree)
        ast.fix_missing_locations(normalized)
        return ast.dump(normalized, annotate_fields=True, include_attributes=False)
    except SyntaxError:
        compact = re.sub(r"\s+", " ", pipeline.strip())
        compact = re.sub(r'["\'][^"\']*["\']', '"__STR__"', compact)
        compact = re.sub(r"\b\d+(\.\d+)?\b", "0", compact)
        return compact


def _pipelines_are_near_duplicates(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if _extract_tool_names_from_pipeline(left) != _extract_tool_names_from_pipeline(right):
        return False
    return _canonicalize_pipeline_for_similarity(left) == _canonicalize_pipeline_for_similarity(right)


def _should_retry_duplicate_optimization_pipeline(previous_attempts: list[dict[str, Any]], pipeline: str) -> bool:
    reference_attempt = _latest_successful_optimization_attempt(previous_attempts)
    if reference_attempt is None:
        return False
    prior_pipeline = str(reference_attempt.get("pipeline", "") or "")
    return _pipelines_are_near_duplicates(prior_pipeline, pipeline)


def _should_retry_duplicate_failure_pipeline(previous_attempts: list[dict[str, Any]], pipeline: str) -> bool:
    if len(previous_attempts) < 2:
        return False
    last_attempt = previous_attempts[-1] or {}
    prior_attempt = previous_attempts[-2] or {}
    last_execution = last_attempt.get("execution", {}) or {}
    prior_execution = prior_attempt.get("execution", {}) or {}
    if last_execution.get("success") or prior_execution.get("success"):
        return False
    last_pipeline = str(last_attempt.get("pipeline", "") or "")
    prior_pipeline = str(prior_attempt.get("pipeline", "") or "")
    current_tools = _extract_tool_names_from_pipeline(pipeline)
    last_tools = _extract_tool_names_from_pipeline(last_pipeline)
    prior_tools = _extract_tool_names_from_pipeline(prior_pipeline)
    if not current_tools or current_tools != last_tools or last_tools != prior_tools:
        return False
    return _pipelines_are_near_duplicates(last_pipeline, pipeline)


def _apply_pipeline_intent_judge_guard(
    task: str,
    pipeline: str,
    execution: dict[str, Any],
    judge: dict[str, Any],
) -> dict[str, Any]:
    task_is_security_audit = _is_broad_security_audit_task(task)
    called_tools = _extract_tool_names_from_pipeline(pipeline)
    calls_security_audit = "security_audit" in called_tools

    if not task_is_security_audit and calls_security_audit:
        updated = {
            **judge,
            "goal_satisfied": False,
            "score": min(_normalize_judge_score(judge.get("score", 0.0)), 0.25),
            "failure_type": "pipeline_intent_mismatch",
            "recommended_next_action": "mutate_pipeline",
            "reason": (
                "The pipeline called `security_audit`, but the user asked for a structured data processing "
                "task rather than a security audit."
            ),
            "capability_gap": {
                "type": "pipeline_intent_mismatch",
                "expected_intent": "structured_data_processing",
                "unexpected_tools": ["security_audit"],
                "called_tools": called_tools,
            },
        }
        domain_metrics = dict(updated.get("domain_metrics", {}) or {})
        domain_metrics["intent_alignment"] = 0.0
        updated["domain_metrics"] = domain_metrics
        return updated

    if (
        not task_is_security_audit
        and execution.get("success")
        and str(judge.get("failure_type", "") or "").startswith("insufficient_security_")
    ):
        updated = {
            **judge,
            "goal_satisfied": True,
            "failure_type": "none",
            "recommended_next_action": "stop_success",
            "reason": "The task is not a security audit, so security coverage gates do not apply here.",
        }
        updated["score"] = max(_normalize_judge_score(updated.get("score", 0.0)), 0.9)
        domain_metrics = dict(updated.get("domain_metrics", {}) or {})
        domain_metrics["intent_alignment"] = 1.0
        updated["domain_metrics"] = domain_metrics
        return updated

    return judge


def _attach_execution_efficiency_metrics(
    judge: dict[str, Any],
    execution_latency_s: float | None,
) -> dict[str, Any]:
    if execution_latency_s is None:
        return judge
    domain_metrics = dict(judge.get("domain_metrics", {}) or {})
    domain_metrics["pipeline_execution_seconds"] = round(float(execution_latency_s), 2)
    judge["domain_metrics"] = domain_metrics
    return judge


def _collect_tool_contexts_from_pipeline(
    pipeline: str,
    registry: ToolRegistry,
    *,
    include_code: bool,
    code_max_len: int = 12000,
    readme_max_len: int = 2000,
) -> list[dict[str, Any]]:
    tool_contexts: list[dict[str, Any]] = []
    for tool_name in _extract_tool_names_from_pipeline(pipeline):
        tool = registry.get(tool_name)
        if tool is None:
            continue
        schema = tool.get_schema()
        try:
            module_path = inspect.getsourcefile(tool.__class__)
        except (TypeError, OSError):
            module_path = None
        module_code = ""
        full_module_code = ""
        if include_code and module_path:
            try:
                full_module_code = Path(module_path).read_text(encoding="utf-8")
                module_code = _truncate_text(full_module_code, code_max_len)
            except Exception:
                module_code = ""
                full_module_code = ""
        readme_entries = load_tool_readme_entries([tool_name], max_len=readme_max_len)
        tool_contexts.append({
            "tool_name": tool_name,
            "description": schema.get("description", ""),
            "parameters": schema.get("parameters", {}),
            "usage_example": schema.get("usage_example", ""),
            "source_file": module_path,
            "source_code": module_code,
            "readme_excerpt": readme_entries[0]["content"] if readme_entries else "",
            "optimization_hints": _summarize_tool_optimization_hints(full_module_code or module_code),
        })
    return tool_contexts


def _summarize_tool_optimization_hints(module_code: str) -> dict[str, Any]:
    if not module_code:
        return {}

    lower_code = module_code.lower()
    llm_generate_sites = len(re.findall(r"context\.llm\.generate\s*\(", module_code))
    helper_generation_sites = len(re.findall(r"_generate_[a-z0-9_]+\s*\(", module_code))
    uses_concurrency = any(
        marker in lower_code
        for marker in (
            "threadpoolexecutor",
            "processpoolexecutor",
            "as_completed",
            "asyncio",
            "concurrent.futures",
        )
    )
    hints: list[str] = []

    if llm_generate_sites >= 1:
        hints.append(
            "This tool performs LLM calls in code. Independent LLM-bound stages may be candidates for bounded concurrency."
        )
    if helper_generation_sites >= 4 and not uses_concurrency:
        hints.append(
            "The source contains several generation/helper stages but no visible concurrency primitives. "
            "Inspect whether category-specific or bucket-specific work can run in parallel safely."
        )
    if re.search(r"\{\s*\w+\s*:\s*self\._[a-z0-9_]+\(", module_code, re.DOTALL):
        hints.append(
            "A dict-comprehension style fan-out was detected. Those per-key tasks may be independent enough for parallel execution."
        )
    if "for " in module_code and "categorized" in lower_code and not uses_concurrency:
        hints.append(
            "Categorized/bucketed iteration was detected without concurrency. Review whether each bucket can be processed independently."
        )

    return {
        "uses_concurrency_primitives": uses_concurrency,
        "llm_generate_call_sites": llm_generate_sites,
        "helper_generation_call_sites": helper_generation_sites,
        "hints": hints,
    }


def _extract_risk_categories_from_checker_names(checker_names: list[str]) -> list[str]:
    if not checker_names:
        return []
    try:
        from tools.security_audit.checker.registry import CheckerRegistry as SecurityCheckerRegistry
    except Exception:
        return []

    categories: list[str] = []
    for checker_name in checker_names:
        try:
            checker_cls = SecurityCheckerRegistry.get(str(checker_name))
        except Exception:
            continue
        risk_type = getattr(checker_cls, "risk_type", None)
        category = getattr(risk_type, "value", None)
        if not category or category in categories:
            continue
        categories.append(str(category))
    return categories


def _coerce_text_block(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_coerce_text_block(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def _truncate_text(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 64].rstrip() + "\n\n# ... truncated ..."


def _toolsmith_execution_summary(execution: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(execution, dict):
        return {}
    return {
        "success": execution.get("success"),
        "error": execution.get("error"),
        "elapsed_seconds": execution.get("elapsed_seconds"),
        "signals": execution.get("signals", {}),
        "result": execution.get("result"),
        "log_excerpt": execution.get("log_excerpt", []),
    }


def _toolsmith_judge_summary(judge: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(judge, dict):
        return {}
    return {
        "goal_satisfied": judge.get("goal_satisfied"),
        "score": judge.get("score"),
        "failure_type": judge.get("failure_type"),
        "capability_gap": judge.get("capability_gap", {}),
        "recommended_next_action": judge.get("recommended_next_action"),
        "reason": judge.get("reason"),
    }


def _normalize_candidate_tool_name(
    raw_name: str,
    *,
    task: str,
    pipeline: str,
    candidate_type: str,
    source_tool_names: list[str] | None = None,
) -> str:
    normalized = _sanitize_tool_name(raw_name)
    semantic_fallback = _suggest_semantic_tool_name(
        task=task,
        pipeline=pipeline,
        candidate_type=candidate_type,
        source_tool_names=source_tool_names or [],
    )
    if not normalized or _looks_like_non_semantic_tool_name(normalized):
        return semantic_fallback
    return normalized


def _sanitize_tool_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    text = re.sub(r"_+", "_", text)
    if not text:
        return ""
    if text[0].isdigit():
        text = f"tool_{text}"
    return text[:80].rstrip("_")


def _looks_like_non_semantic_tool_name(name: str) -> bool:
    if not name:
        return True
    generic_prefixes = [
        "cand_",
        "asset_",
        "derived_",
        "experimental_",
        "candidate_",
        "tool_tmp",
        "draft_",
    ]
    if any(name.startswith(prefix) for prefix in generic_prefixes):
        return True
    if re.search(r"(?:^|_)[0-9a-f]{8,}(?:_|$)", name):
        return True
    if re.fullmatch(r"(tool|candidate|draft|derived|experimental)(?:_\d+)?", name):
        return True
    return False


def _suggest_semantic_tool_name(
    *,
    task: str,
    pipeline: str,
    candidate_type: str,
    source_tool_names: list[str],
) -> str:
    base = _sanitize_tool_name(source_tool_names[0] if source_tool_names else "")
    if not base:
        task_tokens = [
            token for token in _tokenize_text(task)
            if token not in {
                "run", "pilot", "tool", "data", "dataset", "on", "with", "the", "a", "an",
                "security", "audit", "execute", "job",
            }
        ]
        base = _sanitize_tool_name("_".join(task_tokens[:3])) or "derived_tool"
    suffix = "enhanced" if candidate_type == "experimental_python_tool" else "composite"
    if base.endswith(f"_{suffix}") or base == suffix:
        return base
    return f"{base}_{suffix}"


def _force_python_tool_name(code: str, tool_name: str) -> str:
    if not isinstance(code, str) or not code.strip():
        return code
    pattern = re.compile(
        r"(def\s+name\s*\(\s*self\s*\)\s*->\s*str\s*:\s*\n\s*return\s+)([\"'])([^\"']+)([\"'])"
    )
    if pattern.search(code):
        return pattern.sub(
            lambda match: f'{match.group(1)}"{tool_name}"',
            code,
            count=1,
        )
    return code


def _normalize_planner_action(action: Any, fallback_type: str) -> dict[str, Any]:
    if not isinstance(action, dict):
        return {
            "action_type": fallback_type,
            "reason": _coerce_text_block(action),
            "instructions": "",
            "missing_items": [],
            "suggested_defaults": {},
        }

    normalized = dict(action)
    allowed_action_types = {
        "propose_pipeline",
        "mutate_pipeline",
        "derive_composite_tool",
        "derive_python_tool_draft",
        "request_user_input",
        "stop_failed",
    }
    action_type = _coerce_text_block(normalized.get("action_type")) or fallback_type
    if action_type not in allowed_action_types:
        action_type = fallback_type
    normalized["action_type"] = action_type
    normalized["reason"] = _coerce_text_block(normalized.get("reason"))
    normalized["instructions"] = _coerce_text_block(normalized.get("instructions"))

    raw_missing_items = normalized.get("missing_items", [])
    if isinstance(raw_missing_items, str):
        raw_missing_items = [raw_missing_items]
    elif not isinstance(raw_missing_items, list):
        raw_missing_items = []
    normalized["missing_items"] = [
        item
        for item in (_coerce_text_block(entry) for entry in raw_missing_items)
        if item is not None and item != ""
    ]

    suggested_defaults = normalized.get("suggested_defaults", {})
    normalized["suggested_defaults"] = suggested_defaults if isinstance(suggested_defaults, dict) else {}
    return normalized


def _normalize_judge_result(result: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(result) if isinstance(result, dict) else {}
    for key, value in fallback.items():
        normalized.setdefault(key, value)

    normalized["goal_satisfied"] = bool(normalized.get("goal_satisfied", fallback.get("goal_satisfied", False)))
    normalized["failure_type"] = _coerce_text_block(normalized.get("failure_type")) or fallback.get("failure_type", "")

    capability_gap = normalized.get("capability_gap", fallback.get("capability_gap", {}))
    if isinstance(capability_gap, dict):
        normalized["capability_gap"] = capability_gap
    else:
        capability_gap_text = _coerce_text_block(capability_gap)
        normalized["capability_gap"] = {"reason": capability_gap_text} if capability_gap_text else {}

    normalized["reason"] = _coerce_text_block(normalized.get("reason")) or fallback.get("reason", "")

    allowed_next_actions = {
        "stop_success",
        "stop_failed",
        "mutate_pipeline",
        "request_user_input",
        "none",
    }
    raw_next_action = _coerce_text_block(normalized.get("recommended_next_action"))
    if raw_next_action not in allowed_next_actions:
        if raw_next_action:
            normalized["next_action_guidance"] = raw_next_action
            if raw_next_action not in normalized["reason"]:
                normalized["reason"] = (
                    f"{normalized['reason']} Suggested next step: {raw_next_action}"
                ).strip()
        raw_next_action = "none" if normalized["goal_satisfied"] else "mutate_pipeline"
    normalized["recommended_next_action"] = raw_next_action or fallback.get("recommended_next_action", "mutate_pipeline")
    return normalized


def _stabilize_security_audit_result_logging(pipeline: str) -> str:
    """Avoid tool-specific result introspection in generated DSL logs.

    The official structured output is written by save_result(). Progress logs
    should not assume that every tool returns security_audit-style fields.
    """
    result_summary_patterns = (
        "result['result']",
        'result["result"]',
        "audit_result['result']",
        'audit_result["result"]',
        "len(result)",
        "len(audit_result)",
        "flagged_samples",
        "total_samples",
        "security_score",
        "flagged_rate",
    )
    completion_log = 'log_step("Completed tool: security_audit")'
    stabilized_lines: list[str] = []
    previous_was_completion = False

    for line in pipeline.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        is_log_step = stripped.startswith("log_step(")
        is_audit_completion_log = (
            "audit completed" in lowered
            or "completed security audit" in lowered
            or "security audit completed" in lowered
            or "completed tool: security_audit" in lowered
        )
        is_result_summary_log = is_log_step and (
            any(pattern in stripped for pattern in result_summary_patterns)
            or is_audit_completion_log
        )

        if is_result_summary_log:
            if not previous_was_completion:
                stabilized_lines.append(completion_log)
                previous_was_completion = True
            continue

        stabilized_lines.append(line)
        previous_was_completion = stripped == completion_log

    return "\n".join(stabilized_lines)


def _stabilize_security_checker_failover(pipeline: str, previous_attempts: list[dict[str, Any]]) -> str:
    gap = _latest_llm_checker_failure_gap(previous_attempts)
    if not gap:
        return pipeline
    avoid_checkers = set(gap.get("avoid_checkers", []))
    recommended = gap.get("recommended_checker_names", [])
    if not avoid_checkers or not recommended:
        return pipeline

    match = re.search(r"checker_names\s*=\s*(\[[^\]]*\])", pipeline, flags=re.DOTALL)
    if not match:
        return pipeline
    try:
        checker_names = ast.literal_eval(match.group(1))
    except Exception:
        checker_names = []
    if not isinstance(checker_names, list) or not any(str(name) in avoid_checkers for name in checker_names):
        return pipeline

    replacement = "checker_names=" + json.dumps(recommended)
    return pipeline[:match.start()] + replacement + pipeline[match.end():]


def _stabilize_pipeline_logging(pipeline: str) -> str:
    lines = pipeline.splitlines()
    output: list[str] = []
    index = 0

    def previous_meaningful_line() -> str:
        for existing in reversed(output):
            stripped = existing.strip()
            if stripped:
                return stripped
        return ""

    def next_meaningful_line(start: int) -> str:
        for offset in range(start, len(lines)):
            stripped = lines[offset].strip()
            if stripped:
                return stripped
        return ""

    def maybe_add_before(message: str) -> None:
        if "log_step(" not in previous_meaningful_line():
            output.append(f'log_step("{message}")')

    def maybe_add_after(message: str, next_line: str) -> None:
        if "log_step(" not in next_line:
            output.append(f'log_step("{message}")')

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        load_match = re.match(r"^(\w+)\s*=\s*load_dataset\(", stripped)
        if load_match:
            dataset_var = load_match.group(1)
            maybe_add_before("Loading dataset")
            output.append(line)
            maybe_add_after(f'Loaded {{len({dataset_var})}} records', next_meaningful_line(index + 1))
            index += 1
            continue

        save_match = re.match(r"^save_result\((.+)\)\s*$", stripped)
        if save_match:
            maybe_add_before("Saving result to job")
            output.append(line)
            maybe_add_after("Result saved", next_meaningful_line(index + 1))
            index += 1
            continue

        write_file_match = re.match(r"^write_file\(", stripped)
        if write_file_match:
            maybe_add_before("Writing file output")
            output.append(line)
            maybe_add_after("File written", next_meaningful_line(index + 1))
            index += 1
            continue

        write_db_match = re.match(r"^write_db\(", stripped)
        if write_db_match:
            maybe_add_before("Writing database output")
            output.append(line)
            maybe_add_after("Database write completed", next_meaningful_line(index + 1))
            index += 1
            continue

        if "= run_tool(" in stripped:
            tool_name = "tool"
            initial_tool_match = re.search(r'"([^"]+)"', line)
            if initial_tool_match:
                tool_name = initial_tool_match.group(1)
            block_lines = [line]
            maybe_add_before(f"Running tool: {tool_name}")
            index += 1
            while index < len(lines):
                block_line = lines[index]
                block_lines.append(block_line)
                tool_match = re.search(r'"([^"]+)"', block_line)
                if tool_match and tool_name == "tool":
                    tool_name = tool_match.group(1)
                if block_line.strip() == ")":
                    break
                index += 1
            output.extend(block_lines)
            maybe_add_after(f"Completed tool: {tool_name}", next_meaningful_line(index + 1))
            index += 1
            continue

        output.append(line)
        index += 1

    return "\n".join(output)


def _is_broad_security_audit_task(task: str) -> bool:
    return _mentions_security_audit_intent(task)


def _stabilize_attempt_write_targets(pipeline: str, attempt_id: str) -> str:
    paths = [
        path
        for path in _extract_external_write_targets(pipeline)
        if path and path != "<dynamic_path>"
    ]
    if not paths:
        return pipeline

    rewritten = pipeline
    for original_path in paths:
        rewritten_path = _with_attempt_suffix(original_path, attempt_id)
        if rewritten_path == original_path:
            continue
        pattern = re.compile(rf"(?P<quote>['\"])({re.escape(original_path)})(?P=quote)")
        rewritten = pattern.sub(
            lambda match: f"{match.group('quote')}{rewritten_path}{match.group('quote')}",
            rewritten,
        )
    return rewritten


def _with_attempt_suffix(path_value: str, attempt_id: str) -> str:
    suffix = _attempt_suffix(attempt_id)
    if not suffix:
        return path_value

    normalized = str(path_value or "").strip()
    if not normalized or normalized == "<dynamic_path>":
        return path_value

    pure_path = PurePosixPath(normalized)
    name = pure_path.name
    if not name:
        return path_value
    if re.search(rf"_{re.escape(suffix)}(?:\.[^.]+)?$", name):
        return path_value

    if "." in name and not name.startswith("."):
        stem, extension = name.rsplit(".", 1)
        new_name = f"{stem}_{suffix}.{extension}"
    else:
        new_name = f"{name}_{suffix}"

    parent = pure_path.parent
    return new_name if str(parent) == "." else f"{parent.as_posix()}/{new_name}"


def _attempt_suffix(attempt_id: str) -> str:
    match = re.search(r"(\d+)$", str(attempt_id or ""))
    return match.group(1) if match else ""


def _normalize_judge_score(score: Any) -> float:
    try:
        numeric = float(score)
    except Exception:
        return 0.0
    if numeric > 1.0:
        numeric = numeric / 100.0
    return round(max(0.0, min(1.0, numeric)), 2)


def _attach_execution_domain_metrics(judge: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    result_payload = execution.get("result", {}) if isinstance(execution.get("result"), dict) else {}
    existing = dict(judge.get("domain_metrics", {}))
    for key in ["security_score", "flagged_rate", "flagged_samples", "total_samples", "passed"]:
        if key in result_payload and key not in existing:
            existing[key] = result_payload[key]
    if not existing:
        return judge
    return {
        **judge,
        "domain_metrics": existing,
    }


def _looks_like_security_audit_result(result_payload: dict[str, Any]) -> bool:
    return any(
        key in result_payload
        for key in ["security_score", "flagged_rate", "flagged_samples", "total_samples", "passed"]
    )


def _attach_execution_signals(execution: dict[str, Any]) -> dict[str, Any]:
    signals = _extract_execution_signals(execution)
    if not signals:
        return execution
    return {
        **execution,
        "signals": signals,
    }


def _extract_execution_signals(execution: dict[str, Any]) -> dict[str, Any]:
    artifacts = execution.get("artifacts", {}) if isinstance(execution.get("artifacts"), dict) else {}
    sample_results = artifacts.get("security_audit.sample_results")
    if not isinstance(sample_results, list):
        return {}

    content_filter_counts: dict[str, int] = {}
    checker_error_counts: dict[str, int] = {}
    content_filter_examples: list[dict[str, Any]] = []
    checker_error_examples: list[dict[str, Any]] = []
    for sample in sample_results:
        if not isinstance(sample, dict):
            continue
        sample_id = sample.get("sample_id")
        for result in sample.get("results", []):
            if not isinstance(result, dict):
                continue
            checker_name = str(result.get("checker_name", ""))
            details = result.get("details", {})
            detail_text = json.dumps(details, ensure_ascii=False)
            evidence_text = str(result.get("evidence", ""))
            combined = f"{detail_text}\n{evidence_text}".lower()
            error_text = details.get("error") if isinstance(details, dict) else None
            if _looks_like_content_filter_text(combined):
                content_filter_counts[checker_name] = content_filter_counts.get(checker_name, 0) + 1
                if len(content_filter_examples) < 5:
                    content_filter_examples.append({
                        "sample_id": sample_id,
                        "checker_name": checker_name,
                        "error": error_text,
                    })
                continue
            if not _looks_like_llm_checker_name(checker_name):
                continue
            if not _looks_like_checker_execution_error(result):
                continue
            checker_error_counts[checker_name] = checker_error_counts.get(checker_name, 0) + 1
            if len(checker_error_examples) < 5:
                checker_error_examples.append({
                    "sample_id": sample_id,
                    "checker_name": checker_name,
                    "error": error_text,
                })

    signals: dict[str, Any] = {}
    if content_filter_counts:
        affected = [name for name, _count in sorted(content_filter_counts.items()) if name]
        recommended = _security_audit_safe_fallback_checkers(avoid_checkers=affected)
        signals["llm_checker_content_filter"] = {
            "count": sum(content_filter_counts.values()),
            "affected_checkers": affected,
            "checker_counts": content_filter_counts,
            "examples": content_filter_examples,
            "recommended_checker_names": recommended,
            "recommended_action": "mutate_pipeline_without_affected_llm_judges",
        }
    if checker_error_counts:
        affected = [name for name, _count in sorted(checker_error_counts.items()) if name]
        recommended = _security_audit_safe_fallback_checkers(avoid_checkers=affected)
        signals["llm_checker_execution_error"] = {
            "count": sum(checker_error_counts.values()),
            "affected_checkers": affected,
            "checker_counts": checker_error_counts,
            "examples": checker_error_examples,
            "recommended_checker_names": recommended,
            "recommended_action": "mutate_pipeline_without_failed_llm_judges",
        }

    if not signals:
        return {}
    return {
        "security_audit": {
            **signals,
        }
    }


def _apply_execution_signal_judge_policy(task: str, execution: dict[str, Any], judge: dict[str, Any]) -> dict[str, Any]:
    if not _is_broad_security_audit_task(task):
        return judge
    security_signals = execution.get("signals", {}).get("security_audit", {})
    content_filter_signal = security_signals.get("llm_checker_content_filter")
    if not content_filter_signal:
        checker_error_signal = security_signals.get("llm_checker_execution_error")
        if not checker_error_signal:
            return judge
        affected = checker_error_signal.get("affected_checkers", [])
        recommended = checker_error_signal.get("recommended_checker_names", [])
        gap = {
            "type": "llm_checker_execution_error",
            "affected_checkers": affected,
            "avoid_checkers": affected,
            "recommended_checker_names": recommended,
            "recommended_action": checker_error_signal.get("recommended_action"),
            "checker_error_count": checker_error_signal.get("count", 0),
            "reason": (
                "One or more LLM-based security checkers failed during execution. "
                "The next attempt should avoid those unstable checkers instead of retrying them."
            ),
        }
        return {
            **judge,
            "goal_satisfied": False,
            "score": min(_normalize_judge_score(judge.get("score", 0.0)), 0.3),
            "failure_type": "llm_checker_execution_error",
            "recommended_next_action": "mutate_pipeline",
            "capability_gap": gap,
            "reason": (
                f"LLM checker execution failures detected for {affected}. "
                f"Switch to fallback checkers: {recommended}."
            ),
        }

    affected = content_filter_signal.get("affected_checkers", [])
    recommended = content_filter_signal.get("recommended_checker_names", [])
    gap = {
        "type": "llm_checker_content_filter",
        "affected_checkers": affected,
        "avoid_checkers": affected,
        "recommended_checker_names": recommended,
        "recommended_action": content_filter_signal.get("recommended_action"),
        "content_filter_count": content_filter_signal.get("count", 0),
        "reason": (
            "One or more LLM-based security checkers triggered the provider content filter. "
            "The next attempt should avoid those checkers instead of retrying the same prompt."
        ),
    }
    return {
        **judge,
        "goal_satisfied": False,
        "score": min(_normalize_judge_score(judge.get("score", 0.0)), 0.34),
        "failure_type": "llm_checker_content_filter",
        "recommended_next_action": "mutate_pipeline",
        "capability_gap": gap,
        "reason": (
            f"LLM checker content filtering detected for {affected}. "
            f"Switch to fallback checkers: {recommended}."
        ),
    }


def _compact_attempts_for_prompt(previous_attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for attempt in previous_attempts[-5:]:
        execution = attempt.get("execution", {})
        compact.append({
            "attempt_id": attempt.get("attempt_id"),
            "action": attempt.get("action", {}),
            "pipeline": attempt.get("pipeline", ""),
            "execution": {
                "success": execution.get("success"),
                "result": execution.get("result"),
                "error": execution.get("error"),
                "signals": execution.get("signals", {}),
                "log_ref": execution.get("log_ref"),
                "log_excerpt": execution.get("log_excerpt", []),
            },
            "judge": attempt.get("judge", {}),
            "attempt_metrics": attempt.get("attempt_metrics", {}),
            "candidate": attempt.get("candidate", {}),
            "candidates": _attempt_candidates(attempt),
            "continue_optimization": attempt.get("continue_optimization", {}),
        })
    return compact


def _attempt_candidates(attempt: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = attempt.get("candidates")
    if isinstance(candidates, list) and candidates:
        return [candidate for candidate in candidates if isinstance(candidate, dict)]
    candidate = attempt.get("candidate")
    if isinstance(candidate, dict) and candidate:
        return [candidate]
    return []


def _previous_attempt_log_summary(previous_attempts: list[dict[str, Any]], max_attempts: int = 3) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for attempt in previous_attempts[-max_attempts:]:
        excerpt = attempt.get("execution_log_excerpt") or attempt.get("execution", {}).get("log_excerpt", [])
        important = [
            entry for entry in excerpt
            if entry.get("level") in {"WARNING", "ERROR", "CRITICAL"}
        ]
        if not important:
            continue
        summary.append({
            "attempt_id": attempt.get("attempt_id"),
            "log_ref": attempt.get("execution_log_ref") or attempt.get("execution", {}).get("log_ref"),
            "warnings_or_errors": important[-5:],
        })
    return summary


def _latest_llm_checker_failure_gap(previous_attempts: list[dict[str, Any]]) -> dict[str, Any]:
    for attempt in reversed(previous_attempts):
        gap = attempt.get("judge", {}).get("capability_gap", {})
        if not isinstance(gap, dict):
            gap = {}
        if gap.get("type") in {"llm_checker_content_filter", "llm_checker_execution_error"}:
            return gap
        security_signals = attempt.get("execution", {}).get("signals", {}).get("security_audit", {})
        for signal_type in ["llm_checker_content_filter", "llm_checker_execution_error"]:
            signal = security_signals.get(signal_type)
            if not signal:
                continue
            affected = signal.get("affected_checkers", [])
            return {
                "type": signal_type,
                "affected_checkers": affected,
                "avoid_checkers": affected,
                "recommended_checker_names": signal.get(
                    "recommended_checker_names",
                    _security_audit_safe_fallback_checkers(affected),
                ),
            }
    return {}


def _latest_llm_checker_content_filter_gap(previous_attempts: list[dict[str, Any]]) -> dict[str, Any]:
    gap = _latest_llm_checker_failure_gap(previous_attempts)
    if gap.get("type") == "llm_checker_content_filter":
        return gap
    return {}


def _latest_security_quality_gap(previous_attempts: list[dict[str, Any]]) -> dict[str, Any]:
    for attempt in reversed(previous_attempts):
        gap = attempt.get("judge", {}).get("capability_gap", {})
        if not isinstance(gap, dict):
            continue
        if gap.get("type") != "insufficient_security_quality":
            continue
        if attempt.get("judge", {}).get("failure_type") != "insufficient_security_quality":
            continue
        domain_metrics = attempt.get("judge", {}).get("domain_metrics", {}) or {}
        if domain_metrics.get("coverage_ratio") != 1.0:
            continue
        return gap
    return {}


def _latest_experimental_tool_gap(previous_attempts: list[dict[str, Any]]) -> dict[str, Any]:
    if len(previous_attempts) < 2:
        return {}

    last_attempt = previous_attempts[-1]
    prior_attempt = previous_attempts[-2]
    last_judge = last_attempt.get("judge", {}) or {}
    prior_judge = prior_attempt.get("judge", {}) or {}
    last_failure = str(last_judge.get("failure_type", "") or "")
    prior_failure = str(prior_judge.get("failure_type", "") or "")

    repeated_tool_failures = {
        "llm_checker_content_filter",
        "llm_checker_execution_error",
        "execution_failure",
        "tool_execution_error",
    }
    if last_failure in repeated_tool_failures and prior_failure in repeated_tool_failures:
        source_tool_names = _dedupe_preserve_order(
            _extract_tool_names_from_pipeline(last_attempt.get("pipeline", ""))
            + _extract_tool_names_from_pipeline(prior_attempt.get("pipeline", ""))
        )
        return {
            "type": "repeated_tool_failures",
            "source_tool_names": source_tool_names,
            "reason": (
                "The last two attempts both indicate tool-level execution instability. "
                f"Previous failure types: {prior_failure}, {last_failure}."
            ),
        }

    if last_failure == "insufficient_security_quality" and prior_failure == "insufficient_security_quality":
        last_metrics = last_judge.get("domain_metrics", {}) or {}
        prior_metrics = prior_judge.get("domain_metrics", {}) or {}
        if last_metrics.get("coverage_ratio") == 1.0 and prior_metrics.get("coverage_ratio") == 1.0:
            try:
                last_score = float(last_metrics.get("security_score", 0.0))
                prior_score = float(prior_metrics.get("security_score", 0.0))
                last_judge_score = float(last_judge.get("score", 0.0))
                prior_judge_score = float(prior_judge.get("score", 0.0))
            except Exception:
                return {}
            if abs(last_score - prior_score) <= 10.0 and abs(last_judge_score - prior_judge_score) <= 0.1:
                source_tool_names = _dedupe_preserve_order(
                    _extract_tool_names_from_pipeline(last_attempt.get("pipeline", ""))
                    + _extract_tool_names_from_pipeline(prior_attempt.get("pipeline", ""))
                )
                return {
                    "type": "quality_plateau",
                    "source_tool_names": source_tool_names,
                    "reason": (
                        "The last two attempts both satisfied baseline coverage but plateaued on quality. "
                        f"Security scores were {prior_score:.1f} and {last_score:.1f}; "
                        f"judge scores were {prior_judge_score:.2f} and {last_judge_score:.2f}."
                    ),
                }
    return {}


def _latest_failed_experimental_candidate(previous_attempts: list[dict[str, Any]]) -> dict[str, Any]:
    for attempt in reversed(previous_attempts):
        for candidate in reversed(_attempt_candidates(attempt)):
            if candidate.get("candidate_type") != "experimental_python_tool":
                continue
            if candidate.get("validation_status") != "smoke_failed":
                continue
            return {
                "candidate_id": candidate.get("candidate_id"),
                "candidate_name": candidate.get("name"),
                "validation_summary": candidate.get("validation_summary"),
                "source_tool_names": candidate.get("source_tool_names", []),
                "reason": (
                    "A previous experimental Python tool candidate failed validation. "
                    f"Candidate={candidate.get('candidate_id')} "
                    f"name={candidate.get('name')} "
                    f"summary={candidate.get('validation_summary')}."
                ),
            }
    return {}


def _looks_like_content_filter_text(text: str) -> bool:
    normalized = text.lower()
    markers = [
        "content_filter",
        "content filter",
        "content management policy",
        "filtered due to the prompt",
        "moderation",
    ]
    return any(marker in normalized for marker in markers)


def _looks_like_llm_checker_name(checker_name: str) -> bool:
    return "llmjudge" in checker_name.lower()


def _looks_like_checker_execution_error(result: dict[str, Any]) -> bool:
    details = result.get("details", {})
    if not isinstance(details, dict):
        return False
    if details.get("checker_execution_error") is True:
        return True
    error_text = str(details.get("error", "")).lower()
    evidence_text = str(result.get("evidence", "")).lower()
    combined = f"{error_text}\n{evidence_text}"
    return any(marker in combined for marker in [
        "status 5",
        "error code: 5",
        "checker execution failed",
        "request has failed",
        "upstream_error",
        "timeout",
    ])


def _security_audit_safe_fallback_checkers(avoid_checkers: list[str] | set[str] | None = None) -> list[str]:
    avoid = set(avoid_checkers or [])
    fallback = ["PIIRule", "SecretRule", "ToxicityKeywordRule", "HarmfulKeywordRule"]
    return [name for name in fallback if name not in avoid]


def _metric_delta(current: dict[str, Any], baseline: dict[str, Any], key: str) -> float | None:
    if current.get(key) is None or baseline.get(key) is None:
        return None
    try:
        return round(float(current[key]) - float(baseline[key]), 4)
    except Exception:
        return None


def _extract_dataset_name_from_pipeline(pipeline: str) -> str | None:
    match = re.search(r'load_dataset\(\s*["\']([^"\']+)["\']', pipeline)
    if match:
        return match.group(1)
    return None


def _count_run_tool_calls(pipeline: str) -> int:
    return len(re.findall(r"\brun_tool\s*\(", pipeline))


def _extract_external_write_targets(pipeline: str) -> list[str]:
    try:
        tree = ast.parse(pipeline)
    except SyntaxError:
        return []

    targets: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "write_file":
            continue

        path_node = None
        if len(node.args) >= 2:
            path_node = node.args[1]
        else:
            for keyword in node.keywords:
                if keyword.arg == "path":
                    path_node = keyword.value
                    break
        if path_node is None:
            continue

        path_value = _literal_string_value(path_node) or "<dynamic_path>"
        if _is_internal_system_write_target(path_value):
            continue
        if path_value not in targets:
            targets.append(path_value)
    return targets


def _literal_string_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                return None
        return "".join(parts)
    return None


def _is_internal_system_write_target(path_value: str) -> bool:
    normalized = str(path_value or "").strip()
    if not normalized or normalized == "<dynamic_path>":
        return False
    normalized = normalized.replace("\\", "/")
    internal_prefixes = [
        ".elf/",
        "./.elf/",
        ".logs/",
        "./.logs/",
        ".jobs/",
        "./.jobs/",
    ]
    if normalized in {".elf", ".logs", ".jobs"}:
        return True
    return any(normalized.startswith(prefix) for prefix in internal_prefixes)


def _build_write_approval_prompt(paths: list[str]) -> str:
    if not paths:
        return ""
    if len(paths) == 1:
        return (
            "This workflow wants to write to an external file:\n"
            f"- {paths[0]}\n"
            "Allow this write?"
        )
    lines = "\n".join(f"- {path}" for path in paths)
    return (
        "This workflow wants to write to external files:\n"
        f"{lines}\n"
        "Allow these writes?"
    )


def _apply_revised_write_targets_to_pipeline(
    pipeline: str,
    paths: list[str],
    user_answer: str,
) -> tuple[str, list[str]] | None:
    revision = _extract_revised_write_target(user_answer)
    if not revision:
        return None

    rewritten_pipeline = pipeline
    rewritten_paths: list[str] = []
    for original_path in paths:
        revised_path = _resolve_revised_write_path(original_path, revision, user_answer)
        rewritten_pipeline = _replace_literal_path(rewritten_pipeline, original_path, revised_path)
        rewritten_paths.append(revised_path)
    return rewritten_pipeline, rewritten_paths


def _extract_revised_write_target(user_answer: str) -> str:
    text = _coerce_text_block(user_answer)
    if not text:
        return ""
    candidates = re.findall(r"(?:\./|/)?(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+/?", text)
    prioritized = [
        candidate
        for candidate in candidates
        if "/" in candidate or re.search(r"\.[A-Za-z0-9]+$", candidate)
    ]
    if not prioritized:
        return ""
    return max(prioritized, key=len).strip()


def _resolve_revised_write_path(original_path: str, revision: str, user_answer: str) -> str:
    normalized_revision = revision.replace("\\", "/").strip()
    if not normalized_revision:
        return original_path

    original_name = PurePosixPath(original_path).name
    answer_lower = _coerce_text_block(user_answer).lower()
    looks_like_directory = (
        normalized_revision.endswith("/")
        or not re.search(r"\.[A-Za-z0-9]+$", PurePosixPath(normalized_revision).name)
        and any(marker in answer_lower for marker in ["path", "dir", "directory", "路径", "目录", "下"])
    )
    if looks_like_directory:
        return f"{normalized_revision.rstrip('/')}/{original_name}"
    return normalized_revision


def _replace_literal_path(pipeline: str, original_path: str, revised_path: str) -> str:
    pattern = re.compile(rf"(?P<quote>['\"])({re.escape(original_path)})(?P=quote)")
    return pattern.sub(
        lambda match: f"{match.group('quote')}{revised_path}{match.group('quote')}",
        pipeline,
    )


def _tokenize_text(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", value.lower()))


def _task_text_for_analysis(task: str) -> str:
    lines: list[str] = []
    for raw_line in str(task or "").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped in {
            "Clarification update:",
            "Pilot attempt note:",
            "Revision instructions:",
        }:
            continue
        if stripped.startswith("Planner note:"):
            continue
        if stripped.startswith("- Assistant asked:"):
            continue
        if stripped.startswith("- User replied:"):
            lines.append(stripped.split(":", 1)[1].strip())
            continue
        if stripped.startswith("- Accepted defaults:"):
            lines.append(stripped.split(":", 1)[1].strip())
            continue
        if stripped.startswith("- Resolved slots:"):
            lines.append(stripped.split(":", 1)[1].strip())
            continue
        if stripped.startswith("- This is pilot attempt"):
            continue
        if stripped.startswith("- If you write external files"):
            continue
        lines.append(stripped)
    return "\n".join(lines)


def _tool_default_summary(schema: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    properties = schema.get("parameters", {}).get("properties", {})
    for name, spec in properties.items():
        if "default" in spec:
            defaults[name] = spec["default"]
    return defaults


def _configured_tool_names(config: Any) -> set[str] | None:
    tool_names = getattr(config, "tools", None) or []
    return set(tool_names) if tool_names else None


def _visible_tool_schemas(config: Any, registry: ToolRegistry) -> list[dict[str, Any]]:
    allowed_tool_names = _configured_tool_names(config)
    schemas = registry.list_schemas()
    if allowed_tool_names is None:
        return schemas
    return [schema for schema in schemas if schema.get("name") in allowed_tool_names]


def _rank_tool_schemas(
    task: str,
    registry: ToolRegistry,
    allowed_tool_names: set[str] | None = None,
) -> list[tuple[int, dict[str, Any]]]:
    task_tokens = _tokenize_text(_task_text_for_analysis(task))
    ranked: list[tuple[int, dict[str, Any]]] = []

    for tool_name, tool in registry.tools.items():
        if allowed_tool_names is not None and tool_name not in allowed_tool_names:
            continue
        schema = tool.get_schema()
        corpus = " ".join([
            schema.get("name", ""),
            schema.get("description", ""),
            schema.get("usage_example", ""),
        ])
        overlap = len(task_tokens & _tokenize_text(corpus))
        if overlap > 0:
            ranked.append((overlap, schema | {"default_summary": _tool_default_summary(schema)}))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def _select_relevant_tool_schemas(
    task: str,
    registry: ToolRegistry,
    limit: int = 4,
    allowed_tool_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    ranked = _rank_tool_schemas(task, registry, allowed_tool_names=allowed_tool_names)
    shortlist = [schema for _, schema in ranked[:limit]]
    if shortlist:
        return shortlist

    suppress_security_audit = (
        _task_looks_like_structured_data_processing(task)
        and not _mentions_security_audit_intent(task)
    )
    fallback: list[dict[str, Any]] = []
    for tool_name, tool in registry.tools.items():
        if allowed_tool_names is not None and tool_name not in allowed_tool_names:
            continue
        schema = tool.get_schema()
        if suppress_security_audit and str(schema.get("name", "")).lower() == "security_audit":
            continue
        fallback.append(schema | {"default_summary": _tool_default_summary(schema)})
        if len(fallback) >= limit:
            break
    return fallback


def _select_primary_tool_schema(task: str, registry: ToolRegistry) -> dict[str, Any] | None:
    ranked = _rank_tool_schemas(task, registry)
    if not ranked:
        return None
    top_score, top_schema = ranked[0]
    if top_score <= 0:
        return None
    if len(ranked) == 1:
        return top_schema

    second_score, second_schema = ranked[1]
    if second_score < top_score:
        return top_schema

    task_tokens = _tokenize_text(_task_text_for_analysis(task))
    if top_schema.get("name", "").lower() in task_tokens and second_schema.get("name", "").lower() not in task_tokens:
        return top_schema
    return None


def _task_looks_like_structured_data_processing(task: str) -> bool:
    analysis_task = _task_text_for_analysis(task)
    tokens = _tokenize_text(analysis_task)
    data_tokens = {
        "data", "dataset", "record", "records", "row", "rows", "json", "jsonl",
        "field", "fields", "column", "columns", "table", "tables", "id", "ids",
        "message", "messages", "timestamp", "timestamps", "metadata",
    }
    operation_tokens = {
        "dedup", "dedupe", "duplicate", "duplicates", "merge", "merged", "merging",
        "normalize", "normalized", "canonicalize", "canonical", "group", "groupby",
        "aggregate", "collapse", "reconcile", "consolidate", "transform", "clean",
        "cleanup", "standardize", "compare", "matching", "filter", "filtered",
        "extract", "extraction", "select", "export", "write", "count", "counts",
    }
    structured_markers = (
        "抽取",
        "筛选",
        "过滤",
        "写入",
        "导出",
        "总共多少",
        "统计",
        "dataset type",
        "dataset_type",
    )
    return (
        (bool(tokens & data_tokens) and bool(tokens & operation_tokens))
        or any(marker in analysis_task.lower() for marker in structured_markers)
    )


def _contains_standalone_identifier(text: str, identifier: str) -> bool:
    normalized_text = text.lower()
    normalized_identifier = identifier.lower()
    pattern = rf"(?<![a-z0-9_]){re.escape(normalized_identifier)}(?![a-z0-9_])"
    return re.search(pattern, normalized_text) is not None


def _mentions_security_audit_intent(task: str) -> bool:
    analysis_task = _task_text_for_analysis(task)
    normalized = analysis_task.lower()
    if _negates_security_audit_intent(normalized):
        return False
    task_tokens = _tokenize_text(analysis_task)
    checker_names = [
        "HarmfulContentLLMJudge",
        "BiasLLMJudge",
        "ToxicityLLMJudge",
        "PIILLMJudge",
        "SycophancyLLMJudge",
        "AlignmentRefusalBypassRule",
        "ToxicityKeywordRule",
        "HarmfulKeywordRule",
        "PIIRule",
        "SecretRule",
    ]
    explicit_checker = any(_contains_standalone_identifier(normalized, name) for name in checker_names)
    explicit_tool = _contains_standalone_identifier(normalized, "security_audit")
    phrase_match = (
        "security audit" in normalized
        or "安全审计" in normalized
        or "security checker" in normalized
        or "checker_names" in normalized
    )
    checker_combo = ("checker" in task_tokens or "checkers" in task_tokens) and bool(
        task_tokens & {"security", "audit", "harmful", "toxicity", "pii", "secret"}
    )
    audit_combo = "audit" in task_tokens and bool(
        task_tokens & {"security", "harmful", "toxicity", "pii", "secret"}
    )
    return explicit_checker or explicit_tool or phrase_match or checker_combo or audit_combo


def _negates_security_audit_intent(normalized_task: str) -> bool:
    negation_markers = [
        "不需要跑security audit",
        "不需要 security audit",
        "不需要安全审计",
        "不是security audit",
        "不是 security audit",
        "不是安全审计",
        "无需security audit",
        "无需 security audit",
        "无需安全审计",
        "just data processing",
        "only data processing",
        "not a security audit",
        "no security audit",
        "do not run security audit",
        "don't run security audit",
    ]
    return any(marker in normalized_task for marker in negation_markers)


def _parameter_to_slot_name(parameter_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", parameter_name.lower()).strip("_")
    if normalized in {"data", "dataset", "dataset_name", "input_data", "records", "record_data"}:
        return "dataset_name"
    return parameter_name


def _normalize_slot_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _slot_expects_field_reference(slot_spec: dict[str, Any]) -> bool:
    haystack = " ".join([
        str(slot_spec.get("slot_name", "")),
        str(slot_spec.get("parameter_name", "")),
        str(slot_spec.get("property_schema", {}).get("title", "")),
    ]).lower()
    return any(token in haystack for token in ["field", "column", "attribute", "key"])


def _slot_expects_value_reference(slot_spec: dict[str, Any]) -> bool:
    haystack = " ".join([
        str(slot_spec.get("slot_name", "")),
        str(slot_spec.get("parameter_name", "")),
        str(slot_spec.get("property_schema", {}).get("title", "")),
        str(slot_spec.get("property_schema", {}).get("description", "")),
    ]).lower()
    return "value" in haystack


def _candidate_dataset_fields(
    dataset_schemas: dict[str, list[str]],
    preferred_dataset_name: str | None = None,
) -> list[str]:
    ordered_fields: list[str] = []
    seen: set[str] = set()
    dataset_names = list(dataset_schemas)
    if preferred_dataset_name in dataset_schemas:
        dataset_names = [preferred_dataset_name] + [
            name for name in dataset_names if name != preferred_dataset_name
        ]
    for dataset_name in dataset_names:
        for field_name in dataset_schemas.get(dataset_name, []):
            normalized = str(field_name).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered_fields.append(normalized)
    return ordered_fields


def _extract_dataset_field_reference(
    text: str,
    dataset_schemas: dict[str, list[str]],
    preferred_dataset_name: str | None = None,
) -> str | None:
    if not text or not dataset_schemas:
        return None
    normalized_text = _normalize_dataset_text(text)
    if not normalized_text:
        return None

    fields = sorted(
        _candidate_dataset_fields(dataset_schemas, preferred_dataset_name),
        key=lambda item: len(_normalize_dataset_text(item)),
        reverse=True,
    )
    for field_name in fields:
        normalized_field = _normalize_dataset_text(field_name)
        if not normalized_field:
            continue
        if len(normalized_field) <= 2:
            if normalized_field in normalized_text.split("_"):
                return field_name
            continue
        if (
            normalized_text == normalized_field
            or re.search(
                rf"(?<![a-z0-9]){re.escape(normalized_field)}(?![a-z0-9])",
                normalized_text,
            )
        ):
            return field_name
    return None


def _extract_unmatched_dataset_field_reference(
    text: str,
    dataset_schemas: dict[str, list[str]],
    preferred_dataset_name: str | None = None,
) -> str | None:
    if not text or not dataset_schemas:
        return None
    fields = _candidate_dataset_fields(dataset_schemas, preferred_dataset_name)
    if not fields:
        return None
    normalized_fields = {
        _normalize_dataset_text(field_name): field_name
        for field_name in fields
    }
    patterns = [
        r"([a-zA-Z_][a-zA-Z0-9_ ]{1,40}?)\s*(?:==|=|为|是)\s*[\"'`]?[a-zA-Z0-9_.-]+",
        r"(?:field|column|attribute|key|字段|列|属性)\s*(?:名|name)?\s*(?:是|为|:|：)?\s*([a-zA-Z_][a-zA-Z0-9_ ]{1,40})",
    ]
    generic_tokens = {
        "data",
        "dataset",
        "record",
        "records",
        "value",
        "output",
        "json",
        "csv",
        "true",
        "false",
    }
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            candidate = _clean_slot_text(match.group(1))
            normalized_candidate = _normalize_dataset_text(candidate)
            if (
                not normalized_candidate
                or normalized_candidate in normalized_fields
                or normalized_candidate in generic_tokens
                or normalized_candidate.isdigit()
            ):
                continue
            return candidate
    return None


def _extract_comparison_value_from_text(text: str) -> str | None:
    if not text:
        return None
    patterns = [
        r"(?:==|=|为|是|equals?|equal to|set to)\s*[\"'`]?([a-zA-Z0-9_.-]+)[\"'`]?(?:的|$|[,，。；;\)])",
        r"(?:value|值)\s*(?:==|=|:|为|是)\s*[\"'`]?([a-zA-Z0-9_.-]+)[\"'`]?(?:的|$|[,，。；;\)])",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _clean_slot_text(match.group(1))
    return None


def _slot_aliases(slot_name: str, parameter_name: str, property_schema: dict[str, Any]) -> list[str]:
    aliases = [slot_name, parameter_name]
    title = property_schema.get("title")
    if isinstance(title, str) and title.strip():
        aliases.append(title.strip())
    return _dedupe_preserve_order([alias for alias in aliases if alias])


def _fallback_generic_slot_spec(slot_name: str) -> dict[str, Any] | None:
    if slot_name == "filter_field":
        property_schema = {
            "type": "string",
            "title": "filter field",
            "description": "Dataset field name used for filtering.",
        }
    elif slot_name == "filter_value":
        property_schema = {
            "type": "string",
            "title": "filter value",
            "description": "Value to match in the selected field.",
        }
    elif slot_name == "output_format":
        property_schema = {
            "type": "string",
            "enum": ["json", "csv", "parquet"],
            "title": "output format",
            "description": "Export file format.",
        }
    elif slot_name == "output_filename":
        property_schema = {
            "type": "string",
            "title": "output filename",
            "description": "Output file path or filename.",
        }
    else:
        return None
    return {
        "slot_name": slot_name,
        "parameter_name": slot_name,
        "tool_name": None,
        "property_schema": property_schema,
        "aliases": _slot_aliases(slot_name, slot_name, property_schema),
    }


def _schema_required_slot_specs(
    task: str,
    registry: ToolRegistry,
    dataset_schemas: dict[str, list[str]],
) -> dict[str, dict[str, Any]]:
    primary_tool = _select_primary_tool_schema(task, registry)
    if primary_tool is None:
        return {}

    specs: dict[str, dict[str, Any]] = {}
    parameters = primary_tool.get("parameters", {})
    properties = parameters.get("properties", {})
    for parameter_name in parameters.get("required", []):
        property_schema = properties.get(parameter_name, {})
        slot_name = _parameter_to_slot_name(parameter_name)
        if _is_optional_execution_detail(slot_name):
            continue
        if slot_name == "dataset_name":
            continue
        specs[slot_name] = {
            "slot_name": slot_name,
            "parameter_name": parameter_name,
            "tool_name": primary_tool.get("name"),
            "property_schema": property_schema,
            "aliases": _slot_aliases(slot_name, parameter_name, property_schema),
        }
    return specs


def _slot_value_from_resolved_slots(slot_name: str, resolved_slots: dict[str, Any]) -> Any:
    if slot_name == "dataset_name":
        return (
            resolved_slots.get("dataset_name")
            or resolved_slots.get("dataset")
            or resolved_slots.get("data")
        )
    return resolved_slots.get(slot_name)


def _canonical_resolved_slot_key(key: str) -> str:
    normalized = key.lower().strip()
    if normalized in {"dataset", "dataset_name", "data", "input_data", "records", "record_data"}:
        return "dataset_name"
    if "checker" in normalized:
        return "checker_names"
    return key


def _filter_resolved_slots_by_missing_items(
    resolved_slots: dict[str, Any],
    missing_items: list[str],
) -> dict[str, Any]:
    if not isinstance(resolved_slots, dict) or not resolved_slots:
        return {}
    allowed = set(missing_items)
    filtered: dict[str, Any] = {}
    for key, value in resolved_slots.items():
        canonical = _canonical_resolved_slot_key(key)
        if canonical in allowed or (key == "selection_mode" and "checker_names" in allowed):
            filtered[key] = value
    return filtered


def _missing_required_slots(
    slot_specs: dict[str, dict[str, Any]],
    resolved_slots: dict[str, Any],
) -> list[str]:
    missing: list[str] = []
    for slot_name in slot_specs:
        if _slot_value_from_resolved_slots(slot_name, resolved_slots) in (None, "", [], {}):
            missing.append(slot_name)
    return missing


def _default_slots_from_required_specs(
    slot_specs: dict[str, dict[str, Any]],
    missing_items: list[str],
) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for item in missing_items:
        property_schema = slot_specs.get(item, {}).get("property_schema", {})
        if "default" in property_schema:
            defaults[item] = property_schema["default"]
    return defaults


def _extract_named_slot_fragment(text: str, aliases: list[str]) -> str | None:
    for alias in aliases:
        alias_pattern = re.escape(alias).replace(r"\_", "[_\\s]*").replace(r"\ ", "[_\\s]+")
        patterns = [
            rf"{alias_pattern}\s*(?:=|:)\s*([^\n;,]+)",
            rf"{alias_pattern}\s+(?:is|as)\s+([^\n;,]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
    return None


def _clean_slot_text(value: str) -> str:
    return value.strip().strip("\"'`").strip().rstrip(".")


def _coerce_slot_value(raw_value: str, property_schema: dict[str, Any]) -> Any:
    value = _clean_slot_text(raw_value)
    enum_values = property_schema.get("enum", [])
    if isinstance(enum_values, list) and enum_values:
        normalized_map = {
            _normalize_slot_label(str(option)): option
            for option in enum_values
        }
        direct = normalized_map.get(_normalize_slot_label(value))
        if direct is not None:
            return direct

    schema_type = property_schema.get("type")
    if schema_type == "boolean":
        normalized = value.lower()
        if normalized in {"true", "yes", "y", "on", "1"}:
            return True
        if normalized in {"false", "no", "n", "off", "0"}:
            return False
        return None
    if schema_type == "integer":
        match = re.search(r"-?\d+", value)
        return int(match.group(0)) if match else None
    if schema_type == "number":
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        return float(match.group(0)) if match else None
    if schema_type == "array":
        if value.startswith("[") and value.endswith("]"):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
        items = [_clean_slot_text(part) for part in re.split(r"[,，\n]+", value) if _clean_slot_text(part)]
        return items or None
    return value or None


def _extract_generic_slot_values_from_text(
    slot_specs: dict[str, dict[str, Any]],
    missing_items: list[str],
    text: str,
    *,
    allow_freeform_single_value: bool,
    dataset_schemas: dict[str, list[str]] | None = None,
    preferred_dataset_name: str | None = None,
) -> dict[str, Any]:
    if (
        not text
        or _looks_like_default_reply(text)
        or _looks_like_weak_reply(text)
        or _looks_like_option_request(text)
    ):
        return {}

    generic_items = [
        item for item in missing_items
        if item not in {"dataset_name", "checker_names"} and (item in slot_specs or _fallback_generic_slot_spec(item) is not None)
    ]
    if not generic_items:
        return {}

    resolved: dict[str, Any] = {}
    lowered = text.lower()
    for item in generic_items:
        slot_spec = slot_specs.get(item) or _fallback_generic_slot_spec(item) or {}
        property_schema = slot_spec.get("property_schema", {})
        aliases = slot_spec.get("aliases", [item])
        named_fragment = _extract_named_slot_fragment(text, aliases)
        raw_candidate = named_fragment

        if raw_candidate is None and _slot_expects_field_reference(slot_spec):
            raw_candidate = _extract_dataset_field_reference(
                text,
                dataset_schemas or {},
                preferred_dataset_name,
            )

        if raw_candidate is None and property_schema.get("enum"):
            normalized_map = {
                _normalize_slot_label(str(option)): option
                for option in property_schema.get("enum", [])
            }
            normalized_text = _normalize_slot_label(text)
            for normalized_option, option in normalized_map.items():
                option_pattern = re.escape(str(option).lower())
                if normalized_option and (
                    normalized_option == normalized_text
                    or re.search(rf"(?<![a-z0-9]){option_pattern}(?![a-z0-9])", lowered)
                ):
                    raw_candidate = str(option)
                    break

        if raw_candidate is None and _slot_expects_value_reference(slot_spec):
            raw_candidate = _extract_comparison_value_from_text(text)

        if raw_candidate is None and allow_freeform_single_value and len(generic_items) == 1:
            raw_candidate = text

        if raw_candidate is None:
            continue

        coerced = _coerce_slot_value(raw_candidate, property_schema)
        if coerced not in (None, "", [], {}):
            resolved[item] = coerced
    return resolved


def _extract_schema_required_slots_from_text(
    task: str,
    registry: ToolRegistry,
    dataset_schemas: dict[str, list[str]],
) -> dict[str, Any]:
    slot_specs = _schema_required_slot_specs(task, registry, dataset_schemas)
    if not slot_specs:
        return {}

    resolved: dict[str, Any] = {}
    if "dataset_name" in slot_specs:
        dataset_name = _extract_dataset_name_from_user_reply(
            ["dataset_name"],
            task,
            dataset_schemas,
        )
        if dataset_name:
            resolved["dataset_name"] = dataset_name

    resolved |= _extract_generic_slot_values_from_text(
        slot_specs,
        list(slot_specs),
        task,
        allow_freeform_single_value=False,
        dataset_schemas=dataset_schemas,
        preferred_dataset_name=resolved.get("dataset_name"),
    )
    return resolved


def _build_missing_slot_followup_message(
    missing_items: list[str],
    slot_specs: dict[str, dict[str, Any]],
    dataset_schemas: dict[str, list[str]],
) -> str:
    if not missing_items:
        return "Please clarify the missing required inputs."

    first_missing = missing_items[0]
    if first_missing == "dataset_name":
        return _build_dataset_options_message(dataset_schemas)
    if first_missing == "checker_names":
        return "Please specify which security_audit checker_names you want to use."
    if first_missing == "filter_field":
        return "Which field/column should I filter on? For example: `dataset_type`."
    if first_missing == "filter_value":
        return "What value should I match for that field? For example: `rl`."
    if first_missing == "output_format":
        return "Which output format should I use? For example: `json` or `csv`."
    if first_missing == "output_filename":
        return "What output filename or path should I use?"

    slot_spec = slot_specs.get(first_missing, {})
    parameter_name = slot_spec.get("parameter_name", first_missing)
    tool_name = slot_spec.get("tool_name")
    property_schema = slot_spec.get("property_schema", {})
    tool_label = f" for `{tool_name}`" if tool_name else ""
    enum_values = property_schema.get("enum", [])
    if isinstance(enum_values, list) and enum_values:
        return (
            f"Please specify `{parameter_name}`{tool_label}. "
            f"Available options: {', '.join(str(option) for option in enum_values[:8])}."
        )
    if "default" in property_schema:
        return (
            f"Please specify `{parameter_name}`{tool_label} "
            "or reply with `use defaults`."
        )
    description = property_schema.get("description")
    if isinstance(description, str) and description.strip():
        return f"Please specify `{parameter_name}`{tool_label}. {description.strip()}"
    return f"Please specify `{parameter_name}`{tool_label}."


def _is_user_facing_generic_missing_item(item: str) -> bool:
    return item in {
        "dataset_name",
        "checker_names",
        "filter_field",
        "filter_value",
        "output_format",
        "output_filename",
    }


def _should_force_programmatic_missing_slot_followup(
    missing_items: list[str],
    slot_specs: dict[str, dict[str, Any]],
) -> bool:
    if not missing_items:
        return False
    for item in missing_items:
        if item in slot_specs or _is_user_facing_generic_missing_item(item):
            continue
        return False
    return True


def _build_unknown_dataset_field_message(
    dataset_name: str,
    unknown_field: str,
    dataset_schemas: dict[str, list[str]],
) -> str:
    available_fields = dataset_schemas.get(dataset_name, [])
    preview = ", ".join(f"`{field}`" for field in available_fields[:8])
    suffix = f" Available fields are: {preview}." if preview else ""
    return (
        f"The dataset `{dataset_name}` does not have a field/column named `{unknown_field}`."
        f"{suffix} Which field did you mean?"
    )


def _find_dataset_field_reference_issue(
    *,
    task_text: str,
    last_user_reply: str,
    resolved_slots: dict[str, Any],
    dataset_schemas: dict[str, list[str]],
) -> dict[str, Any] | None:
    if not (
        any(key in resolved_slots for key in ["filter_field", "filter_value"])
        or _looks_like_dataset_field_operation(task_text)
        or _looks_like_dataset_field_operation(last_user_reply)
    ):
        return None
    dataset_name = _slot_value_from_resolved_slots("dataset_name", resolved_slots)
    if not dataset_name or dataset_name not in dataset_schemas:
        return None

    candidate_texts: list[str] = []
    if last_user_reply:
        candidate_texts.append(last_user_reply)
    if task_text and task_text not in candidate_texts:
        candidate_texts.append(task_text)

    for text in candidate_texts:
        if _extract_dataset_field_reference(text, dataset_schemas, dataset_name):
            return None
        unknown_field = _extract_unmatched_dataset_field_reference(
            text,
            dataset_schemas,
            dataset_name,
        )
        if unknown_field:
            return {
                "unknown_field": unknown_field,
                "message": _build_unknown_dataset_field_message(
                    dataset_name,
                    unknown_field,
                    dataset_schemas,
                ),
            }
    return None


def _looks_like_dataset_field_operation(text: str) -> bool:
    normalized = str(text or "").lower()
    if not normalized:
        return False
    markers = [
        "filter",
        "field",
        "column",
        "attribute",
        "where",
        "flag",
        "match value",
        "dataset type",
        "==",
        "=",
        "为",
        "字段",
        "列",
        "筛选",
        "过滤",
        "按",
        "提取",
    ]
    return any(marker in normalized for marker in markers)


def _update_missing_item_retry_counts(
    retry_counts: dict[str, int],
    unresolved_missing_items: list[str],
) -> None:
    current = set(unresolved_missing_items)
    for item in list(retry_counts):
        if item not in current:
            retry_counts.pop(item, None)
    for item in unresolved_missing_items:
        retry_counts[item] = retry_counts.get(item, 0) + 1


def _looks_like_meaningful_clarification_reply(user_reply: str) -> bool:
    if (
        not user_reply
        or _looks_like_weak_reply(user_reply)
        or _looks_like_option_request(user_reply)
        or _looks_like_default_reply(user_reply)
    ):
        return False
    tokens = re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]+", user_reply)
    return len(tokens) >= 2


def _should_trust_semantic_user_reply_for_missing_items(
    *,
    user_reply: str,
    unresolved_missing_items: list[str],
    retry_counts: dict[str, int],
) -> bool:
    if not unresolved_missing_items:
        return False
    if not _looks_like_meaningful_clarification_reply(user_reply):
        return False
    return any(retry_counts.get(item, 0) >= 2 for item in unresolved_missing_items)


def _build_security_audit_hints(
    task: str,
    registry: ToolRegistry,
) -> dict[str, Any] | None:
    if registry.get("security_audit") is None:
        return None
    if not _mentions_security_audit_intent(task):
        return None

    try:
        from tools.security_audit.checker.registry import CheckerRegistry

        available_runtime_checkers = list(CheckerRegistry.list_all().keys())
    except Exception:
        available_runtime_checkers = []
    combined_checkers = _dedupe_preserve_order([
        "PIIRule",
        "SecretRule",
        "ToxicityKeywordRule",
        "HarmfulKeywordRule",
        "BiasKeywordRule",
        "AlignmentRefusalBypassRule",
        "HarmfulContentLLMJudge",
        "BiasLLMJudge",
        "ToxicityLLMJudge",
        "PIILLMJudge",
        "SycophancyLLMJudge",
    ])
    if available_runtime_checkers:
        combined_checkers = [name for name in combined_checkers if name in available_runtime_checkers]
    combined_checkers = [
        name
        for name in combined_checkers
        if name not in {
            "JailbreakLLMJudge",
            "PromptInjectionLLMJudge",
            "JailbreakClassifier",
            "PromptInjectionClassifier",
        }
    ]
    llm_required = [name for name in combined_checkers if "LLMJudge" in name]
    model_based = [name for name in combined_checkers if name.endswith(("Classifier", "Detector"))]
    rule_based = [name for name in combined_checkers if name.endswith("Rule")]

    return {
        "tool_name": "security_audit",
        "checker_names_available": combined_checkers,
        "default_checker_combo": [
            "PIIRule",
            "SecretRule",
            "ToxicityKeywordRule",
            "HarmfulKeywordRule",
        ],
        "llm_required_checkers": llm_required,
        "model_based_checkers": model_based,
        "rule_based_checkers": rule_based,
        "quick_baseline_checkers": [
            "PIIRule",
            "SecretRule",
            "ToxicityKeywordRule",
            "HarmfulKeywordRule",
        ],
        "cost_guidance": {
            "cheapest": ["PIIRule", "SecretRule", "ToxicityKeywordRule", "HarmfulKeywordRule", "BiasKeywordRule"],
            "more_expensive_llm": llm_required,
            "model_based_gpu_optional": model_based,
        },
        "recommended_reply_examples": [
            "use defaults",
            "use HarmfulContentLLMJudge, ToxicityLLMJudge, PIILLMJudge",
        ],
    }


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
