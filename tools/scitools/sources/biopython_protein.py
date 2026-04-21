# -*- coding: utf-8 -*-
"""
sources/biopython_protein.py
============================
基于 BioPython 的本地蛋白质理化性质计算。
完全离线，无网络请求，<1秒/条。

提供：
  - analyze_sequence(seq)  → dict，包含 MW / pI / GRAVY 等属性
"""
from __future__ import annotations
import json
from typing import Any

from Bio.SeqUtils.ProtParam import ProteinAnalysis


# 标准20种氨基酸（过滤非标准字符）
_VALID_AA = set("ACDEFGHIKLMNPQRSTVWY")


def _clean(seq: str) -> str:
    """去掉空白、转大写、过滤非标准氨基酸。"""
    return "".join(c for c in seq.upper().strip() if c in _VALID_AA)


def analyze_sequence(seq: str) -> dict[str, Any]:
    """
    本地计算蛋白质理化性质。

    Parameters
    ----------
    seq : 氨基酸序列（单字母码）

    Returns
    -------
    dict，键：
      status          : "success" | "error"
      mw              : 分子量 (Da)
      pI              : 等电点
      instability     : 不稳定指数（< 40 表示稳定）
      gravy           : 大平均疏水性
      helix_frac      : α-螺旋比例
      turn_frac       : β-转角比例
      sheet_frac      : β-折叠比例
      aa_composition  : JSON 字符串，各氨基酸占比
      n_aa            : 有效序列长度（过滤非标准字符后）
    """
    cleaned = _clean(seq)
    if len(cleaned) < 10:
        return {
            "status": "error",
            "error":  f"序列过短或含过多非标准字符（有效长度={len(cleaned)}）",
        }

    try:
        pa = ProteinAnalysis(cleaned)

        mw          = round(pa.molecular_weight(), 2)
        pI          = round(pa.isoelectric_point(), 3)
        instability = round(pa.instability_index(), 2)
        gravy       = round(pa.gravy(), 4)
        helix, turn, sheet = pa.secondary_structure_fraction()

        # 氨基酸组成（百分比）
        aa_comp = {
            aa: round(pct * 100, 2)
            for aa, pct in pa.amino_acids_percent.items()
            if pct > 0
        }

        return {
            "status":         "success",
            "mw":             mw,
            "pI":             pI,
            "instability":    instability,
            "is_stable":      instability < 40,
            "gravy":          gravy,
            "helix_frac":     round(helix, 4),
            "turn_frac":      round(turn, 4),
            "sheet_frac":     round(sheet, 4),
            "aa_composition": json.dumps(aa_comp, ensure_ascii=False),
            "n_aa":           len(cleaned),
        }

    except Exception as e:
        return {"status": "error", "error": str(e)}