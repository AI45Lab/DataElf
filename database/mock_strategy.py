from __future__ import annotations

from typing import Any, Optional

from .base_strategy import DatabaseStrategy


class MockDatabaseStrategy(DatabaseStrategy):
    def __init__(self, path: str = ":memory:", **connection_params: Any):
        super().__init__(path, **connection_params)
        self._data: dict[str, list[dict[str, Any]]] = {}
        self._logs: list[dict[str, Any]] = []

        # Sample data for smoke tests and local development.
        self._data["companies"] = [
            {"id": 1, "name": "Company A", "value": 100},
            {"id": 2, "name": "Company B", "value": 200},
            {"id": 3, "name": "Company C", "value": 150},
        ]
        self._data["security_logs"] = [
            {"timestamp": "2024-01-01", "event": "login", "user": "alice"},
            {"timestamp": "2024-01-01", "event": "logout", "user": "bob"},
            {"timestamp": "2024-01-02", "event": "login", "user": "charlie"},
        ]

    def read_table(
        self,
        table_name: str,
        filters: dict[str, Any] | None = None,
        limit: Optional[int] = None,
        columns: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        rows = self._data.get(table_name, [])

        if columns:
            rows = [{k: v for k, v in row.items() if k in columns} for row in rows]

        if filters:
            filtered_rows = []
            for row in rows:
                match = True
                for key, value in filters.items():
                    if row.get(key) != value:
                        match = False
                        break
                if match:
                    filtered_rows.append(row)
            rows = filtered_rows

        if limit:
            rows = rows[:limit]

        return rows

    def write_table(self, table_name: str, data: list[dict[str, Any]] | dict[str, Any]) -> None:
        if table_name not in self._data:
            self._data[table_name] = []

        if isinstance(data, dict):
            self._data[table_name].append(data)
        else:
            self._data[table_name].extend(data)

    def write_log(self, log_entry: dict[str, Any]) -> None:
        self._logs.append(log_entry)

    def get_logs(self) -> list[dict[str, Any]]:
        return self._logs.copy()

    def list_tables(self) -> list[str]:
        return sorted(self._data.keys())

    def close(self) -> None:
        self._data.clear()
        self._logs.clear()
