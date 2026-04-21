# -*- coding: utf-8 -*-
"""
NCBI BLAST 客户端，使用 BioPython NCBIWWW。
提供：
  - run_blast(seq, ...)  → dict
"""
from __future__ import annotations
from typing import Any

from Bio.Blast import NCBIWWW, NCBIXML


def run_blast(
    sequence: str,
    program:  str   = "blastp",
    database: str   = "swissprot",
    max_hits: int   = 5,
    evalue:   float = 0.01,
) -> dict[str, Any]:
    """
    用 BioPython 调 NCBI BLAST，搜索同源蛋白。

    Parameters
    ----------
    sequence : 氨基酸序列
    program  : "blastp"（蛋白质）或 "blastn"（核酸）
    database : "swissprot"（reviewed）/ "nr" 等
    max_hits : 最多返回 hit 数
    evalue   : E-value 阈值

    Returns
    -------
    dict，键：
      status      : "success" | "error"
      n_hits      : hit 数量
      hits        : list[dict]，每条含 accession/identity_percent/evalue/title
    """
    seq = sequence.strip()
    if len(seq) < 10:
        return {"status": "error", "error": "序列过短"}

    try:
        result_handle = NCBIWWW.qblast(
            program,
            database,
            seq,
            hitlist_size=max_hits,
            expect=evalue,
        )
        blast_record = next(NCBIXML.parse(result_handle))
    except Exception as e:
        return {"status": "error", "error": str(e)}

    hits = []
    for alignment in blast_record.alignments[:max_hits]:
        if not alignment.hsps: #hsps 为空时跳过
            continue
        hsp = alignment.hsps[0]  # 取最佳 HSP
        identity_pct = round(hsp.identities / hsp.align_length * 100, 1)
        hits.append({
            "accession":        alignment.accession,
            "title":            alignment.title[:80],
            "identity_percent": identity_pct,
            "evalue":           hsp.expect,
            "score":            hsp.score,
            "align_length":     hsp.align_length,
        })

    return {
        "status": "success",
        "n_hits": len(hits),
        "hits":   hits,
    }