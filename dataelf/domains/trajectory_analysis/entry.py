"""trajectory_analysis owns selection of its stable legacy execution route."""
from dataelf.discovery.legacy.explorer_factory import create_explorer
from dataelf.discovery.legacy.workflow import run_job as run_legacy_job


def run_job(spec, config, registry=None, *, plugin=None, explorer=None, control=None):
    if (spec.domain, spec.workflow_profile, spec.workflow_policy) != ('trajectory_analysis', 'research', 'legacy'):
        raise ValueError('trajectory_analysis' + ' entry requires its legacy/research workflow')
    return run_legacy_job(spec, config, registry, plugin=plugin, explorer=explorer,
                          control=control, explorer_factory=create_explorer)
