"""
PubChem REST API 客户端。
提供：
  - fetch_smiles() : 按化合物名查询 Canonical SMILES
"""

from __future__ import annotations

from ._http import _get

PUBCHEM_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"


def fetch_smiles(compound_name: str) -> str | None:
    """
    按化合物名查询 Canonical SMILES。

    Parameters
    ----------
    compound_name : 化合物名，如 "ethanol"、"glucose"

    Returns
    -------
    SMILES 字符串，未找到时返回 None
    """
    try:
        url  = f"{PUBCHEM_BASE}/compound/name/{compound_name}/property/CanonicalSMILES/JSON"
        data = _get(url, fmt="json")
        if data:
            props = data.get("PropertyTable", {}).get("Properties", [])
            if props:
                return props[0].get("CanonicalSMILES")
    except Exception:
        pass
    return None