import json
from pathlib import Path

import pytest

from dataelf.config import DataElfConfig
from dataelf.discovery.contracts import (DiscoveryContext,DiscoveryJob,DomainManifest,JobSpec,OutputContract,OutputArtifactSpec,ExplorerRunResult)
from dataelf.discovery.analysis_review.contracts import check_decision
from dataelf.discovery.analysis_review.submissions import freeze,install,verify
from dataelf.discovery.analysis_review.tools import dispatch,compute,schemas
from dataelf.discovery.analysis_review.coordinator import run_analysis
from dataelf.domains.ai_index.review import insight_output_schema
from dataelf.ontology.graph.store import write_json


def setup(workspace):
    workspace.mkdir(parents=True,exist_ok=True)
    for folder in ['raw','tables','scripts','notes','deep_dives','insights','prompts','logs']:(workspace/folder).mkdir(exist_ok=True)
    (workspace/'raw/values.json').write_text('{"before":100,"after":120}')
    (workspace/'tables/analysis.csv').write_text('before,after\n100,120\n')
    (workspace/'deep_dives/check.md').write_text('The increase is (120-100)/100 = 20%; not a population trend.')
    (workspace/'insights/candidate_signals.json').write_text(json.dumps({'candidate_signals':[{'signal_id':'s1','summary':'A measured change in the frozen example'}]}))
    insight={'insight_id':'i1','title':'A bounded change','thesis':'100 to 120 corresponds to 20% in this sample.',
        'why_now':'This is the supplied observation window, not a claim of historical novelty.',
        'supporting_signals':['s1'],'analysis_artifacts':[{'path':'tables/analysis.csv','role':'calculation'},'deep_dives/check.md'],
        'related_entities':[],'external_support':[{'path':'raw/values.json','title':'Frozen evidence','extra_preserved':'yes'}],
        'counterarguments':['Only this sample is observed'],'confidence':0.7,'next_questions':[]}
    (workspace/'insights/insight_candidates.json').write_text(json.dumps({'insight_candidates':[insight]}))
    (workspace/'insights/final_brief.md').write_text('The sample change is 20%, from 100 to 120. See raw/values.json; scope is limited to the given sample.')
    contract=OutputContract(contract_id='ai_index.insight_discovery',version='2',artifacts=[
        OutputArtifactSpec(artifact_id='candidate_signals',path='insights/candidate_signals.json',kind='candidate_signals',media_type='application/json',json_root='candidate_signals'),
        OutputArtifactSpec(artifact_id='insight_candidates',path='insights/insight_candidates.json',kind='insight_candidates',media_type='application/json',json_root='insight_candidates',json_schema=insight_output_schema(new_submission=True)),
        OutputArtifactSpec(artifact_id='final_brief',path='insights/final_brief.md',kind='final_brief',media_type='text/markdown')])
    spec=JobSpec(domain='ai_index',objective='Compare the observed values',workflow_policy='ai_index_cli_skill_review_v1',parameters={'expected_outputs':3})
    context={'role':'analyst','workspace':str(workspace),'spec':spec.model_dump(mode='json'),'contract':contract.model_dump(mode='json'),'graph_refs':[],'protected_hashes':{}}
    return context,spec,contract


def approve(submission,**kw):
    return dict(submission_id=submission['submission_id'],submission_hash=submission['submission_hash'],
        verdict='approve',task_assessment='fulfilled',summary='Independent recomputation supports the sample conclusion',
        checks=[{'location':'tables/analysis.csv','method':'(120-100)/100','result':0.2,'evidence':['raw/values.json']}],**kw)


def test_submission_full_content_idempotency_references_and_tamper(tmp_path):
    context,spec,contract=setup(tmp_path);submission=freeze(context,'first')
    assert freeze(context,'first')==submission
    snapshot=verify(tmp_path,submission)
    assert json.loads((snapshot/'insights/insight_candidates.json').read_text())['insight_candidates'][0]['external_support'][0]['extra_preserved']=='yes'
    outputs=install(tmp_path,submission,approve(submission))
    assert len(outputs)==3
    (tmp_path/'insights/final_brief.md').write_text('changed')
    with pytest.raises(ValueError,match='changed after submission'):install(tmp_path,submission,approve(submission))
    with pytest.raises(ValueError,match='idempotency key conflict'):freeze(context,'first')


def test_wrong_root_and_missing_signal_are_repairable_not_renamed(tmp_path):
    context,_,_=setup(tmp_path);path=tmp_path/'insights/candidate_signals.json';original=path.read_text()
    path.write_text('{"signals":[]}')
    with pytest.raises(ValueError,match='candidate_signals'):freeze(context,'a')
    assert json.loads(path.read_text())=={'signals':[]}
    path.write_text(original);p=tmp_path/'insights/insight_candidates.json';data=json.loads(p.read_text());data['insight_candidates'][0]['supporting_signals']=['nonexistent'];p.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='unknown signal'):freeze(context,'b')


def test_issue_lifecycle_no_style_blockers_and_exact_binding(tmp_path):
    context,_,_=setup(tmp_path);s=freeze(context,'first')
    with pytest.raises(ValueError,match='exact submission'):check_decision({**approve(s),'submission_hash':'other'},s)
    with pytest.raises(ValueError,match='concrete blocking'):check_decision({**approve(s),'verdict':'revise'},s)
    previous={'issues':[{'issue_id':'denominator','blocking':True}]}
    with pytest.raises(ValueError,match='previous blocker'):check_decision(approve(s),s,previous)
    decision=check_decision(approve(s,issue_updates=[{'issue_id':'denominator','status':'withdrawn','evidence':['raw/values.json'],'explanation':'Analyst counterevidence corrected the reviewer assumption'}]),s,previous)
    assert decision.verdict=='approve'


def test_reviewer_has_no_write_tools_and_read_cannot_escape_snapshot(tmp_path):
    context,_,_=setup(tmp_path);s=freeze(context,'first');ctx=tmp_path/'review/context.json';write_json(ctx,{**context,'role':'reviewer','submission':s})
    for tool in ['analysis_submit','write','bash','graph_update']:
        with pytest.raises(ValueError,match='not permitted'):dispatch(ctx,tool,{})
    with pytest.raises(ValueError,match='escapes'):dispatch(ctx,'analysis_read',{'path':'../../raw/values.json'})
    assert 'analysis_review_submit' in schemas('reviewer')


def test_isolated_recomputation_denies_snapshot_write_network_and_host_read(tmp_path):
    snapshot=tmp_path/'snapshot';snapshot.mkdir();(snapshot/'x.json').write_text('{"before":100,"after":120}')
    result=compute(snapshot,tmp_path/'scratch', '''import json, socket, os
x=json.loads((SNAPSHOT/'x.json').read_text())
print((x['after']-x['before'])/x['before'])
for attempt in [lambda:(SNAPSHOT/'x.json').write_text('bad'), lambda:socket.socket(), lambda:open('/etc/passwd').read()]:
 try: attempt(); print('ESCAPED')
 except (PermissionError,FileNotFoundError): print('blocked')
(SCRATCH/'answer.json').write_text('{"increase":0.2}')
''')
    assert result['exit_code']==0,result
    assert result['stdout']=='0.2\nblocked\nblocked\nblocked\n'
    assert (snapshot/'x.json').read_text()=='{"before":100,"after":120}'
    assert (tmp_path/'scratch/answer.json').is_file()


def test_coordinator_runs_past_old_repair_limits_and_preserves_complete_outputs(tmp_path):
    base,spec,contract=setup(tmp_path);(tmp_path/'prompts/start.md').write_text('Original goal and evidence scope')
    job=DiscoveryJob(job_id='job_test',spec=spec,workspace_path=str(tmp_path))
    ctx=DiscoveryContext(workspace_path=str(tmp_path),spec=spec,manifest=DomainManifest(domain='ai_index',version='1',display_name='AI',plugin='test'),prompt_path=str(tmp_path/'prompts/start.md'))
    class Analyst:
        def __init__(self):self.calls=0
        def run(self,job,context):
            self.calls+=1;p=Path(context.env['DATAELF_ANALYSIS_CONTEXT'])
            dispatch(p,'analysis_submit',{'idempotency_key':str(self.calls)})
            return ExplorerRunResult(status='completed')
    class Reviewer:
        def __init__(self):self.calls=0
        def run(self,job,context):
            self.calls+=1;p=Path(context.env['DATAELF_ANALYSIS_CONTEXT']);task=json.loads(p.read_text());s=task['submission'];d=approve(s)
            if task.get('previous_review'):
                d['issue_updates']=[{'issue_id':'check','status':'resolved' if self.calls==8 else 'unresolved','evidence':['raw/values.json'],'explanation':'Repeated controlled repair sequence'}]
            if self.calls<8:
                d.update(verdict='revise',issues=[{'issue_id':'check','blocking':True,'severity':'high','category':'computation','nature':'confirmed_error','location':'tables/analysis.csv','message':'Controlled unresolved numerical check','evidence':['raw/values.json'],'impact':'Core result depends on it','required_change':'Resolve the demonstrated discrepancy or provide counterevidence','acceptance_criteria':'Independent check agrees'}])
            dispatch(p,'analysis_review_submit',d);return ExplorerRunResult(status='completed')
    analyst=Analyst();reviewer=Reviewer();result,review=run_analysis(job,ctx,contract,DataElfConfig(),analyst=analyst,reviewer=reviewer)
    assert result.status=='completed' and review.status=='pass'
    assert analyst.calls==reviewer.calls==8
    assert json.loads((tmp_path/'insights/insight_candidates.json').read_text())['insight_candidates'][0]['insight_id']=='i1'
    assert len(json.loads((tmp_path/'reviews/analysis/history.json').read_text()))==8


def test_graph_ref_normalization_and_explicit_empty_submission_scope(tmp_path):
    _,spec,contract=setup(tmp_path);(tmp_path/'prompts/start.md').write_text('Read the original scope')
    job=DiscoveryJob(job_id='scope',spec=spec,workspace_path=str(tmp_path))
    ctx=DiscoveryContext(workspace_path=str(tmp_path),spec=spec,prompt_path=str(tmp_path/'prompts/start.md'),
        manifest=DomainManifest(domain='ai_index',version='1',display_name='AI',plugin='test'),
        domain_context={'graph_ref':{'graph_id':'g','revision_id':'r','workspace_id':None}})
    class Analyst:
        def run(self,job,context):
            p=Path(context.env['DATAELF_ANALYSIS_CONTEXT']);task=json.loads(p.read_text())
            assert task['graph_refs']==[{'graph_id':'g','revision_id':'r'}]
            dispatch(p,'analysis_submit',{'idempotency_key':'raw-only','graph_refs':[]})
            return ExplorerRunResult(status='completed')
    class Reviewer:
        def run(self,job,context):
            p=Path(context.env['DATAELF_ANALYSIS_CONTEXT']);task=json.loads(p.read_text())
            assert task['graph_refs']==[]
            assert not any(k.startswith('analysis_graph_') for k in json.loads((p.parent/'tools.json').read_text()))
            dispatch(p,'analysis_review_submit',approve(task['submission']))
            return ExplorerRunResult(status='completed')
    result,_=run_analysis(job,ctx,contract,DataElfConfig(),analyst=Analyst(),reviewer=Reviewer())
    assert result.status=='completed'


def test_cancel_is_journaled_without_automatic_recovery(tmp_path):
    from dataelf.discovery.run_control import RunCancelled
    _,spec,contract=setup(tmp_path);(tmp_path/'prompts/start.md').write_text('Original task')
    job=DiscoveryJob(job_id='cancel',spec=spec,workspace_path=str(tmp_path))
    ctx=DiscoveryContext(workspace_path=str(tmp_path),spec=spec,prompt_path=str(tmp_path/'prompts/start.md'),
        manifest=DomainManifest(domain='ai_index',version='1',display_name='AI',plugin='test'))
    class Cancel:
        calls=0
        def run(self,*args):
            self.calls+=1
            raise RunCancelled('Explicit cancellation')
    actor=Cancel()
    with pytest.raises(RunCancelled):run_analysis(job,ctx,contract,DataElfConfig(),analyst=actor)
    assert actor.calls==1
    assert json.loads((tmp_path/'reviews/analysis/state.json').read_text())['phase']=='cancelled'
    assert (tmp_path/'insights/insight_candidates.json').is_file()


def test_native_argument_guard_abort_keeps_files_and_can_continue(tmp_path):
    _,spec,contract=setup(tmp_path);(tmp_path/'prompts/start.md').write_text('Original task')
    job=DiscoveryJob(job_id='native-interrupted',spec=spec,workspace_path=str(tmp_path))
    ctx=DiscoveryContext(workspace_path=str(tmp_path),spec=spec,prompt_path=str(tmp_path/'prompts/start.md'),
        manifest=DomainManifest(domain='ai_index',version='1',display_name='AI',plugin='test'))
    class Analyst:
        calls=0
        def run(self,job,context):
            self.calls+=1;p=Path(context.env['DATAELF_ANALYSIS_CONTEXT'])
            if self.calls==1:
                write_json(p.parent/'analysis_transport.json',{'code':'ONTOLOGY_TOOL_ARGUMENTS_INCOMPLETE',
                    'issues':[{'stop_reason':'length','error_message':None}]})
                return ExplorerRunResult(status='failed',error_code='PI_PROCESS_NONZERO_EXIT:143')
            assert json.loads(p.read_text())['feedback']['reason']=='ONTOLOGY_TOOL_ARGUMENTS_INCOMPLETE'
            dispatch(p,'analysis_submit',{'idempotency_key':'after-partial-call'})
            return ExplorerRunResult(status='completed')
    class Reviewer:
        def run(self,job,context):
            p=Path(context.env['DATAELF_ANALYSIS_CONTEXT']);task=json.loads(p.read_text())
            dispatch(p,'analysis_review_submit',approve(task['submission']));return ExplorerRunResult(status='completed')
    actor=Analyst();result,_=run_analysis(job,ctx,contract,DataElfConfig(),analyst=actor,reviewer=Reviewer())
    assert actor.calls==2 and result.status=='completed'


def test_markup_is_diagnostic_only_and_stops_incompatible_channel(tmp_path):
    from dataelf.discovery.analysis_review.coordinator import evidence_excerpt
    _,spec,contract=setup(tmp_path);(tmp_path/'prompts/start.md').write_text('Original task')
    job=DiscoveryJob(job_id='markup',spec=spec,workspace_path=str(tmp_path))
    ctx=DiscoveryContext(workspace_path=str(tmp_path),spec=spec,prompt_path=str(tmp_path/'prompts/start.md'),
        manifest=DomainManifest(domain='ai_index',version='1',display_name='AI',plugin='test'))
    class Markup:
        calls=0
        def run(self,*_):
            self.calls+=1
            event={'type':'message_end','message':{'role':'assistant','stopReason':'stop','content':[
                {'type':'text','text':'<tool_call>{"name":"analysis_submit","arguments":{"idempotency_key":"fake"}}</tool_call>'}]}}
            (tmp_path/'logs/pi_events.jsonl').write_text(json.dumps(event)+'\n')
            return ExplorerRunResult(status='completed')
    actor=Markup();result,_=run_analysis(job,ctx,contract,DataElfConfig(),analyst=actor)
    assert result.status=='failed' and result.error_code=='PI_NATIVE_TOOL_CHANNEL_INCOMPATIBLE'
    assert actor.calls==1 and not (tmp_path/'submissions').exists()
    assert evidence_excerpt(tmp_path/'logs/pi_events.jsonl')['usage_unknown_responses']==1
