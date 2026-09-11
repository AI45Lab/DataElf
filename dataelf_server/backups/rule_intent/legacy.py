import re
from typing import Any

def _build_scope(query: str) -> dict[str, Any]:
    """Mirror the original DataElf intent parsing used by both Pi explorers."""

    topic_match = re.search(r"围绕\s*([^，,]+)", query)
    if topic_match:
        topic = topic_match.group(1).strip()
    elif "Agentic" in query or "agent" in query.lower():
        topic = "Agentic LLMs"
    else:
        topic = "AI science intelligence"
    domains = ["LLMs"] if "llm" in query.lower() or "Agentic" in topic else []
    sub_domains = (
        ["Agentic LLMs"]
        if "agent" in query.lower() or "智能体" in query
        else []
    )
    return {
        "domain": "ai_index",
        "topic": topic,
        "goal": "discover_insights",
        "domains": domains or ["LLMs"],
        "sub_domains": sub_domains or ["Agentic LLMs"],
        "time_window": "last_6_months",
        "expected_outputs": 3,
        "need_web_search": False,
        "need_code_analysis": True,
    }

