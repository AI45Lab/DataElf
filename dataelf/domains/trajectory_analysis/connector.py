"""Bounded evidence capture/projection only. No WT SDK or query implementation."""
from __future__ import annotations

import fcntl
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from dataelf.discovery.artifacts import resolve_workspace_path

RAW = "raw/trajectory_analysis/tool_calls.json"
METADATA = "tables/trajectory_analysis/query_metadata.json"
SUMMARY = "reports/query_summary.json"
FIELDS = ("chosen_trace", "messages", "response", "rejected_trace", "meta_json")
MAX_CALLS = 6


class EvidenceError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise EvidenceError(code)


def read_json(workspace: Path, relative: str):
    return json.loads(resolve_workspace_path(workspace, relative).read_text(encoding="utf-8"))


def _write(workspace, relative, value):
    path = resolve_workspace_path(workspace, relative)
    # Fixed temporary name is contained too; replacement never follows a final symlink.
    temporary = resolve_workspace_path(workspace, relative + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def validate_arguments(tool, args):
    require(isinstance(args, dict), "QUERY_ARGUMENTS_INVALID")
    if tool == "wt_search_records":
        require({"reward", "limit"} <= set(args) <= {"reward", "limit", "job_id", "session_id"} and type(args['reward']) in (int, float)
                and args['reward'] == 0 and type(args['limit']) is int and args['limit'] == 1,
                "QUERY_SEARCH_ARGUMENTS")
        for key in ("job_id", "session_id"):
            if key in args:
                value = args[key]
                require(isinstance(value, str) and 0 < len(value) <= 1024
                        and all(ord(c) >= 32 and ord(c) != 127 for c in value),
                        "QUERY_SEARCH_ARGUMENTS")
    else:
        require(tool == "wt_get_record" and {'record_id', 'fields'} <= set(args) <= {'record_id', 'fields', 'job_id'}
                and isinstance(args['fields'], list) and len(args['fields']) == 1
                and args['fields'][0] in FIELDS
                and ('job_id' not in args or (isinstance(args['job_id'], str)
                     and 0 < len(args['job_id']) <= 1024
                     and all(ord(c) >= 32 and ord(c) != 127 for c in args['job_id']))) and isinstance(args['record_id'], str)
                and 0 < len(args['record_id']) <= 1024
                and all(ord(c) >= 32 and ord(c) != 127 for c in args['record_id']), "QUERY_GET_ARGUMENTS")


def envelope_result(envelope, transport):
    require(transport in ("ok", "error"), "QUERY_TRANSPORT_INVALID")
    if transport == "error":
        require(envelope is None, "QUERY_ENVELOPE_INVALID")
        return None
    require(isinstance(envelope, dict) and set(envelope) == {'isError', 'result'}
            and type(envelope['isError']) is bool, "QUERY_ENVELOPE_INVALID")
    if envelope['isError']:
        require(envelope['result'] == {'error': 'WT_READ_FAILED'}, "QUERY_ENVELOPE_INVALID")
        return None
    data = envelope['result']
    require(isinstance(data, dict) and set(data) == {'records', 'limit', 'truncated', 'omitted_fields', 'omitted_records'}, "QUERY_RESULT_INVALID")
    require(type(data['limit']) is int and data['limit'] == 1 and isinstance(data['records'], list)
            and len(data['records']) <= 1 and all(isinstance(r, dict) for r in data['records']), "QUERY_RESULT_INVALID")
    require(type(data['truncated']) is bool and type(data['omitted_records']) is int
            and data['omitted_records'] >= 0 and isinstance(data['omitted_fields'], list), "QUERY_RESULT_INVALID")
    for item in data['omitted_fields']:
        require(isinstance(item, dict) and set(item) == {'record_index', 'field'}
                and type(item['record_index']) is int and item['record_index'] == 0
                and item['field'] in {'messages', 'response', 'chosen_trace', 'rejected_trace', 'meta_json'}, "QUERY_RESULT_INVALID")
    require(data['truncated'] or not (data['omitted_records'] or data['omitted_fields']), "QUERY_RESULT_INVALID")
    return data


def project(call):
    data = envelope_result(call['envelope'], call['transport'])
    base = dict(call_id=call['call_id'], tool=call['tool'], success=data is not None,
                count=None, reward_zero_match=None, truncated=None, omitted_fields=None,
                omitted_records=None, chosen_trace=None)
    if data is None:
        return base
    rows = data['records']
    base.update(count=len(rows), reward_zero_match=(all(type(r.get('reward')) in (int, float)
                and r['reward'] == 0 for r in rows) if rows else None),
                truncated=data['truncated'], omitted_fields=data['omitted_fields'], omitted_records=data['omitted_records'])
    base['fields'] = {}
    if call['tool'] == 'wt_get_record' and rows:
        row = rows[0]
        for field in call['arguments']['fields']:
            exists = field in row
            value = row.get(field)
            omitted = {'record_index': 0, 'field': field} in data['omitted_fields']
            require(not (exists and omitted), 'QUERY_RESULT_INVALID')
            kind = ('missing' if not exists else 'null' if value is None else 'boolean' if isinstance(value, bool)
                    else 'array' if isinstance(value, list) else 'object' if isinstance(value, dict)
                    else 'string' if isinstance(value, str) else 'number')
            shape = dict(exists=exists, type=kind,
                length=len(value) if isinstance(value, (str, list, dict)) else None,
                state='omitted' if omitted else 'missing' if not exists else 'null' if value is None
                else 'empty' if isinstance(value, (str, list, dict)) and len(value) == 0 else 'present')
            base['fields'][field] = shape
            if field == 'chosen_trace':
                base['chosen_trace'] = shape
    return base


def validate_sequence(calls, *, complete=False):
    require(isinstance(calls, list) and len(calls) <= MAX_CALLS, 'QUERY_CALL_COUNT')
    selected = None
    requested = set()
    stopped = False
    for index, call in enumerate(calls):
        require(isinstance(call, dict) and set(call) == {
            'call_id', 'tool', 'arguments', 'parent_call_id', 'transport', 'envelope'},
            'QUERY_PROVENANCE_INVALID')
        require(not stopped, 'QUERY_ACQUISITION_STOPPED')
        require(call['call_id'] == f'call_{index + 1}'
                and call['parent_call_id'] == (None if index == 0 else 'call_1')
                and call['tool'] == ('wt_search_records' if index == 0 else 'wt_get_record'),
                'QUERY_CALL_ORDER')
        args = call['arguments']
        validate_arguments(call['tool'], args)
        if index:
            require(selected is not None and args['record_id'] == selected['id'],
                    'QUERY_RECORD_LINK_MISMATCH')
            if selected.get('job_id') is not None:
                require(args.get('job_id') == selected['job_id'], 'QUERY_RECORD_LINK_MISMATCH')
            field = args['fields'][0]
            require(field not in requested, 'QUERY_DUPLICATE_FIELD')
            require(index != 1 or field == 'chosen_trace', 'QUERY_FIRST_GET_FIELD')
            requested.add(field)
        data = envelope_result(call['envelope'], call['transport'])
        project(call)
        if data is None or not data['records']:
            stopped = True
            continue
        row = data['records'][0]
        require(type(row.get('reward')) in (int, float) and row['reward'] == 0, 'QUERY_REWARD_MISMATCH')
        if index == 0:
            require(isinstance(row.get('id'), str) and bool(row['id']), 'QUERY_RECORD_LINK_MISMATCH')
            require(all(row.get(key) == args[key]
                        for key in ("job_id", "session_id") if key in args),
                    "QUERY_SEARCH_FILTER_MISMATCH")
            selected = row
        else:
            for key in ('id', 'job_id', 'session_id', 'step_id'):
                if key in selected:
                    require(key in row and type(row[key]) is type(selected[key])
                            and row[key] == selected[key], 'QUERY_RECORD_LINK_MISMATCH')
    if complete:
        require(bool(calls), 'QUERY_CALL_COUNT')
        require(selected is None or len(calls) >= 2, 'QUERY_GET_REQUIRED')


@contextmanager
def capture_lock(workspace):
    resolve_workspace_path(workspace, RAW + '.lock')
    path = workspace / (RAW + '.lock')
    require(not path.is_symlink(), 'QUERY_LOCK_INVALID')
    # Refuse even an internal symlink for the lock, to keep one stable lock inode.
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def load_capture(workspace):
    path = resolve_workspace_path(workspace, RAW)
    raw = read_json(workspace, RAW) if path.exists() else {'calls': []}
    require(isinstance(raw, dict) and set(raw) == {'calls'}, 'QUERY_RAW_INVALID')
    validate_sequence(raw['calls'])
    return raw


def persist_capture(workspace, raw):
    _write(workspace, RAW, raw)
    _write(workspace, METADATA, {'calls': [project(item) for item in raw['calls']]})


def append_call(raw, tool, arguments, envelope, transport):
    calls = raw['calls']
    call = dict(call_id=f'call_{len(calls) + 1}', tool=tool, arguments=arguments,
                parent_call_id=None if not calls else calls[0]['call_id'], transport=transport, envelope=envelope)
    validate_sequence([*calls, call])
    calls.append(call)


def record_call(workspace: Path, tool: str, arguments: dict, envelope, transport='ok'):
    """Synthetic/recorder boundary; reject disallowed calls without inventing executions."""
    with capture_lock(workspace):
        raw = load_capture(workspace)
        append_call(raw, tool, arguments, envelope, transport)
        persist_capture(workspace, raw)


def record_fixture(workspace: Path, calls: list[dict]):
    for call in calls:
        record_call(workspace, call['tool'], call['arguments'], call['envelope'], call.get('transport', 'ok'))


def main():
    # Invoked by the existing WT extension only when prepare explicitly opts in.
    try:
        require(os.environ.get('DATAELF_TRAJECTORY_CAPTURE') == '1', 'QUERY_CAPTURE_DISABLED')
        candidate = Path(os.environ['DATAELF_JOB_WORKSPACE']).resolve()
        require(read_json(candidate, 'job_spec.json')['domain'] == 'trajectory_analysis', 'QUERY_WORKSPACE_INVALID')
        workspace = candidate
        request = json.loads(sys.stdin.buffer.read(80 * 1024))
        # Parse original CLI text here, without a JS parse/stringify round trip.
        envelope = json.loads(request['envelope']) if isinstance(request['envelope'], str) else request['envelope']
        record_call(workspace, request['tool'], request['arguments'], envelope, request['transport'])
    except Exception:
        # Latch capture failures when storage remains writable, so earlier successful
        # calls cannot make this task pass after a rejected/failed later capture.
        try:
            raw = read_json(workspace, RAW) if resolve_workspace_path(workspace, RAW).exists() else {'calls': []}
            raw['capture_failed'] = True
            _write(workspace, RAW, raw)
        except Exception:
            pass  # Missing/unwritable outputs also fail the existing artifact gate.
        # Do not stringify filesystem, JSON, credential or source-data exceptions.
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
