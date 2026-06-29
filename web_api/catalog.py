from __future__ import annotations

from typing import Any


DATASET_CATALOG: list[dict[str, str]] = [
    {"name": "alfworld_sample", "rows": "12", "nesting": "5", "size": "28 KB"},
    {"name": "alpaca_data", "rows": "50", "nesting": "1", "size": "26 KB"},
    {"name": "companies", "rows": "5", "nesting": "1", "size": "243 B"},
    {"name": "search_sample", "rows": "8", "nesting": "6", "size": "12 KB"},
    {"name": "security_audit_samples", "rows": "45", "nesting": "3", "size": "18 KB"},
    {"name": "webshop_sample", "rows": "13", "nesting": "6", "size": "36 KB"},
]


def dataset_catalog_with_columns(dataset_schemas: dict[str, list[str]]) -> list[dict[str, Any]]:
    columns_by_name = dataset_schemas or {}
    catalog_names = {item["name"] for item in DATASET_CATALOG}
    datasets: list[dict[str, Any]] = [
        {**item, "columns": columns_by_name.get(item["name"], [])}
        for item in DATASET_CATALOG
    ]

    for name, columns in sorted(columns_by_name.items()):
        if name in catalog_names:
            continue
        datasets.append({
            "name": name,
            "rows": "NA",
            "nesting": "NA",
            "size": "NA",
            "columns": columns,
        })

    return datasets
