"""Recovery at actual handoff boundaries, without a second workflow system."""
import json
from pathlib import Path
import pytest

from dataelf.ontology import run_ontology
from dataelf.ontology.capabilities import CLI_SKILL_REVIEW
from dataelf.ontology.contracts import OntologyRequest
from dataelf.ontology.config import OntologyConfig
from dataelf.ontology.collaboration import ReviewLedger,ReviewHandoff
from dataelf.ontology.collaboration_tools import dispatch as review_dispatch
from dataelf.ontology.draft import save_draft,draft_state
from dataelf.ontology.graph import GraphService
from dataelf.ontology.graph.store import write_json
from tests.test_ai_index_dynamic_sources import register,query_contract


@pytest.mark.parametrize('interruption',['request','feedback','publication'])
def test_graph_feedback_recovery_keeps_same_draft_and_publication(tmp_path,monkeypatch,interruption):
    row=register(tmp_path);contract=query_contract(row)
    source=row['response_file']+'#/0'
    request=OntologyRequest(domain='ai_index',workspace=tmp_path,workflow_policy=CLI_SKILL_REVIEW,objective='Inspect the actual zero value')
    calls={'author':0,'local':0,'formal':0};cut=False
    problem={'issue_id':'representation','nature':'question','severity':'low','target':'contract','blocking':True,
        'location':'review_object.json#/properties/value','message':'Check the integer representation',
        'evidence':[source],'impact':'Zero may be omitted','required_change':'Prove or correct the mapping',
        'acceptance_criteria':'Independent source and RDF comparison preserves zero'}
    def actor(role,ctx,directory,limits):
        nonlocal cut
        path=directory/'context.json';write_json(path,{**ctx,'role':role});ctx=json.loads(path.read_text())
        if role=='modeler':
            calls['author']+=1
            if not ctx.get('previous_draft'):
                save_draft(path,contract.model_dump(mode='json'))
                result=review_dispatch(path,'review_request',{'object_kind':'draft','question':'Does this mapping preserve zero?',
                    'evidence':[source],'idempotency_key':'check-zero'})
                if interruption=='request' and not cut:cut=True;raise RuntimeError('Injected crash after request persisted')
                raise ReviewHandoff(ReviewLedger(tmp_path,'graph').load(result['handoff']['request_id']))
            assert draft_state(path,ctx)['mappings'][0]['id']=='entries'
            assert ctx['previous_review']['issues'][0]['issue_id']=='representation'
            review_dispatch(path,'review_respond',{'issue_id':'representation','response':'counterevidence',
                'explanation':'The source and mapping explicitly retain integer zero','evidence':[source]})
            return draft_state(path,ctx)
        if ctx['review_mode']=='local':
            calls['local']+=1
            from dataelf.ontology.tools import dispatch as ontology_dispatch
            inspected=ontology_dispatch(path,'ontology_inspect',{'resource':'draft'})
            assert json.loads(inspected['text'])['mappings'][0]['id']=='entries'
            feedback={'request_id':ctx['review_request_id'],'answer':'Confirm the zero representation','issues':[problem],'checks':[]}
            review_dispatch(path,'review_feedback_submit',feedback)
            if interruption=='feedback' and not cut:cut=True;raise RuntimeError('Injected crash after feedback persisted')
            return feedback
        calls['formal']+=1
        service=GraphService(ctx['graph_root'],tmp_path)
        assert service.query(ctx['graph_ref'],'ASK {?s <urn:sample:value> 0}')['answer']
        return {'verdict':'approve','summary':'Rechecked original evidence and candidate; concern is disproven','issues':[],
            'issue_updates':[{'issue_id':'representation','status':'withdrawn','evidence':[source],
                'explanation':'Integer zero is present in source and current RDF'}]}
    original=GraphService.commit
    def commit(self,*args,**kwargs):
        nonlocal cut
        result=original(self,*args,**kwargs)
        if interruption=='publication' and result.status=='completed' and not cut:
            cut=True;raise RuntimeError('Injected crash after atomic publication')
        return result
    monkeypatch.setattr(GraphService,'commit',commit)
    first=run_ontology(request,config=OntologyConfig(),agent=actor)
    assert first.status=='failed'
    result=run_ontology(request,config=OntologyConfig(),agent=actor)
    assert result.status=='completed',result.error_message
    assert first.graph_id==result.graph_id and calls['local']==calls['formal']==1
    assert len(list((tmp_path/'ontology/graphs').glob('*/revisions/*')))==1
    ledger=ReviewLedger(tmp_path,'graph')
    assert ledger.issues()['representation']['status']=='withdrawn'
    assert ledger.requests()[0]['status']=='delivered'
    assert len(ledger.responses())==1


def test_new_reviewer_compute_sees_registered_supplement_and_own_previous_scratch(tmp_path):
    from tests.test_ai_index_analysis_review import setup
    from dataelf.discovery.analysis_review.tools import dispatch
    _,spec,contract=setup(tmp_path);path=tmp_path/'context.json'
    snapshot=tmp_path/'snapshot';snapshot.mkdir();(snapshot/'partial.txt').write_text('Partial object')
    (tmp_path/'additional.json').write_text('[{"value":0}]')
    from dataelf.domains.ai_index.evidence import EvidenceCatalog
    source=EvidenceCatalog(tmp_path).import_file('additional.json',role='analysis_reviewer')
    scratch=tmp_path/'review_scratch';(scratch/'prior').mkdir(parents=True)
    (scratch/'prior/result.json').write_text('{"before":1}')
    context={'role':'reviewer','workspace':str(tmp_path),'workflow_policy':CLI_SKILL_REVIEW,
        'review_mode':'local','review_snapshot':str(snapshot),'review_scratch':str(scratch),
        'contract':contract.model_dump(mode='json'),'submission':{'graph_refs':[]}}
    write_json(path,context)
    code=('from pathlib import Path\nimport json\n'
          'print(json.loads((SNAPSHOT/'+repr(source['response_file'])+').read_text())[0]["value"])\n'
          'print(json.loads((SNAPSHOT/"reviewer_scratch/prior/result.json").read_text())["before"])')
    result=dispatch(path,'analysis_verify',{'code':code})
    assert result['exit_code']==0,result
    assert result['stdout'].strip()=='0\n1'
    assert not (snapshot/source['response_file']).exists()


@pytest.mark.parametrize('interrupt_role',['analyst','reviewer'])
def test_analysis_native_submit_recovered_without_repeating_model(tmp_path,interrupt_role):
    from tests.test_ai_index_analysis_review import setup,approve
    from dataelf.config import DataElfConfig
    from dataelf.discovery.contracts import DiscoveryContext,DiscoveryJob,DomainManifest,ExplorerRunResult
    from dataelf.discovery.analysis_review.coordinator import run_analysis
    from dataelf.discovery.analysis_review.tools import dispatch
    _,spec,contract=setup(tmp_path);spec=spec.model_copy(update={'workflow_policy':CLI_SKILL_REVIEW})
    prompt=tmp_path/'prompts/initial.md';prompt.write_text(spec.objective)
    context=DiscoveryContext(workspace_path=str(tmp_path),spec=spec,prompt_path=str(prompt),
        manifest=DomainManifest(domain='ai_index',version='1',display_name='AI',plugin='test'))
    calls={'analyst':0,'reviewer':0};cut=False
    class Actor:
        def run(self,job,ctx):
            nonlocal cut
            path=Path(ctx.env['DATAELF_ANALYSIS_CONTEXT']);task=json.loads(path.read_text());role=task['role'];calls[role]+=1
            if role=='analyst':dispatch(path,'analysis_submit',{'idempotency_key':'one-complete-report'})
            else:dispatch(path,'analysis_review_submit',approve(task['submission']))
            if role==interrupt_role and not cut:cut=True;raise RuntimeError('Injected crash after native handoff')
            return ExplorerRunResult(status='completed')
    actor=Actor();job=DiscoveryJob(job_id='recover',spec=spec,workspace_path=str(tmp_path))
    with pytest.raises(RuntimeError,match='Injected'):
        run_analysis(job,context,contract,DataElfConfig(),analyst=actor,reviewer=actor)
    result,_=run_analysis(job,context,contract,DataElfConfig(),analyst=actor,reviewer=actor)
    assert result.status=='completed' and calls=={'analyst':1,'reviewer':1}
    assert len(list((tmp_path/'submissions').glob('submission_*')))==1
