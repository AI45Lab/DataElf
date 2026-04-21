import sys
import json
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).parent.parent))

from agentic import PilotController
from cli.common import bootstrap_environment
from cli.pretty import action, banner, error, focus, inspect, insight, metric, section, success, table, trophy, warn


def _fmt_delta(value: object) -> str:
    if value is None:
        return "-"
    try:
        numeric = float(value)
    except Exception:
        return str(value)
    if numeric > 0:
        return f"+{numeric}"
    return str(numeric)


@click.command()
@click.argument("task")
@click.option("--config", "-c", type=click.Path(exists=True), help="Path to config file")
@click.option("--prefix", "-p", type=str, help="Config file prefix (resolves to <prefix>-config.yaml)")
@click.option("--wait", "-w", is_flag=True, help="Wait for pilot loop completion")
@click.option("--budget-steps", type=int, default=3, show_default=True, help="Max pilot attempts")
@click.option("--allow-experimental-tools", is_flag=True, help="Allow pilot to generate and load experimental Python tool drafts")
@click.option("--debug", is_flag=True, help="Print per-attempt planner/judge JSON and LLM call states")
def pilot(
    task: str,
    config: str | None,
    prefix: str | None,
    wait: bool,
    budget_steps: int,
    allow_experimental_tools: bool,
    debug: bool,
) -> None:
    env = bootstrap_environment(
        config_path=config,
        prefix=prefix,
        allow_experimental_tools=allow_experimental_tools,
        include_candidate_tools=False,
    )
    controller = PilotController(
        config=env["config"],
        job_manager=env["job_manager"],
        executor=env["executor"],
        registry=env["registry"],
        asset_manager=env["asset_manager"],
        llm_provider=env["llm_provider"],
    )

    banner(
        "🎯",
        "DataElf Pilot",
        subtitle=f"Agent: {env['config'].agent.type} | Model: {env['config'].agent.model}",
        color="blue",
    )
    action("Mode", f"Pilot loop · budget_steps={budget_steps}")

    def handle_checkpoint(checkpoint: dict[str, object]) -> dict[str, object]:
        checkpoint_type = checkpoint.get("checkpoint_type")
        payload = checkpoint.get("payload", {}) or {}

        if checkpoint_type == "goal_clarification":
            click.echo("")
            section("❓", "Pilot Clarification Checkpoint", color="yellow")
            click.echo(payload.get("prompt", "Please clarify the goal."))
            suggested_defaults = payload.get("suggested_defaults")
            if suggested_defaults:
                insight("Suggested Defaults", json.dumps(suggested_defaults, ensure_ascii=False))
            answer = click.prompt("> ", prompt_suffix="", default="", show_default=False).strip()
            if not answer:
                return {"decision": "answer", "answer": "use defaults"}
            return {"decision": "answer", "answer": answer}

        if checkpoint_type == "candidate_approval":
            click.echo("")
            section("🏆", "Pilot Candidate Checkpoint", color="green")
            trophy("Candidate", f"{payload.get('candidate_id')} ({payload.get('candidate_type')})")
            focus("From Attempt", str(payload.get("attempt_id")))
            if payload.get("judge_score") is not None:
                metric("Judge Score", str(payload.get("judge_score")))
            if payload.get("validation_status"):
                focus("Validation", str(payload.get("validation_status")))
            if payload.get("validation_summary"):
                insight("Summary", str(payload.get("validation_summary")))
            action("Decision Options", "approve / reject / continue")
            decision = click.prompt("> ", prompt_suffix="", default="continue", show_default=False).strip().lower()
            if decision not in {"approve", "reject", "continue"}:
                decision = "continue"
            return {"decision": decision}

        if checkpoint_type == "write_approval":
            click.echo("")
            section("📝", "Write Approval Checkpoint", color="yellow")
            click.echo(payload.get("prompt", "This workflow wants to write to external files. Allow?"))
            action("Decision Options", "allow / deny / or reply with a revised filename or path")
            answer = click.prompt("> ", prompt_suffix="", default="deny", show_default=False).strip()
            decision = answer.lower()
            if decision in {"allow", "deny"}:
                return {"decision": decision}
            if not answer:
                return {"decision": "deny"}
            return {"decision": "answer", "answer": answer}

        return {"decision": "continue"}

    def emit_event(event: dict[str, object]) -> None:
        attempt_id = event.get("attempt_id", "attempt")
        event_type = event.get("type")

        if event_type == "stage_started":
            stage = event.get("stage")
            if stage == "goal_clarification":
                inspect("Pilot Goal Clarification", f"Running model={event.get('model')}")
                return
            if stage == "security_checker_clarification":
                inspect("Security Checker Clarification", "Checking required slots")
                return
            if stage == "planner":
                action(f"{attempt_id} Planner", f"Running model={event.get('model')}")
                return
            if stage == "pipeline_generation":
                action(f"{attempt_id} Pipeline", f"Running model={event.get('model')}")
                return
            if stage == "execution":
                action(f"{attempt_id} Execution", "Running pipeline")
                return
            if stage == "judge":
                inspect(f"{attempt_id} Judge", f"Running model={event.get('model')}")
                return

        if event_type == "stage_completed":
            stage = event.get("stage")
            if stage == "goal_clarification":
                success(
                    "Pilot Goal Clarification",
                    f"status={event.get('status')} · turns={event.get('turns', 0)}",
                )
                return
            if stage == "security_checker_clarification":
                success(
                    "Security Checker Clarification",
                    f"status={event.get('status')} · turns={event.get('turns', 0)}",
                )
                return
            if stage == "execution":
                extra = ""
                if event.get("elapsed_seconds") is not None:
                    extra = f" elapsed={event.get('elapsed_seconds')}s"
                if event.get("success"):
                    success(f"{attempt_id} Execution", f"status=success{extra}")
                else:
                    error(f"{attempt_id} Execution", f"status=failed{extra}")
                if event.get("error"):
                    error(f"{attempt_id} Execution Error", str(event.get("error")))
                return

        if event_type == "attempt_started":
            index = event.get("index", "?")
            total = event.get("budget_steps", "?")
            click.echo("")
            banner("👉", f"Attempt {index}/{total}", subtitle=f"Attempt ID: {attempt_id}", color="cyan")
            action("Planning", attempt_id)
            return

        if event_type == "planner":
            llm = event.get("llm", {})
            planner_action = event.get("action", {})
            success(
                f"{attempt_id} Planner",
                f"status={llm.get('status')} · model={llm.get('model')} · action={planner_action.get('action_type')}",
            )
            if llm.get("error"):
                error(f"{attempt_id} Planner Error", str(llm.get("error")))
            if debug:
                click.echo(json.dumps({
                    "attempt_id": attempt_id,
                    "planner_action": planner_action,
                    "planner_llm": llm,
                }, ensure_ascii=False, indent=2))
            return

        if event_type == "pipeline":
            llm = event.get("llm", {})
            pipeline = event.get("pipeline", "") or ""
            extra = ""
            if llm.get("elapsed_seconds") is not None:
                extra = f" elapsed={llm.get('elapsed_seconds')}s"
            success(
                f"{attempt_id} Pipeline",
                f"status={llm.get('status')} · model={llm.get('model')}{extra}",
            )
            if llm.get("error"):
                error(f"{attempt_id} Pipeline Error", str(llm.get("error")))
            if pipeline:
                section("🔧", f"{attempt_id} Pipeline DSL", color="blue")
                click.echo(pipeline)
            return

        if event_type == "judge":
            llm = event.get("llm", {})
            judge = event.get("judge", {})
            domain_metrics = judge.get("domain_metrics", {}) or {}
            success(
                f"{attempt_id} Judge",
                f"status={llm.get('status')} · model={llm.get('model')} · goal={judge.get('goal_satisfied')}",
            )
            metric(f"{attempt_id} Judge Score", str(judge.get("score")))
            if domain_metrics.get("security_score") is not None:
                metric(
                    f"{attempt_id} Security Metrics",
                    "score="
                    f"{domain_metrics.get('security_score')} · flagged_rate={domain_metrics.get('flagged_rate')} "
                    f"· flagged_samples={domain_metrics.get('flagged_samples')}",
                )
            focus(
                f"{attempt_id} Next Action",
                f"{judge.get('recommended_next_action')} · failure_type={judge.get('failure_type')}",
            )
            if llm.get("error"):
                error(f"{attempt_id} Judge Error", str(llm.get("error")))
            if debug:
                click.echo(json.dumps({
                    "attempt_id": attempt_id,
                    "judge": judge,
                    "judge_llm": llm,
                }, ensure_ascii=False, indent=2))
            return

        if event_type == "candidate_saved":
            candidate = event.get("candidate", {})
            trophy(
                f"{attempt_id} Candidate Saved",
                f"{candidate.get('candidate_id')} · type={candidate.get('candidate_type')}",
            )
            return

        if event_type == "candidate_validated":
            validation = event.get("validation", {})
            focus(
                f"{attempt_id} Candidate Validation",
                f"{validation.get('validation_status')} · {validation.get('validation_summary')}",
            )
            return

        if event_type == "checkpoint_paused":
            warn(f"{attempt_id} Checkpoint Paused", str(event.get("checkpoint_type")))
            return

        if event_type == "checkpoint_resolved":
            response = event.get("response", {})
            success(
                f"{attempt_id} Checkpoint Resolved",
                f"{event.get('checkpoint_type')} · decision={response.get('decision')}",
            )

    if wait:
        response = controller.execute(
            task=task,
            dataset_schemas=env["dataset_schemas"],
            budget_steps=budget_steps,
            allow_experimental_tools=allow_experimental_tools,
            ask_user=sys.stdin.isatty(),
            checkpoint_handler=handle_checkpoint,
            event_handler=emit_event,
        )
        section("🏆", "Pilot Summary", color="green")
        trophy("Job ID", response["job_id"])
        focus("Pilot Status", response["status"])
        metric("Attempts", str(len(response["attempts"])))
        if response.get("pipeline_candidate_id"):
            trophy("Pipeline Candidate", response["pipeline_candidate_id"])
        best_attempt = response.get("best_attempt")
        if best_attempt:
            trophy("Best Attempt", best_attempt["attempt_id"])
            judge = best_attempt.get("judge", {})
            metric("Judge Score", str(judge.get("score", 0.0)))
            domain_metrics = judge.get("domain_metrics", {}) or {}
            if domain_metrics.get("security_score") is not None:
                metric("Security Score", str(domain_metrics.get("security_score")))
        if response.get("approved_asset_ids"):
            success("Approved Assets", ", ".join(response["approved_asset_ids"]))
        if response.get("pilot_summary"):
            summary = response["pilot_summary"]
            section("📊", "Pilot Summary Table", color="magenta")
            table(
                ["Best", "Final", "Candidates"],
                [[
                    summary.get("best_attempt_id"),
                    summary.get("final_attempt_id"),
                    len(summary.get("candidate_ids", [])),
                ]],
                color="magenta",
            )
            if summary.get("attempt_count", 0) > 1:
                best_vs_first = summary.get("best_vs_first", {})
                best_vs_final = summary.get("best_vs_final", {})
                section("📈", "Delta Table", color="magenta")
                table(
                    ["Comparison", "Judge Δ", "Security Δ", "Attempt Total Δ", "Pipeline Exe Δ"],
                    [
                        [
                            "Best vs First",
                            _fmt_delta(best_vs_first.get("judge_score_delta")),
                            _fmt_delta(best_vs_first.get("security_score_delta")),
                            _fmt_delta(best_vs_first.get("attempt_total_latency_delta")),
                            _fmt_delta(best_vs_first.get("pipeline_execution_latency_delta")),
                        ],
                        [
                            "Best vs Final",
                            _fmt_delta(best_vs_final.get("judge_score_delta")),
                            _fmt_delta(best_vs_final.get("security_score_delta")),
                            _fmt_delta(best_vs_final.get("attempt_total_latency_delta")),
                            _fmt_delta(best_vs_final.get("pipeline_execution_latency_delta")),
                        ],
                    ],
                    color="magenta",
                )
            else:
                insight("Best vs First", "n/a (single attempt)")
                insight("Best vs Final", "n/a (single attempt)")
        for attempt in response["attempts"]:
            judge = attempt.get("judge", {})
            domain_metrics = judge.get("domain_metrics", {}) or {}
            section("👉", attempt["attempt_id"], color="cyan")
            metrics = attempt.get("attempt_metrics", {})
            table(
                [
                    "Action",
                    "Success",
                    "Judge Score",
                    "Failure Type",
                    "Next Step",
                    "Pipeline Exe (s)",
                    "Attempt Total (s)",
                    "Tools",
                    "Derived",
                    "Security Score",
                ],
                [[
                    attempt["action"].get("action_type"),
                    attempt["execution"].get("success"),
                    judge.get("score", 0.0),
                    judge.get("failure_type"),
                    judge.get("recommended_next_action"),
                    metrics.get("pipeline_execution_latency_s"),
                    metrics.get("attempt_total_latency_s"),
                    metrics.get("tool_count"),
                    metrics.get("derived_candidate_count"),
                    domain_metrics.get("security_score"),
                ]],
                color="cyan",
            )
            log_ref = attempt.get("execution_log_ref") or attempt.get("execution", {}).get("log_ref")
            if log_ref:
                inspect("Log Ref", str(log_ref))
            log_excerpt = attempt.get("execution_log_excerpt") or attempt.get("execution", {}).get("log_excerpt", [])
            important_logs = [
                entry for entry in log_excerpt
                if entry.get("level") in {"WARNING", "ERROR", "CRITICAL"}
            ]
            for entry in important_logs[:3]:
                warn(
                    "Important Log",
                    f"{entry.get('level')} · {entry.get('step')} · {entry.get('message')}",
                )
            candidates = attempt.get("candidates") or ([attempt["candidate"]] if attempt.get("candidate") else [])
            if candidates:
                table(
                    ["Candidate ID", "Type", "Status", "Validation"],
                    [[
                        candidate.get("candidate_id"),
                        candidate.get("candidate_type"),
                        candidate.get("status"),
                        candidate.get("validation_status"),
                    ] for candidate in candidates],
                    color="green",
                )
    else:
        warn("Pilot Mode", "Currently runs synchronously; rerun with --wait for full output")

    env["database"].close()


if __name__ == "__main__":
    pilot()
