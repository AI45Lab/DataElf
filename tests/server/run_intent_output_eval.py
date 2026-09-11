"""Opt-in real-model extraction checks; no search, modeling or Insight generation.

Run from repository root with the swproxy environment already enabled:
  python -m tests.server.run_intent_output_eval --repeat 2 --report /tmp/intent-v2.json
Normal run_intent.py calls still only print JSON and never record files.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
from pathlib import Path
import time

from dataelf_server.intent import Domain, IntentModelConfig, IntentRecognizer, Profile, SERVE_PROFILE, Source
from dataelf_server.intent.prompts import build_prompt_components, render_system_prompt


REFERENCE = datetime.fromisoformat("2026-09-08T12:00:00+08:00")


def lookup(value, path):
    for key in path.split("."):
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def check_result(actual, case):
    errors = []
    for path, expected in case["expected"].items():
        found = lookup(actual, path)
        equal = (sorted(found) == sorted(expected)) if isinstance(found, list) and isinstance(expected, list) and all(isinstance(v, str) for v in found + expected) else found == expected
        if not equal:
            errors.append({"path": path, "expected": expected, "actual": found})
    for path, fragments in case.get("contains", {}).items():
        found = json.dumps(lookup(actual, path), ensure_ascii=False).casefold()
        for fragment in fragments:
            if fragment.casefold() not in found:
                errors.append({"path": path, "missing_fragment": fragment, "actual": lookup(actual, path)})
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--case", action="append", help="Run only selected case IDs")
    parser.add_argument("--cases-file", type=Path, default=Path(__file__).with_name("intent_output_cases.json"), help="Alternative extraction regression cases")
    parser.add_argument("--report", type=Path, help="Optional explicit evaluation report; never contains credentials")
    args = parser.parse_args()
    if args.repeat < 1 or not 1 <= args.workers <= 4:
        parser.error("repeat must be positive and workers must be between 1 and 4")
    cases = json.loads(args.cases_file.read_text())
    if args.case:
        available = {case["id"] for case in cases}
        if set(args.case) - available:
            parser.error("Unknown case ID")
        cases = [case for case in cases if case["id"] in args.case]
    config = IntentModelConfig.from_env()
    config.validate_for_run()
    profiles = {
        "serve": SERVE_PROFILE,
        "papers": Profile(domains=(Domain("papers", "论文检索", (Source("arxiv", "预印本", ("arXiv",)),)),), default_domains=("papers",)),
    }
    report = {
        "model": config.model_name, "endpoint": config.endpoint,
        "reference_time": REFERENCE.isoformat(), "repeat": args.repeat,
        "scope": "extraction only; semantic assertions are not a general accuracy estimate",
        "schema": SERVE_PROFILE.json_schema(),
        "system_prompt": render_system_prompt(build_prompt_components(SERVE_PROFILE, REFERENCE, "Asia/Shanghai")),
        "results": [],
    }
    def run(case, iteration):
        start = time.monotonic()
        row = {"case": case["id"], "iteration": iteration, "query": case["query"], "expected": case["expected"], "contains": case.get("contains", {})}
        class RecordedRecognizer(IntentRecognizer):
            def _request(self, payload):
                row["model_request"] = payload  # Body only; authorization headers are never captured.
                envelope = super()._request(payload)
                row["model_response"] = envelope
                return envelope
        try:
            result = RecordedRecognizer(profiles[case.get("profile", "serve")], config=config).extract(case["query"], reference_time=REFERENCE).model_dump()
            row.update(intent=result, errors=check_result(result, case))
        except Exception as exc:
            # Production IntentError is already redacted; never serialize request headers/config.
            row.update(errors=[{"type": type(exc).__name__, "error": str(exc)}])
        row.update(passed=not row["errors"], elapsed_seconds=round(time.monotonic() - start, 3))
        return row
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run, case, iteration) for iteration in range(1, args.repeat + 1) for case in cases]
        for future in as_completed(futures):
            row = future.result()
            report["results"].append(row)
            print(json.dumps({k: row[k] for k in ("case", "iteration", "passed", "elapsed_seconds", "errors")}, ensure_ascii=False), flush=True)
            report["passed"] = sum(r["passed"] for r in report["results"])
            report["total"] = len(report["results"])
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Passed {report['passed']}/{report['total']} real model calls", flush=True)
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
