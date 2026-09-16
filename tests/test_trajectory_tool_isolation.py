"""Offline tests for domain-scoped Pi resources and Python acquisition.

Default discovery must not expose WT. Positive acquisition uses existing Pi code execution.
"""
import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dataelf.config import DataElfConfig
from dataelf.discovery.contracts import DiscoveryContext, DiscoveryJob, JobSpec
from dataelf.discovery.domain_registry import DomainRegistry
from dataelf.discovery.explorer_factory import create_explorer
from dataelf.domains.trajectory_analysis.config import ConfigurationError, TrajectoryConfig

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = ROOT / "dataelf/domains/trajectory_analysis"
TOOL = DOMAIN / "tools/wt_serving"
def node_binary():
    """Only Pi integration tests need Node; collection/SDK-only tests do not."""
    requested = os.environ.get("DATAELF_TRAJECTORY_TEST_NODE", "node")
    binary = shutil.which(requested)
    if not binary:
        pytest.fail("Node required: set DATAELF_TRAJECTORY_TEST_NODE or add Node to PATH")
    result = subprocess.run([binary, "--version"], env={}, capture_output=True,
                            text=True, timeout=5, check=False)
    try:
        version = tuple(int(part) for part in result.stdout.strip().removeprefix("v").split("."))
    except ValueError:
        version = ()
    if result.returncode or version < (22, 19, 0):
        pytest.fail("Node 22.19+ required for trajectory Pi integration tests")
    return Path(binary).absolute()


def pi_module(relative):
    root = Path(os.environ.get("DATAELF_TRAJECTORY_TEST_PI_ROOT",
                str(ROOT / "node_modules/@earendil-works/pi-coding-agent")))
    path = root / "dist/core" / relative
    if not path.is_file():
        pytest.fail("Prepared Pi package required: run dataelf setup or set DATAELF_TRAJECTORY_TEST_PI_ROOT")
    return path.resolve()


def test_tool_is_an_internal_package_without_other_domain_imports():
    for path in TOOL.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = ([n.name for n in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            assert all(not n.startswith(("wt_serving_tool", "pi.")) for n in names)
            assert all(not n.startswith("dataelf.domains.") or n.startswith(
                "dataelf.domains.trajectory_analysis") for n in names)
    assert (TOOL / "__init__.py").is_file()
    assert not (DOMAIN / "tools/wt-serving-tool").exists()
    assert not list(DOMAIN.rglob("pyproject.toml"))


def test_skill_default_is_domain_owned_and_cwd_independent():
    skill = Path(TrajectoryConfig().skill_path)
    assert skill == DOMAIN / "pi/skills/wt-serving-query/SKILL.md"
    assert skill.is_file() and skill.is_absolute()
    assert not skill.is_relative_to(ROOT / ".pi")


def test_tool_python_rejects_an_invalid_interpreter(tmp_path):
    fake = tmp_path / "not-compatible-python"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    with pytest.raises(ConfigurationError, match="python_3_11_required"):
        TrajectoryConfig(tool_python=str(fake)).tool_environment()


def test_stage_env_extra_args_is_not_a_resource_loading_interface(tmp_path):
    cfg = DataElfConfig(domains={"trajectory_analysis": {"mode": "fixture"}})
    manifest = DomainRegistry().load_manifest("trajectory_analysis")
    spec = JobSpec(domain="trajectory_analysis", objective="synthetic")
    job = DiscoveryJob(job_id="synthetic", spec=spec, workspace_path=str(tmp_path))
    ctx = DiscoveryContext(workspace_path=str(tmp_path), spec=spec, manifest=manifest,
                           env={"DATAELF_PI_EXTRA_ARGS": "-e SYNTHETIC_EXTENSION"})
    explorer = create_explorer(cfg)
    command = explorer._build_command("synthetic-pi", tmp_path / "prompt.md")
    env = explorer._build_env(tmp_path, job, ctx)
    assert env["DATAELF_PI_EXTRA_ARGS"] == "-e SYNTHETIC_EXTENSION"
    assert "SYNTHETIC_EXTENSION" not in " ".join(command)


def pi_probe(tmp_path, mode):
    node = node_binary()
    loader = pi_module("extensions/loader.js")
    skills = pi_module("skills.js")
    script = tmp_path / "discovery.mjs"
    script.write_text(f"""
import {{mkdir, writeFile}} from 'node:fs/promises';
import {{join}} from 'node:path';
import {{discoverAndLoadExtensions}} from {json.dumps(loader.as_uri())};
import {{loadSkills, formatSkillsForPrompt}} from {json.dumps(skills.as_uri())};
const root = {json.dumps(str(ROOT))}, scratch = {json.dumps(str(tmp_path))};
const agent = join(scratch, 'agent');
await mkdir(join(agent, 'extensions'), {{recursive:true}});
await writeFile(join(agent, 'extensions/other.ts'),
  'import {{Type}} from "typebox"; export default pi => pi.registerTool({{name:"other_tool",label:"Other",description:"Synthetic",parameters:Type.Object({{}}),execute:async()=>({{content:[]}})}})');
await mkdir(join(agent, 'skills/other'), {{recursive:true}});
await writeFile(join(agent, 'skills/other/SKILL.md'),
  '---\\nname: other\\ndescription: Synthetic other skill\\n---\\nOther instructions.\\n');
process.env.DATAELF_DOMAIN = {json.dumps(mode)};
const loaded = await discoverAndLoadExtensions([], root, agent);
const discovered = loadSkills({{cwd:root, agentDir:agent, skillPaths:[], includeDefaults:true}});
console.log(JSON.stringify({{
  errors:loaded.errors.length,
  other_tool_preserved:loaded.extensions.some(e=>e.tools.has('other_tool')),
  wt:loaded.extensions.flatMap(e => [...e.tools.keys()]).filter(n=>n.startsWith('wt_')),
  wt_skill_prompt:formatSkillsForPrompt(discovered.skills).includes('wt-serving-query'),
  other_skill_preserved:discovered.skills.some(s=>s.name==='other')
}}));
""")
    env = {"PATH": str(node.parent) + ":/usr/bin:/bin", "HOME": str(tmp_path),
           "JITI_CACHE_DIR": str(tmp_path / "jiti"), "TMPDIR": str(tmp_path),
           "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0"}
    result = subprocess.run([str(node), str(script)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("domain", ["synthetic_other", "trajectory_analysis"])
def test_project_discovery_must_not_register_wt(tmp_path, domain):
    result = pi_probe(tmp_path, domain)
    assert result["errors"] == 0
    assert result["wt"] == [], "project entry must not expose WT across domains"
    assert result["wt_skill_prompt"] is False
    assert result["other_skill_preserved"] is True
    assert result["other_tool_preserved"] is True


@pytest.mark.parametrize("capture_failure", [False, True])
def test_run_job_sequence_does_not_mutate_parent_config_or_forward_wt(tmp_path, capture_failure):
    """Fake Pi checks env plumbing only, not actual Pi discovery or LLM accuracy."""
    fixture = ROOT / "tests/fixtures/trajectory_analysis/early_conversion"
    calls = json.loads((fixture / 'input.json').read_text())
    sdk = synthetic_sdk(tmp_path, calls[1]['envelope']['result']['records'])
    fake = tmp_path / "fake-pi"
    fake.write_text(f'''#!{sys.executable} -B
import json, os
from pathlib import Path
from dataelf.domains.trajectory_analysis.connector import record_fixture
w=Path(os.environ['DATAELF_JOB_WORKSPACE']); domain=os.environ['DATAELF_DOMAIN']
keys=[k for k in os.environ if k.startswith(('WT_', 'AWS_', 'DATAELF_TRAJECTORY_'))]
Path({str(tmp_path)!r}, domain+'.env-keys.json').write_text(json.dumps(keys))
if domain == 'trajectory_analysis':
    from dataelf.domains.trajectory_analysis.client import TrajectoryClient
    assert not (w/'raw/trajectory_analysis/tool_calls.json').exists()
    assert not (w/'reports/failure_analysis.json').exists()
    assert 'search' in Path(os.environ['DATAELF_TRAJECTORY_SKILL']).read_text()
    if {capture_failure!r}:
        (w/'tables/trajectory_analysis/query_metadata.json').symlink_to(Path({str(tmp_path)!r})/'outside.json')
    client=TrajectoryClient.from_env()
    search=client.search_records()
    assert not search['isError']
    assert not client.get_record(search['result']['records'][0]['id'])['isError']
    report=json.loads(Path({str(fixture / 'expected.json')!r}).read_text())['report']
    (w/'reports/failure_analysis.json').write_text(json.dumps(report))
else:
    (w/'fake/result.json').write_text('{{"items":[]}}')
print('{{"type":"agent_end"}}')
''')
    fake.chmod(0o755)
    driver = tmp_path / "sequence.py"
    driver.write_text(f'''
import json, os
from pathlib import Path
from dataelf.config import DataElfConfig, ExplorerConfig, PiConfig, RuntimeConfig
from dataelf.discovery.contracts import JobSpec
from dataelf.discovery.workflow import run_job
from test_discovery_mvp import _fake_registry
scratch=Path({str(tmp_path)!r})
report=json.loads(Path({str(fixture / 'expected.json')!r}).read_text())['report']
cfg=DataElfConfig(runtime=RuntimeConfig(workspace_dir=scratch/'state',workspaces_dir=scratch/'jobs'),
    explorer=ExplorerConfig(pi=PiConfig(binary={str(fake)!r},cwd=scratch,log_mode='quiet')),
    domains={{'trajectory_analysis':{{'tool_python':{sys.executable!r}}}}})
before=cfg.model_dump(mode='json'); parent=dict(os.environ)
first=run_job(JobSpec(domain='trajectory_analysis',objective=report['objective']),cfg)
second=run_job(JobSpec(domain='fake',objective='synthetic other'),cfg,registry=_fake_registry(scratch))
assert first.status == {'failed' if capture_failure else 'completed'!r}
assert second.status == 'completed'
if {capture_failure!r}:
    assert json.loads(Path(first.workspace_path,'raw/trajectory_analysis/tool_calls.json').read_text())['capture_failed']
    assert not (scratch/'outside.json').exists()
assert cfg.model_dump(mode='json') == before and dict(os.environ) == parent
first_keys=json.loads((scratch/'trajectory_analysis.env-keys.json').read_text())
second_keys=json.loads((scratch/'fake.env-keys.json').read_text())
assert 'WT_SDK_DB_URI' in first_keys and 'DATAELF_TRAJECTORY_CAPTURE' in first_keys
assert second_keys == []
prompt=Path(second.workspace_path,'prompts/discovery_prompt.md').read_text()
assert 'wt-serving-query' not in prompt and 'wt_search_records' not in prompt and 'TrajectoryClient' not in prompt
for job in [first,second]:
    for name in ['artifact_manifest.json','workspace_index.json','reviews/quality_review.json']:
        assert Path(job.workspace_path,name).is_file()
print('synthetic sequence passed; actual Pi resource selection is not exercised')
''')
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "PYTHONPATH": str(ROOT) + os.pathsep + str(ROOT / "tests") + os.pathsep + str(sdk),
           "PYTHONDONTWRITEBYTECODE": "1", "WT_SDK_PROFILE": "test",
           "WT_SDK_DB_URI": "scheme://synthetic:FAKE_ONLY@invalid/db",
           "WT_SDK_S3_ENDPOINT": "https://invalid.example", "AWS_ACCESS_KEY_ID": "FAKE_ONLY",
           "AWS_SECRET_ACCESS_KEY": "FAKE_ONLY"}
    result = subprocess.run([sys.executable, "-B", str(driver)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def synthetic_sdk(tmp_path, rows=None, fail=False):
    """Only SDK I/O is synthetic; Client, bridge, adapter and recorder run unchanged."""
    sdk = tmp_path / "sdk"
    for package in ["wt_sdk", "dldb"]:
        (sdk / package).mkdir(parents=True)
        (sdk / package / "__init__.py").write_text("")
    (sdk / "dldb/session.py").write_text("InformationSchemaTable = object\n")
    (sdk / "wt_sdk/config.py").write_text(
        "from types import SimpleNamespace\nclass GatewayConfig:\n"
        "    tables = SimpleNamespace(profile='test', serving_table='serving_test')\n")
    (sdk / "wt_sdk/client.py").write_text(f"""
import json
from pathlib import Path
class WTGatewayClient:
    def __init__(self, config): self.config = config
    def close(self): pass
    def query_data(self, **kwargs):
        with Path({str(tmp_path / 'sdk-calls.jsonl')!r}).open('a') as out:
            out.write(json.dumps(kwargs)+'\\n')
        if {fail!r}: raise RuntimeError('SYNTHETIC_PRIVATE_ERROR')
        return {rows!r}
""")
    return sdk


def client_env(tmp_path, sdk):
    return {"PATH": os.defpath, "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(ROOT) + os.pathsep + str(sdk),
            "DATAELF_JOB_WORKSPACE": str(tmp_path), "DATAELF_DOMAIN": "trajectory_analysis",
            "DATAELF_TRAJECTORY_CAPTURE": "1", "DATAELF_TRAJECTORY_TOOL_PYTHON": sys.executable,
            "DATAELF_TRAJECTORY_SKILL": TrajectoryConfig().skill_path,
            "WT_SDK_PROFILE": "test", "WT_SDK_DB_URI": "scheme://synthetic:FAKE@invalid/db",
            "WT_SDK_S3_ENDPOINT": "https://invalid.example", "AWS_ACCESS_KEY_ID": "FAKE_ONLY",
            "AWS_SECRET_ACCESS_KEY": "FAKE_ONLY"}


def init_client_workspace(tmp_path):
    (tmp_path / "job_spec.json").write_text(JobSpec(
        domain="trajectory_analysis", objective="synthetic").model_dump_json())
    for relative in ["raw/trajectory_analysis", "tables/trajectory_analysis", "scripts"]:
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)


@pytest.mark.parametrize("trace", [[None, {"synthetic": True}], None, {"synthetic": []}, "x" * 70000], ids=["list", "null", "dict", "omitted"])
def test_agent_python_client_executes_and_records_via_existing_pi_bash(tmp_path, trace):
    import shlex
    node = node_binary()
    bash_module = pi_module("tools/bash.js")
    init_client_workspace(tmp_path)
    sdk = synthetic_sdk(tmp_path, [{"id": "SYNTHETIC_ID", "reward": 0, "chosen_trace": trace}])
    script = tmp_path / "scripts/query.py"
    script.write_text("""
import os
from pathlib import Path
from dataelf.domains.trajectory_analysis.client import TrajectoryClient
assert 'search' in Path(os.environ['DATAELF_TRAJECTORY_SKILL']).read_text()
client=TrajectoryClient.from_env()
s=client.search_records(reward=0, limit=1)
assert not s['isError']
g=client.get_record(s['result']['records'][0]['id'], fields=['chosen_trace'])
assert not g['isError']
print('synthetic client completed')
""")
    js = tmp_path / "execute.mjs"
    js.write_text(f"""
import {{createBashTool}} from {json.dumps(bash_module.as_uri())};
const result = await createBashTool({json.dumps(str(tmp_path))}).execute('synthetic', {{
    command: {json.dumps(shlex.join([sys.executable, '-B', str(script)]))}, timeout: 30}});
if (!JSON.stringify(result).includes('synthetic client completed')) throw new Error('CLIENT_EXECUTION_FAILED');
console.log('synthetic Pi code execution passed');
""")
    env = client_env(tmp_path, sdk)
    env["PATH"] = str(node.parent) + os.pathsep + os.defpath
    result = subprocess.run([str(node), str(js)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    raw = json.loads((tmp_path / 'raw/trajectory_analysis/tool_calls.json').read_text())['calls']
    metadata = json.loads((tmp_path / 'tables/trajectory_analysis/query_metadata.json').read_text())['calls']
    assert [c['tool'] for c in raw] == ['wt_search_records', 'wt_get_record']
    assert raw[0]['arguments'] == {'reward': 0, 'limit': 1}
    assert raw[1]['arguments'] == {'record_id': 'SYNTHETIC_ID', 'fields': ['chosen_trace']}
    assert all(c['transport'] == 'ok' and not c['envelope']['isError'] for c in raw)
    assert metadata[1]['chosen_trace']['state'] == ('omitted' if isinstance(trace, str) else 'null' if trace is None else 'present')
    if not isinstance(trace, str):
        assert raw[1]['envelope']['result']['records'][0]['chosen_trace'] == trace
    sdk_calls = [json.loads(line) for line in (tmp_path / 'sdk-calls.jsonl').read_text().splitlines()]
    assert len(sdk_calls) == 2 and sdk_calls[0]['filter_query'] == 'reward = 0'
    assert 'is_session_completed' not in sdk_calls[0]['filter_query']
    assert sdk_calls[1]['columns'][-1] == 'chosen_trace'
    assert not (ROOT / '.pi/extensions/wt-serving.ts').exists()


@pytest.mark.parametrize('failure', ['sdk', 'transport', 'capture', 'other_domain'])
def test_client_failure_is_visible_and_never_unrecorded_success(tmp_path, failure):
    init_client_workspace(tmp_path)
    sdk = synthetic_sdk(tmp_path, [], fail=True)
    env = client_env(tmp_path, sdk)
    if failure == 'transport':
        env['DATAELF_TRAJECTORY_TOOL_PYTHON'] = '/missing/python'
    if failure == 'other_domain':
        env['DATAELF_DOMAIN'] = 'fake'
    if failure == 'capture':
        (tmp_path / 'tables/trajectory_analysis/query_metadata.json').symlink_to(tmp_path.parent / 'outside.json')
    result = subprocess.run([sys.executable, '-B', '-c',
        "from dataelf.domains.trajectory_analysis.client import TrajectoryClient; "
        "assert TrajectoryClient.from_env().search_records()['isError']"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    raw_path = tmp_path / 'raw/trajectory_analysis/tool_calls.json'
    if failure in ['sdk', 'transport']:
        assert result.returncode == 0
        call = json.loads(raw_path.read_text())['calls'][0]
        assert call['transport'] == ('error' if failure == 'transport' else 'ok')
        assert call['envelope'] is None if failure == 'transport' else call['envelope']['isError']
    elif failure == 'capture':
        assert result.returncode != 0 and 'QUERY_CAPTURE_FAILED' in result.stderr
        assert json.loads(raw_path.read_text())['capture_failed'] is True
        assert not (tmp_path.parent / 'outside.json').exists()
    else:
        assert result.returncode != 0 and not raw_path.exists()
        assert not (tmp_path / 'sdk-calls.jsonl').exists()
    assert 'SYNTHETIC_PRIVATE_ERROR' not in result.stdout + result.stderr


def test_domain_resource_commands_are_scoped(tmp_path):
    """Build commands for two domains without leaking the WT Skill."""
    from dataelf.discovery.agent_resources import resolve_domain_resources
    from test_discovery_mvp import _fake_registry

    cfg = DataElfConfig(domains={"trajectory_analysis": {"mode": "fixture"}})
    skill = (DOMAIN / "pi/skills/wt-serving-query/SKILL.md").resolve()
    explorer = create_explorer(cfg)

    for registry, domain, expected in [
        (DomainRegistry(), "trajectory_analysis", [skill]),
        (_fake_registry(tmp_path), "fake", []),
    ]:
        spec = JobSpec(domain=domain, objective="synthetic resource selection")
        plugin = registry.load_plugin(domain, cfg)
        resources = resolve_domain_resources(
            registry.domain_path(domain), plugin, spec, cfg,
        )
        assert resources.skills == expected
        assert resources.extensions == []

        ctx = DiscoveryContext(
            workspace_path=str(tmp_path),
            spec=spec,
            manifest=plugin.manifest,
            agent_resources=resources,
        )
        command = explorer._build_command(
            "synthetic-pi", tmp_path / "prompt.md", ctx, {},
        )
        assert "--no-skills" in command
        assert "--no-extensions" in command
        loaded = [
            Path(command[i + 1])
            for i, arg in enumerate(command) if arg == "--skill"
        ]
        assert loaded == expected
        assert "--extension" not in command
