"""Offline MOCK tests. No real WT, LLM, network or user credentials."""
import copy
import json
import inspect
import os
from pathlib import Path
import sys

import pytest
from typer.testing import CliRunner

from dataelf.cli import app
from dataelf.config import DataElfConfig, ExplorerConfig, PiConfig, RuntimeConfig
from dataelf.discovery.artifacts import ArtifactContractError, validate_outputs, validate_stage_artifacts
from dataelf.discovery.contracts import DiscoveryContext, DiscoveryJob, JobSpec
from dataelf.discovery.domain_registry import DomainRegistry
from dataelf.discovery.workspace import prepare_workspace
from dataelf.discovery.workflow import run_job
from dataelf.domains.trajectory_analysis.config import ConfigurationError, TrajectoryConfig
from dataelf.domains.trajectory_analysis.connector import RAW, METADATA, SUMMARY, record_fixture, read_json
from dataelf.domains.trajectory_analysis.review import summary_from_calls
from dataelf.domains.trajectory_analysis.analysis import ANALYSIS


def envelope(rows):
    return dict(isError=False, result=dict(records=rows, limit=1, truncated=False, omitted_fields=[], omitted_records=0))


def fixture_calls(case='success'):
    search = envelope([{'id': 'MOCK_PRIVATE_ID', 'reward': 0}])
    get = envelope([{'id': 'MOCK_PRIVATE_ID', 'reward': 0, 'chosen_trace': ['MOCK_PRIVATE_TEXT', {'original': None}]}])
    if case == 'empty': search = envelope([])
    if case == 'null': get['result']['records'][0]['chosen_trace'] = None
    if case in ('missing', 'omitted'): del get['result']['records'][0]['chosen_trace']
    if case == 'omitted': get['result'].update(truncated=True, omitted_fields=[{'record_index': 0, 'field': 'chosen_trace'}])
    if case == 'get_empty': get = envelope([])
    if case == 'records_omitted': search['result'].update(records=[], truncated=True, omitted_records=1)
    if case == 'get_error': get = {'isError': True, 'result': {'error': 'WT_READ_FAILED'}}
    if case == 'search_error': search = {'isError': True, 'result': {'error': 'WT_READ_FAILED'}}
    calls = [dict(tool='wt_search_records', arguments={'reward': 0, 'limit': 1}, envelope=search)]
    if search['result'].get('records'):
        calls.append(dict(tool='wt_get_record', arguments={'record_id': 'MOCK_PRIVATE_ID', 'fields': ['chosen_trace']}, envelope=get))
    return calls


def unlocated_report(calls, objective):
    """Fake explorer output for old query fixtures, which lack task context."""
    from dataelf.domains.trajectory_analysis.analysis import evidence_scope
    from dataelf.domains.trajectory_analysis.review import summary_from_calls
    scope = evidence_scope(calls)
    acquisition = summary_from_calls(calls)['status']
    status = {'error': 'read_failed', 'empty': 'no_records'}.get(acquisition, 'insufficient_evidence')
    limits = ['bounded_record_not_full_session', 'granularity_unverified',
              'task_goal_unknown', 'success_condition_unknown', 'outcome_unknown']
    if scope.truncated:
        limits.append('truncated_output')
    if scope.trace_state != 'present':
        limits.append('chosen_trace_' + scope.trace_state)
    return dict(schema_version='2', result_id='failure_analysis', status=status,
                objective=objective, scope=scope.model_dump(), task_goal=None,
                success_condition=None, key_failure=None, direct_cause=None, outcome=None,
                possible_root_causes=[], other_explanations=[],
                uncertainty='Synthetic query fixture lacks task context; no cause is asserted.',
                limitations=limits)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith(('WT_', 'AWS_', 'DATAELF_', 'PI_')) or any(s in key for s in ('API_KEY', 'API_BASE', 'BASE_URL')):
            monkeypatch.delenv(key)


@pytest.fixture
def setup(tmp_path):
    fixture = tmp_path / 'synthetic.json'
    fixture.write_text(json.dumps(fixture_calls()))
    fake = tmp_path / 'fake_pi'
    fake.write_text(f'''#!{sys.executable} -B
import json, os
from pathlib import Path
from dataelf.domains.trajectory_analysis.connector import record_fixture, read_json, RAW
from dataelf.domains.trajectory_analysis.analysis import ANALYSIS
{inspect.getsource(unlocated_report)}
w = Path(os.environ['DATAELF_JOB_WORKSPACE'])
assert not (w / RAW).exists()
record_fixture(w, read_json(w, 'raw/trajectory_analysis/fixture_input.json'))
report = unlocated_report(read_json(w, RAW)['calls'], read_json(w, 'job_spec.json')['objective'])
(w / ANALYSIS).write_text(json.dumps(report))
print(json.dumps({{"type":"agent_end"}}))
''')
    fake.chmod(0o755)
    cfg = DataElfConfig(runtime=RuntimeConfig(workspace_dir=tmp_path / 'state', workspaces_dir=tmp_path / 'jobs'),
        explorer=ExplorerConfig(pi=PiConfig(binary=str(fake), log_mode='quiet')),
        domains={'trajectory_analysis': {'mode': 'fixture'}})
    spec = JobSpec(domain='trajectory_analysis', objective='Original objective', inputs={'fixture_file': str(fixture)})
    return cfg, spec, fixture


@pytest.mark.parametrize('case,status', [('success','pass_with_warnings'), ('empty','pass_with_warnings'), ('null','pass_with_warnings'),
    ('missing','pass_with_warnings'), ('omitted','pass_with_warnings'), ('get_empty','pass_with_warnings'),
    ('records_omitted','pass_with_warnings'), ('get_error','failed'), ('search_error','failed')])
def test_fake_pi_run_job(setup, case, status):
    cfg, spec, fixture = setup
    fixture.write_text(json.dumps(fixture_calls(case)))
    job = run_job(spec, cfg)
    ws = Path(job.workspace_path)
    review = read_json(ws, 'reviews/quality_review.json')
    assert review['status'] == status
    assert job.status == ('failed' if status == 'failed' else 'completed')
    assert job.spec.objective == spec.objective
    analysis = read_json(ws, ANALYSIS)
    assert analysis['status'] == ('read_failed' if case in {'get_error', 'search_error'}
                                 else 'no_records' if case == 'empty' else 'insufficient_evidence')
    assert review['metrics']['localization_reported'] is False
    raw = read_json(ws, RAW)
    assert [c['envelope'] for c in raw['calls']] == [c['envelope'] for c in fixture_calls(case)]
    assert 'MOCK_PRIVATE' not in json.dumps(review)
    assert 'MOCK_PRIVATE' not in (ws / METADATA).read_text() + (ws / ANALYSIS).read_text()
    manifest = read_json(ws, 'artifact_manifest.json')
    assert {RAW, METADATA, ANALYSIS} <= {a['path'] for a in manifest['artifacts']}
    index = read_json(ws, 'workspace_index.json')
    assert index['status'] == job.status
    assert index['result_ids'] == ([] if status == 'failed' else ['failure_analysis'])
    assert not cfg.runtime.sqlite_path.exists()
    prompt = (ws / 'prompts/discovery_prompt.md').read_text()
    assert RAW in prompt and ANALYSIS in prompt and 'fixture_input.json' in prompt


def test_cli_uses_real_workflow(setup, monkeypatch):
    cfg, spec, _ = setup
    # CLI has no fixture-file flag: inject only the fixture input at the JobSpec boundary.
    original = run_job
    def run(spec, config):
        spec.inputs['fixture_file'] = setup[1].inputs['fixture_file']
        return original(spec, config)
    monkeypatch.setattr('dataelf.cli._config', lambda: cfg)
    monkeypatch.setattr('dataelf.cli.run_job', run)
    result = CliRunner().invoke(app, ['run', '--domain', 'trajectory_analysis', '--no-modeling', spec.objective])
    assert result.exit_code == 0, result.output
    assert 'completed' in result.output


def test_manifest_prepare_and_normalization(setup, tmp_path):
    cfg, spec, _ = setup
    plugin = DomainRegistry().load_plugin(spec.domain, cfg)
    assert plugin.manifest.domain == 'trajectory_analysis'
    spec = spec.model_copy(update={'modeling_strategy': 'dormant', 'constraints': {'max_runtime_minutes': 2}})
    normalized = plugin.normalize_spec(spec)
    assert normalized.inputs == spec.inputs and normalized.constraints == spec.constraints
    assert normalized.modeling_strategy == 'dormant' and plugin.normalize_spec(normalized) == normalized
    ws = prepare_workspace(tmp_path / 'prepare', spec)
    assert not (ws / 'raw/trajectory_analysis').exists()
    stage = plugin.prepare(normalized, str(ws), cfg)
    assert stage.status == 'completed'
    validate_stage_artifacts(ws, stage.artifacts)
    for path in (RAW, METADATA, ANALYSIS): assert not (ws / path).exists()
    assert plugin.create_modeler(normalized, cfg) is None
    context = DiscoveryContext(workspace_path=str(ws), spec=normalized, manifest=plugin.manifest)
    prompt = plugin.build_prompt(None, context)
    assert str(ws) not in prompt and ANALYSIS not in prompt
    assert "Failure analysis JSON schema" in prompt
    for update in ({'parameters': {'limit': 2}}, {'parameters': {'endpoint': 'fake'}}, {'inputs': {'credential': 'fake'}}):
        with pytest.raises(ValueError): plugin.normalize_spec(spec.model_copy(update=update))


def test_modeling_disabled_dormant_and_enabled_rejected(setup, monkeypatch):
    cfg, spec, _ = setup
    cfg.domains['trajectory_analysis']['modeling'] = {'enabled': False, 'strategy': 'unknown', 'ontology_template': '/missing'}
    assert run_job(spec, cfg).status == 'completed'
    cfg.domains['trajectory_analysis']['modeling']['enabled'] = True
    monkeypatch.setattr('dataelf.discovery.workflow.create_explorer', lambda _: pytest.fail('explorer must not start'))
    job = run_job(spec, cfg)
    assert job.error_code == 'TRAJECTORY_MODELING_UNSUPPORTED'


def test_fixture_connector_projection(tmp_path):
    for p in ('raw/trajectory_analysis', 'tables/trajectory_analysis'): (tmp_path / p).mkdir(parents=True)
    calls = fixture_calls('omitted')
    record_fixture(tmp_path, calls)
    assert read_json(tmp_path, RAW)['calls'][1]['envelope'] == calls[1]['envelope']
    shape = read_json(tmp_path, METADATA)['calls'][1]['chosen_trace']
    assert shape == dict(exists=False, type='missing', length=None, state='omitted')


@pytest.mark.parametrize('mutation', ['metadata', 'summary', 'id', 'no_get', 'no_calls', 'wrong_reward', 'raw_extra'])
def test_review_contradictions(setup, mutation):
    cfg, spec, _ = setup
    job = run_job(spec, cfg)
    ws = Path(job.workspace_path)
    path = METADATA if mutation == 'metadata' else ANALYSIS if mutation == 'summary' else RAW
    data = read_json(ws, path)
    if mutation == 'metadata': data['calls'][0]['count'] = 99
    if mutation == 'summary': data['status'] = 'error'
    if mutation == 'id': data['calls'][1]['arguments']['record_id'] = 'MOCK_OTHER'
    if mutation == 'no_get': data['calls'].pop()
    if mutation == 'no_calls': data['calls'] = []
    if mutation == 'wrong_reward': data['calls'][0]['arguments']['reward'] = 1
    if mutation == 'raw_extra': data['extra'] = 'MOCK_PRIVATE'
    (ws / path).write_text(json.dumps(data))
    review = DomainRegistry().load_plugin(spec.domain, cfg).review(job, str(ws))
    assert review.status == 'failed'
    assert 'MOCK_' not in review.model_dump_json()


@pytest.mark.parametrize('mutation', ['missing', 'empty', 'json', 'root', 'escape'])
def test_output_contract_failures(setup, mutation, tmp_path):
    cfg, spec, _ = setup
    job = run_job(spec, cfg)
    ws = Path(job.workspace_path)
    raw = ws / RAW
    if mutation in ('missing', 'escape'): raw.unlink()
    if mutation == 'empty': raw.write_text('')
    if mutation == 'json': raw.write_text('{')
    if mutation == 'root': raw.write_text('{"calls": {}}')
    if mutation == 'escape':
        outside = tmp_path / 'outside.json'; outside.write_text('{"calls": []}')
        raw.symlink_to(outside)
    with pytest.raises(ArtifactContractError):
        validate_outputs(ws, DomainRegistry().load_plugin(spec.domain, cfg).output_contract(spec))


def test_typed_config_and_env_override(tmp_path, monkeypatch):
    for values in ({'unknown': 1}, {'mode': 'production'}, {'tool_python': 42}, {'profile': 'production'}, {'modeling': {'enabled': 'false'}}):
        with pytest.raises(ConfigurationError) as error: TrajectoryConfig.from_mapping(values)
        assert 'production' not in str(error.value)
    monkeypatch.setenv('DATAELF_TRAJECTORY_TOOL_PYTHON', '/fake/environment-tool')
    assert TrajectoryConfig.from_mapping({'tool_python': '/fake/file-tool'}).tool_python == '/fake/environment-tool'
    monkeypatch.setenv('WT_SDK_PROFILE', 'production')
    with pytest.raises(ConfigurationError): TrajectoryConfig.from_mapping({})
    assert TrajectoryConfig.from_mapping({'mode': 'fixture'}).profile == 'test'


def test_tool_preflight_no_network(tmp_path, monkeypatch):
    skill = tmp_path / 'wt-serving-query/SKILL.md'; skill.parent.mkdir(); skill.write_text('synthetic')
    cfg = TrajectoryConfig(skill_path=str(skill), tool_python=sys.executable)
    with pytest.raises(ConfigurationError, match='WT_SDK_DB_URI: required'): cfg.tool_environment()
    for key, value in dict(WT_SDK_DB_URI='postgres://fake:FAKE_PASSWORD@invalid/db',
        WT_SDK_S3_ENDPOINT='https://invalid.example', AWS_ACCESS_KEY_ID='FAKE_ACCESS', AWS_SECRET_ACCESS_KEY='FAKE_SECRET').items():
        monkeypatch.setenv(key, value)
    env = cfg.tool_environment()
    assert env['WT_SDK_PROFILE'] == 'test'
    monkeypatch.setenv('WT_SDK_DB_URI', 'FAKE_INVALID_DSN')
    with pytest.raises(ConfigurationError) as error: cfg.tool_environment()
    assert 'FAKE_INVALID_DSN' not in str(error.value)


def test_prepare_escape_fails(setup, tmp_path):
    cfg, spec, _ = setup
    ws = prepare_workspace(tmp_path / 'unsafe', spec)
    outside = tmp_path / 'outside'; outside.mkdir()
    (ws / 'raw/trajectory_analysis').symlink_to(outside)
    stage = DomainRegistry().load_plugin(spec.domain, cfg).prepare(spec, str(ws), cfg)
    assert stage.status == 'failed'


def test_runtime_readiness_gate_preserved(setup, monkeypatch):
    cfg, spec, _ = setup
    monkeypatch.setattr('dataelf.discovery.pi_cli_explorer.runtime_ready_for_process', lambda *args: False)
    monkeypatch.setattr('dataelf.discovery.pi_cli_explorer._run_pi_process', lambda *args, **kw: pytest.fail('runtime gate bypassed'))
    job = run_job(spec, cfg)
    assert job.error_code == 'EXPLORER_RUNTIME_NOT_READY'


def test_real_preflight_missing_never_falls_back(setup, monkeypatch):
    cfg, _, _ = setup
    cfg.domains['trajectory_analysis'] = {'mode': 'tool', 'tool_python': '/missing/tool'}
    monkeypatch.setattr('dataelf.discovery.workflow.create_explorer', lambda *args: pytest.fail('preflight bypassed'))
    job = run_job(JobSpec(domain='trajectory_analysis', objective='query'), cfg)
    assert job.error_code == 'TRAJECTORY_PREFLIGHT_FAILED'
    assert not (Path(job.workspace_path) / RAW).exists()


def test_wheel_domain_resources_and_optional_dependency(tmp_path):
    import shutil
    import subprocess
    import zipfile
    root = Path(__file__).resolve().parents[1]
    build = tmp_path / 'package'; build.mkdir()
    shutil.copytree(root / 'dataelf', build / 'dataelf', ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('pyproject.toml', 'README.md'): shutil.copy2(root / name, build / name)
    completed = subprocess.run([sys.executable, '-c', 'from setuptools.build_meta import build_wheel; build_wheel("dist")'],
                               cwd=build, capture_output=True, text=True)
    assert completed.returncode == 0, 'offline wheel build failed'
    wheel = next((build / 'dist').glob('*.whl'))
    installed = tmp_path / 'installed'; installed.mkdir()
    with zipfile.ZipFile(wheel) as package:
        assert 'dataelf/domains/trajectory_analysis/domain.yaml' in package.namelist()
        for resource in (
            'dataelf/domains/trajectory_analysis/pi/skills/wt-serving-query/SKILL.md',
            'dataelf/domains/trajectory_analysis/requirements-wt.txt',
        ):
            assert package.read(resource) == (root / resource).read_bytes()
        package.extractall(installed)
    code = ('from dataelf.config import DataElfConfig; from dataelf.discovery.domain_registry import DomainRegistry; '
            'p=DomainRegistry().load_plugin("trajectory_analysis",DataElfConfig()); '
            'assert p.manifest.domain == "trajectory_analysis"; '
            'assert "installed" in str(DomainRegistry().root)')
    import textwrap
    code += "\n" + textwrap.dedent("""

        import os
        from pathlib import Path
        from importlib.metadata import distribution
        from packaging.requirements import Requirement
        from dataelf.discovery.agent_resources import resolve_domain_resources
        from dataelf.discovery.contracts import DiscoveryContext, JobSpec
        from dataelf.discovery.explorer_factory import create_explorer

        installed_root = Path(os.environ['PYTHONPATH']).resolve()
        registry = DomainRegistry()
        assert registry.root.is_relative_to(installed_root)

        domain_root = registry.domain_path('trajectory_analysis')
        skill = Path(p.config.skill_path)
        assert skill.is_relative_to(installed_root) and skill.is_file()
        assert 'name: wt-serving-query' in skill.read_text()

        dist = distribution('dataelf')
        assert Path(dist.locate_file('')).resolve() == installed_root
        sdk = [
            req for value in dist.requires or []
            if (req := Requirement(value)).name == 'wt-data-platform-sdk'
        ]
        assert len(sdk) == 1
        pin = next(
            Requirement(line).url
            for line in (domain_root / 'requirements-wt.txt').read_text().splitlines()
            if line.startswith('wt-data-platform-sdk')
        )
        assert sdk[0].url == pin
        assert sdk[0].marker is not None
        assert sdk[0].marker.evaluate({'extra': 'trajectory'})
        assert not sdk[0].marker.evaluate({'extra': ''})
        assert not sdk[0].marker.evaluate({'extra': 'dev'})

        config = DataElfConfig()
        spec = JobSpec(domain='trajectory_analysis', objective='offline wheel check')
        resources = resolve_domain_resources(domain_root, p, spec, config)
        assert resources.skills == [skill]
        assert resources.extensions == []

        context = DiscoveryContext(
            workspace_path=str(Path.cwd()), spec=spec,
            manifest=p.manifest, agent_resources=resources,
        )
        command = create_explorer(config)._build_command(
            'synthetic-pi', Path('prompt.md'), context, {},
        )
        assert '--no-skills' in command and '--no-extensions' in command
        assert [
            Path(command[i + 1])
            for i, arg in enumerate(command) if arg == '--skill'
        ] == [skill]
    """)
    checked = subprocess.run([sys.executable, '-c', code], cwd=tmp_path, env={'PYTHONPATH': str(installed)}, capture_output=True)
    assert checked.returncode == 0, 'unpacked installed wheel manifest/factory failed'


def test_extra_attempts_remain_evidence_and_fail_review(setup):
    from dataelf.domains.trajectory_analysis.connector import record_call
    cfg, spec, _ = setup
    job = run_job(spec, cfg)
    ws = Path(job.workspace_path)
    with pytest.raises(ValueError, match='QUERY_CALL_ORDER'):
        record_call(ws, 'wt_search_records', {'reward': 0, 'limit': 1}, envelope([]))
    assert len(read_json(ws, RAW)['calls']) == 2
