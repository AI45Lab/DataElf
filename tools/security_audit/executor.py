import inspect
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, List, Optional

from .checker.base import BaseChecker, HeuristicChecker, LLMJudgeChecker, ModelBasedChecker
from .checker.registry import CheckerRegistry
from .config import AuditConfig
from .schema import DataSample
from .result import CheckResult, SampleReport, TaskReport
from llm.tracing import llm_trace_context

_fallback_log = logging.getLogger(__name__)

def _bar(ratio: float, width: int = 20) -> str:
    """ASCII progress bar using block characters."""
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


def _risk_emoji(risk: str) -> str:
    _map = {
        "harmful": "☣️",
        "toxicity": "🤬",
        "bias": "⚖️",
        "pii": "🪪",
        "secret": "🔑",
        "label_flipping": "🔀",
        "factual_inconsistency": "📉",
        "self_contradiction": "🔄",
        "instruction_mismatch": "❌",
        "duplicate": "📋",
        "backdoor": "🚪",
        "prompt_injection": "💉",
        "jailbreak": "🔓",
        "sycophancy": "🎭",
    }
    return _map.get(risk, "⚠️")


def _build_report_md(report: TaskReport, sample_reports: list | None = None) -> str:
    """Render a TaskReport as a rich Markdown report."""
    lines = []

    # ── Header ──────────────────────────────────────────────────────────────
    lines += [
        "# 🔒 Security Audit Report",
        "",
        "---",
        "",
        "## 📋 Overview",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| **Task** | `{report.task_name}` |",
        f"| **Created** | {report.create_time} |",
        f"| **Finished** | {report.finish_time} |",
        f"| **Output** | `{report.output_path}` |",
        "",
    ]

    # ── Safety Summary ───────────────────────────────────────────────────────
    flagged_rate = report.flagged_rate()
    safe_rate = 1.0 - flagged_rate
    lines += [
        "## 🛡️ Safety Summary",
        "",
        "```",
        f"  Total Samples   {report.total_samples:>6}",
        f"  Safe            {report.safe_samples:>6}  ({safe_rate:.1%})   {_bar(safe_rate)}",
        f"  Flagged         {report.flagged_samples:>6}  ({flagged_rate:.1%})   {_bar(flagged_rate)}",
        "```",
        "",
    ]

    # ── Risk Distribution ────────────────────────────────────────────────────
    lines += [
        "## ⚠️ Risk Distribution",
        "",
        "| Risk Type | Flagged | Total | Rate | Distribution |",
        "|-----------|--------:|------:|-----:|:-------------|",
    ]
    for risk, stats in sorted(
        report.risk_distribution.items(),
        key=lambda x: x[1]["flagged"] / x[1]["total"] if x[1]["total"] else 0,
        reverse=True,
    ):
        total = stats["total"]
        flagged = stats["flagged"]
        pct = flagged / total if total else 0.0
        emoji = _risk_emoji(risk)
        lines.append(
            f"| {emoji} `{risk}` | **{flagged}** | {total} | {pct:.1%} | `{_bar(pct, 15)}` |"
        )
    lines.append("")

    # ── Checker Statistics ───────────────────────────────────────────────────
    lines += [
        "## 🔍 Checker Statistics",
        "",
        "| Checker | Flagged | Total | Flag Rate | Errors | Content Filter |",
        "|---------|--------:|------:|----------:|-------:|---------------:|",
    ]
    for checker, stats in sorted(
        report.checker_stats.items(),
        key=lambda x: x[1]["flagged"] / x[1]["total"] if x[1]["total"] else 0,
        reverse=True,
    ):
        total = stats.get("total", 0)
        flagged = stats.get("flagged", 0)
        error = stats.get("error", 0)
        cf = stats.get("content_filter", 0)
        pct = flagged / total if total else 0.0
        error_str = f"⚠️ {error}" if error else "—"
        cf_str = f"🚫 {cf}" if cf else "—"
        lines.append(
            f"| `{checker}` | **{flagged}** | {total} | {pct:.1%} | {error_str} | {cf_str} |"
        )
    lines.append("")

    # ── Top Flagged Samples ──────────────────────────────────────────────────
    if sample_reports:
        flagged_samples = [sr for sr in sample_reports if sr.flagged]
        if flagged_samples:
            lines += [
                "## 🚨 Top Flagged Samples",
                "",
                f"> Showing top {min(20, len(flagged_samples))} of {len(flagged_samples)} flagged samples",
                "",
                "| # | Sample ID | Risk Categories | Max Score |",
                "|---|-----------|-----------------|----------:|",
            ]
            flagged_samples.sort(
                key=lambda sr: (
                    sum(1 for v in sr.categories.values() if v),
                    max(sr.category_scores.values(), default=0.0),
                ),
                reverse=True,
            )
            for idx, sr in enumerate(flagged_samples[:20], 1):
                flagged_cats = [
                    f"{_risk_emoji(rt)} `{rt}`"
                    for rt, is_flagged in sr.categories.items()
                    if is_flagged
                ]
                max_score = max(sr.category_scores.values(), default=0.0)
                cats_str = " ".join(flagged_cats) if flagged_cats else "—"
                lines.append(
                    f"| {idx} | `{sr.sample_id}` | {cats_str} | **{max_score:.3f}** |"
                )
            lines.append("")

    # ── Footer ───────────────────────────────────────────────────────────────
    lines += [
        "---",
        "",
        f"*Generated by SecurityAuditTool · {report.finish_time}*",
    ]

    return "\n".join(lines)


class Executor:
    """schedules checkers to audit data samples."""

    def __init__(
        self,
        config: AuditConfig,
        logger: Optional[Any] = None,
        llm: Optional[Any] = None,
        job_id: str | None = None,
        mode: str | None = None,
    ):
        self.config = config
        self._log = logger or _fallback_log
        self._llm = llm
        self.job_id = job_id
        self.mode = mode or "unknown"
        self.checkers: List[BaseChecker] = []
        self.heuristic_checkers: List[HeuristicChecker] = []

    def setup(self) -> None:
        """Initialise checker instances from config."""
        checker_classes = []
        seen = set()

        for cfg in self.config.checkers:
            if not cfg.enabled:
                continue
            cls = CheckerRegistry.get(cfg.name)
            if cls.__name__ not in seen:
                checker_classes.append((cls, cfg.params))
                seen.add(cls.__name__)

        for tag in self.config.checker_tags:
            for cls in CheckerRegistry.get_by_tag(tag):
                if cls.__name__ not in seen:
                    checker_classes.append((cls, {}))
                    seen.add(cls.__name__)

        llm_model = self.config.llm.model if self.config.llm else ""
        for cls, params in checker_classes:
            if issubclass(cls, LLMJudgeChecker):
                instance = cls(llm=self._llm, **params)
                instance.llm_model = llm_model
            else:
                instance = cls(**params)
            instance._log = self._log
            if isinstance(instance, HeuristicChecker):
                self.heuristic_checkers.append(instance)
            else:
                self.checkers.append(instance)

        self._log.info(
            f"Loaded {len(self.checkers)+len(self.heuristic_checkers)} checkers."
        )

    def run(self, samples: List[DataSample]) -> TaskReport:
        """Run the full audit pipeline on the given samples."""
        cfg = self.config
        report = TaskReport(
            task_name=cfg.task_name,
            output_path=cfg.output_path,
            create_time=datetime.now().isoformat(),
            total_samples=len(samples),
        )

        start = cfg.executor.start_index
        end = cfg.executor.end_index if cfg.executor.end_index >= 0 else len(samples)
        samples = samples[start:end]

        sample_reports: List[SampleReport] = []

        # standard checkers: concurrent per-sample
        if self.checkers:
            self._log.info(f"Running {len(self.checkers)} per-sample checkers on {len(samples)} samples ...")
            with ThreadPoolExecutor(max_workers=cfg.executor.max_workers) as pool:
                futures = [pool.submit(self._check_single, s) for s in samples]
                sample_reports = [f.result() for f in futures]

        # heuristic checkers: batch mode
        if self.heuristic_checkers:
            self._log.info(f"Running {len(self.heuristic_checkers)} heuristic checkers ...")
            for hchecker in self.heuristic_checkers:
                h_results = hchecker.check_batch(samples)
                for sr, result in zip(sample_reports, h_results):
                    sr.results.append(result)

        # compute per-category scores for each sample
        for sr in sample_reports:
            sr.compute_category_scores()

        self._aggregate(report, sample_reports)
        report.finish_time = datetime.now().isoformat()

        # write artifacts to output_path (skipped when output_path is empty)
        if cfg.output_path:
            os.makedirs(cfg.output_path, exist_ok=True)

            json_path = os.path.join(cfg.output_path, "sample_results.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump([sr.model_dump() for sr in sample_reports], f, ensure_ascii=False, indent=2)
            self._log.info(f"Sample results written to {json_path}")

            md_path = os.path.join(cfg.output_path, "report.md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(_build_report_md(report, sample_reports))
            self._log.info(f"Report written to {md_path}")

        return report, sample_reports

    def _check_single(self, sample: DataSample) -> SampleReport:
        """Run all non-heuristic checkers on a single sample."""
        sr = SampleReport(sample_id=sample.id)
        for checker in self.checkers:
            if (checker.supported_formats is not None
                    and sample.dataset_type not in checker.supported_formats):
                continue
            try:
                with llm_trace_context(
                    job_id=self.job_id,
                    mode=self.mode,
                    scope="tool",
                    caller=f"security_audit.{checker.name}",
                    tool_name="security_audit",
                    tool_component=checker.name,
                    tool_call_context={
                        "risk_type": getattr(checker.risk_type, "value", str(checker.risk_type)),
                        "sample_id": sample.id,
                    },
                ):
                    result = checker.check(sample)
            except Exception as e:
                self._log.warning(f"Checker {checker.name} failed on {sample.id}: {e}")
                result = CheckResult(
                    checker_name=checker.name,
                    risk_type=checker.risk_type,
                    details={"error": str(e)},
                )
            sr.results.append(result)
        return sr

    def _aggregate(self, report: TaskReport, sample_reports: List[SampleReport]) -> None:
        for sr in sample_reports:
            if sr.flagged:
                report.flagged_samples += 1
            else:
                report.safe_samples += 1

            for rt, flagged in sr.categories.items():
                report.risk_distribution.setdefault(rt, { "total": 0,"flagged": 0})
                report.risk_distribution[rt]["total"] += 1
                if flagged:
                    report.risk_distribution[rt]["flagged"] += 1

            for result in sr.results:
                cn = result.checker_name
                report.checker_stats.setdefault(cn, {"total": 0, "flagged": 0, "error": 0, "content_filter": 0})
                if not result.success:
                    report.checker_stats[cn]["error"] += 1
                    if result.details.get("content_filter_triggered"):
                        report.checker_stats[cn]["content_filter"] += 1
                else:
                    report.checker_stats[cn]["total"] += 1
                    if result.flagged:
                        report.checker_stats[cn]["flagged"] += 1
