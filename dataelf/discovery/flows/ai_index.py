"""Explicit Skill workflow; never called as a fallback for a legacy job."""
import json


def execute(job, config, store, workspace, plugin, context, explorer, contract):
    from dataelf.discovery.analysis_review.coordinator import run_analysis
    from dataelf.discovery.workflow import _fail, _write_review, _finalize
    result, review = run_analysis(job, context, contract, config, analyst=explorer)
    job.artifacts.extend(result.artifacts)
    if result.status != 'completed':
        return _fail(job, store, workspace, 'explorer', result.error_code, result.error_message)
    approved = json.loads((workspace/'reviews/analysis/approved.json').read_text())
    for ref in approved.get('graph_refs', []):
        if ref not in job.graph_refs:
            job.graph_refs.append(ref)
    _write_review(workspace, review)
    store.save_quality_review(review)
    return _finalize(job, store, workspace, plugin, review)
