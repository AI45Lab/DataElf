"""Markdown structure only; the model decides whether a heading means writing."""
from __future__ import annotations

import re


def parse_sections(query: str) -> list[dict]:
    """Split Markdown structure without classifying a heading's meaning."""
    sections = [{"id": "default", "title": None, "level": 0, "body": ""}]
    fence_character = None
    fence_length = 0
    for line in query.splitlines():
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence_character:
            if fence and fence[1][0] == fence_character and len(fence[1]) >= fence_length and not fence[2].strip():
                fence_character = None
            sections[-1]["body"] += line + "\n"
            continue
        if fence:
            fence_character, fence_length = fence[1][0], len(fence[1])
            sections[-1]["body"] += line + "\n"
            continue
        heading = re.match(r"^ {0,3}(#{1,6})[\t ]+(\S.*)$", line)
        if heading:
            sections.append({"id": f"section_{len(sections)}", "title": heading[2], "level": len(heading[1]), "body": ""})
        else:
            sections[-1]["body"] += line + "\n"
    return sections


def has_content_heading(query: str) -> bool:
    return len(parse_sections(query)) > 1


def has_default_content(sections: list[dict], writing_ids: list[str]) -> bool:
    stack = []
    for section in sections:
        while stack and stack[-1][0] >= section["level"]:
            stack.pop()
        writing = section["id"] in writing_ids or bool(stack and stack[-1][1])
        if section["body"].strip() and not writing:
            return True
        stack.append((section["level"], writing))
    return False
