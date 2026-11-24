"""Host dispatch must not require ordinary domains to select a core runtime."""

from pathlib import Path

import pytest

from dataelf.config import DataElfConfig
from dataelf.discovery.contracts import DomainWorkflow, JobSpec
from dataelf.discovery.entry import run_job
from dataelf.discovery.flows import resolve_spec
from dataelf.discovery.policies import capabilities, workflow_executor


@pytest.mark.parametrize('domain', ['finance', 'trajectory_analysis'])
def test_plain_domain_dispatch_preserves_legacy_components(domain, monkeypatch):
    from dataelf.discovery.legacy import workflow
    from dataelf.discovery.legacy.pi_explorer import LegacyPiExplorer

    config = DataElfConfig()
    spec = JobSpec(domain=domain, objective='original')
    plugin, explorer, control, registry = (object() for _ in range(4))
    # No workspace means no saved selection; this test checks forwarding only.
    from types import SimpleNamespace
    control = SimpleNamespace(workspace_path=None)
    seen = {}

    def execute(actual, cfg, actual_registry, **kwargs):
        seen.update(spec=actual, registry=actual_registry, kwargs=kwargs)
        assert isinstance(workflow.create_explorer(cfg), LegacyPiExplorer)
        return 'legacy-result'

    monkeypatch.setattr(workflow, 'run_job', execute)
    assert run_job(spec, config, registry, plugin=plugin, explorer=explorer,
                   control=control) == 'legacy-result'
    assert seen['spec'].workflow_policy == 'legacy'
    assert seen['registry'] is registry
    assert seen['kwargs'] == dict(plugin=plugin, explorer=explorer, control=control)
    assert not capabilities(seen['spec']).analysis_review


@pytest.mark.parametrize('field', ['profiles', 'aliases', 'capabilities', 'recovery', 'unbounded'])
def test_business_hook_rejects_host_runtime_fields(field):
    with pytest.raises(ValueError, match='Extra inputs'):
        DomainWorkflow.model_validate({'entry': 'example.workflow:execute', field: True})


@pytest.mark.parametrize('field', ['domain', 'workflow_profile', 'workflow_policy'])
def test_saved_task_cannot_switch_runtime(field):
    saved = JobSpec.from_saved({'domain': 'ai_index', 'objective': 'original'})
    update = {'domain': 'finance', 'workflow_profile': 'server',
              'workflow_policy': 'cli_skill_review_v1'}
    spec = JobSpec.model_validate({**saved.model_dump(), field: update[field]})
    with pytest.raises(ValueError, match='Cannot'):
        resolve_spec(spec, DataElfConfig(), saved)


def test_canonical_selection_resumes_saved_alias():
    saved = JobSpec.from_saved({
        'domain': 'ai_index', 'objective': 'original', 'workflow_policy': 'ai_index_cli_skill_review_v1',
    })
    current = JobSpec.model_validate({**saved.model_dump(), 'workflow_policy': 'cli_skill_review_v1'})
    assert resolve_spec(current, DataElfConfig(), saved).workflow_policy == saved.workflow_policy


def test_config_default_does_not_switch_server_or_saved_job():
    cfg = DataElfConfig(workflow_policy='cli_skill_review_v1')
    server = JobSpec(domain='ai_index', objective='serve', workflow_profile='server')
    assert resolve_spec(server, cfg).workflow_policy == 'legacy'
    saved = JobSpec.from_saved({'domain': 'ai_index', 'objective': 'old'})
    assert resolve_spec(saved, cfg, saved).workflow_policy == 'legacy'
    fresh = JobSpec(domain='ai_index', objective='new')
    selected = resolve_spec(fresh, cfg)
    assert selected.workflow_policy == 'cli_skill_review_v1'
    assert workflow_executor(selected).__module__ == 'dataelf.domains.ai_index.workflow'


def test_server_dispatch_does_not_enter_cli(monkeypatch):
    import dataelf_server.workflows.workflow as server
    import dataelf.discovery.legacy.workflow as legacy

    def forbidden(*args, **kwargs):
        raise AssertionError('CLI selected for server')

    monkeypatch.setattr(legacy, 'run_job', forbidden)
    marker = object()
    monkeypatch.setattr(server, 'run_job', lambda *args, **kwargs: marker)
    spec = JobSpec(domain='ai_index', objective='serve', workflow_profile='server')
    assert run_job(spec, DataElfConfig(workflow_policy='cli_skill_review_v1')) is marker


def test_removed_domain_wrappers_are_not_required():
    root = Path(__file__).resolve().parents[1]
    for domain in ('finance', 'trajectory_analysis'):
        folder = root / 'dataelf/domains' / domain
        assert not (folder / 'entry.py').exists()
        assert 'legacy_entry:' not in (folder / 'domain.yaml').read_text()
