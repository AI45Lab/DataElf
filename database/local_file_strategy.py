from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base_strategy import DatabaseStrategy
from exceptions import DatasetNotFoundError, DatabaseError


class LocalFileStrategy(DatabaseStrategy):
    """
    Usage in config.yaml:
        database:
            type: local_file
            path: ./data  # Directory containing JSON files. Allow contains multiple files.
    """

    def __init__(self, path: str, **connection_params: Any):
        super().__init__(path, **connection_params)
        self.data_dir = Path(path)

        if not self.data_dir.exists():
            raise DatabaseError(f"Data directory does not exist: {path}")

        if not self.data_dir.is_dir():
            raise DatabaseError(f"Path is not a directory: {path}")

        # Cache for loaded data
        self._cache: dict[str, list[dict[str, Any]]] = {}

    def _get_file_path(self, table_name: str) -> Path:
        return self.data_dir / f"{table_name}.json"

    def _load_table(self, table_name: str) -> list[dict[str, Any]]:
        if table_name in self._cache:
            return self._cache[table_name]

        file_path = self._get_file_path(table_name)

        if not file_path.exists():
            raise DatasetNotFoundError(
                f"Dataset '{table_name}' not found. "
                f"Expected file: {file_path}"
            )

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise DatabaseError(f"Invalid JSON in {file_path}: {e}")
        except Exception as e:
            raise DatabaseError(f"Failed to read {file_path}: {e}")

        if not isinstance(data, list):
            raise DatabaseError(
                f"Invalid data format in {file_path}: "
                f"Expected list of dicts, got {type(data).__name__}"
            )

        self._cache[table_name] = data
        return data

    #Read data from a JSON file with optional filtering.
    def read_table(
        self,
        table_name: str,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
        columns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        data = self._load_table(table_name)

        # Apply filters
        if filters:
            data = [
                row for row in data
                if all(row.get(k) == v for k, v in filters.items())
            ]

        # Select columns
        if columns:
            data = [{k: row.get(k) for k in columns} for row in data]

        # Apply limit
        if limit is not None:
            data = data[:limit]

        return data

    #Write data to a JSON file.
    def write_table(self, table_name: str, data: list[dict[str, Any]] | dict[str, Any]) -> None:
        if isinstance(data, dict):
            data = [data]

        file_path = self._get_file_path(table_name)

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            raise DatabaseError(f"Failed to write {file_path}: {e}")

        # Update cache
        self._cache[table_name] = data

    #Write log entry to .logs/{job_id}.json.
    def write_log(self, log_entry: dict[str, Any]) -> None:
        job_id = log_entry.get("job_id", "unknown")
        logs_dir = self.data_dir.parent / ".logs"
        log_file = logs_dir / f"{job_id}.json"

        # Load existing logs or create new list
        if log_file.exists():
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    logs = json.load(f)
            except (json.JSONDecodeError, Exception):
                logs = []
        else:
            logs = []
            logs_dir.mkdir(parents=True, exist_ok=True)

        logs.append(log_entry)

        try:
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(logs, f, indent=2, ensure_ascii=False)
        except Exception as e:
            raise DatabaseError(f"Failed to write log for job {job_id}: {e}")

    def close(self) -> None:
        self._cache.clear()

    def list_tables(self) -> list[str]:
        return [
            f.stem for f in self.data_dir.glob("*.json")
            if f.is_file() and f.stem != "logs"
        ]
