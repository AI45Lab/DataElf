"""Stable extraction contract; every key is required, even when its value is empty."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


Text = Annotated[str, StringConstraints(min_length=1)]
DateText = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=240)]
PositiveInt = Annotated[int, Field(gt=0)]
LanguageTag = Literal["zh-CN", "en"]
OUTPUT_SCHEMA_VERSION = "2"


class Fields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("*", mode="after")
    @classmethod
    def validate_text_lists(cls, value):
        if isinstance(value, str) and (not value.strip() or value != value.strip()):
            raise ValueError("Text must be nonblank and trimmed")
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            if any(not item.strip() or item != item.strip() for item in value):
                raise ValueError("List entries must be nonblank, trimmed strings")
            if len(value) != len(set(value)):
                raise ValueError("List entries must be unique")
        return value


class Retrieval(Fields):
    keywords: list[Text]
    entities: list[Text]
    exclude_keywords: list[Text]


class TimeRange(Fields):
    start_date: DateText | None
    end_date: DateText | None

    @model_validator(mode="after")
    def validate_dates(self):
        start = date.fromisoformat(self.start_date) if self.start_date else None
        end = date.fromisoformat(self.end_date) if self.end_date else None
        if start and end and start > end:
            raise ValueError("start_date must be on or before end_date")
        return self


class DomainSelection(Fields):
    id: Text
    sources: list[Text]


class Comparison(Fields):
    subjects: list[ShortText]
    dimensions: list[ShortText]


class Quantity(Fields):
    target: PositiveInt | None
    min: PositiveInt | None
    max: PositiveInt | None

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must be on or below max")
        if self.target is not None and (
            (self.min is not None and self.target < self.min)
            or (self.max is not None and self.target > self.max)
        ):
            raise ValueError("target must be within explicit bounds")
        return self


class BodyLength(Quantity):
    scope: Literal["per_item", "total"] | None
    unit: Literal["characters", "words"] | None


class Synthesis(Fields):
    organization: Literal["theme", "entity", "event"] | None
    evidence_mode: Literal["cross_record", "per_record"] | None


class ContentRules(Fields):
    required: list[Literal["analytical_judgment", "potential_impacts"]]
    avoid: list[Literal["title_rewrite", "metrics_only", "one_item_per_record"]]


class Output(Fields):
    task_types: list[Literal["summary", "analysis", "comparison"]] = Field(description="主任务多选；保留明确的总结/分析/比较，不因数量修饰或混合非法指令而漏填。")
    focus_points: list[ShortText] = Field(description="明确业务角度或内容问题；不填泛化的关注点、模块名、执行指令。")
    comparison: Comparison
    audience: ShortText | None
    language: LanguageTag | None
    style: list[Literal["professional", "plain", "concise", "objective"]] = Field(description="仅明确表达风格；不根据研报、读者或任务类型推断。")
    item_count: Quantity = Field(description="输出内容条数，不是检索资料数、引用数或正文长度。")
    body_length: BodyLength = Field(description="只表示正文篇幅；没有明确篇幅要求时所有子字段均为null，不能挪用输出条数。")
    synthesis: Synthesis
    content_rules: ContentRules

    @model_validator(mode="after")
    def validate_evidence_mode(self):
        if self.synthesis.evidence_mode == "per_record" and "one_item_per_record" in self.content_rules.avoid:
            raise ValueError("per_record conflicts with avoiding one item per record")
        return self

    @classmethod
    def unspecified(cls) -> "Output":
        """Extraction defaults only; service writing defaults belong to its consumer."""
        return cls.model_validate({
            "task_types": [], "focus_points": [],
            "comparison": {"subjects": [], "dimensions": []},
            "audience": None, "language": None, "style": [],
            "item_count": {"target": None, "min": None, "max": None},
            "body_length": {"scope": None, "target": None, "min": None, "max": None, "unit": None},
            "synthesis": {"organization": None, "evidence_mode": None},
            "content_rules": {"required": [], "avoid": []},
        })


class Intent(Fields):
    retrieval: Retrieval = Field(description="只从默认段提取检索条件，不能使用写作类标题管辖正文中的内容。")
    time_range: TimeRange = Field(description="只从默认段提取检索日期；默认段未指定则起止日期均为null。")
    domains: list[DomainSelection] = Field(description="只从默认段提取检索领域/来源；写作段提到新闻、GitHub等不改变来源。")
    output: Output = Field(description="只从写作/总结等语义标题管辖的正文提取；没有此类标题时整个output保持空对象。")

    @model_validator(mode="after")
    def validate_domains(self):
        ids = [domain.id for domain in self.domains]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("domains must be nonempty with unique IDs")
        return self
