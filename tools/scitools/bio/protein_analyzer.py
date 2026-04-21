"""ProteinAnalyzer — protein sequence physicochemical analysis (BioPython) + optional BLAST."""

from __future__ import annotations

import logging
import os
import traceback
from pathlib import Path
from typing import Any

import pandas as pd

from tools.scitools.sources.biopython_protein import analyze_sequence
from tools.scitools.sources.blast import run_blast

OUTPUT_DIR = Path(os.environ.get("SDC_OUTPUT_DIR", "/tmp/sdc_outputs")) / "bio"

_SEQ_ALIASES = {"sequence", "seq", "protein_sequence", "aa_sequence"}
_ID_ALIASES  = {"id", "uniprot_id", "accession", "entry"}
_VALID_AA    = set("ACDEFGHIKLMNPQRSTVWYX")

logger = logging.getLogger("ProteinAnalyzer")


# --- Input normalization ---

def _clean_seq(raw: str) -> str:
    return raw.upper().strip().replace(" ", "").replace("\n", "")


def _validate_seq(seq: str, label: str) -> str | None:
    """Return error reason string, or None if valid."""
    if len(seq) < 10:
        return f"sequence too short (len={len(seq)})"
    invalid = set(seq) - _VALID_AA
    if invalid:
        return f"invalid amino acid characters: {invalid}"
    return None


def _normalize_records(records: list) -> tuple[list[dict], list[dict]]:
    """Normalize bare strings or dicts into [{"id", "sequence", "protein_name"}]."""
    normalized, skipped, seen_ids = [], [], set()

    for i, rec in enumerate(records):
        if isinstance(rec, str):
            seq = _clean_seq(rec)
            if reason := _validate_seq(seq, f"index={i}"):
                skipped.append({"index": i, "reason": reason})
                continue
            normalized.append({"id": f"seq_{i}", "sequence": seq, "protein_name": ""})

        elif isinstance(rec, dict):
            seq_key = next((k for k in _SEQ_ALIASES if k in rec), None)
            if seq_key is None:
                skipped.append({"index": i, "reason": "no sequence field found"})
                continue
            seq = _clean_seq(str(rec[seq_key]))
            if reason := _validate_seq(seq, f"index={i}"):
                skipped.append({"index": i, "reason": reason})
                continue
            uid = next((str(rec[k]) for k in _ID_ALIASES if k in rec), f"seq_{i}")
            if uid in seen_ids:
                skipped.append({"index": i, "id": uid, "reason": "duplicate id"})
                continue
            seen_ids.add(uid)
            normalized.append({"id": uid, "sequence": seq,
                                "protein_name": str(rec.get("protein_name", ""))})
        else:
            skipped.append({"index": i, "reason": f"unsupported type: {type(rec).__name__}"})

    return normalized, skipped


def _df_to_records(df: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    id_col = next((c for c in _ID_ALIASES if c in df.columns), df.columns[0])
    records, skipped = [], []
    for _, row in df.iterrows():
        seq = _clean_seq(str(row.get("sequence", "") or ""))
        uid = str(row.get(id_col, ""))
        if reason := _validate_seq(seq, uid):
            skipped.append({"id": uid, "reason": reason})
            continue
        records.append({"id": uid, "sequence": seq,
                        "protein_name": str(row.get("protein_name", ""))})
    return records, skipped


def _load_sequences(source: Any) -> tuple[list[dict], list[dict]]:
    if isinstance(source, (str, Path)):
        return _df_to_records(pd.read_parquet(source))
    if isinstance(source, pd.DataFrame):
        return _df_to_records(source)
    if isinstance(source, list):
        return _normalize_records(source)
    raise ValueError(f"Unsupported source type: {type(source)}")


# --- Analysis ---

def _analyze_one(record: dict, do_blast: bool) -> dict:
    seq = record["sequence"]
    row: dict[str, Any] = {
        "uniprot_id":     record["id"],
        "protein_name":   record["protein_name"],
        "seq_length":     len(seq),
        "mw":             None, "pI":          None,
        "instability":    None, "is_stable":   None,
        "gravy":          None, "helix_frac":  None,
        "turn_frac":      None, "sheet_frac":  None,
        "aa_composition": None, "local_status": "error",
        "n_blast_hits":   None, "top_hit_id":  None,
        "top_identity":   None, "top_evalue":  None,
        "top_title":      None, "blast_status": "skipped",
    }

    props = analyze_sequence(seq)
    if props["status"] == "success":
        row.update({k: props[k] for k in
                    ("mw", "pI", "instability", "is_stable", "gravy",
                     "helix_frac", "turn_frac", "sheet_frac", "aa_composition")})
        row["local_status"] = "success"
    else:
        row["local_status"] = props.get("error", "error")

    if do_blast:
        try:
            bl = run_blast(seq)
            if bl["status"] == "success" and bl["hits"]:
                top = bl["hits"][0]
                row.update({
                    "n_blast_hits": bl["n_hits"],
                    "top_hit_id":   top.get("accession", ""),
                    "top_identity": top.get("identity_percent"),
                    "top_evalue":   top.get("evalue"),
                    "top_title":    top.get("title", ""),
                    "blast_status": "success",
                })
            else:
                row["blast_status"] = bl.get("error", "no_hits")
        except Exception:
            logger.error("BLAST failed id=%r\n%s", record["id"], traceback.format_exc())
            row["blast_status"] = "exception"

    return row


def _compute_summary(df: pd.DataFrame, do_blast: bool) -> dict[str, Any]:
    num = df.select_dtypes("number")
    summary: dict[str, Any] = {
        "n_local_success":  int((df["local_status"] == "success").sum()),
        "n_local_error":    int((df["local_status"] != "success").sum()),
        "pct_stable":       round(float(df["is_stable"].dropna().mean()) * 100, 1)
                            if df["is_stable"].notna().any() else None,
        "mean_seq_length":  round(float(df["seq_length"].mean()), 1),
        "mean_mw":          round(float(df["mw"].dropna().mean()), 2)
                            if df["mw"].notna().any() else None,
        "mean_pI":          round(float(df["pI"].dropna().mean()), 2)
                            if df["pI"].notna().any() else None,
        "mean_gravy":       round(float(df["gravy"].dropna().mean()), 3)
                            if df["gravy"].notna().any() else None,
    }
    if do_blast:
        summary["n_blast_success"] = int((df["blast_status"] == "success").sum())
        summary["n_blast_hits_total"] = int(df["n_blast_hits"].fillna(0).sum())
    return summary


# --- Public entry point ---

def run_protein_analyzer(
    source:           Any,
    filename:         str = "protein_analysis.parquet",
    run_blast_search: bool = True,
    output_dir:       Path | None = None,
) -> dict[str, Any]:
    out_dir = Path(output_dir) if output_dir else OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        records, skipped = _load_sequences(source)
    except Exception:
        tb = traceback.format_exc()
        logger.error("Load failed. Raw source type: %r, value: %r\n%s",
                     type(source).__name__, source, tb)
        return {"status": "error", "code": "LOAD_FAILED",
                "repair_hint": "source must be a parquet path, DataFrame, list[dict], or list[str].",
                "detail": tb}

    if not records:
        return {"status": "error", "code": "NO_SEQUENCES",
                "repair_hint": "No valid sequences found (length < 10 or invalid characters).",
                "skipped": skipped}

    rows, errors = [], []
    for rec in records:
        try:
            rows.append(_analyze_one(rec, run_blast_search))
        except Exception:
            tb = traceback.format_exc()
            logger.error("Analysis failed id=%r. Raw record: %r\n%s", rec["id"], rec, tb)
            errors.append({"id": rec["id"], "error": "UNEXPECTED_ERROR", "traceback": tb})

    if not rows:
        return {"status": "error", "code": "ALL_FAILED",
                "n_errors": len(errors), "errors": errors, "skipped": skipped}

    df = pd.DataFrame(rows)
    out_path = out_dir / filename
    df.to_parquet(out_path, index=False)

    summary = _compute_summary(df, run_blast_search)
    status  = "partial_success" if errors else "success"

    logger.info("Written → %s  (%d records, %d errors)", out_path, len(rows), len(errors))
    return {
        "status":    status,
        "file_path": str(out_path),
        "asset_type": "Tabular",
        "n_records":  len(rows),
        "n_errors":   len(errors),
        "n_skipped":  len(skipped),
        "columns":    list(df.columns),
        "summary":    summary,
        "errors":     errors,
        "skipped":    skipped,
    }