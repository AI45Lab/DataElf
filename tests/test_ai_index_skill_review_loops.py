import json
from pathlib import Path

from dataelf.config import DataElfConfig
from dataelf.discovery.contracts import DiscoveryContext,DiscoveryJob,DomainManifest,ExplorerRunResult
from dataelf.discovery.analysis_review.coordinator import run_analysis
from dataelf.discovery.analysis_review.tools import dispatch
from dataelf.ontology.capabilities import CLI_SKILL_REVIEW
from dataelf.ontology.collaboration import ReviewLedger
from tests.test_ai_index_analysis_review import setup,approve


def test_local_report_feedback_actual_repair_then_formal_acceptance(tmp_path):
    _,spec,contract=setup(tmp_path)
    spec=spec.model_copy(update={'workflow_policy':CLI_SKILL_REVIEW})
    (tmp_path/'prompts/start.md').write_text('Original user objective')
    original=json.loads((tmp_path/'insights/insight_candidates.json').read_text())
    bad=json.loads(json.dumps(original));bad['insight_candidates'][0]['thesis']='100 to 120 is 25%'
    (tmp_path/'insights/insight_candidates.json').write_text(json.dumps(bad))
    (tmp_path/'insights/final_brief.md').write_text('The increase is 25%.')
    ctx=DiscoveryContext(workspace_path=str(tmp_path),spec=spec,prompt_path=str(tmp_path/'prompts/start.md'),
        manifest=DomainManifest(domain='ai_index',version='1',display_name='AI',plugin='test'))
    problem={'issue_id':'denominator','nature':'confirmed_error','blocking':True,'severity':'low','category':'computation',
        'location':'insights/insight_candidates.json#/insight_candidates/0/thesis','message':'25% uses the wrong baseline',
        'evidence':['raw/values.json#'],'impact':'The key claim is wrong','required_change':'Correct the percentage',
        'acceptance_criteria':'Independent calculation and all report copies agree on 20%'}
    class Author:
        calls=0
        def run(self,job,context):
            self.calls+=1;p=Path(context.env['DATAELF_ANALYSIS_CONTEXT']);task=json.loads(p.read_text())
            if self.calls==1:
                result=dispatch(p,'review_request',{'object_kind':'report','paths':['insights/insight_candidates.json','tables/analysis.csv'],
                    'question':'Is this percentage supported?','evidence':['raw/values.json#'],'idempotency_key':'percentage-question'})
                assert result['handoff']['kind']=='local_review'
                assert not list((tmp_path/'submissions').glob('submission_*'))
            else:
                assert task['previous_review']['issues'][0]['issue_id']=='denominator'
                (tmp_path/'insights/insight_candidates.json').write_text(json.dumps(original))
                (tmp_path/'insights/final_brief.md').write_text('The increase is 20%, from 100 to 120.')
                dispatch(p,'review_respond',{'issue_id':'denominator','response':'accept','explanation':'Recomputed with the 100 baseline',
                    'evidence':['raw/values.json#'],'change_refs':['insights/insight_candidates.json','insights/final_brief.md']})
                assert ReviewLedger(tmp_path,'analysis').issues()['denominator']['status']=='unresolved'
                dispatch(p,'analysis_submit',{'idempotency_key':'complete-fixed'})
            return ExplorerRunResult(status='completed')
    class Reviewer:
        local=0;formal=0
        def run(self,job,context):
            p=Path(context.env['DATAELF_ANALYSIS_CONTEXT']);task=json.loads(p.read_text())
            assert context.agent_resources.skills
            if task['review_mode']=='local':
                self.local+=1
                inspected=dispatch(p,'analysis_inspect',{'resource':'submission'})
                assert inspected['review_request_id']==task['review_request_id'] and 'manifest_path' not in inspected
                assert not Path(task['review_snapshot'],'insights/final_brief.md').exists()
                checked=dispatch(p,'review_checks_save',{'method':'(after-before)/before','result':0.2,
                    'conclusion':'25% is wrong','coverage':'Percentage claim only','dependencies':['raw/values.json','tables/analysis.csv']})
                dispatch(p,'review_feedback_submit',{'request_id':task['review_request_id'],'answer':'Use the stated baseline',
                    'issues':[problem],'checks':[checked['check_id']]})
            else:
                self.formal+=1
                read=dispatch(p,'analysis_read',{'path':'insights/insight_candidates.json'})
                assert json.loads(read['text'])==original
                decision=approve(task['submission'],issue_updates=[{'issue_id':'denominator','status':'resolved',
                    'evidence':['raw/values.json','insights/insight_candidates.json'],'explanation':'Independently recomputed and checked both outputs'}])
                dispatch(p,'analysis_review_submit',decision)
            return ExplorerRunResult(status='completed')
    author=Author();reviewer=Reviewer()
    result,review=run_analysis(DiscoveryJob(job_id='local',spec=spec,workspace_path=str(tmp_path)),ctx,contract,
        DataElfConfig(),analyst=author,reviewer=reviewer)
    assert result.status=='completed',result
    assert author.calls==2 and reviewer.local==reviewer.formal==1
    ledger=ReviewLedger(tmp_path,'analysis')
    assert ledger.issues()['denominator']['status']=='resolved'
    assert len(ledger.responses())==1 and ledger.requests()[0]['status']=='delivered'
    assert review.metrics['review_rounds']==1  # Local feedback is not formal approval.
    saved=json.loads((tmp_path/'reviews/analysis/approved.json').read_text())
    assert saved['decision']['issue_updates'][0]['status']=='resolved'
    # Re-entering after installation is idempotent and creates no model calls.
    resumed,_=run_analysis(DiscoveryJob(job_id='local',spec=spec,workspace_path=str(tmp_path)),ctx,contract,
        DataElfConfig(),analyst=author,reviewer=reviewer)
    assert resumed.status=='completed' and author.calls==2 and reviewer.formal==1
