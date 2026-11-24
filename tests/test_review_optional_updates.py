import copy
import pytest
from dataelf.discovery.analysis_review.contracts import check_decision


def case():
    submission = {"submission_id":"s", "submission_hash":"h", "policy_version":"analysis-review.v1", "spec":{"domain":"ai_index", "workflow_policy":"ai_index_cli_skill_review_v1"}}
    previous = {"issues":[{"issue_id":ident, "blocking":blocking} for ident,blocking in (("b1",True),("b2",True),("b3",True),("n1",False),("n2",False))]}
    decision = {"submission_id":"s", "submission_hash":"h", "verdict":"approve", "task_assessment":"fulfilled", "summary":"Rechecked corrected facts and optional suggestions", "checks":[{"location":"report.md","method":"independent source comparison","evidence":["raw/data.json"],"result":True}], "issue_updates":[{"issue_id":ident,"status":"resolved","evidence":["report.md"],"explanation":"Independent check confirms correction"} for ident in ("b1","b2","b3","n1","n2")]}
    return submission,previous,decision


def test_all_known_updates_are_valid_without_weakening_blockers():
    s,p,d=case()
    assert len(check_decision(d,s,p).issue_updates)==5
    d["issue_updates"]=d["issue_updates"][:3]
    assert len(check_decision(d,s,p).issue_updates)==3


@pytest.mark.parametrize("fault,match",[("missing","previous blocker"),("duplicate","duplicate"),("unknown","known previous"),("unresolved","remain blocking"),("binding","exact submission")])
def test_invalid_reviews_still_rejected(fault,match):
    s,p,d=case()
    if fault=="missing":d["issue_updates"].pop(0)
    elif fault=="duplicate":d["issue_updates"].append(copy.deepcopy(d["issue_updates"][0]))
    elif fault=="unknown":d["issue_updates"][-1]["issue_id"]="invented"
    elif fault=="unresolved":d["issue_updates"][0]["status"]="unresolved"
    else:d["submission_hash"]="wrong"
    with pytest.raises(ValueError,match=match):check_decision(d,s,p)


def test_optional_status_does_not_force_escalation():
    s,p,d=case();d["issue_updates"][-1]["status"]="unresolved"
    d["issues"]=[{"issue_id":"n2","nature":"suggestion","blocking":False,"severity":"low","category":"clarity","location":"report.md","message":"Optional presentation improvement","evidence":["report.md"],"impact":"Readability only","required_change":"Optional rewording","acceptance_criteria":"Meaning stays accurate"}]
    assert check_decision(d,s,p).verdict=="approve"
    d["issues"]=[]
    with pytest.raises(ValueError,match="optional issue"):check_decision(d,s,p)
