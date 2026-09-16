"""Read-only replacement for DLDB's auto-creating catalog initializer.

Scoped to the pinned SDK/DLDB constructor in a dedicated bridge process.
"""
from __future__ import annotations

from typing import Any


class ServingCatalog:
    def __init__(self, db_conn: Any):
        from dldb.table import InformationSchemaRecord

        # Necessary physical catalog metadata; never list/read other logical tables.
        # open_table fails if absent, whereas DLDB's default initializer creates it.
        rows = (
            db_conn.open_table("information_schema").search()
            .where("table_name = 'serving_test'").limit(1).to_arrow().to_pylist()
        )
        if len(rows) != 1 or rows[0].get("table_name") != "serving_test":
            raise ValueError("Serving schema unavailable")
        self._record = InformationSchemaRecord(rows[0])

    def get(self, table_name: str) -> Any:
        if table_name != "serving_test":
            raise ValueError("Serving table required")
        return self._record

    def exist(self, table_name: str) -> bool:
        return table_name == "serving_test"
