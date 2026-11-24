import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from dataelf.domains.ai_index.evidence import COMMANDS, EvidenceCatalog, SKILL_ROOT
from dataelf.domains.ai_index.skill_tools import dispatch
from dataelf.ontology.capabilities import CLI_SKILL_REVIEW


@pytest.fixture
def fake_curl(tmp_path, monkeypatch):
    bindir = tmp_path / 'bin'; bindir.mkdir()
    script = bindir / 'curl'
    script.write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, sys
config=sys.stdin.read()
assert 'X-AI-Index-Key: ak_1234567890AbCdEf' in config
pathlib.Path(os.environ['CURL_ARGS']).write_text(json.dumps(sys.argv[1:],ensure_ascii=False))
print(os.environ.get('FAKE_RESPONSE','{"code":0,"data":{"total":0,"list":[]}}'))
print('DATAELF_HTTP_STATUS='+os.environ.get('FAKE_STATUS','200'),file=sys.stderr)
sys.exit(int(os.environ.get('FAKE_EXIT','0')))
''')
    script.chmod(0o755)
    monkeypatch.setenv('PATH', str(bindir) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('AI_INDEX_API_KEY', 'ak_1234567890AbCdEf')
    monkeypatch.setenv('CURL_ARGS', str(tmp_path / 'curl_args.json'))
    return tmp_path / 'curl_args.json'


@pytest.mark.parametrize('command', COMMANDS)
def test_client_sends_exact_json_and_funding_path(tmp_path, fake_curl, command):
    params = {'institution_id': 'id-safe_1'} if command == 'institution-funding' else {'title': '带引号 "x" 和 } 字符', 'page': 2, 'size': 3}
    catalog = EvidenceCatalog(tmp_path)
    row = catalog.query(command, params, role='ontology_modeler')
    args = json.loads(fake_curl.read_text())
    assert not any('ak_' in a for a in args)
    if command == 'institution-funding':
        assert args[-1].endswith('/openapi/institutions/id-safe_1/funding-profile')
        assert '--data-binary' not in args
    else:
        assert json.loads(args[args.index('--data-binary') + 1]) == params
    assert row['outcome'] == 'success_empty'
    assert json.loads((tmp_path / row['response_file']).read_text()) == {'code': 0, 'data': {'total': 0, 'list': []}}


def test_client_default_body_is_an_object(fake_curl):
    subprocess.run(['/bin/sh', str(SKILL_ROOT / 'scripts/ai_index_openapi.sh'), 'paper-search'], check=True, capture_output=True)
    args = json.loads(fake_curl.read_text())
    assert json.loads(args[args.index('--data-binary') + 1]) == {}


@pytest.mark.parametrize('response,status,exit_code,outcome', [
    ('{"code":0,"data":{"list":[{"id":"a"}]}}', '200', '0', 'success_data'),
    ('{"code":0,"data":{"list":[]}}', '200', '0', 'success_empty'),
    ('{"code":7,"msg":"denied"}', '200', '0', 'business_error'),
    ('not json', '200', '0', 'non_json'),
    ('{"code":401}', '401', '22', 'http_error'),
    ('', '000', '6', 'network_error'),
])
def test_outcomes_are_distinct_and_only_success_becomes_source(tmp_path, fake_curl, monkeypatch, response, status, exit_code, outcome):
    monkeypatch.setenv('FAKE_RESPONSE', response); monkeypatch.setenv('FAKE_STATUS', status); monkeypatch.setenv('FAKE_EXIT', exit_code)
    catalog = EvidenceCatalog(tmp_path)
    row = catalog.query('paper-search', {}, role='analysis_reviewer')
    assert row['outcome'] == outcome
    assert len(catalog.sources()) == int(outcome.startswith('success_'))


def test_append_refresh_idempotency_and_tamper(tmp_path, fake_curl):
    catalog = EvidenceCatalog(tmp_path)
    a = catalog.query('paper-search', {'page': 1}, role='ontology_modeler', idempotency_key='first')
    assert catalog.query('paper-search', {'page': 1}, role='ontology_modeler', idempotency_key='first') == a
    b = catalog.query('paper-search', {'page': 2}, role='ontology_modeler', idempotency_key='second')
    c = catalog.query('paper-search', {'page': 1}, role='ontology_modeler', idempotency_key='refresh', refresh=True)
    assert len({r['source_id'] for r in [a,b,c]}) == 3
    with pytest.raises(ValueError, match='conflict'):
        catalog.query('paper-search', {'page': 2}, role='ontology_modeler', idempotency_key='first')
    assert len(catalog.records()) == 3
    (tmp_path / a['response_file']).write_text('{}')
    with pytest.raises(ValueError, match='changed'): catalog.sources([a['source_id']])


def test_role_resource_permissions_and_pagination(tmp_path, fake_curl):
    path = tmp_path / 'context.json'
    path.write_text(json.dumps({'workflow_policy': CLI_SKILL_REVIEW, 'workspace': str(tmp_path), 'role': 'reviewer'}))
    response = dispatch(path, 'ai_index_query', {'command': 'paper-search', 'params': {}})
    page = dispatch(path, 'ai_index_read', {'query_id': response['query']['query_id']})
    assert json.loads(page['text'])['code'] == 0
    assert page['pagination_layer'] == 'saved_response_bytes'
    assert dispatch(path, 'ai_index_skill', {})['resource'] == 'SKILL.md'
    with pytest.raises(ValueError, match='Only this Skill'):
        dispatch(path, 'ai_index_skill', {'path': '../../../../../.pi/agent/models.json'})
    with pytest.raises(ValueError, match='not permitted'):
        dispatch(path, 'ai_index_register', {'path': 'other.json'})


def test_user_secret_or_invalid_page_never_reaches_client(tmp_path, fake_curl):
    catalog = EvidenceCatalog(tmp_path)
    for params in [{'api_key':'fake'}, {'page':'1'}, {'size':51}]:
        with pytest.raises(ValueError):catalog.query('paper-search', params, role='analyst')
    with pytest.raises(ValueError):catalog.query('huggingface-search', {'size':21}, role='analyst')
    assert not fake_curl.exists()


def test_institution_keyword_is_not_a_credential(tmp_path,fake_curl):
    row=EvidenceCatalog(tmp_path).query('institution-search',{'keyword':'OpenAI','page':1,'size':1},role='ontology_modeler')
    args=json.loads(fake_curl.read_text())
    assert json.loads(args[args.index('--data-binary')+1])=={'keyword':'OpenAI','page':1,'size':1}
    assert row['outcome']=='success_empty'


@pytest.mark.parametrize('name',['apiKey','api_key','access_token','clientSecret','OpenAI_API_KEY','password'])
def test_sensitive_parameter_names_still_do_not_reach_client(tmp_path,fake_curl,name):
    with pytest.raises(ValueError,match='Credentials'):
        EvidenceCatalog(tmp_path).query('institution-search',{name:'forbidden'},role='ontology_modeler')
    assert not fake_curl.exists()
