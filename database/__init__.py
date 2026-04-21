from typing import Any

from .base_strategy import DatabaseStrategy
from .local_file_strategy import LocalFileStrategy
from .mock_strategy import MockDatabaseStrategy

__all__ = [
    "DatabaseStrategy",
    "MockDatabaseStrategy",
    "LocalFileStrategy",
    "create_database_strategy",
]


def create_database_strategy(
    db_type: str,
    path: str,
    table_name: str = "data_pilot_test",
    **connection_params: Any,
) -> DatabaseStrategy:
    strategies: dict[str, type[DatabaseStrategy]] = {
        "mock": MockDatabaseStrategy,
        "local_file": LocalFileStrategy,
    }

    db_type_lower = db_type.lower()
    if db_type_lower == "lancedb":
        raise ValueError(
            "The open-source build does not include the internal LanceDB strategy. "
            "Use `database.type: local_file` or `database.type: mock` instead."
        )

    strategy_class = strategies.get(db_type_lower)

    if strategy_class is None:
        available = ", ".join(strategies.keys())
        raise ValueError(f"Unsupported database type: {db_type}. Available: {available}")

    return strategy_class(path, **connection_params)
