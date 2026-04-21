"""EnzymeAcquire — cross-database enzyme attribute retrieval (UniProt / KEGG / PubChem)."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from threading import Semaphore
from typing import Any, Literal

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from tools.scitools.sources import (
    fetch_by_id, fetch_by_name, fetch_enzyme,
    fetch_smiles as _fetch_smiles_raw, parse_entry,
)

OUTPUT_DIR = Path(os.environ.get("SDC_OUTPUT_DIR", "/tmp/sdc_outputs")) / "bio"

OUTPUT_COLUMNS = [
    "query", "input_type", "uniprot_id", "protein_name",
    "gene_name", "organism", "ec_number",
    "reactions", "substrates", "products", "pathways",
    "substrate_smiles", "sequence", "seq_length", "source_db",
]

_MAX_QUERY_LEN   = 200
_ALLOWED_PATTERN = re.compile(r"^[\w\s\.\:\-\/\(\)]+$")
_API_SEMAPHORE   = Semaphore(4)

logger = logging.getLogger("EnzymeAcquire")


# --- Validation ---

def validate_queries(queries: list[str]) -> tuple[list[str], list[dict]]:
    """Filter empty, oversized, invalid-char, and duplicate queries."""
    valid, skipped, seen = [], [], set()
    for raw in queries:
        q = raw.strip()
        if not q:
            skipped.append({"query": repr(raw), "reason": "EMPTY_INPUT"})
        elif len(q) > _MAX_QUERY_LEN:
            skipped.append({"query": q[:60] + "…", "reason": f"TOO_LONG (len={len(q)})"})
        elif not _ALLOWED_PATTERN.match(q):
            skipped.append({"query": q, "reason": "INVALID_CHARACTERS"})
        elif q.lower() in seen:
            skipped.append({"query": q, "reason": "DUPLICATE_QUERY"})
        else:
            seen.add(q.lower())
            valid.append(q)
    return valid, skipped


def _classify_input(query: str) -> str:
    q = query.strip()
    if re.match(r"^[OPQ][0-9][A-Z0-9]{3}[0-9]$|^[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}$", q):
        return "uniprot_id"
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", q):
        return "ec_number"
    if re.match(r"^[a-z]{2,4}:\S+$", q, re.IGNORECASE):
        return "kegg_id"
    return "name"


# --- Retry / cached fetchers ---

def _with_retry(fn, *args, max_attempts: int = 3, base_delay: float = 1.0, **kwargs):
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            delay = base_delay * (2 ** attempt)
            logger.warning("Attempt %d/%d failed — retrying in %.1fs:\n%s",
                           attempt + 1, max_attempts, delay, traceback.format_exc())
            time.sleep(delay)
    raise RuntimeError(f"All {max_attempts} attempts failed") from last_exc


@lru_cache(maxsize=256)
def _fetch_by_id(uid: str):
    with _API_SEMAPHORE:
        return fetch_by_id(uid)


@lru_cache(maxsize=256)
def _fetch_enzyme(ec: str):
    with _API_SEMAPHORE:
        return fetch_enzyme(ec)


@lru_cache(maxsize=512)
def _fetch_smiles(substrate: str) -> str:
    with _API_SEMAPHORE:
        return _fetch_smiles_raw(substrate) or ""


def _fetch_by_name(query: str, max_results: int):
    with _API_SEMAPHORE:
        return fetch_by_name(query, max_results)


# --- Single-query fetch ---

def _acquire_single(query: str, max_results: int, do_smiles: bool, do_kegg: bool) -> list[dict]:
    input_type = _classify_input(query)
    records: list[dict] = []

    if input_type == "uniprot_id":
        entry = _with_retry(_fetch_by_id, query)
        uniprot_entries = [entry] if entry else []
    elif input_type in ("name", "ec_number"):
        q = f"ec:{query}" if input_type == "ec_number" else query
        uniprot_entries = _with_retry(_fetch_by_name, q, max_results)
    else:
        uniprot_entries = []

    for entry in uniprot_entries:
        rec = parse_entry(entry)
        if do_kegg and rec.get("ec_number"):
            ec = rec["ec_number"].split(";")[0].strip()
            try:
                rec.update(_with_retry(_fetch_enzyme, ec))
            except Exception:
                logger.warning("KEGG enrichment failed ec=%r\n%s", ec, traceback.format_exc())
                rec["kegg_ec"] = "fetch_failed"
        if do_smiles and rec.get("substrates"):
            sub = rec["substrates"].split(";")[0].strip()
            try:
                rec["substrate_smiles"] = _with_retry(_fetch_smiles, sub) if sub else ""
            except Exception:
                logger.warning("SMILES fetch failed substrate=%r\n%s", sub, traceback.format_exc())
                rec["substrate_smiles"] = ""
        else:
            rec["substrate_smiles"] = ""
        records.append(rec)

    if input_type == "kegg_id" and not records:
        ec = query.replace("ec:", "")
        try:
            kegg_data = _with_retry(_fetch_enzyme, ec)
            if kegg_data:
                records.append({
                    "uniprot_id": "", "protein_name": "", "gene_name": "",
                    "organism": "", "ec_number": ec, "sequence": "",
                    "seq_length": None, "source_db": "KEGG",
                    "substrate_smiles": "", **kegg_data,
                })
        except Exception:
            logger.warning("KEGG direct fetch failed query=%r\n%s", query, traceback.format_exc())

    logger.info("query=%r → %d record(s)", query, len(records))
    return records


# --- Batch orchestration ---

def _acquire_batch(
    queries: list[str], max_results: int, do_smiles: bool,
    do_kegg: bool, max_workers: int,
) -> tuple[list[dict], list[dict]]:
    results_map: dict[str, list[dict]] = {}
    errors: list[dict] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_acquire_single, q, max_results, do_smiles, do_kegg): q
                   for q in queries}
        for future in as_completed(futures):
            q = futures[future]
            try:
                records = future.result()
                if records:
                    for rec in records:
                        rec["query"]      = q
                        rec["input_type"] = _classify_input(q)
                    results_map[q] = records
                else:
                    errors.append({"query": q, "error": "NO_RESULTS",
                                   "repair_hint": f"No results found for query {q!r}."})
            except RuntimeError as exc:
                tb = traceback.format_exc()
                logger.error("Network error query=%r. Raw value: %r\n%s", q, q, tb)
                errors.append({"query": q, "error": "NETWORK_ERROR",
                               "repair_hint": str(exc), "traceback": tb})
            except Exception:
                tb = traceback.format_exc()
                logger.error("Unexpected error query=%r. Raw value: %r\n%s", q, q, tb)
                errors.append({"query": q, "error": "UNEXPECTED_ERROR",
                               "repair_hint": "See traceback field.", "traceback": tb})

    return [rec for q in queries for rec in results_map.get(q, [])], errors


# --- Post-processing ---

def _dedup_records(records: list[dict]) -> tuple[list[dict], int]:
    """Deduplicate by uniprot_id; empty-uid (KEGG-only) entries are always kept."""
    seen, deduped = set(), []
    for rec in records:
        uid = rec.get("uniprot_id", "").strip()
        if uid and uid in seen:
            continue
        if uid:
            seen.add(uid)
        deduped.append(rec)
    return deduped, len(records) - len(deduped)


def _compute_summary(df: pd.DataFrame) -> dict[str, Any]:
    has_seq    = df["sequence"].str.len() > 0
    has_smiles = df["substrate_smiles"].str.len() > 0
    has_rxn    = df["reactions"].str.len() > 0
    return {
        "n_with_sequence":   int(has_seq.sum()),
        "n_with_smiles":     int(has_smiles.sum()),
        "n_with_reactions":  int(has_rxn.sum()),
        "pct_with_sequence": round(float(has_seq.mean()) * 100, 1),
        "pct_with_smiles":   round(float(has_smiles.mean()) * 100, 1),
        "ec_class_dist": (
            df["ec_number"].str.extract(r"^(\d+)\.", expand=False)
            .dropna().value_counts().rename(lambda x: f"class_{x}").to_dict()
        ),
        "top_organisms": (
            df["organism"].replace("", pd.NA).dropna()
            .value_counts().head(5).to_dict()
        ),
        "source_db_dist": df["source_db"].value_counts().to_dict(),
    }


def _write_output(df: pd.DataFrame, output_dir: Path, filename: str,
                  fmt: Literal["parquet", "csv"]) -> Path:
    stem = Path(filename).stem
    if fmt == "csv":
        out_path = output_dir / f"{stem}.csv"
        df.to_csv(out_path, index=False, encoding="utf-8")
    else:
        out_path = output_dir / f"{stem}.parquet"
        pq.write_table(pa.Table.from_pandas(df, preserve_index=False), out_path)
    return out_path


# --- Public entry point ---

def run_enzyme_acquire(
    queries:       list[str] | str,
    filename:      str = "enzyme_attributes",
    max_results:   int = 5,
    fetch_smiles:  bool = True,
    fetch_kegg:    bool = True,
    offline_mode:  bool = False,
    output_dir:    Path | None = None,
    max_workers:   int = 4,
    output_format: Literal["parquet", "csv"] = "parquet",
) -> dict[str, Any]:
    if isinstance(queries, str):
        queries = [queries]
    if offline_mode:
        return {"status": "error", "code": "OFFLINE_MODE",
                "repair_hint": "EnzymeAcquire requires network access. Set offline_mode=False."}

    out_dir = Path(output_dir) if output_dir else OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    valid_queries, skipped = validate_queries(queries)
    if skipped:
        logger.warning("%d query(ies) skipped: %s", len(skipped), [s["query"] for s in skipped])
    if not valid_queries:
        return {"status": "error", "code": "ALL_QUERIES_INVALID",
                "repair_hint": "All queries failed validation.", "skipped": skipped}

    t0 = time.perf_counter()
    all_records, errors = _acquire_batch(
        valid_queries, max_results, fetch_smiles, fetch_kegg, max_workers)
    logger.info("Fetch done in %.2fs — %d record(s), %d error(s)",
                time.perf_counter() - t0, len(all_records), len(errors))

    n_failed    = len(errors)
    n_succeeded = len(valid_queries) - n_failed

    if not all_records:
        return {"status": "error", "code": "ALL_QUERIES_FAILED",
                "repair_hint": "All valid queries returned no results.",
                "n_queries_requested": len(queries), "n_queries_valid": len(valid_queries),
                "n_queries_succeeded": 0, "n_queries_failed": n_failed,
                "n_skipped": len(skipped), "skipped": skipped, "errors": errors}

    deduped, dropped = _dedup_records(all_records)
    if dropped:
        logger.info("Dedup: removed %d duplicate(s)", dropped)

    df = pd.DataFrame(deduped)
    for col in OUTPUT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df = df[OUTPUT_COLUMNS]
    df["seq_length"] = pd.to_numeric(df["seq_length"], errors="coerce").fillna(0).astype(int)
    str_cols = [c for c in OUTPUT_COLUMNS if c != "seq_length"]
    df[str_cols] = df[str_cols].fillna("")

    summary = _compute_summary(df)

    try:
        out_path = _write_output(df, out_dir, filename, output_format)
    except Exception:
        return {"status": "error", "code": "WRITE_FAILED",
                "repair_hint": "Failed to write output. Check disk space.",
                "detail": traceback.format_exc()}

    with open(out_dir / "enzyme_acquire_meta.json", "w") as f:
        json.dump({"tool": "EnzymeAcquire", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "columns": OUTPUT_COLUMNS, "summary": summary,
                   "errors": errors, "skipped": skipped}, f, indent=2, default=str)
    logger.info("Written → %s", out_path)

    return {
        "status":               "partial_success" if errors else "success",
        "file_path":            str(out_path),
        "asset_type":           "Tabular",
        "n_queries_requested":  len(queries),
        "n_queries_valid":      len(valid_queries),
        "n_queries_succeeded":  n_succeeded,
        "n_queries_failed":     n_failed,
        "n_skipped":            len(skipped),
        "n_records":            len(df),
        "n_duplicates_dropped": dropped,
        "columns":              list(df.columns),
        "summary":              summary,
        "errors":               errors,
        "skipped":              skipped,
    }

