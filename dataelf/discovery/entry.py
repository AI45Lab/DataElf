"""Resolve a saved selection, then enter its owned runtime exactly once.

This router owns no prompt, model loop, artifact completion or reviewer policy.
"""
import json
from pathlib import Path


def run_job(spec, config, registry=None, *, plugin=None, explorer=None, control=None):
    from .contracts import JobSpec
    from .flows import resolve_spec
    saved = None
    if control and control.workspace_path:
        path = Path(control.workspace_path)/'job_spec.json'
        if path.exists():
            saved = JobSpec.from_saved(json.loads(path.read_text()))
    spec = resolve_spec(spec, config, saved)
    config = config.model_copy(update={'workflow_policy': spec.workflow_policy})
    if spec.workflow_profile == 'server':
        from dataelf_server.workflows.workflow import run_job as execute
    elif spec.workflow_policy != 'legacy':
        from .workflow import run_selected_job as execute
    else:
        from .domain_registry import DomainRegistry
        from .legacy.workflow import run_job as fallback
        execute = (registry or DomainRegistry()).legacy_entry(spec.domain, plugin.manifest if plugin else None) or fallback
    return execute(spec, config, registry, plugin=plugin, explorer=explorer, control=control)
