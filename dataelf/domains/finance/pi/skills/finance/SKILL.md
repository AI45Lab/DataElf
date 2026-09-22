---
name: finance
description: Analyze finance data through the finance domain tool set (relational, workspace files, SEC filings, XBRL facts, market prices, web sources) when an agent needs finance data; excludes any source outside the exposed tools, writes beyond the declared output contract, and unbounded dumps of raw data.
---

Use when the user requests finance data analysis, including multi-step work that
combines SQL, documents, filings, prices, and web sources.

For a finance job, use only the tools exposed in the prompt; the runtime composes
the effective tool set from the selected benchmark or the generic configuration
and it may include SQL tools, Python tools, web tools from the common layer, and
terminal access. Do not read the underlying database, SEC endpoints, or
workspace files through any path other than these tools.

For a relational source, start with `get_database_info` and `describe_table`.
Keep all SQL read-only, select explicit columns, and page with `limit`
(default 20 rows, hard cap 100 per `execute_query`). Filter by the requested
entity or company when applicable.

For files or documents, inspect with `list_files` (prefer `path`, `pattern`,
`recursive=false`; default 100 items, hard cap 500) and `get_field_description`
before loading. Keep non-trivial calculations in reproducible Python via
`execute_code`, printing summaries and samples rather than full datasets
(stdout capped at 20000 characters).

For SEC filings, find filings with `edgar_search` (record the accession number,
form, and filing date; default 10, hard cap 25 per call), then parse the
document with `parse_html_page` (by URL or cached workspace path) for bounded
text (default 12000 characters returned) and tables. Document URLs follow
`https://www.sec.gov/Archives/edgar/data/<cik>/<accession-no-dashes>/<primary_doc>`.

For SEC company data, use `company_profile` to resolve a ticker or CIK and
inspect recent submissions (default 10 filings, max 50), and `company_facts`
for XBRL facts with explicit taxonomy, concepts (at most 10), form, and date
filters (default 50 rows, hard cap 200). Retain accession, unit, period, and
filing metadata. Treat duplicate or restated facts as separate observations
until the reporting context is resolved.

For targeted filing extraction, pass `key` to `parse_html_page` to persist the
full text, then call `retrieve_information` with a prompt containing `{{key}}`.
Each key is capped at 16000 characters and the expanded prompt at 30000;
documents above the per-key limit require `input_character_ranges` windows
(12000 characters per call is a good working size). The tool makes one
helper-model call inside the existing Pi tool execution and returns only the
extraction plus `prompt_length`/`truncated` metadata — never the raw document
text.

For market prices, use `price_history` for OHLCV series (Yahoo Finance chart
data; plain tickers such as AAPL, BRK-B, 0700.HK; default 100 rows — prefer
weekly or monthly bars for long windows). State the symbol, interval, and
observation window for every figure.

For a web source, prefer `web_search` then `fetch_content` on primary pages;
record URLs and retrieval dates. PDF content is handled by `fetch_content`.
For terminal access, keep commands inside the authorized workspace and prefer
scripted, reproducible steps.

On multi-step tool chains, append a compact checkpoint to `notes/` after each
milestone — one short paragraph per step (params, key numbers, source URLs).
Never paste raw document text into notes; full text belongs in
`raw/finance/storage/` via `parse_html_page(key=...)`, which
`retrieve_information` expands without loading it into context. Before writing
`results/results.json`, re-read only the latest checkpoint or a note's summary
header, not the accumulated history.

Check `truncated`, `stdout_truncated`, and `truncated_cells` indicators before
interpreting results; a clipped value is not the true value. Verify units,
reporting period, scope, duplicate records, and denominators. Every final claim
must cite the query, analysis artifact, or source that supports it.

Write only the outputs declared in the prompt's output contract (payload plus
brief) and keep reusable calculations under `scripts/`.
