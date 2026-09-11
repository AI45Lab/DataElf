"""Actual Pi/TS runtime with an offline, local OpenAI-compatible provider."""
from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from dataelf.discovery.contracts import JobSpec
from dataelf.discovery.run_control import RunControl
from dataelf.discovery.workflow import run_job
from dataelf_server.workflows.explorer import ServerExplorer
from dataelf_server.workflows.profile import ServerProfile
from tests.server.helpers import settings, FakeIntentRecognizer
from tests.server.test_workflow import prefetch, FIXTURES

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('transport,stale_tool,language,min_correction', [
    ('inherit', False, 'zh-CN', False), ('nonstream', False, 'zh-CN', False),
    ('inherit', True, 'zh-CN', False), ('nonstream', True, 'zh-CN', False),
    ('inherit', False, 'en', False), ('nonstream', False, 'en', False),
    ('inherit', False, 'zh-CN', True),
])
def test_actual_pi_extension_converges_with_configured_provider(tmp_path, transport, stale_tool, language, min_correction):
    binary = ROOT/'node_modules/.bin/pi'
    if not binary.is_file():
        pytest.skip('repository npm dependencies are required for Pi integration')
    workspace = tmp_path/'attempt'
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(request)
            tool=request.get('tool_choice',{}).get('function',{}).get('name')
            if tool == 'bash':
                arguments={'command':'echo orientation'}
            elif tool == 'dataelf_submit_rdf_analysis':
                if stale_tool:
                    tool,arguments='bash',{}
                else:
                    arguments={'script':(FIXTURES/'analyze.py').read_text()}
            elif tool == 'dataelf_finalize_rdf_insights':
                signals=json.loads((workspace/'insights/candidate_signals.json').read_text())['candidate_signals']
                arguments={'insights':[{'title':'模型工具降低采用门槛','thesis':'模型工具发布降低开发者采用门槛，后续采用效果仍有待持续观察。','source_ids':[signals[0]['source_ids'][0]],'supporting_signal_ids':['sig_001']}]}
                if language == 'en':
                    arguments['insights'][0].update(title='Adoption benefits remain uncertain', thesis='The release may ease adoption, but sustained benefits remain uncertain.')
                if min_correction and sum(r.get('tool_choice', {}).get('function', {}).get('name') == tool for r in requests) > 1:
                    arguments['insights'][0]['thesis'] += '这些证据来自本次公开资料，长期采用效果仍需要后续验证。'
            else:
                self.send_error(400,'Expected forced tool selection')
                return
            tool_call={'id':f'call_{len(requests)}','type':'function','function':{'name':tool,'arguments':json.dumps(arguments,ensure_ascii=False)}}
            if request.get('stream'):
                chunks=[{'id':'local','object':'chat.completion.chunk','choices':[{'index':0,'delta':{'role':'assistant','tool_calls':[{'index':0,**tool_call}]},'finish_reason':None}]}, {'id':'local','object':'chat.completion.chunk','choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}]}]
                payload=(''.join('data: '+json.dumps(c,ensure_ascii=False)+'\n\n' for c in chunks)+'data: [DONE]\n\n').encode()
                content_type='text/event-stream'
            else:
                payload=json.dumps({'id':'local','object':'chat.completion','model':'fixture-model','choices':[{'index':0,'message':{'role':'assistant','content':None,'tool_calls':[tool_call]},'finish_reason':'tool_calls'}],'usage':{'prompt_tokens':1,'completion_tokens':1}},ensure_ascii=False).encode()
                content_type='application/json'
            self.send_response(200)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    agent=tmp_path/'agent';agent.mkdir()
    (agent/'models.json').write_text(json.dumps({'providers':{'local-fixture':{'baseUrl':f'http://127.0.0.1:{server.server_port}/v1','apiKey':'test-key','api':'openai-completions','models':[{'id':'fixture-model','name':'Fixture Model','reasoning':True,'input':['text'],'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0},'contextWindow':128000,'maxTokens':16000,'compat':{'supportsDeveloperRole':False,'supportsReasoningEffort':False,'thinkingFormat':'chat-template','chatTemplateKwargs':{'enable_thinking':{'$var':'thinking.enabled'}}}}]}}}))
    cfg=settings(project_root=tmp_path,state_dir=tmp_path/'state')
    cfg.core.explorer.pi.binary=str(binary)
    cfg.core.explorer.pi.model='local-fixture/fixture-model'
    cfg.core.explorer.pi.timeout_seconds=45
    cfg.core.explorer.pi.extra_args='--offline --thinking off'
    cfg.core.explorer.pi.log_mode='quiet'
    cfg.core.env.update(PI_CODING_AGENT_DIR=str(agent),DATAELF_SERVER_TRANSPORT=transport)
    cfg.server.pi.transport=transport
    class Recognizer(FakeIntentRecognizer):
        def extract(self, query, **kwargs):
            intent = super().extract(query, **kwargs)
            if min_correction:
                intent.output.body_length.min = 40
            if language != 'zh-CN':
                intent.output.language = language
                intent.output.focus_points = ['adoption constraints']
                intent.output.item_count.max = 2
            return intent
    def content_prefetch(plan, workspace_path):
        result = prefetch(plan, workspace_path)
        for source in result.execution['sources'].values():
            for item in source['items']:
                original = item['data']
                enriched = {'irrelevant_metadata': 'x' * 1200, **original,
                            'core_ideology': {'main_theme': 'CONTENT_FIRST_EVIDENCE_MARKER'}}
                # Keep the original response and filtered view identical so the
                # real modeler can still establish the exact raw JSON pointer.
                raw_path = result.result_path.parent / item['raw_ref']
                raw = json.loads(raw_path.read_text())
                raw['data']['list'] = [enriched if row == original else row for row in raw['data']['list']]
                raw_path.write_text(json.dumps(raw))
                item['data'] = enriched
        result.result_path.write_text(json.dumps(result.execution))
        return result
    try:
        job=run_job(JobSpec(domain='ai_index',objective='2026-08-27 快讯模块总结 RAW_REQUEST_AUDIT_ONLY',workflow_profile='server'),cfg.execution_config(),plugin=ServerProfile(cfg,prefetcher=content_prefetch,recognizer=Recognizer()),explorer=ServerExplorer(cfg),control=RunControl(workspace_path=workspace))
        controlled=[r for r in requests if r['tool_choice']['function']['name']!='bash']
        assert controlled
        assert all(r['chat_template_kwargs']['enable_thinking'] is False for r in requests)
        if transport=='nonstream':
            assert all(r['max_tokens']==16000 for r in requests)
        assert all('MANDATORY SERVER PHASE' in next(m['content'] for m in r['messages'] if m['role'] in ('system','developer')) for r in controlled)
        assert 'RAW_REQUEST_AUDIT_ONLY' not in json.dumps(requests)
        if language != 'zh-CN':
            for request in controlled:
                system = next(m['content'] for m in request['messages'] if m['role'] in ('system','developer'))
                assert f'"language": "{language}"' in system
                assert 'adoption constraints' in system
                assert 'Chinese insight body' not in json.dumps(request)
                assert '80-120' not in json.dumps(request)
        if stale_tool:
            assert job.status=='failed'
            assert not (workspace/'insights/insight_candidates.json').exists()
            assert not (workspace/'prompts/synthesis_retry.md').exists()
            # The optional transport rejects forced-name mismatches itself;
            # native streaming needs a guard even when schema validation fails.
            if transport=='inherit':
                assert json.loads((workspace/'logs/pi_control.json').read_text())['reason']=='model_request_limit'
                assert len(requests)<=21
                assert any('previous bash call was rejected' in m['content'] for r in controlled for m in r['messages'] if isinstance(m.get('content'),str))
            return
        assert job.status=='completed', (job.error_message,(workspace/'logs/pi_stderr.log').read_text()[-2500:])
        final_request = requests[-1]
        system = next(m['content'] for m in final_request['messages'] if m['role'] in ('system', 'developer'))
        assert 'CONTENT_FIRST_EVIDENCE_MARKER' in system
        expected = ['bash','bash','bash','dataelf_submit_rdf_analysis','dataelf_finalize_rdf_insights']
        if min_correction:
            expected.append('dataelf_finalize_rdf_insights')
            assert 'one content revision' in json.dumps(requests[-1])
            review = json.loads((workspace/'reviews/writing_review.json').read_text())
            assert review['body_lengths'][0] >= 40
        assert [r['tool_choice']['function']['name'] for r in requests] == expected
        assert all(r['model']=='fixture-model' for r in requests)
        assert all([t["function"]["name"] for t in r["tools"]] == [r["tool_choice"]["function"]["name"]] for r in requests)
        assert all(bool(r.get('stream')) == (transport=='inherit') for r in requests)
        assert not (workspace/'prompts/synthesis_retry.md').exists()
        assert json.loads((agent/'models.json').read_text())['providers']['local-fixture']['apiKey']=='test-key'
        assert 'test-key' not in (workspace/'logs/pi_stdout.log').read_text()
    finally:
        server.shutdown();server.server_close();thread.join(2)
