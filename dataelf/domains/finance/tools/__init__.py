"""Local runner for the Python-side finance tools (stdin JSON bridge).

The market-data tools (edgar_search, company_profile, company_facts,
price_history) are implemented natively in the finance_edgar_prices.ts
extension; this package serves the tools that need Python, one module per
tool family: the sqlite SQL trio (sqlite_tools), the workspace code trio
(code_tools), filing HTML parsing (html_tools), and stored-text retrieval
(retrieve_tools). Shared result limits live in limits.py; env-resolved
workspace paths and argument guards in workspace.py. ``main`` reads one
{tool, arguments} request from stdin and prints one JSON response;
``dispatch`` is the name → handler registry the tests drive directly.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .code_tools import execute_code, get_field_description, list_files
from .html_tools import parse_html_page
from .retrieve_tools import retrieve_information
from .sqlite_tools import describe_table, execute_query, get_database_info

_HANDLERS = {
    "get_database_info": get_database_info,
    "describe_table": describe_table,
    "execute_query": execute_query,
    "list_files": list_files,
    "get_field_description": get_field_description,
    "parse_html_page": parse_html_page,
    "retrieve_information": retrieve_information,
    "execute_code": execute_code,
}


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"Unknown finance tool: {name}")
    return handler(args)


def main() -> int:
    request = json.load(sys.stdin)
    name = str(request.get("tool", ""))
    arguments = request.get("arguments") or {}
    try:
        result = dispatch(name, arguments)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"error": {"type": type(exc).__name__, "message": str(exc)}}, ensure_ascii=False))
        return 0
