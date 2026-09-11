from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from dataelf_server.intent.schema import Output
from dataelf_server.analysis.writing import resolve_output, render_writing, writing_checks, write_writing_review


CONTRACT_VERSION = "dataelf-insight-output-contract.v1"
CONTRACT_JSON_NAME = "insight_output_contract.json"
CONTRACT_MARKDOWN_NAME = "insight_output_contract.md"

MODULE_LABELS = {
    "comprehensive": "综合模块",
    "brief": "快讯模块（新闻）",
    "opinion": "观点模块（Twitter/X）",
    "open_source": "开源社区模块（GitHub / Hugging Face）",
    "dissemination": "传播模块（YouTube）",
}

FORBIDDEN_OPENINGS = (
    "今日多条新闻显示",
    "多条新闻显示",
    "今日新闻显示",
    "今日数据表明",
    "数据表明",
    "围绕",
    "提及",
    "据报道",
    "有消息称",
    "网友认为",
    "社区热议",
    "多位用户提到",
)

COMMON_RULES = """## 公共写作要求

- 最终 `title` 和 `thesis` 必须使用简体中文；公司、模型、产品、项目及账号等专有名词可保留原文。
- `title` 必须是直接表达判断的主标题，去除空白后不超过 18 个可见字符。
- `thesis` 是 API 对外的 `content`，必须直接陈述核心事实和判断，使用一个紧凑自然段。
- 禁止以“今日多条新闻显示”“围绕”“提及”“数据表明”“据报道”“有消息称”等来源或过程铺垫开头。
- 不得输出表格、Markdown 标题、项目符号、长列表、分析方法、来源说明或无关背景。
- 语言应客观、克制、专业、判断明确，适合线上业务首屏展示；不得写成材料汇总、新闻流水账或单纯标题复述。
- 每项事实、机构、人物、产品和数字必须来自本任务预取数据或已执行的分析制品；不得补写、猜测或编造。
- 数据不足时只做保守判断，宁可减少洞察，也不得硬凑结论。
- 来源只通过 `external_support` 提交，正文不要解释引用过程；来源标题必须保持原文，不翻译、不截断、不改写。
- 洞察按重要性排序，并删除语义重复或只有措辞差异的项目。
"""

MODULE_RULES = {
    "comprehensive": """## 综合模块要求

- 每条 `thesis` 去除空白后必须为 80–120 个可见字符。
- 每条正文直接陈述核心事件和趋势含义，不得使用“今日多条新闻显示”等铺垫句。
- 数据型洞察必须写清“现象 + 关键数据 + 简短解读”；只有来源中确实存在数字时才能引用数字。
- 新闻型洞察必须写清“事件 + 影响判断 + 趋势含义”，不强制提供数字，但必须写成研报判断而不是新闻摘要。
- 优先选择能代表当前 AI 趋势变化的模型发布、产品迭代、头部公司动作、机构或人才流动、政策监管和开源社区活跃信号。
- 不要输出方法解释、来源说明、无关背景或为了满足长度而重复同一判断。
""",
    "brief": """## 快讯模块要求（新闻）

- 每条洞察必须包含“事件概括 + 影响判断 + 趋势含义”。
- 新闻场景不强制数字支撑，但必须基于明确新闻事件。
- 直接进入判断，禁止“今日多条新闻显示”“围绕”“提及”“据报道”“有消息称”等来源铺垫。
- 不得罗列新闻标题、逐条复述材料或写成新闻流水账。
- 不得编造公司、产品、人物、融资、监管或时间信息。
""",
    "opinion": """## 观点模块要求（Twitter/X）

- 每条洞察应包含“核心分歧或共识 + 代表性观点 + 对行业趋势的判断”。
- 不得复述单条推文，不得写成舆情摘要。
- 禁止“网友认为”“社区热议”“多位用户提到”等空泛表达。
- 存在明确对立时写清分歧点；多个独立来源趋同时才可概括共识。
- 只有单一或不足以比较的来源时，只能陈述代表性观点及其潜在行业含义，不得虚构共识或分歧。
- 不得编造账号、观点、转发量或影响力数据。
""",
    "open_source": """## 开源社区模块要求（GitHub / Hugging Face）

- 每条洞察必须包含“项目或模型变化 + 热度或能力信号 + 对开发生态的影响”。
- 来源存在 star、下载量、模型热度或提交活跃度等数据时应优先使用；没有数字时不得补写数字。
- 优先关注新模型发布、热门仓库信号、工具链变化和开发者采用门槛变化。
- 不得写成项目清单，不得只罗列名称。
- 不得编造仓库、模型、作者、star、下载量或 benchmark。
""",
    "dissemination": """## 传播模块要求（YouTube）

- 每条洞察必须包含“传播主题 + 受众关注点 + 对市场认知的影响”。
- 来源存在播放量、互动量、频道或发布时间数据时可以使用；没有数字时不得补写数字。
- 不得写成视频列表，不得逐条复述标题。
- 优先判断哪些 AI 议题正在从技术圈扩散到产品用户、开发者或普通消费者。
- 不得编造频道、播放量、观点或视频内容。
""",
}


def build_contract(mode: str, modules: Iterable[str], *, output: Output | None = None,
                   retrieval: dict[str, Any] | None = None) -> dict[str, Any]:
    selected = _normalize_modules(mode, modules)
    contract = {
        "version": CONTRACT_VERSION,
        "mode": str(mode or "unknown"),
        "modules": selected,
        "title_max_chars": 18,
        "require_chinese": True,
        "forbidden_openings": list(FORBIDDEN_OPENINGS),
        "comprehensive_content_min_chars": 80,
        "comprehensive_content_max_chars": 120,
    }
    if output is not None:
        effective = resolve_output(output, selected)
        contract.update(requested_output=output.model_dump(), effective_output=effective.model_dump(),
                        writing_policy_version=2, require_chinese=effective.language.startswith("zh"))
        if retrieval is not None:
            contract["retrieval"] = retrieval
    return contract


def render_contract(contract: dict[str, Any]) -> str:
    if "effective_output" in contract:
        return render_writing(contract)
    modules = [str(value) for value in contract.get("modules", [])]
    labels = "、".join(MODULE_LABELS.get(value, value) for value in modules)
    sections = [
        "# DataElf Insight Output Contract",
        "",
        "这是最终 Insight 的强制写作契约，优先级高于基础 DataElf Prompt 中的示例措辞。",
        f"当前任务适用模块：{labels or '通用'}。",
        "",
        COMMON_RULES.strip(),
    ]
    if len(modules) > 1:
        sections.extend(
            [
                "",
                "## 多模块适用规则",
                "",
                "- 每条 Insight 按其主要来源应用对应模块要求，不得把所有模块的句式强行塞入同一条正文。",
                "- 不同模块的 Insight 保持在同一数组中，并按整体重要性排序。",
            ]
        )
    for module in modules:
        rules = MODULE_RULES.get(module)
        if rules:
            sections.extend(["", rules.strip()])
    return "\n".join(sections).rstrip() + "\n"


def write_contract(workspace: Path, *, mode: str, modules: Iterable[str], output: Output | None = None,
                   retrieval: dict[str, Any] | None = None) -> Path:
    prompt_dir = workspace / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    contract = build_contract(mode, modules, output=output, retrieval=retrieval)
    json_path = prompt_dir / CONTRACT_JSON_NAME
    markdown_path = prompt_dir / CONTRACT_MARKDOWN_NAME
    json_path.write_text(
        json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(render_contract(contract), encoding="utf-8")
    return markdown_path.resolve()


def load_contract(workspace: Path) -> dict[str, Any] | None:
    path = workspace / "prompts" / CONTRACT_JSON_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("version") != CONTRACT_VERSION:
        return None
    return value


def load_contract_text(workspace: Path) -> str:
    path = workspace / "prompts" / CONTRACT_MARKDOWN_NAME
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def validate_workspace_insights(workspace: Path) -> list[str]:
    contract = load_contract(workspace)
    if contract is None:
        return []
    path = workspace / "insights" / "insight_candidates.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    insights = document.get("insight_candidates") if isinstance(document, dict) else None
    if not isinstance(insights, list):
        return []
    source_evidence = load_source_evidence(workspace)
    write_writing_review(workspace, insights, contract)
    return validate_insights(
        insights,
        contract,
        source_evidence=source_evidence or None,
    )


def validate_insights(
    insights: list[Any],
    contract: dict[str, Any],
    *,
    source_evidence: dict[str, str] | None = None,
) -> list[str]:
    issues: list[str] = []
    configurable = "effective_output" in contract
    language = contract.get("effective_output", {}).get("language", "zh-CN")
    seen_content: set[str] = set()
    forbidden = tuple(str(value) for value in contract.get("forbidden_openings", []))

    for index, item in enumerate(insights, start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        content = str(item.get("thesis") or "").strip()
        if title and language.startswith("zh") and not _contains_chinese(title):
            issues.append(f"Insight {index} title must contain Simplified Chinese.")
        if content and language.startswith("zh") and not _contains_chinese(content):
            issues.append(f"Insight {index} thesis must contain Simplified Chinese.")
        opening = next((value for value in forbidden if content.startswith(value)), None) if not configurable else None
        if opening:
            issues.append(
                f"Insight {index} thesis uses forbidden opening phrase: {opening}."
            )
        if not configurable and _contains_markdown_structure(content):
            issues.append(
                f"Insight {index} thesis must be one prose paragraph without Markdown tables, headings, or lists."
            )
        if source_evidence is not None:
            cited = _cited_evidence(item, source_evidence)
            if not cited:
                issues.append(
                    f"Insight {index} must cite at least one valid prefetched source."
                )
            else:
                unsupported = _unsupported_verbatim_claim_tokens(
                    title + " " + content, cited, prose_language=language
                )
                if unsupported:
                    issues.append(
                        f"Insight {index} contains numbers or Latin proper nouns not found in its cited sources: {', '.join(unsupported)}."
                    )
        identity = re.sub(r"[\W_]+", "", title + content, flags=re.UNICODE).casefold()
        if identity and identity in seen_content:
            issues.append(f"Insight {index} duplicates an earlier insight.")
        seen_content.add(identity)
    if configurable:
        issues.extend(writing_checks(insights, contract)["errors"])
    return issues


def visible_char_count(value: str) -> int:
    return len(re.sub(r"\s+", "", value))


def _normalize_modules(mode: str, modules: Iterable[str]) -> list[str]:
    values = [str(value) for value in modules if str(value) in MODULE_LABELS]
    if mode == "comprehensive_daily" or "comprehensive" in values:
        return ["comprehensive"]
    return list(dict.fromkeys(values))


def _contains_chinese(value: str) -> bool:
    return re.search(r"[\u3400-\u9fff]", value) is not None


def _contains_markdown_structure(value: str) -> bool:
    lines = value.splitlines()
    return any(
        re.match(r"^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s)", line)
        or (line.count("|") >= 2)
        for line in lines
    )


def load_source_evidence(workspace: Path) -> dict[str, str]:
    """Return the exact prefetched evidence indexed by Scope V2 source ID."""
    evidence: dict[str, str] = {}
    for path in sorted(workspace.glob("scope_v2/*/result.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sources = document.get("sources") if isinstance(document, dict) else None
        if not isinstance(sources, dict):
            continue
        for block in sources.values():
            items = block.get("items") if isinstance(block, dict) else None
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                source_id = str(item.get("source_id") or "").strip()
                if source_id:
                    evidence[source_id] = json.dumps(
                        {
                            "title": item.get("title"),
                            "published_at": item.get("published_at"),
                            "data": item.get("data"),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ).casefold()
    return evidence


def _cited_evidence(
    insight: dict[str, Any], source_evidence: dict[str, str]
) -> str:
    support = insight.get("external_support")
    if not isinstance(support, list):
        return ""
    values: list[str] = []
    for reference in support:
        if not isinstance(reference, dict):
            continue
        source_id = str(reference.get("source_id") or "").strip()
        value = source_evidence.get(source_id)
        if value:
            values.append(value)
    return "\n".join(values)


def _unsupported_verbatim_claim_tokens(text: str, evidence: str, *, prose_language: str = "zh-CN") -> list[str]:
    numeric_candidates = re.findall(
        r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)*(?:%|万|亿)?(?![A-Za-z0-9])",
        text,
    )
    latin_candidates = [
        token
        for token in re.findall(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9.+#_-]{2,}", text)
        if any(character.isupper() or character.isdigit() for character in token)
    ]
    if prose_language == "en":
        # Sentence/title capitalization is normal prose, not proof of a proper noun.
        # Keep acronyms, model identifiers and mixed-case names; semantic claims still
        # need evidence review and are not certified by this lexical check.
        latin_candidates = [token for token in latin_candidates if token.isupper()
                            or any(c.isdigit() for c in token) or any(c.isupper() for c in token[1:])]
    unsupported: list[str] = []
    normalized_evidence = _normalize_latin_evidence(evidence)
    evidence_numbers = _extract_canonical_numbers(evidence)
    for token in dict.fromkeys(numeric_candidates):
        normalized = re.sub(r"[\s,]+", "", token.casefold())
        canonical = _canonical_number(token)
        if normalized not in normalized_evidence and (
            canonical is None or canonical not in evidence_numbers
        ):
            unsupported.append(token)
    for token in dict.fromkeys(latin_candidates):
        normalized = _normalize_latin_evidence(token)
        if normalized and normalized not in normalized_evidence:
            unsupported.append(token)
    return unsupported[:12]


def _normalize_latin_evidence(value: str) -> str:
    """Normalize harmless formatting differences in names such as W4A8-C8."""
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _extract_canonical_numbers(value: str) -> set[tuple[Decimal, str]]:
    matches = re.findall(
        r"(?<![A-Za-z0-9])(?:[$¥￥€£]\s*)?"
        r"\d+(?:,\d{3})*(?:\.\d+)?\s*"
        r"(?:%|万|亿|k|m|b|bn|thousand|million|billion)?"
        r"(?![A-Za-z0-9])",
        value,
        flags=re.IGNORECASE,
    )
    return {
        canonical
        for match in matches
        if (canonical := _canonical_number(match)) is not None
    }


def _canonical_number(value: str) -> tuple[Decimal, str] | None:
    cleaned = re.sub(r"[$¥￥€£\s,]+", "", value.casefold())
    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)(%|万|亿|k|m|b|bn|thousand|million|billion)?",
        cleaned,
    )
    if match is None:
        return None
    try:
        number = Decimal(match.group(1))
    except InvalidOperation:
        return None
    unit = match.group(2) or ""
    if unit == "%":
        return number, "percent"
    multiplier = {
        "": Decimal(1),
        "k": Decimal(1_000),
        "thousand": Decimal(1_000),
        "万": Decimal(10_000),
        "m": Decimal(1_000_000),
        "million": Decimal(1_000_000),
        "亿": Decimal(100_000_000),
        "b": Decimal(1_000_000_000),
        "bn": Decimal(1_000_000_000),
        "billion": Decimal(1_000_000_000),
    }[unit]
    return number * multiplier, "scalar"


__all__ = [
    "CONTRACT_JSON_NAME",
    "CONTRACT_MARKDOWN_NAME",
    "build_contract",
    "load_contract",
    "load_contract_text",
    "render_contract",
    "validate_insights",
    "validate_workspace_insights",
    "visible_char_count",
    "write_contract",
]
