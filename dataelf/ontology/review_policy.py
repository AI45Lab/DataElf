"""Shared strict acceptance rule, enabled only by the host-selected policy."""
def validate_issues(review):
    issues = review.issues
    for issue in issues:
        if 'nature' not in issue.model_fields_set:
            raise ValueError('Classify each issue explicitly as confirmed_error, question or suggestion')
        if issue.nature == 'confirmed_error' and not issue.blocking:
            raise ValueError('Confirmed errors require repair or evidenced withdrawal, regardless of severity')
        if issue.nature == 'suggestion' and issue.blocking:
            raise ValueError('Optional suggestions cannot block acceptance')
    if review.verdict == 'approve' and any(i.blocking for i in issues):
        raise ValueError('Approval contains blocking issues, regardless of severity')
