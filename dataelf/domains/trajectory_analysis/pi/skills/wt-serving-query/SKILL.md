---
name: wt-serving-query
description: Query read-only trajectory and evaluation records from WT Serving through the trajectory_analysis domain JSON CLI when an agent needs WT data; excludes Delivery, Landing, production, writes, and failure analysis.
---

Use when the user requests WT Serving trajectory or evaluation records, including
requests for failure trajectory data.

For a trajectory_analysis job, use the configured Python 3.11+ interpreter to execute
scripts importing `dataelf.domains.trajectory_analysis.client.TrajectoryClient`.
Read this Skill before making a call:
```python
from dataelf.domains.trajectory_analysis.client import TrajectoryClient
client = TrajectoryClient.from_env()
search = client.search_records(reward=0, limit=1)
if not search["isError"] and search["result"]["records"]:
    detail = client.get_record(search["result"]["records"][0]["id"], fields=["chosen_trace"])
```
Both methods return the unchanged JSON envelope;
`isError=true` means the read failed. The Client persists actual calls through the domain
recorder before returning. Do not call the lower-level bridge or SDK directly in a job.
Stop acquisition on error or empty search; do not print full responses or record IDs.
The analysis recorder allows at most one search and five gets (six attempts total per job).
First get requests chosen_trace. For a remaining gap, optionally get messages, response,
rejected_trace or meta_json, one new field per call and each at most once, using the same
selected record id. The Client preserves the selected job_id constraint when present and
checks returned locator fields. Restarting Python or constructing a new Client does not
reset the persisted budget. Stop on error or empty get; failed attempts count. Stop early
when sufficient. Do not repeat or reconstruct omitted fields or raise the 64 KiB limit.

For failure trajectory queries, use `reward=0`. Do not automatically add
`is_session_completed=true`; completed status does not participate in the current
failure definition.

Access Serving only. Delivery API, Landing, production and writes are forbidden.
Never supply table, endpoint, credential or raw SQL arguments. If the domain Python/SDK or WT
test credentials are not configured in the runtime environment, stop and report
that requirement; do not search for, print or guess credentials.

Check `truncated` and `omitted_fields` before interpreting results. An omitted
field is not null. Preserve WT list/dict/null structures and do not invent
`step_id` values from positions in the `chosen_trace` array. Do not locate failure
steps or analyze failure causes unless the user separately requests that work.
