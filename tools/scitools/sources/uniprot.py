"""
UniProt REST API 客户端。

提供：
  - fetch_by_id()    : 按 Accession 直接拉取蛋白条目
  - fetch_by_name()  : 按酶名/关键词搜索
  - parse_entry()    : 从 UniProt JSON 提取标准字段
"""

from __future__ import annotations

from typing import Any

from ._http import _get

UNIPROT_BASE = "https://rest.uniprot.org"


def fetch_by_id(uniprot_id: str) -> dict[str, Any] | None:
    """按 UniProt Accession 直接拉取条目（如 P00533）。"""
    return _get(f"{UNIPROT_BASE}/uniprotkb/{uniprot_id}", fmt="json")


def fetch_by_name(name: str, max_results: int = 5) -> list[dict]:
    """按酶名搜索 UniProt，返回前 N 条 reviewed 结果。"""
    params = {
        "query":  name if name.startswith("ec:") else f"(protein_name:{name}) AND (reviewed:true)",
        "format": "json",
        "size":   max_results,
        "fields": "accession,protein_name,gene_names,organism_name,ec,sequence",
    }
    data = _get(f"{UNIPROT_BASE}/uniprotkb/search", params=params)
    if data and "results" in data:
        return data["results"]
    return []


def parse_entry(entry: dict) -> dict[str, Any]:
    """从 UniProt JSON 条目提取关键字段，返回标准化 dict。"""
    result: dict[str, Any] = {
        "uniprot_id":   entry.get("primaryAccession", ""),
        "protein_name": "",
        "gene_name":    "",
        "organism":     "",
        "ec_number":    "",
        "sequence":     "",
        "seq_length":   None,
        "source_db":    "UniProt",
    }

    pn  = entry.get("proteinDescription", {})
    rec = pn.get("recommendedName") or {}
    result["protein_name"] = rec.get("fullName", {}).get("value", "")

    genes = entry.get("genes", [])
    if genes:
        result["gene_name"] = genes[0].get("geneName", {}).get("value", "")

    result["organism"] = entry.get("organism", {}).get("scientificName", "")

    ec_list = [
        fn.get("value", "")
        for fn in rec.get("ecNumbers", [])
    ]
    if not ec_list:
        ec_list = [
            ref.get("id", "")
            for ref in entry.get("dbCrossReferences", [])
            if ref.get("database") == "EC"
        ]
    result["ec_number"] = "; ".join(ec_list)

    seq = entry.get("sequence", {})
    result["sequence"]   = seq.get("value", "")
    result["seq_length"] = seq.get("length")

    return result