from __future__ import annotations


def insight(number: int) -> dict:
    return {
        "insight_id": f"ins_{number:03d}",
        "title": f"Insight {number}",
        "thesis": f"Grounded thesis {number}",
        "why_now": "The dated news cluster makes this timely.",
        "supporting_signals": [f"news_{number}: https://example.com/{number}"],
        "analysis_artifacts": ["scripts/analyze.py", f"deep_dives/ins_{number:03d}.md"],
        "related_entities": ["Wayve"],
        "external_support": [
            {
                "source_id": f"news_{number}",
                "url": f"https://example.com/{number}",
                "summary": "Source evidence",
            }
        ],
        "counterarguments": ["Only one API page was analyzed."],
        "confidence": 0.6,
        "next_questions": ["Does the signal persist in later news?"],
    }



def settings(*, project_root, state_dir, ai_index_api_key="test-key", openai_base_url="", openai_api_key=""):
    from dataelf.config import DataElfConfig
    from dataelf_server.settings import Settings, ServerConfig
    core = DataElfConfig(domains={"ai_index": {"source": {"api_key": ai_index_api_key}}})
    core.explorer.pi.cwd = project_root
    return Settings(core=core, server=ServerConfig(state_dir=state_dir))


class FakeIntentRecognizer:
    """Offline model double with explicit fixtures; production never uses this."""
    def extract(self, query, **kwargs):
        from dataelf_server.intent import SERVE_PROFILE
        from dataelf_server.scope_v2.contracts import ScopeV2Error
        if query in {"无效指令", "不支持的指令"}:
            raise ScopeV2Error("unsupported_scope", "Unsupported test input")
        value = SERVE_PROFILE.defaults().model_dump()
        value["time_range"] = {"start_date": "2026-08-27", "end_date": "2026-08-27"}
        value["domains"][0]["sources"] = ["news", "twitter", "github", "huggingface", "youtube"] if "综合" in query else ["news"]
        return SERVE_PROFILE.validate(value)
