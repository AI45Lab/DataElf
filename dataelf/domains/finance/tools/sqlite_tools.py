"""Read-only SQLite trio: get_database_info, describe_table, execute_query.

The database path comes from DATAELF_FINANCE_DB; connections always open with
``mode=ro`` and queries pass the read-only guard before execution.
"""

from __future__ import annotations

import ast
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from .limits import QUERY_CELL_CHARS_MAX, QUERY_ROWS_DEFAULT, QUERY_ROWS_HARD_LIMIT
from .workspace import bounded_int


def _db_path() -> Path:
    value = os.environ.get("DATAELF_FINANCE_DB")
    if not value:
        raise ValueError("DATAELF_FINANCE_DB is not configured")
    path = Path(value).resolve()
    if not path.is_file():
        raise ValueError(f"Finance database does not exist: {path}")
    return path


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _safe_identifier(value: Any) -> str:
    text = str(value or "")
    # SQLite permits punctuation in quoted identifiers.  Keep the accepted
    # set deliberately narrow while allowing common names such as
    # ``price-history`` and ``2024.results``; callers quote the result.
    if not text or re.fullmatch(r"[A-Za-z0-9_.-]+", text) is None:
        raise ValueError("invalid SQL identifier")
    return text


def _assert_read_only(query: str) -> None:
    if not query:
        raise ValueError("query is required")
    try:
        tree = ast.parse(query, mode="eval")
    except SyntaxError:
        tree = None
    normalized = query.lstrip().lower()
    if tree is None and not normalized.startswith(("select", "with", "pragma", "explain")):
        raise ValueError("only read-only SQL is allowed")
    # Ignore quoted strings and comments before looking for write keywords;
    # a SELECT returning the literal ``'attach '`` remains read-only.
    scrubbed = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\n]*|/\*.*?\*/", " ", normalized, flags=re.DOTALL)
    if re.search(r"\b(?:insert|update|delete|drop|alter|create|attach|replace)\s+|\bvacuum\b", scrubbed):
        raise ValueError("only read-only SQL is allowed")


def get_database_info(_args: dict[str, Any]) -> dict[str, Any]:
    with _connect() as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {"database_path": str(_db_path()), "table_count": len(tables), "tables": tables}


def describe_table(args: dict[str, Any]) -> dict[str, Any]:
    table = _safe_identifier(args.get("table_name"))
    with _connect() as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            raise ValueError(f"Unknown table: {table}")
        columns = []
        for row in conn.execute(f'PRAGMA table_info("{table}")'):
            columns.append({"name": row[1], "type": row[2], "not_null": bool(row[3]), "default_value": row[4], "primary_key": bool(row[5])})
        count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    return {"table_name": table, "row_count": count, "column_count": len(columns), "columns": columns}


def execute_query(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    _assert_read_only(query)
    limit = bounded_int(args.get("limit"), 1, QUERY_ROWS_HARD_LIMIT, QUERY_ROWS_DEFAULT)
    with _connect() as conn:
        cursor = conn.execute(query)
        columns = [item[0] for item in cursor.description or []]
        fetched = cursor.fetchmany(limit + 1)
    rows: list[dict[str, Any]] = []
    truncated_cells = 0
    for row in fetched[:limit]:
        values: dict[str, Any] = {}
        for column, value in zip(columns, row):
            if isinstance(value, str) and len(value) > QUERY_CELL_CHARS_MAX:
                values[column] = value[:QUERY_CELL_CHARS_MAX]
                truncated_cells += 1
            else:
                values[column] = value
        rows.append(values)
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "returned": len(rows),
        "truncated": len(fetched) > limit,
        "truncated_cells": truncated_cells,
        "result_limit": {"unit": "rows", "limit": limit},
        "query": query,
    }
