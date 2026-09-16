"""Offline SDK readiness and configurable test-runtime checks."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dataelf.discovery.contracts import JobSpec
from dataelf.discovery.domain_registry import DomainRegistry
from dataelf.discovery.workflow import run_job
from dataelf.domains.trajectory_analysis.config import ConfigurationError, TrajectoryConfig
from test_trajectory_failure_analysis import configuration, FIXTURES
from test_trajectory_tool_isolation import node_binary, pi_module


@pytest.fixture(autouse=True)
def synthetic_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith(('WT_', 'AWS_', 'DATAELF_', 'PI_')) or 'API_KEY' in name:
            monkeypatch.delenv(name)
    for name, value in {
        'WT_SDK_DB_URI': 'scheme://synthetic:FAKE_ONLY@invalid/db',
        'WT_SDK_S3_ENDPOINT': 'https://invalid.example',
        'AWS_ACCESS_KEY_ID': 'FAKE_ACCESS',
        'AWS_SECRET_ACCESS_KEY': 'FAKE_SECRET',
    }.items():
        monkeypatch.setenv(name, value)


def sdk_python(tmp_path, state):
    """Selected executable exposes synthetic SDK symbols; never uses installed SDK."""
    sdk = tmp_path / 'sdk'
    for package in ('wt_sdk', 'dldb'):
        (sdk / package).mkdir(parents=True)
        (sdk / package / '__init__.py').write_text('')
    sentinel = "def __init__(self, *args, **kwargs): raise AssertionError('must not construct')"
    (sdk / 'wt_sdk/client.py').write_text('class WTGatewayClient:\n    ' + sentinel + '\n')
    (sdk / 'wt_sdk/config.py').write_text('class GatewayConfig:\n    ' + sentinel + '\n')
    (sdk / 'dldb/session.py').write_text('class InformationSchemaTable:\n    ' + sentinel + '\n')
    if state == 'missing':
        (sdk / 'wt_sdk/__init__.py').write_text("raise ImportError('SYNTHETIC_PRIVATE_IMPORT_ERROR')\n")
    if state == 'broken':
        (sdk / 'dldb/session.py').write_text("raise RuntimeError('SYNTHETIC_PRIVATE_NATIVE_ERROR')\n")
    binary = tmp_path / 'selected-python'
    binary.write_text(f'''#!{sys.executable} -B
import os, sys
assert not any(k.startswith(('WT_', 'AWS_', 'DATAELF_')) for k in os.environ)
sys.path.insert(0, {str(sdk)!r})
exec(sys.argv[-1])
''')
    binary.chmod(0o755)
    return str(binary)


@pytest.mark.parametrize('state', ['ready', 'missing', 'broken'])
def test_selected_python_sdk_import_only(tmp_path, state, capsys):
    cfg = TrajectoryConfig(tool_python=sdk_python(tmp_path, state))
    if state == 'ready':
        assert cfg.tool_environment()['WT_SDK_PROFILE'] == 'test'
    else:
        with pytest.raises(ConfigurationError, match='^tool_python: wt_sdk_import_required$'):
            cfg.tool_environment()
    assert 'SYNTHETIC_PRIVATE' not in str(capsys.readouterr())


@pytest.mark.parametrize('failure', ['timeout', 'oserror', 'nonzero'])
def test_sdk_probe_failure_is_fixed_and_credential_free(monkeypatch, failure):
    def run(args, **kwargs):
        assert args[:3] == ['/synthetic/python', '-I', '-B']
        assert kwargs['env'] == {} and kwargs['timeout'] == 10
        assert kwargs['stdout'] == subprocess.DEVNULL and kwargs['stderr'] == subprocess.DEVNULL
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('SYNTHETIC_PRIVATE', 10)
        if failure == 'oserror':
            raise OSError('SYNTHETIC_PRIVATE')
        return subprocess.CompletedProcess(args, 1)
    monkeypatch.setattr('dataelf.domains.trajectory_analysis.config.subprocess.run', run)
    with pytest.raises(ConfigurationError, match='^tool_python: wt_sdk_import_required$'):
        TrajectoryConfig._validate_sdk('/synthetic/python')


def test_missing_sdk_stops_run_job_before_explorer(tmp_path, monkeypatch):
    cfg = configuration(tmp_path)
    cfg.domains['trajectory_analysis'] = {'mode': 'tool', 'tool_python': sdk_python(tmp_path, 'missing')}
    monkeypatch.setattr('dataelf.discovery.workflow.create_explorer',
                        lambda *args: pytest.fail('explorer must not start'))
    job = run_job(JobSpec(domain='trajectory_analysis', objective='synthetic readiness'), cfg)
    assert job.error_code == 'TRAJECTORY_PREFLIGHT_FAILED'
    ws = Path(job.workspace_path)
    assert not (ws / 'raw/trajectory_analysis/tool_calls.json').exists()
    assert not (ws / 'reports/failure_analysis.json').exists()
    assert (ws / 'artifact_manifest.json').is_file()
    assert 'SYNTHETIC_PRIVATE' not in json.dumps(job.model_dump(mode='json'))


def test_fixture_prepare_does_not_check_wt(tmp_path, monkeypatch):
    monkeypatch.setattr(TrajectoryConfig, 'tool_environment',
                        lambda *args: pytest.fail('fixture must not require WT'))
    cfg = configuration(tmp_path)
    plugin = DomainRegistry().load_plugin('trajectory_analysis', cfg)
    spec = JobSpec(domain='trajectory_analysis', objective='synthetic',
                   inputs={'fixture_file': str(FIXTURES / 'early_conversion/input.json')})
    assert plugin.prepare(spec, str(tmp_path / 'fixture'), cfg).status == 'completed'


def test_other_domain_does_not_import_sdk(tmp_path):
    code = '''
import importlib.abc, sys
class NoWT(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'wt_sdk', 'dldb'}:
            raise AssertionError('other domain requested WT')
sys.meta_path.insert(0, NoWT())
from dataelf.config import DataElfConfig
from dataelf.discovery.domain_registry import DomainRegistry
p = DomainRegistry().load_plugin('ai_index', DataElfConfig())
assert p.manifest.domain == 'ai_index'
'''
    result = subprocess.run([sys.executable, '-B', '-c', code],
                            env={'PYTHONPATH': str(Path(__file__).resolve().parents[1])},
                            cwd=tmp_path, capture_output=True, check=False)
    assert result.returncode == 0


def test_node_selection_uses_override_and_path(tmp_path, monkeypatch):
    binary = tmp_path / 'node'
    binary.write_text('#!/bin/sh\nprintf "v22.19.0\\n"\n')
    binary.chmod(0o755)
    monkeypatch.setenv('DATAELF_TRAJECTORY_TEST_NODE', str(binary))
    assert node_binary() == binary
    monkeypatch.delenv('DATAELF_TRAJECTORY_TEST_NODE')
    monkeypatch.setenv('PATH', str(tmp_path))
    assert node_binary() == binary
    binary.write_text('#!/bin/sh\nprintf "v16.0.0\\n"\n')
    with pytest.raises(pytest.fail.Exception, match='Node 22.19'):
        node_binary()
    monkeypatch.setenv('DATAELF_TRAJECTORY_TEST_NODE', str(tmp_path / 'missing'))
    with pytest.raises(pytest.fail.Exception, match='Node required'):
        node_binary()


def test_pi_package_selection_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv('DATAELF_TRAJECTORY_TEST_PI_ROOT', str(tmp_path))
    with pytest.raises(pytest.fail.Exception, match='dataelf setup'):
        pi_module('tools/bash.js')
    module = tmp_path / 'dist/core/tools/bash.js'
    module.parent.mkdir(parents=True)
    module.write_text('// synthetic module path')
    assert pi_module('tools/bash.js') == module
