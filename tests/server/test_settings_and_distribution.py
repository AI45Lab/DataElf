from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path
import pytest
from pydantic import ValidationError
from dataelf.config import DataElfConfig
from dataelf_server.settings import Settings, ServerConfig
from dataelf_server.runtime.agent import isolated_agent


def test_current_config_server_overlay_is_dormant_for_cli(tmp_path, monkeypatch):
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'runtime':{'workspace_dir':str(tmp_path/'data')},'server':{'host':'0.0.0.0','port':9000,'pi':{'transport':'inherit'}}}))
    monkeypatch.setenv('DATAELF_CONFIG_FILE',str(config))
    monkeypatch.setenv('DATAELF_SERVER_PORT','9001')
    monkeypatch.setenv('DATAELF_SERVER_SOURCE_MAX_PAGES','500')
    monkeypatch.setenv('DATAELF_SERVER_MAX_CONCURRENT_JOBS','3')
    core=DataElfConfig.from_env()
    server=Settings.from_env()
    assert server.server.port==9001 and core.server['port']==9000
    assert server.server.source.max_pages == 500
    assert server.server.max_concurrent_jobs == 3
    assert ServerConfig().max_concurrent_jobs == 5
    assert ServerConfig().source.max_pages == 50
    with pytest.raises(ValidationError):
        ServerConfig(source={'max_pages': 1001})
    assert server.state_dir==tmp_path/'data/server'
    assert server.execution_config() is not core
    core.server={'deliberately_invalid_server_option':True}
    # Core can read and save an optional section without HTTP dependencies.
    assert core.model_dump()['server']
    with pytest.raises(ValidationError):
        ServerConfig.model_validate(core.server)


def test_cli_import_does_not_require_web_dependencies():
    program='''import builtins
original=builtins.__import__
def guarded(name,*a,**kw):
    if name.split('.')[0] in {'fastapi','uvicorn','dataelf_server'}:
        raise AssertionError('CLI loaded optional HTTP runtime')
    return original(name,*a,**kw)
builtins.__import__=guarded
from dataelf.cli import app
from dataelf.config import DataElfConfig
assert DataElfConfig(server={'port':8000}).server
'''
    subprocess.run([sys.executable,'-c',program],check=True,capture_output=True,text=True)


def test_agent_snapshot_resolves_endpoint_without_mutating_registry(tmp_path):
    agent=tmp_path/'agent';agent.mkdir()
    source={'providers':{'sample':{'baseUrl':'$OPENAI_BASE_URL','apiKey':'$OPENAI_API_KEY','models':[]}}}
    path=agent/'models.json';path.write_text(json.dumps(source))
    cfg=Settings(core=DataElfConfig(),server=ServerConfig(state_dir=tmp_path/'state'))
    cfg.core.explorer.pi.cwd=tmp_path
    env={'PI_CODING_AGENT_DIR':str(agent),'OPENAI_BASE_URL':'http://127.0.0.1:9000/v1/chat/completions','OPENAI_API_KEY':'test-secret'}
    with isolated_agent(cfg,env) as child:
        temp=Path(child['PI_CODING_AGENT_DIR'])
        assert json.loads((temp/'models.json').read_text())['providers']['sample']['baseUrl']=='http://127.0.0.1:9000/v1'
        assert 'test-secret' not in (temp/'models.json').read_text()
        assert (temp/'models.json').stat().st_mode & 0o777==0o600
    assert not temp.exists()
    assert json.loads(path.read_text())==source


def test_deployment_entrypoints_share_main():
    root=Path(__file__).resolve().parents[2]
    for path in (root/'dataelf_server/deployment').glob('*.sh'):
        subprocess.run(['bash','-n',str(path)],check=True)
    assert '-m dataelf_server' in (root/'dataelf_server/deployment/start.sh').read_text()
    assert 'dataelf_server.__main__:main' in (root/'pyproject.toml').read_text()
    subprocess.run([sys.executable,'-m','dataelf_server','--help'],check=True,capture_output=True)


@pytest.mark.parametrize('limit', [0, 6, -1, 1.5])
def test_concurrency_limit_rejects_out_of_range_values(limit):
    with pytest.raises(ValidationError):
        ServerConfig(max_concurrent_jobs=limit)


def test_start_entrypoint_passes_concurrency_override(monkeypatch):
    from dataelf_server.__main__ import main
    import uvicorn
    captured = {}
    monkeypatch.setenv('DATAELF_SERVER_MAX_CONCURRENT_JOBS', '2')
    monkeypatch.setattr(sys, 'argv', ['dataelf-serve', '--max-concurrent-jobs', '5'])
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kw: captured.update(kw))
    main()
    assert Settings.from_env().server.max_concurrent_jobs == 5
    assert captured['workers'] == 1
