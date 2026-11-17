import os
"""Actual installed Pi + native calls + strict submission + isolated reviewer."""
import json
import pytest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from dataelf.config import DataElfConfig
from dataelf.discovery.contracts import DiscoveryJob,DiscoveryContext,DomainManifest
from dataelf.discovery.analysis_review.coordinator import run_analysis
from test_ai_index_analysis_review import setup,approve

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('workflow_policy',['ai_index_cli_skill_review_v1'])
def test_actual_pi_repairs_format_then_semantics_and_reviews_exact_snapshot(tmp_path,workflow_policy,monkeypatch):
    for key in list(os.environ):
        if key.lower().endswith('_proxy'):monkeypatch.delenv(key,raising=False)
    workspace=tmp_path/'workspace';base,spec,contract=setup(workspace)
    spec=spec.model_copy(update={'workflow_policy':workflow_policy})
    (workspace/'prompts/start.md').write_text('Compute the change from the frozen original source. Preserve complete Insight and evidence.')
    original=json.loads((workspace/'insights/candidate_signals.json').read_text())
    item=json.loads((workspace/'insights/insight_candidates.json').read_text())['insight_candidates'][0]
    (workspace/'insights/insight_candidates.json').write_text(json.dumps({'insight_candidates':[{**item,'thesis':'100 to 120 means an increase of 25%.'}]}))
    requests=[];role_steps={};actors=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(request)
            dirs=sorted((workspace/'reviews/analysis/sessions').glob('*'))
            directory=dirs[-1];task=json.loads((directory/'context.json').read_text());role=task['role'];sid=directory.name
            role_steps[sid]=role_steps.get(sid,0)+1;step=role_steps[sid];actors.append(role)
            assert request.get('tool_choice')=='auto'
            if role=='analyst' and not task.get('previous_review'):
                if step==1:tool='write';args={'path':str(workspace/'insights/candidate_signals.json'),'content':'{"signals":[]}'}
                elif step==2:tool='analysis_submit';args={'idempotency_key':'wrong-root'}
                elif step==3:tool='write';args={'path':str(workspace/'insights/candidate_signals.json'),'content':json.dumps(original)}
                else:tool='analysis_submit';args={'idempotency_key':'first'}
            elif role=='analyst':
                if step==1:tool='dataelf_submit_output';args={'artifact_id':'insight_candidates','data':{'insight_candidates':[item]}}
                else:tool='analysis_submit';args={'idempotency_key':'repaired'}
            elif step==1:
                if workflow_policy=='legacy':assert all(t['function']['name'].startswith('analysis_') for t in request['tools'])
                else:
                    assert all(t['function']['name'].startswith(('analysis_','ai_index_','review_')) for t in request['tools'])
                    assert '数据源 Skill' in request['messages'][0]['content']
                    assert 'ai-index-search' in request['messages'][0]['content']
                tool='analysis_verify';args={'code':"import json\nx=json.loads((SNAPSHOT/'raw/values.json').read_text());print((x['after']-x['before'])/x['before'])"}
            else:
                submission=task['submission'];tool='analysis_review_submit';args=approve(submission)
                if not task.get('previous_review'):
                    args.update(verdict='revise',issues=[{'issue_id':'denominator','blocking':True,'severity':'high','category':'computation',
                        **({'nature':'confirmed_error'} if workflow_policy!='legacy' else {}),
                        'location':'insights/insight_candidates.json#/insight_candidates/0/thesis','message':'25% uses the wrong denominator',
                        'evidence':['raw/values.json','analysis_verify stdout=0.2'],'impact':'The principal numerical claim is wrong',
                        'required_change':'Recompute the increase with the stated baseline and correct the thesis',
                        'acceptance_criteria':'(120-100)/100=0.2, and the report consistently states 20%'}])
                    args['issues'].append({'issue_id':'optional-wording','blocking':False,'severity':'low','category':'clarity',
                        'nature':'suggestion','location':'insights/final_brief.md','message':'Optional clarification of sample scope',
                        'evidence':['insights/final_brief.md'],'impact':'Readability only',
                        'required_change':'Clarify the sample if useful','acceptance_criteria':'Sample scope remains explicit'})
                else:
                    args['issue_updates']=[{'issue_id':'denominator','status':'resolved','evidence':['analysis_verify stdout=0.2','insights/insight_candidates.json'],'explanation':'Thesis now agrees with independent computation'},
                        {'issue_id':'optional-wording','status':'withdrawn','evidence':['insights/final_brief.md'],'explanation':'Existing sample wording already makes the scope explicit'}]
            call={'index':0,'id':f'c{len(requests)}','type':'function','function':{'name':tool,'arguments':json.dumps(args)}}
            chunks=[{'choices':[{'index':0,'delta':{'role':'assistant','tool_calls':[call]},'finish_reason':None}]},
                    {'choices':[{'index':0,'delta':{},'finish_reason':'stop'}],'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}]
            body=(''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    agent=tmp_path/'agent';agent.mkdir();(tmp_path/'npm').symlink_to(ROOT/'.pi/npm',target_is_directory=True)
    (agent/'settings.json').write_text(json.dumps({'retry':{'enabled':False},'compaction':{'enabled':False}}))
    (agent/'models.json').write_text(json.dumps({'providers':{'controlled':{'baseUrl':f'http://127.0.0.1:{server.server_port}/v1','apiKey':'test-key','api':'openai-completions',
        'models':[{'id':'fixture','name':'Fixture','reasoning':False,'input':['text'],'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0},'contextWindow':128000,'maxTokens':16384}]}}}))
    cfg=DataElfConfig(explorer={'pi':{'binary':str(ROOT/'node_modules/.bin/pi'),'cwd':str(ROOT),'model':'controlled/fixture','extra_args':'--offline --thinking off','log_mode':'quiet'}})
    ctx=DiscoveryContext(workspace_path=str(workspace),spec=spec,manifest=DomainManifest(domain='ai_index',version='1',display_name='AI',plugin='test'),
        prompt_path=str(workspace/'prompts/start.md'),env={'PI_CODING_AGENT_DIR':str(agent)})
    # Single-file submission tool uses the exact declared contract too.
    (workspace/'prompts/output_contract.json').write_text(json.dumps({'contract':contract.model_dump(mode='json'),'protected_paths':[]}))
    from dataelf.discovery.output_submission import submission_schema
    from dataelf.ontology.wire_schema import provider_schema
    (workspace/'prompts/output_tool_schema.json').write_text(json.dumps(provider_schema(submission_schema(contract))))
    try:
        result,review=run_analysis(DiscoveryJob(job_id='native',spec=spec,workspace_path=str(workspace)),ctx,contract,cfg)
        assert result.status=='completed',result
        assert review.status=='pass'
        assert len(json.loads((workspace/'reviews/analysis/history.json').read_text()))==2
        assert json.loads((workspace/'insights/insight_candidates.json').read_text())['insight_candidates'][0]==item
        assert actors==['analyst']*4+['reviewer']*2+['analyst']*2+['reviewer']*2
        assert not any(t['function']['name'].startswith(('graph_','ontology_')) for r in requests for t in r['tools'])
        scratch=workspace/'reviews/analysis/collaboration/scratch' if workflow_policy!='legacy' else workspace/'reviews/analysis/sessions'
        paths=list(scratch.glob('*/*/execution.json')) if workflow_policy!='legacy' else list(scratch.glob('*/scratch/*/execution.json'))
        assert len(paths)==2
        assert all(json.loads(p.read_text())['exit_code']==0 and json.loads(p.read_text())['stdout'].strip()=='0.2'
            for p in paths)
        if workflow_policy!='legacy':
            captures=list((workspace/'reviews/analysis/sessions').glob('*/effective_requests/*.json'))
            assert captures
            assert all(json.loads(p.read_text())['policy']==workflow_policy for p in captures)
    finally:server.shutdown();server.server_close();thread.join(2)
