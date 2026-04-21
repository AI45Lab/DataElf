"""Shared keyword scanning utilities for ToxicityKeywordRule and HarmfulKeywordRule."""
import os
import re
from typing import List


# CJK Unicode range; used to determine whether to use word-boundary matching
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")

_WORDLIST_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "resources", "wordlists")
)


def load_wordlist(*filenames: str) -> List[str]:
    """Load one or more wordlist files into a deduplicated list.

    Format: one word/phrase per line; lines starting with # are ignored.
    """
    keywords: list[str] = []
    seen: set[str] = set()
    for fname in filenames:
        path = os.path.join(_WORDLIST_DIR, fname)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                word = line.strip()
                if not word or word.startswith("#"):
                    continue
                key = word.lower()
                if key not in seen:
                    seen.add(key)
                    keywords.append(word)
    return keywords


def scan_keywords(text: str, keywords: List[str], field_name: str) -> List[dict]:
    """Scan text for keyword matches and return a list of hit records.

    Strategy:
    - CJK keywords: substring match (no word boundaries in Chinese)
    - English keywords: \\b word-boundary match, case-insensitive
    """
    hits = []
    text_lower = text.lower()

    for kw in keywords:
        kw_lower = kw.lower()
        if _CJK_RE.search(kw):
            # CJK: substring search
            idx = text.find(kw)
            if idx == -1:
                idx = text_lower.find(kw_lower)
            if idx != -1:
                hits.append(_make_hit(field_name, kw, idx, idx + len(kw), text))
        else:
            # English: word-boundary match
            pattern = re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)
            for m in pattern.finditer(text):
                hits.append(_make_hit(field_name, kw, m.start(), m.end(), text))

    return hits


def _make_hit(field_name: str, keyword: str, start: int, end: int, text: str) -> dict:
    """Build a hit record with 40-char context around the match."""
    ctx_start = max(0, start - 40)
    ctx_end = min(len(text), end + 40)
    prefix = "..." if ctx_start > 0 else ""
    suffix = "..." if ctx_end < len(text) else ""
    context = prefix + text[ctx_start:ctx_end] + suffix
    return {
        "field_name": field_name,
        "keyword": keyword,
        "start": start,
        "end": end,
        "context": context,
    }
