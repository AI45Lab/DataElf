"""Evidence-located review feedback, using existing candidate and session storage."""
from .errors import OntologyError
from .sources import digest


def check_review(review,previous,*,strict=False):
    """Validate the feedback protocol, never replace semantic reviewer judgment."""
    def invalid(message):
        raise OntologyError('ONTOLOGY_REVIEW_FEEDBACK_INVALID','format',[{'message':message,'recoverable':True}])
    if strict:
        from .review_policy import validate_issues
        try:validate_issues(review)
        except ValueError as exc:invalid(str(exc))
    resolved={'resolved','withdrawn'} if strict else {'resolved'}
    if review.verdict=='unusable':
        if review.blocker:
            raise OntologyError('ONTOLOGY_REVIEW_BLOCKED','review',[review.blocker.model_dump(mode='json')])
        if review.issues:review=review.model_copy(update={'verdict':'revise'})
        else:invalid('unusable requires an evidence-located genuine blocker; repairable defects require actionable revise issues')
    if review.blocker:invalid('A genuine blocker cannot accompany approve/revise')
    prior={i['issue_id']:i for i in (previous or {}).get('issues',[]) if i.get('blocking',True)}
    updates={u.issue_id:u for u in review.issue_updates}
    if len(updates)!=len(review.issue_updates):invalid('issue_updates must not duplicate issue_id')
    if set(prior)-set(updates):invalid('Assess every previous blocking issue_id with resolved/unresolved/regressed and concrete evidence')
    for issue in review.issues:
        if not issue.location or not issue.impact:invalid('Each issue requires a location and impact in addition to evidence and acceptance_criteria')
        if not issue.issue_id:issue.issue_id='issue-'+digest({'location':issue.location,'target':issue.target,'criteria':issue.acceptance_criteria})[:16]
    blocking=[i for i in review.issues if i.blocking]
    current_ids={i.issue_id for i in blocking}
    for key in prior:
        if updates[key].status not in resolved and key not in current_ids:invalid('Unresolved/regressed previous blocking issues must remain in current issues with the same issue_id')
        if updates[key].status in resolved and key in current_ids:invalid('A resolved issue cannot simultaneously remain blocking; report regression explicitly')
    if review.verdict=='approve' and (blocking or any(updates[k].status not in resolved for k in prior)):
        invalid('Approval cannot retain blocking issues or unresolved/regressed prior issues')
    if review.verdict=='revise' and not blocking:invalid('revise needs actionable blocking defects; nonblocking suggestions alone do not require remodeling')
    return review
