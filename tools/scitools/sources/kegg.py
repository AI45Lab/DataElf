"""
KEGG REST API 客户端。

提供：
  - fetch_enzyme() : 按 EC 编号拉取反应/通路信息
"""

from __future__ import annotations

from typing import Any

from ._http import _get

KEGG_BASE = "https://rest.kegg.jp"


def fetch_enzyme(ec: str) -> dict[str, Any]:
    """
    按 EC 编号从 KEGG 拉取酶的反应、底物、产物、通路信息。

    Parameters
    ----------
    ec : EC 编号，如 "1.1.1.1" 或 "ec:1.1.1.1"

    Returns
    -------
    dict，含 kegg_ec / reactions / substrates / products / pathways
    """
    ec_clean = ec.strip().lstrip("ec:")
    text = _get(f"{KEGG_BASE}/get/ec:{ec_clean}", fmt="text")
    if not text:
        return {}

    result: dict[str, Any] = {
        "kegg_ec":    ec_clean,
        "reactions":  [],
        "substrates": [],
        "products":   [],
        "pathways":   [],
    }

    section = None
    for line in text.splitlines():
        if line.startswith("REACTION"):
            section = "reactions";  result["reactions"].append(line[12:].strip())
        elif line.startswith("SUBSTRATE"):
            section = "substrates"; result["substrates"].append(line[12:].strip())
        elif line.startswith("PRODUCT"):
            section = "products";   result["products"].append(line[12:].strip())
        elif line.startswith("PATHWAY"):
            section = "pathways";   result["pathways"].append(line[12:].strip())
        elif line.startswith("            ") and section:
            val = line.strip()
            if val:
                result[section].append(val)
        else:
            section = None

    result["reactions"]  = "; ".join(result["reactions"][:3])
    result["substrates"] = "; ".join(result["substrates"][:5])
    result["products"]   = "; ".join(result["products"][:5])
    result["pathways"]   = "; ".join(result["pathways"][:3])

    return result