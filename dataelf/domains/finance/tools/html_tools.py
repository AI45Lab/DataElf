"""parse_html_page: SEC-style filing HTML into bounded text and table grids.

Filing parsing rides the stdlib html.parser tokenizer (zero third-party
dependencies); URL fetches go through urllib, which honors
HTTP(S)_PROXY/NO_PROXY environment variables natively, cache once under
``raw/finance/pages``, and with ``key`` set persist the full text for later
retrieve_information range reads.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .limits import (
    PARSE_CELL_CHARS_MAX,
    PARSE_ROW_CELLS_MAX,
    PARSE_TABLE_ROWS_MAX,
    PARSE_TABLES_MAX,
    PARSE_TEXT_DEFAULT_CHARS,
    PARSE_TEXT_MAX_CHARS,
    PARSE_TEXT_MIN_CHARS,
)
from .workspace import bounded_int, code_root, safe_relative, storage_path, workspace_root

# SEC fair-access policy wants a contact address in the UA (Archives returns
# 403 otherwise); operators set analysis.http_user_agent in the finance
# config, injected into this process as DATAELF_FINANCE_USER_AGENT.
_USER_AGENT = os.environ.get("DATAELF_FINANCE_USER_AGENT") or \
    "DataElf-Finance-Domain/1.0 (analysis runtime; contact: admin@dataelf.local)"
_BLOCK_TAGS = frozenset({"p", "div", "br", "li", "ul", "ol", "hr", "table", "tr",
                         "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"})
_VOID_BLOCK_TAGS = frozenset({"br", "hr"})  # never receive an endtag event


class _FilingParser(HTMLParser):
    """Event state machine over the stdlib tokenizer.

    Faithful port of the previous TS implementation (htmlparser2 handler):
    block text flushed at block-tag boundaries, table grids via table/tr/td
    tracking, script/style skipped, <title> captured, entities decoded.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.text_chunks: list[str] = []
        self.tables: list[list[list[str]]] = []
        self._in_title = False
        self._skip_depth = 0
        self._chunk: list[str] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def _flush_chunk(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._chunk)).strip()
        if text:
            self.text_chunks.append(text)
        self._chunk = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag in _VOID_BLOCK_TAGS:
            self._flush_chunk()

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._table is not None and self._row:
                self._table.append(self._row)
                # Include tabular facts in the persisted text as well as the
                # bounded table grid, so retrieve_information can recover
                # values that are not repeated in surrounding prose.
                self.text_chunks.append(" | ".join(self._row))
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None
        if tag in _BLOCK_TAGS:
            self._flush_chunk()

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        elif self._cell is not None:
            self._cell.append(data)
        else:
            self._chunk.append(data)

    def result(self) -> dict[str, Any]:
        self._flush_chunk()
        return {
            "title": re.sub(r"\s+", " ", self.title).strip(),
            "text_chunks": self.text_chunks,
            "tables": self.tables,
        }


def _parse_filing_html(html: str) -> dict[str, Any]:
    parser = _FilingParser()
    parser.feed(html)
    parser.close()
    return parser.result()


def _http_text(url: str, timeout: int = 30) -> str:
    # urllib builds its proxy handler from HTTP(S)_PROXY/NO_PROXY by default
    # (suffix matching, no CIDR — same scope as the previous undici setup).
    request = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    })
    last_error = ""
    for attempt in range(2):  # one retry on 429 / transient 5xx
        if attempt:
            time.sleep(1.5)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return body.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            last_error = f"remote source returned HTTP {exc.code} for {url}"
            if exc.code != 429 and exc.code < 500:
                break
        except Exception as exc:
            raise ValueError(f"cannot reach remote source {url}: {exc}") from None
    raise ValueError(last_error)


def _bounded_tables(tables: list[list[list[str]]]) -> tuple[list[list[list[str]]], bool]:
    """Clip table grids to the plan limits; report whether anything was dropped."""
    limited: list[list[list[str]]] = []
    truncated = False
    if len(tables) > PARSE_TABLES_MAX:
        truncated = True
    for table in tables[:PARSE_TABLES_MAX]:
        if len(table) > PARSE_TABLE_ROWS_MAX:
            truncated = True
        rows: list[list[str]] = []
        for row in table[:PARSE_TABLE_ROWS_MAX]:
            if len(row) > PARSE_ROW_CELLS_MAX:
                truncated = True
            cells = []
            for cell in row[:PARSE_ROW_CELLS_MAX]:
                if len(cell) > PARSE_CELL_CHARS_MAX:
                    truncated = True
                cells.append(cell[:PARSE_CELL_CHARS_MAX])
            rows.append(cells)
        limited.append(rows)
    return limited, truncated


def _build_parse_result(label: str, parsed: dict[str, Any], args: dict[str, Any], url: str | None = None) -> dict[str, Any]:
    extract = "all" if args.get("extract") is None else str(args.get("extract"))
    if extract not in ("all", "text", "tables"):
        raise ValueError("extract must be one of all/text/tables")
    max_length = bounded_int(args.get("max_length"), PARSE_TEXT_MIN_CHARS, PARSE_TEXT_MAX_CHARS, PARSE_TEXT_DEFAULT_CHARS)
    text = "\n".join(parsed["text_chunks"])
    text_included = extract in ("all", "text")
    text_truncated = text_included and len(text) > max_length
    tables, table_truncated = _bounded_tables(parsed["tables"])
    result: dict[str, Any] = {
        "path": label,
        "title": parsed["title"],
        "text_length": len(text),
        "table_count": len(parsed["tables"]),
        "truncated": text_truncated or table_truncated,
    }
    if text_included:
        result["text"] = text[:max_length] + ("...[truncated]" if text_truncated else "")
        result["returned_text_length"] = min(len(text), max_length)
        result["result_limit"] = {"unit": "characters", "limit": max_length}
    if extract in ("all", "tables"):
        result["tables"] = tables
        result["table_truncated"] = table_truncated
        result["table_limits"] = {
            "tables": PARSE_TABLES_MAX,
            "rows_per_table": PARSE_TABLE_ROWS_MAX,
            "cells_per_row": PARSE_ROW_CELLS_MAX,
            "cell_characters": PARSE_CELL_CHARS_MAX,
        }
    if url:
        result["url"] = url
    key = str(args.get("key") or "").strip()
    if key:
        # The FULL text (never the clipped excerpt) is persisted so later
        # retrieve_information calls can read any character range.
        storage = storage_path(key)
        storage.write_text(text, encoding="utf-8")
        result["storage_key"] = key
        result["storage_path"] = str(storage.relative_to(workspace_root()))
    return result


def parse_html_page(args: dict[str, Any]) -> dict[str, Any]:
    url = str(args.get("url") or "").strip()
    path = str(args.get("path") or "").strip()
    if bool(url) == bool(path):
        raise ValueError("provide exactly one of url or path")
    if url:
        if not url.lower().startswith("https://"):
            raise ValueError("url must be an https:// link")
        pages_dir = code_root().parent / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        cached = pages_dir / f"{digest}.html"
        if cached.is_file():
            html = cached.read_text(encoding="utf-8")
        else:
            html = _http_text(url)
            cached.write_text(html, encoding="utf-8")
        return _build_parse_result(f"pages/{digest}.html", _parse_filing_html(html), args, url=url)
    relative = safe_relative(path)
    root = code_root()
    candidate = (root / relative).resolve()
    authorized = [root]
    authorized.extend(
        Path(item).resolve()
        for item in os.environ.get("DATAELF_FINANCE_PROTECTED", "").split(os.pathsep)
        if item
    )
    if not any(candidate.is_relative_to(base) for base in authorized) or not candidate.is_file():
        raise ValueError("path is outside the authorized finance workspace")
    if candidate.suffix.lower() not in (".html", ".htm"):
        raise ValueError("parse_html_page expects an .html/.htm file")
    html = candidate.read_text(encoding="utf-8")
    return _build_parse_result(relative.as_posix(), _parse_filing_html(html), args)
