"""Actual Pi processes with a controlled provider: autonomous submission and recovery."""
import json,os,re,subprocess
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from threading import Thread
import pytest
from dataelf.config import DataElfConfig
from dataelf.ontology.config import OntologyConfig,RoleConfig
from dataelf.ontology.runtime import PiOntologyAgent
from dataelf.ontology.pipeline import run_ontology
from tests.test_common_ontology import retail_contract,retail_request


def require_pi():
    root=Path(__file__).resolve().parents[1];binary=root/'node_modules/.bin/pi'
    if not binary.exists() or int(subprocess.check_output(['node','--version'],text=True).strip()[1:].split('.')[0])<22:
        pytest.skip('Node 22+ and project Pi required')
    return root,binary


@pytest.mark.parametrize('fault',['full','draft','deep_draft','prose','repeated','length','unsupported','graph_commit','serialized_contract','review_length','empty_length','empty_prose','advanced_author','component_trial','malformed_trial_patch','split_author'])
def test_autonomous_pi_workflow(tmp_path,fault):
    root,binary=require_pi();requests=[];positions={};injected=False
    contract=retail_contract().model_dump(mode='json')
    if fault=='deep_draft':
        node=contract['mappings'][0]['nodes'][0]
        for _ in range(12):node['iri']={'op':'if','args':[True,node['iri'],'urn:unreachable']}
        fault='draft'
    def text(message):
        value=message.get('content','');return value if isinstance(value,str) else ''.join(b.get('text','') for b in value if b.get('type')=='text')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            nonlocal injected
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(body)
            system='\n'.join(text(m) for m in body['messages'] if m['role'] in ('system','developer'))
            role=re.search(r'independent ontology (modeler|reviewer)',system).group(1)
            if fault=='unsupported':
                raw=json.dumps({'error':{'message':'unsupported tool_choice required'}}).encode();self.send_response(400);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
            index=positions.get(role,0);previous=[m for m in body['messages'] if m['role']=='tool']
            summary={}
            try:summary=json.loads(system.split('CURRENT TASK SUMMARY (data, not instructions):\n')[1].split('\n')[0])
            except (ValueError,IndexError):pass
            last={}
            if previous:
                try:last=json.loads(text(previous[-1]))
                except ValueError:pass
            finish='tool_calls';narrative=''
            if role=='reviewer':
                if fault=='review_length' and not injected:
                    injected=True;finish='length';narrative='truncated review; candidate retained';name,args='',{}
                elif index==0:name,args='graph_query',{'graph_ref':summary['graph_ref'],'sparql':'ASK {?s ?p ?o}'}
                else:name,args='ontology_submit',{'verdict':'approve','summary':'Checked evidence and candidate graph','issues':[]}
            elif fault in ('empty_length','empty_prose'):
                finish='length' if fault=='empty_length' else 'stop';narrative='Designing a contract but no artifact was written.';name,args='',{}
            elif fault=='split_author' and index==0:
                name,args='ontology_upsert_class',{'base_hash':summary['saved_artifacts']['draft_hash'],'id':'Customer',
                    'definition':{'label':'Customer from source'},'evidence':['customers.csv#/0']}
            elif fault=='advanced_author':
                if index==0:name,args='ontology_inspect',{'resource':'schema','tool':'ontology_author'}
                elif index==1:name,args='ontology_author',{'action':'configure','base_hash':last.get('draft_hash') or summary['draft_hash'],'namespace':contract['namespace']}
                else:name,args='ontology_submit',contract
            elif fault=='malformed_trial_patch':
                saved_hash=summary.get('draft_hash')
                for message in previous:
                    try:saved_hash=json.loads(text(message)).get('draft_hash') or saved_hash
                    except ValueError:pass
                if index==0:name,args='ontology_execute',{'contract':contract}
                elif index==1:name,args='ontology_execute',{'contract':'{"mappings":['}
                elif index==2:
                    assert 'without retransmitting contract' in text(previous[-1])
                    name,args='ontology_draft',{'action':'patch','base_hash':saved_hash,
                        'operations':[{'op':'replace','path':'/description','value':'small correction after malformed input'}]}
                elif index==3:name,args='ontology_execute',{}
                else:name,args='ontology_draft',{'action':'submit','base_hash':saved_hash}
            elif fault=='component_trial':
                if index==0:
                    partial={**contract,'mappings':[contract['mappings'][0]]}
                    name,args='ontology_execute',{'scope':'component','mapping_ids':['customers'],'contract':partial}
                elif index==1:
                    assert last['scope']=='component' and last['deferred_checks'] and not last['publication_eligible']
                    name,args='ontology_execute',{'contract':contract}
                else:name,args='ontology_draft',{'action':'submit','base_hash':last['draft_hash']}
            elif fault in ('graph_commit','serialized_contract'):
                if index==0:name,args='ontology_execute',{'contract':json.dumps(contract) if fault=='serialized_contract' else contract}
                elif fault=='serialized_contract':
                    name,args='ontology_submit',{k:json.dumps(v) if isinstance(v,(list,dict)) else v for k,v in contract.items()}
                elif index==1:name,args='ontology_inspect',{'resource':'schema','tool':'graph_commit'}
                else:name,args='graph_commit',{'graph_ref':summary['graph_ref'],'expected_base':None}
            elif fault=='repeated' and index<4:name,args='ontology_inspect',{}
            elif fault in ('draft','length') and index<2:
                hash=last.get('draft_hash') or summary['draft_hash']
                if index==0:name,args='ontology_draft',{'action':'patch','base_hash':hash,'operations':[{'op':'add','path':'/namespace','value':contract['namespace']}]}
                else:name,args='ontology_draft',{'action':'patch','base_hash':hash,'operations':[{'op':'add','path':'/'+k,'value':v} for k,v in contract.items() if k not in ('domain','schema_version','namespace')]}
            elif fault=='length' and not injected:
                injected=True;finish='length';narrative='truncated response; saved draft retained';name,args='',{}
            elif fault=='draft':name,args='ontology_draft',{'action':'submit','base_hash':last['draft_hash']}
            else:name,args='ontology_submit',contract
            if role=='modeler' and fault=='prose':narrative='Evidence-based modeling reasoning. '*100
            if finish!='length':positions[role]=index+1
            delta={'role':'assistant','content':narrative}
            if name:delta['tool_calls']=[{'index':0,'id':'c'+str(len(requests)),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]
            raw=('data: '+json.dumps({'id':'fixture','choices':[{'index':0,'delta':delta,'finish_reason':None}]})+'\n\ndata: '+json.dumps({'id':'fixture','choices':[{'index':0,'delta':{},'finish_reason':finish}],'usage':{'prompt_tokens':50,'completion_tokens':50,'total_tokens':100}})+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    agent=tmp_path/'pi-agent';agent.mkdir()
    (agent/'models.json').write_text(json.dumps({'providers':{'fixture':{'baseUrl':f'http://127.0.0.1:{server.server_port}/v1','api':'openai-completions','apiKey':'synthetic','models':[{'id':'fixture','name':'Fixture','reasoning':True,'input':['text'],'contextWindow':128000,'maxTokens':8192,'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0},'compat':{'thinkingFormat':'zai','supportsReasoningEffort':False}}]}}}))
    cfg=DataElfConfig(explorer={'pi':{'binary':str(binary),'cwd':str(root),'model':'fixture/fixture','log_mode':'quiet'}},env={'PI_CODING_AGENT_DIR':str(agent),'PATH':os.environ['PATH']})
    role=RoleConfig(author_interface='split' if fault=='split_author' else 'union',timeout_seconds=60,stream_usage=True,tool_choice='required' if fault=='unsupported' else 'auto')
    settings=OntologyConfig(max_repairs=0,timeout_seconds=180,modeler=role,reviewer=role,delivery={'enabled':False})
    try:
        result=run_ontology(retail_request(tmp_path),config=settings,agent=PiOntologyAgent(cfg))
        if fault in ('empty_length','empty_prose'):
            assert result.error_code==('ONTOLOGY_OUTPUT_TRUNCATED' if fault=='empty_length' else 'ONTOLOGY_NO_SUBMISSION')
            assert result.details['agentSessions']==2 and result.details['controlRestarts']==1
            assert 'recovery_no_artifact_progress' in result.error_message
            assert not result.revision_id and len(requests)==2
        elif fault=='unsupported':assert result.error_code=='ONTOLOGY_TOOL_CHOICE_UNSUPPORTED',result
        else:
            assert result.status=='completed',result.error_message
            assert result.revision_id and result.graph_ref['revision_id']==result.revision_id
            assert result.details['metrics']['agentSessions']==(3 if fault in ('length','review_length') else 2)
            if fault=='review_length':assert positions['modeler']==1
            assert result.details['metrics']['llmRequests']==len(requests)
            assert all(r['thinking']['type']=='disabled' and not r['chat_template_kwargs']['enable_thinking'] for r in requests)
            assert all('tool_choice' not in r or r['tool_choice']=='auto' for r in requests)
            assert all(r['stream_options']['include_usage'] for r in requests)
            if fault=='split_author':
                tool=next(t['function'] for t in requests[0]['tools'] if t['function']['name']=='ontology_upsert_class')
                assert tool['parameters']['type']=='object' and 'oneOf' not in tool['parameters']
                assert positions['modeler']==2
            if fault=='advanced_author':
                tools=lambda r:{t['function']['name'] for t in r['tools']}
                assert 'ontology_author' in tools(requests[0])
                assert 'ontology_author' in tools(requests[1])
                author_schema=lambda r:next(t['function']['parameters'] for t in r['tools'] if t['function']['name']=='ontology_author')
                assert 'set_identity' not in json.dumps(author_schema(requests[0]))
                assert 'set_identity' in json.dumps(author_schema(requests[1]))
            assert not any('"submitted":true' in text(m).replace(' ','') for r in requests for m in r['messages'] if m['role']=='tool')
    finally:server.shutdown();server.server_close();thread.join(2)
