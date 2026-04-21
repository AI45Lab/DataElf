from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class DatabaseStrategy(ABC):

    def __init__(self, path: str, **connection_params: Any):
        self.path = path
        self.connection_params = connection_params

    @abstractmethod
    def read_table(
        self,
        table_name: str,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
        columns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def write_table(self, table_name: str, data: list[dict[str, Any]] | dict[str, Any]) -> None:
        pass

    @abstractmethod
    def write_log(self, log_entry: dict[str, Any]) -> None:
        pass

    @abstractmethod
    def close(self) -> None:
        pass

    def list_tables(self) -> list[str]:
        """Return available table names for schema extraction during prompt building."""
        return []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
