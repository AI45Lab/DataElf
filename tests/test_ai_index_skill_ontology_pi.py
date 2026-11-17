"""Actual Pi and packaged source client, entirely controlled offline providers."""
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from threading import Thread

from dataelf.config import DataElfConfig
from dataelf.ontology import run_ontology
from dataelf.ontology.contracts import OntologyRequest
from dataelf.ontology.config import OntologyConfig
from dataelf.ontology.runtime import PiOntologyAgent
from dataelf.ontology.capabilities import CLI_SKILL_REVIEW
from dataelf.ontology.draft import draft_state
from dataelf.ontology.sources import digest
from dataelf.domains.ai_index.evidence import EvidenceCatalog
from tests.test_ontology_pi_runtime import require_pi
from tests.test_ai_index_skill_evidence import fake_curl


def test_native_pi_queries_skill_saves_components_and_independently_reviews(tmp_path,fake_curl,monkeypatch):
    for key in list(os.environ):
        if key.lower().endswith('_proxy'):monkeypatch.delenv(key,raising=False)
    repo,binary=require_pi();workspace=tmp_path/'workspace';workspace.mkdir()
    monkeypatch.setenv('FAKE_RESPONSE','{"code":0,"data":{"total":1,"list":[{"id":"a","value":0}]}}')
    requests=[];positions={};tools_run=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(body)
            assert body['tool_choice']=='auto'
            assert not {'max_tokens','max_completion_tokens','max_output_tokens'} & body.keys(), body.keys()
            path=sorted(workspace.glob('ontology/runs/*/sessions/*/context.json'))[-1]
            context=json.loads(path.read_text());role=context['role'];i=positions.get(role,0);positions[role]=i+1
            system=body['messages'][0]['content']
            assert '数据源 Skill' in system and 'ai-index-search' in system
            assert 'CURRENT TASK SUMMARY' not in system and 'ak_1234567890AbCdEf' not in json.dumps(body)
            visible={t['function']['name'] for t in body['tools']}
            if role=='modeler':
                assert 'ontology_upsert_class' in visible and 'ai_index_query' in visible
                current=digest(draft_state(path,context))
                rows=EvidenceCatalog(workspace).records();row=rows[0] if rows else None
                evidence=[row['response_file']+'#/data/list/0'] if row else []
                if i==0:name,args='ai_index_skill',{}
                elif i==1:name,args='ai_index_query',{'command':'paper-search','params':{'title':'Sourced example','page':1,'size':1}}
                elif i==2:name,args='ontology_configure',{'base_hash':current,'namespace':'urn:example:'}
                elif i==3:name,args='ontology_upsert_class',{'base_hash':current,'id':'Entry','definition':{'label':'Entry'},'evidence':evidence}
                elif i==4:name,args='ontology_upsert_property',{'base_hash':current,'id':'value','definition':{'domain':'Entry','range':'xsd:integer'},'evidence':evidence}
                elif i==5:name,args='ontology_upsert_mapping',{'base_hash':current,'mapping':{'id':'entries','source':row['source_id'],
                    'selector':'/data/list/*','evidence':evidence,
                    'nodes':[{'id':'entry','class_id':'Entry','iri':{'op':'concat','args':['urn:example:entry/',{'path':'/id'}]}}],
                    'triples':[{'subject':'entry','predicate':'value','value':{'path':'/value'}}]},'evidence':evidence}
                elif i==6:name,args='ontology_execute',{}
                else:name,args='ontology_draft',{'action':'submit','base_hash':current}
            else:
                assert 'review_checks_save' in visible and not any(n.startswith('ontology_upsert_') for n in visible)
                if i==0:name,args='ai_index_skill',{}
                elif i==1:
                    row=EvidenceCatalog(workspace).records()[0]
                    name,args='ai_index_read',{'query_id':row['query_id']}
                elif i==2:name,args='graph_query',{'graph_ref':context['graph_ref'],'sparql':'ASK {?s <urn:example:value> 0}'}
                else:name,args='ontology_submit',{'verdict':'approve','summary':'Independently checked the exact source and integer zero','issues':[]}
            assert name in visible
            tools_run.append((role,name))
            call={'index':0,'id':'c'+str(len(requests)),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}
            events=[{'choices':[{'index':0,'delta':{'role':'assistant','tool_calls':[call]},'finish_reason':None}]},
                {'choices':[{'index':0,'delta':{},'finish_reason':'stop'}],'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}]
            data=(''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    agent=tmp_path/'agent';agent.mkdir();(agent/'settings.json').write_text(json.dumps({'retry':{'enabled':False}}))
    (agent/'models.json').write_text(json.dumps({'providers':{'controlled':{'baseUrl':f'http://127.0.0.1:{server.server_port}/v1',
        'apiKey':'fixture-model-key','api':'openai-completions','models':[{'id':'fixture','name':'Fixture','reasoning':False,
            'input':['text'],'contextWindow':128000,'maxTokens':4096,'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0}}]}}}))
    config=DataElfConfig(explorer={'pi':{'binary':str(binary),'cwd':str(repo),'model':'controlled/fixture','log_mode':'quiet'}},
        env={'PATH':os.environ['PATH'],'PI_CODING_AGENT_DIR':str(agent),'AI_INDEX_API_KEY':'ak_1234567890AbCdEf',
             'CURL_ARGS':str(fake_curl),'FAKE_RESPONSE':os.environ['FAKE_RESPONSE']})
    try:
        result=run_ontology(OntologyRequest(domain='ai_index',workspace=workspace,workflow_policy=CLI_SKILL_REVIEW,
            objective='Use the supplied API to inspect sourced entries and zero values'),config=OntologyConfig(),agent=PiOntologyAgent(config))
        assert result.status=='completed',result.error_message
        assert result.template_path is None
        assert tools_run.count(('modeler','ai_index_query'))==1
        assert ('modeler','ontology_execute') in tools_run and ('reviewer','graph_query') in tools_run
        records=EvidenceCatalog(workspace).records();assert len(records)==1 and records[0]['outcome']=='success_data'
        bundle=Path(result.bundle_path);manifest=json.loads((bundle/'manifest.json').read_text())
        assert records[0]['response_file'] in manifest['sourceFiles']
        assert any(p.endswith('record.json') for p in manifest['sourceFiles'])
        captures=list(workspace.glob('ontology/runs/*/sessions/*/effective_requests/*.json'))
        assert len(captures)==len(requests)==12
        assert all('ai-index-search' in json.loads(p.read_text())['payload']['messages'][0]['content'] for p in captures)
        # Re-entering a completed task reuses exactly its published revision.
        resumed=run_ontology(OntologyRequest(domain='ai_index',workspace=workspace,workflow_policy=CLI_SKILL_REVIEW,
            objective='Use the supplied API to inspect sourced entries and zero values'),config=OntologyConfig(),agent=lambda *a:(_ for _ in ()).throw(AssertionError('must not call a model')))
        assert resumed.graph_ref==result.graph_ref
    finally:server.shutdown();server.server_close();thread.join(2)
