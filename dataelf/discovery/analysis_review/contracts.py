from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

POLICY_VERSION = 'ai-index-analysis.v1'


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Issue(Strict):
    nature: Literal['confirmed_error','question','suggestion'] = 'question'
    issue_id: str = Field(min_length=1)
    blocking: bool
    severity: Literal['low', 'medium', 'high']
    category: str = Field(min_length=1)
    location: str = Field(min_length=1)
    message: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    impact: str = Field(min_length=1)
    required_change: str = Field(min_length=1)
    acceptance_criteria: str = Field(min_length=1)


class IssueUpdate(Strict):
    issue_id: str
    status: Literal['resolved', 'unresolved', 'regressed', 'withdrawn']
    evidence: list[str] = Field(min_length=1)
    explanation: str = Field(min_length=1)


class Check(Strict):
    location: str = Field(min_length=1)
    method: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    result: Any
    coverage: str = 'Specified claim only; not an assertion of exhaustive checking'


class ReviewDecision(Strict):
    schema_version: Literal['dataelf.analysis.review.v1'] = 'dataelf.analysis.review.v1'
    submission_id: str
    submission_hash: str
    review_policy_version: Literal['ai-index-analysis.v1'] = POLICY_VERSION
    verdict: Literal['approve', 'revise', 'blocked']
    task_assessment: Literal['fulfilled', 'limited', 'unfulfilled']
    summary: str = Field(min_length=1)
    checks: list[Check] = Field(min_length=1)
    issues: list[Issue] = Field(default_factory=list)
    issue_updates: list[IssueUpdate] = Field(default_factory=list)
    blocker: dict[str, Any] | None = None

    @model_validator(mode='after')
    def coherent(self):
        blockers=[i for i in self.issues if i.blocking]
        if self.verdict == 'approve' and blockers:
            raise ValueError('Approval cannot contain blocking issues')
        if self.verdict == 'revise' and not blockers:
            raise ValueError('Revise needs a concrete blocking issue; suggestions alone do not block')
        if self.verdict == 'blocked':
            if not self.blocker or any(not self.blocker.get(k) for k in ['condition','attempted_checks','resume_when']):
                raise ValueError('Blocked requires external condition, attempted_checks and resume_when')
        elif self.blocker:
            raise ValueError('Only blocked may carry an external blocker')
        if len({i.issue_id for i in self.issues}) != len(self.issues):
            raise ValueError('Issue IDs must be unique')
        return self


def check_decision(decision, submission, previous=None):
    decision=ReviewDecision.model_validate(decision)
    from dataelf.ontology.capabilities import skill_review
    if skill_review(submission.get('spec',{})):
        from dataelf.ontology.review_policy import validate_issues
        validate_issues(decision)
        if decision.verdict=='approve' and decision.task_assessment!='fulfilled':
            raise ValueError('Incomplete work requires actionable revise or an evidenced external blocker')
    if decision.submission_id != submission['submission_id'] or decision.submission_hash != submission['submission_hash']:
        raise ValueError('Review is not bound to the exact submission')
    prior={i['issue_id'] for i in (previous or {}).get('issues',[]) if i['blocking']}
    updates={u.issue_id:u for u in decision.issue_updates}
    if len(updates)!=len(decision.issue_updates) or set(updates)!=prior:
        raise ValueError('Each previous blocker needs exactly one evidenced issue update')
    current={i.issue_id for i in decision.issues if i.blocking}
    for ident,update in updates.items():
        unresolved=update.status in {'unresolved','regressed'}
        if unresolved != (ident in current):
            raise ValueError('Unresolved/regressed issues must remain blocking; resolved/withdrawn issues must not')
    return decision
