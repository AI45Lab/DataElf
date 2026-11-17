"""Explicit Skill workflow; never called as a fallback for a legacy job."""

import json


def execute(job, config, store, workspace, plugin, context, explorer, contract):
    from dataelf.discovery.analysis_review.coordinator import run_analysis
    from dataelf.discovery.workflow import fail_job, write_review, finalize_job

    if context.domain_context.get("graph_ref"):
        job.context.setdefault("graph_refs", []).append(
            dict(context.domain_context["graph_ref"])
        )
    result, review = run_analysis(job, context, contract, config, analyst=explorer)
    job.artifacts.extend(result.artifacts)
    if result.status != "completed":
        return fail_job(
            job, store, workspace, "explorer", result.error_code, result.error_message
        )
    approved = json.loads((workspace / "reviews/analysis/approved.json").read_text())
    for ref in approved.get("graph_refs", []):
        if ref not in job.context.setdefault("graph_refs", []):
            job.context.setdefault("graph_refs", []).append(ref)
    write_review(workspace, review)
    store.save_quality_review(review)
    return finalize_job(job, store, workspace, plugin, review)
