import json
from pathlib import Path
import pytest

from dataelf.ontology.capabilities import CLI_SKILL_REVIEW
from dataelf.ontology.collaboration import ReviewLedger
from dataelf.ontology.collaboration_tools import dispatch
from dataelf.ontology.contracts import OntologyRequest,Review
from dataelf.ontology.graph.store import write_json
from dataelf.ontology.review_loop import check_review


def context(tmp_path,role='modeler',**values):
    path=tmp_path/role/'context.json'
    request=OntologyRequest(domain='ai_index',workspace=tmp_path,workflow_policy=CLI_SKILL_REVIEW)
    obj={'request':request.model_dump(mode='json'),'role':role,'workflow_policy':CLI_SKILL_REVIEW,
         'checkpoint_directory':str(tmp_path/'checkpoint'),'source_fingerprint':'empty',**values}
    write_json(path,obj);return path,obj


def issue():
    return {'issue_id':'scope','nature':'confirmed_error','severity':'low','target':'contract','blocking':True,
        'location':'review_object.json#/namespace','message':'Observed namespace is wrong for this task',
        'evidence':['review_object.json#/namespace'],'impact':'References cannot be interpreted',
        'required_change':'Correct or provide counterevidence','acceptance_criteria':'References independently resolve'}


def test_incomplete_draft_feedback_handoff_response_and_withdrawal(tmp_path):
    author,ctx=context(tmp_path)
    requested=dispatch(author,'review_request',{'object_kind':'draft','question':'Can this incomplete identity be used?',
        'evidence':[],'idempotency_key':'draft-1'})
    assert requested['handoff']['kind']=='local_review' and not requested['publication_eligible']
    ledger=ReviewLedger(tmp_path,'graph');row=ledger.load(requested['handoff']['request_id'])
    # Empty/incomplete contracts are reviewable without materializing RDF.
    assert Path(row['snapshot_path'],'review_object.json').is_file()
    reviewer,rctx=context(tmp_path,'reviewer',review_family='graph',review_mode='local',
        review_request_id=row['request_id'],review_object_hash=row['object_hash'],review_snapshot=row['snapshot_path'])
    check=dispatch(reviewer,'review_checks_save',{'method':'Read the saved namespace','result':{'namespace':None},
        'conclusion':'Question requires clarification','coverage':'Namespace only','dependencies':['review_object.json']})
    feedback={'request_id':row['request_id'],'answer':'Please clarify this namespace using evidence',
        'issues':[issue()],'checks':[check['check_id']]}
    saved=dispatch(reviewer,'review_feedback_submit',feedback)
    assert saved['handoff']['kind']=='local_feedback'
    assert dispatch(reviewer,'review_feedback_submit',feedback)['saved']
    with pytest.raises(ValueError,match='immutable'):
        dispatch(reviewer,'review_feedback_submit',{**feedback,'answer':'Different answer'})
    response=dispatch(author,'review_respond',{'issue_id':'scope','response':'counterevidence',
        'explanation':'The source documents a different namespace','evidence':['review_object.json#/namespace']})
    assert response['issue_status_unchanged'] and ledger.issues()['scope']['status']=='unresolved'
    with pytest.raises(ValueError):
        check_review(Review(verdict='approve',summary='Looks fine'),ledger.prior(),strict=True)
    approved=check_review(Review(verdict='approve',summary='Counterevidence disproves the prior claim',issue_updates=[
        {'issue_id':'scope','status':'withdrawn','evidence':['review_object.json'],'explanation':'Rechecked original specification'}]),ledger.prior(),strict=True)
    ledger.record_review(approved.model_dump(mode='json'))
    assert ledger.issues()['scope']['status']=='withdrawn'
    assert not list(tmp_path.glob('ontology/graphs/*/revisions/*'))
    with pytest.raises(ValueError,match='not permitted'):
        dispatch(reviewer,'review_request',{'object_kind':'draft','question':'Recursive review','evidence':[],'idempotency_key':'nested'})


def test_snapshot_hash_idempotency_and_tamper(tmp_path):
    author,ctx=context(tmp_path)
    args={'object_kind':'draft','question':'Inspect identity','evidence':[],'idempotency_key':'same'}
    a=dispatch(author,'review_request',args);b=dispatch(author,'review_request',args)
    assert a==b
    with pytest.raises(ValueError,match='conflict'):
        dispatch(author,'review_request',{**args,'question':'Different question'})
    ledger=ReviewLedger(tmp_path,'graph');row=ledger.load(a['handoff']['request_id'])
    Path(row['snapshot_path'],'review_object.json').write_text('{}')
    with pytest.raises(ValueError,match='snapshot changed'):ledger.verify(row)


def test_low_blocking_and_confirmed_error_rules_are_policy_specific():
    with pytest.raises(ValueError):check_review(Review(verdict='approve',summary='Ignore',issues=[issue()]),None,strict=True)
    optional={**issue(),'blocking':False}
    with pytest.raises(ValueError):check_review(Review(verdict='approve',summary='Ignore',issues=[optional]),None,strict=True)
    suggestion={**optional,'nature':'suggestion'}
    assert check_review(Review(verdict='approve',summary='Correct with optional suggestion',issues=[suggestion]),None,strict=True).verdict=='approve'


def test_checks_reuse_requires_unchanged_dependencies(tmp_path):
    author,ctx=context(tmp_path)
    row=dispatch(author,'review_request',{'object_kind':'draft','question':'Read this draft','evidence':[],'idempotency_key':'one'})
    ledger=ReviewLedger(tmp_path,'graph');request=ledger.load(row['handoff']['request_id'])
    reviewer,rctx=context(tmp_path,'reviewer',review_snapshot=request['snapshot_path'])
    saved=dispatch(reviewer,'review_checks_save',{'method':'Read bytes','result':{},'conclusion':'Incomplete',
        'coverage':'Draft only','dependencies':['review_object.json']})
    ledger.validate_checks(rctx,[saved['check_id']])
    # Appending an unused source does not invalidate this check.
    from tests.test_ai_index_dynamic_sources import register
    register(tmp_path)
    ledger.validate_checks(rctx,[saved['check_id']])
    Path(request['snapshot_path'],'review_object.json').write_text('{}')
    with pytest.raises(ValueError,match='dependency changed'):ledger.validate_checks(rctx,[saved['check_id']])


def test_check_binds_source_query_conditions_as_well_as_response_bytes(tmp_path):
    from tests.test_ai_index_dynamic_sources import register
    from dataelf.domains.ai_index.evidence import EvidenceCatalog
    row=register(tmp_path);reviewer,ctx=context(tmp_path,'reviewer')
    result=dispatch(reviewer,'review_checks_save',{'method':'Read sourced value','result':0,'conclusion':'Zero is present',
        'coverage':'One source record','dependencies':[row['response_file']]})
    metadata=EvidenceCatalog(tmp_path).root/row['query_id']/'record.json'
    assert str(metadata.relative_to(tmp_path)) in result['dependencies']
    changed=json.loads(metadata.read_text());changed['params']={'different':'scope'};write_json(metadata,changed)
    with pytest.raises(ValueError,match='dependency changed'):
        ReviewLedger(tmp_path,'graph').validate_checks(ctx,[result['check_id']])
