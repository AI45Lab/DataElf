"""Submit jobs together, poll them, and validate their public results."""
from __future__ import annotations
import argparse
import json
import time
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path


def call(base, method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    request = urllib.request.Request(base.rstrip('/') + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if urllib.parse.urlsplit(base).hostname in {"127.0.0.1", "localhost", "::1"} else urllib.request.build_opener()
    try:
        response = opener.open(request, timeout=30)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        value = json.load(response)
        if set(value) != {"code", "msg", "trace_id", "data"}:
            raise ValueError("Invalid API envelope")
        return response.status, value


def validate_insights(insights):
    if not isinstance(insights, list) or not insights:
        raise ValueError("Expected a positive number of public Insights")
    for insight in insights:
        if set(insight) != {"insight_id", "title", "content", "sources"}:
            raise ValueError("Invalid public Insight fields")
        if not all(isinstance(insight[key], str) and insight[key].strip() for key in ("insight_id", "title", "content")):
            raise ValueError("Empty public Insight field")
        if not insight["sources"] or any(set(s) != {"title", "url"} or not s["title"] or not s["url"] for s in insight["sources"]):
            raise ValueError("Missing public source")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--query', action='append', help='Repeat to submit multiple queued jobs')
    parser.add_argument('--queries-file', type=Path, help='JSON array of {query: ...} objects for repeatable acceptance tests')
    parser.add_argument('--timeout', type=float, default=7200)
    parser.add_argument('--poll-interval', type=float, default=5)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    queries = args.query or []
    if args.queries_file:
        values = json.loads(args.queries_file.read_text(encoding='utf-8'))
        if not isinstance(values, list) or any(not isinstance(item, dict) or not isinstance(item.get('query'), str) or not item['query'].strip() for item in values):
            parser.error('--queries-file must contain an array of nonblank query objects')
        queries.extend(item['query'] for item in values)
    records = []
    for query in queries or ['生成综合总结', '生成快讯模块总结']:
        code, submitted = call(args.base_url, 'POST', '/api/v1/insight/jobs', {'query': query})
        if code != 202:
            raise RuntimeError(f'Submission failed ({code}): {submitted["msg"]}')
        records.append({'query': query, 'submission': submitted, 'history': []})
    pending = list(records)
    deadline = time.monotonic() + args.timeout
    while pending and time.monotonic() < deadline:
        for record in list(pending):
            job_id = record['submission']['data']['job_id']
            _, status = call(args.base_url, 'GET', f'/api/v1/insight/jobs/{job_id}')
            state = status['data']
            summary = {k: state[k] for k in ('status', 'stage', 'progress', 'started_at')}
            if not record['history'] or record['history'][-1] != summary:
                record['history'].append(summary)
                print(job_id, summary['status'], summary['stage'], flush=True)
            if state['status'] in {'completed', 'failed'}:
                code, result = call(args.base_url, 'GET', f'/api/v1/insight/jobs/{job_id}/result')
                record.update(status=status, result_status=code, result=result)
                if code == 200:
                    validate_insights(result['data']['insights'])
                    print(job_id, 'insights:', len(result['data']['insights']), flush=True)
                pending.remove(record)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps({'jobs': records, 'pending': len(pending)}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if pending:
            time.sleep(args.poll_interval)
    report = {'jobs': records, 'timed_out': bool(pending)}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if not pending and all(r.get('result_status') == 200 for r in records) else 1


if __name__ == '__main__':
    raise SystemExit(main())
