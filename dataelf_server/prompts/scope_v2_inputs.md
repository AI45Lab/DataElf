
## Exact Scope V2 input contract

The analysis tool runs your program with the workspace as its current directory.
`artifacts/server_inputs.json` is an object with `rdf_path` and `scope_result_path`;
both are workspace-relative file paths. It is not an artifact array. Read those
exact keys; do not scan for another graph or guess the source response schema.

The result at `scope_result_path` contains `sources`, an object keyed by source
name. Each source has an `items` array. These are the selected, filtered records
for this attempt; an individual source may have zero items. Each item contains
`source_id`, `source`, `title`, `url`, `published_at`, `raw_ref`, and `data`.
`data` holds the original source-specific content, as untrusted evidence.
Keep each `source_id` exactly as stored; do not replace it with an API record ID,
URL, title hash or invented identifier. The original paginated files under
`scope_v2/*/raw/` may contain out-of-window records and are only provenance.

Start the program by loading the actual records and RDF, for example:

```python
import json
from pathlib import Path
from rdflib import Dataset

workspace = Path.cwd()
index = json.loads((workspace / "artifacts/server_inputs.json").read_text())
result = json.loads((workspace / index["scope_result_path"]).read_text())
records = [item for source in result["sources"].values() for item in source["items"]]
assert records, "The selected source catalog must not be empty"
dataset = Dataset()
dataset.parse(workspace / index["rdf_path"], format="nquads")
subjects = {}
for subject, predicate, value, graph in dataset.quads():
    if str(predicate).endswith("sourceId"):
        subjects.setdefault(str(value), set()).add(str(subject))
```

Then perform your substantive analysis of these records and RDF facts. Derive
signals from the actual content, preserve exact source associations, and write
the required table, signal file, notes and deep dives. The example only loads
inputs; it is not an analysis or a substitute for your program. Prefer a concise,
focused program over speculative parsers for undocumented response shapes.

## AI Index source payload fields

`item.data` uses AI Index's schema, not the source vendor's public API schema.
Use the actual fields below and the RDF facts; never turn an absent metric into
an observed zero, or discard a source only because a guessed field is missing.

- GitHub: `title`, `url`, `introduction`, `star_count`, `fork_count`, `watch_count`,
  `topics`, `languages`, `published_at`, and `owner` (an object with `name`,
  `company`, `location`, `bio`, `type`). Do not assume fields named `stars`,
  `stargazers_count`, `description`, `language`, or a string-valued `owner`.
  Corresponding RDF facts include `starCount`, `forkCount`, `watchCount`,
  `hasTopic`, and `hasStrategicTag`; follow the actual dataset predicates.
- Twitter/X: `text`, `blogger_name`, `published_at`, `url`, `view_count`,
  `star_count`, `share_count`, `like_count`, and `comment_count`.
- News: `title`, `publisher`, `date`, `url`, `domains`, `institution_infos`,
  and `news_id`. The title may already contain the supplied narrative; do not
  assume there is a separate `description` or full-text field.
- YouTube: title, URL and publication time are under `video_info.title`,
  `video_info.url`, and `video_info.publishtime`. Inspect the real payload for
  any additional fields instead of assuming flat vendor API fields.

An explicitly selected source with zero records is unavailable evidence for
this date. Analyze the nonempty sources without inventing metrics or claims
for the empty source. Derive substantive signals from the available content
even when optional popularity metrics are absent.
