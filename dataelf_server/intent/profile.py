"""Scene capabilities, separate from extraction and physical tool endpoints."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .schema import Intent, Output


@dataclass(frozen=True)
class Source:
    id: str
    description: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Domain:
    id: str
    description: str
    sources: tuple[Source, ...]
    aliases: tuple[str, ...] = ()
    all_sources_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Profile:
    domains: tuple[Domain, ...]
    default_domains: tuple[str, ...]

    def __post_init__(self):
        ids = [domain.id for domain in self.domains]
        if not ids or len(ids) != len(set(ids)) or any(not value.strip() for value in ids):
            raise ValueError("Profile domain IDs must be nonblank and unique")
        if not self.default_domains or len(self.default_domains) != len(set(self.default_domains)):
            raise ValueError("Profile must have unique default domains")
        if not set(self.default_domains).issubset(ids):
            raise ValueError("Default domains must belong to the profile")
        for domain in self.domains:
            sources = [source.id for source in domain.sources]
            if not sources or len(sources) != len(set(sources)) or any(not value.strip() for value in sources):
                raise ValueError("Source IDs must be nonblank and unique within each domain")

    def defaults(self) -> Intent:
        return Intent.model_validate({
            "retrieval": {"keywords": [], "entities": [], "exclude_keywords": []},
            "time_range": {"start_date": None, "end_date": None},
            "domains": [{"id": value, "sources": []} for value in self.default_domains],
            "output": Output.unspecified().model_dump(),
        })

    def catalog(self) -> list[dict]:
        return [asdict(domain) for domain in self.domains]

    def json_schema(self) -> dict:
        schema = Intent.model_json_schema()
        selections = []
        for domain in self.domains:
            selections.append({
                "type": "object",
                "properties": {
                    "id": {"type": "string", "enum": [domain.id]},
                    "sources": {"type": "array", "items": {"type": "string", "enum": [source.id for source in domain.sources]}},
                },
                "required": ["id", "sources"],
                "additionalProperties": False,
            })
        schema["properties"]["domains"]["items"] = selections[0] if len(selections) == 1 else {"anyOf": selections}
        schema["properties"]["domains"]["minItems"] = 1
        schema["properties"]["domains"]["maxItems"] = len(selections)
        schema["$defs"].pop("DomainSelection", None)
        return schema

    def validate(self, payload: dict) -> Intent:
        intent = Intent.model_validate(payload)
        catalog = {domain.id: {source.id for source in domain.sources} for domain in self.domains}
        for domain in intent.domains:
            if domain.id not in catalog or not set(domain.sources).issubset(catalog[domain.id]):
                raise ValueError("Model selected an unavailable domain or source")
        return intent


SERVE_PROFILE = Profile(
    domains=(Domain(
        id="ai_index", description="AI Index 搜索数据域", aliases=("AI Index", "AI指数"),
        all_sources_aliases=("综合总结", "综合简报", "默认总结", "总结"),
        sources=(
            Source("news", "新闻、快讯", ("新闻", "快讯")),
            Source("twitter", "Twitter/X 上的观点", ("Twitter", "推特", "X", "X平台", "观点")),
            Source("github", "GitHub 开源项目", ("GitHub", "开源社区", "开源")),
            Source("huggingface", "Hugging Face 模型和数据集", ("Hugging Face", "HuggingFace", "HF", "开源社区", "开源")),
            Source("youtube", "YouTube 视频与传播", ("YouTube", "视频", "传播")),
        ),
    ),),
    default_domains=("ai_index",),
)
