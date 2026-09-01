from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dataelf.config import DataElfConfig
from dataelf.domains.ai_index.config import AIIndexDomainConfig
from dataelf.domains.ai_index.connector import AIIndexConnector
from dataelf.domains.ai_index.table_builder import _items, normalize_institutions_response, normalize_papers_response, normalize_scholars_response, update_tables_from_response, write_tables


class AIIndexClient:
    def __init__(self, connector: AIIndexConnector, workspace_path: Path | None = None):
        self.connector = connector
        self.workspace_path = workspace_path
        self.max_calls = _env_int("DATAELF_AI_INDEX_MAX_CALLS")
        self.max_page_size = _env_int("DATAELF_AI_INDEX_MAX_PAGE_SIZE")
        self._call_count = 0

    @classmethod
    def from_env(cls, workspace_path: str | Path | None = None) -> "AIIndexClient":
        config = DataElfConfig.from_env()
        domain = AIIndexDomainConfig.from_mapping(config.domain_config("ai_index"))
        env_workspace = os.getenv("DATAELF_JOB_WORKSPACE") or os.getenv("DATAELF_WORKSPACE")
        path = Path(workspace_path) if workspace_path else Path(env_workspace) if env_workspace else None
        connector = AIIndexConnector(
            mode=domain.source.mode,
            base_url=domain.source.base_url,
            api_key=domain.source.api_key,
            fixtures_dir=domain.source.fixtures_dir,
            workspace_path=path,
        )
        return cls(connector=connector, workspace_path=path)

    def search_papers(self, **kwargs: Any) -> dict[str, Any]:
        kwargs = self._bounded_kwargs("search_papers", kwargs)
        response = self.connector.search_papers(**kwargs)
        self._update_tables(response)
        return response

    def search_institutions(self, **kwargs: Any) -> dict[str, Any]:
        kwargs = self._bounded_kwargs("search_institutions", kwargs)
        response = self.connector.search_institutions(**kwargs)
        self._update_tables(response)
        return response

    def search_scholars(self, **kwargs: Any) -> dict[str, Any]:
        kwargs = self._bounded_kwargs("search_scholars", kwargs)
        response = self.connector.search_scholars(**kwargs)
        self._update_tables(response)
        return response

    def fetch_institution_funding(self, institution_id: str) -> dict[str, Any]:
        self._register_call("fetch_institution_funding")
        response = self.connector.fetch_institution_funding(institution_id)
        self._update_tables(response)
        return response

    def collect_papers(self, max_pages: int = 3, size: int = 50, **kwargs: Any):
        return self._collect("papers", max_pages=max_pages, size=size, **kwargs)

    def collect_institutions(self, max_pages: int = 3, size: int = 50, **kwargs: Any):
        return self._collect("institutions", max_pages=max_pages, size=size, **kwargs)

    def collect_scholars(self, max_pages: int = 3, size: int = 50, **kwargs: Any):
        return self._collect("scholars", max_pages=max_pages, size=size, **kwargs)

    def to_dataframe(self, table_name: str, response: dict[str, Any]):
        rows = self.to_rows(table_name, response)
        try:
            import pandas as pd
        except ImportError:
            return rows
        return pd.DataFrame(rows)

    def to_rows(self, table_name: str, response: dict[str, Any]) -> list[dict[str, Any]]:
        if table_name == "papers":
            return normalize_papers_response(response).get("papers", [])
        if table_name == "institutions":
            return normalize_institutions_response(response).get("institutions", [])
        if table_name == "scholars":
            return normalize_scholars_response(response).get("scholars", [])
        return _items(response)

    def save_raw(self, name: str, response: dict[str, Any], workspace_path: str | Path | None = None) -> Path:
        target_workspace = Path(workspace_path) if workspace_path else self.workspace_path
        if target_workspace is None:
            raise ValueError("AIIndexClient.save_raw requires workspace_path.")
        raw_dir = target_workspace / "raw" / "ai_index"
        raw_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in name).strip("_") or "ai_index_response"
        path = raw_dir / f"{safe_name}.json"
        path.write_text(json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def save_table(self, table_name: str, data: Any, workspace_path: str | Path | None = None) -> None:
        target_workspace = Path(workspace_path) if workspace_path else self.workspace_path
        if target_workspace is None:
            raise ValueError("AIIndexClient.save_table requires workspace_path.")
        rows = _rows_from_data(data)
        write_tables(target_workspace, {table_name: rows}, append=True)

    def _collect(self, kind: str, max_pages: int, size: int, **kwargs: Any):
        all_rows: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            response = getattr(self, f"search_{kind}")(**kwargs, page=page, size=size)
            rows = _items(response)
            all_rows.extend(rows)
            total = response.get("data", {}).get("total") if isinstance(response.get("data"), dict) else None
            if not rows or (total is not None and len(all_rows) >= int(total)):
                break
        try:
            import pandas as pd
        except ImportError:
            return all_rows
        return pd.DataFrame(all_rows)

    def _update_tables(self, response: dict[str, Any]) -> None:
        if self.workspace_path is not None:
            update_tables_from_response(self.workspace_path, response)

    def _bounded_kwargs(self, method_name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        self._register_call(method_name)
        bounded = dict(kwargs)
        if self.max_page_size is not None:
            try:
                requested = int(bounded.get("size", self.max_page_size))
            except (TypeError, ValueError):
                requested = self.max_page_size
            bounded["size"] = max(1, min(requested, self.max_page_size))
        return bounded

    def _register_call(self, method_name: str) -> None:
        self._call_count += 1
        if self.max_calls is not None and self._call_count > self.max_calls:
            raise RuntimeError(
                f"AI Index call budget exceeded in {method_name}: "
                f"{self._call_count}/{self.max_calls}. Narrow the query or analyze existing workspace tables first."
            )


def _rows_from_data(data: Any) -> list[dict[str, Any]]:
    if hasattr(data, "to_dict"):
        return data.to_dict(orient="records")
    if isinstance(data, list):
        return [dict(row) for row in data]
    raise TypeError("save_table expects a pandas DataFrame or list[dict].")


def _env_int(name: str) -> int | None:
    value = os.getenv(name)
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None
