"""Observed acquisition coverage, separate from claims about unseen source data."""


def new_scan():
    return dict(records_returned=0, records_with_valid_date=0, records_in_date_range=0,
                records_after_retrieval=0, records_retained=0, requests_attempted=0,
                stop_reason=None, scan_complete=False)


def finish_scan(scan, *, complete, stop_reason, retained=0):
    scan.update(scan_complete=complete, stop_reason=stop_reason, records_retained=retained)
    if retained:
        observed = 'records_retained'
    elif not scan['records_returned']:
        observed = 'source_empty'
    elif not scan['records_with_valid_date']:
        observed = 'invalid_or_missing_dates'
    elif not scan['records_in_date_range']:
        observed = 'no_date_match_in_scanned_records'
    else:
        observed = 'strict_conditions_no_match'
    scan['observed_reason'] = observed
    scan['reason'] = 'scan_incomplete' if not complete else observed
    if stop_reason == 'source_request_failed':scan['reason'] = 'source_request_failed'
    return dict(scan)


def empty_result_reason(sources):
    """Legacy synthetic results without counters keep their old error code."""
    scans = [source.get('diagnostics') for source in sources.values()]
    if not scans or any(not scan for scan in scans):return 'no_source_data'
    reasons = {scan['reason'] for scan in scans}
    if 'scan_incomplete' in reasons:return 'ai_index_scan_incomplete'
    if len(reasons) == 1:return 'ai_index_' + reasons.pop()
    return 'ai_index_no_matching_records'


EMPTY_MESSAGES = {
    'ai_index_source_empty':'Requested sources returned no records.',
    'ai_index_invalid_or_missing_dates':'Returned records have no valid publication dates; date coverage cannot be established.',
    'ai_index_no_date_match_in_scanned_records':'No records in the scanned data match the requested date interval.',
    'ai_index_strict_conditions_no_match':'Records exist in the date interval, but none match the explicit strict retrieval conditions.',
    'ai_index_scan_incomplete':'The page limit was reached before coverage was complete; no matching records were retained.',
    'ai_index_no_matching_records':'No records were retained; consult per-source acquisition diagnostics for distinct causes.',
    'no_source_data':'Scope V2 produced no retained source records.',
}
