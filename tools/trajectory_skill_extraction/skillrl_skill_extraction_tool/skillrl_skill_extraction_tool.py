from __future__ import annotations

import json
from time import perf_counter
from typing import Any

from ...base_tool import BaseTool, ToolContext

MemoryRecord = dict[str, Any]
CategoryBuckets = dict[str, dict[str, list[MemoryRecord]]]

SUPPORTED_DOMAINS = ("alfworld", "search", "webshop")
DEFAULT_LLM_MODEL = "o3"
DEFAULT_MAX_COMPLETION_TOKENS = 4096
SUCCESS_OUTCOME = "success"
FAILURE_OUTCOME = "failure"

ALFWORLD_TASK_TYPES = (
    "pick_and_place",
    "look_at_obj_in_light",
    "clean",
    "heat",
    "cool",
    "examine",
)

ALFWORLD_TASK_DESCRIPTIONS = {
    "pick_and_place": "Pick up object(s) from one location and place them at a target location",
    "look_at_obj_in_light": "Find an object and examine it under a light source (usually desklamp)",
    "clean": "Find an object, clean it in a sink/basin, then place it somewhere",
    "heat": "Find an object, heat it in microwave, then place it somewhere",
    "cool": "Find an object, cool it in fridge, then place it somewhere",
    "examine": "Find and examine a specific object",
}

SEARCH_QUERY_TYPES = (
    "direct_retrieval",
    "multi_hop_reasoning",
    "entity_attribute_lookup",
    "comparison",
)

SEARCH_QUERY_TYPE_DESCRIPTIONS = {
    "direct_retrieval": (
        "Answer factoid questions (who/what/when/where) by searching and extracting answers "
        "directly from documents. Sources: Natural Questions, TriviaQA."
    ),
    "multi_hop_reasoning": (
        "Answer questions requiring chained reasoning across multiple entities or facts. "
        "Must decompose the question, search for intermediate facts, and combine them. "
        "Sources: HotpotQA, 2WikiMultiHopQA, MuSiQue, Bamboogle."
    ),
    "entity_attribute_lookup": (
        "Look up specific attributes (occupation, birthplace, genre, etc.) of named entities. "
        "Sources: PopQA."
    ),
    "comparison": (
        "Compare two or more entities on a specific attribute. Requires retrieving info about "
        "each entity and then synthesizing the comparison."
    ),
}

SEARCH_COMPARISON_KEYWORDS = (
    "both",
    "are the",
    "which of",
    "same",
    "common",
    "more",
    "less",
    "older",
    "younger",
    "taller",
    "shorter",
)

WEBSHOP_PRODUCT_TYPES = (
    "apparel",
    "footwear",
    "home_decor",
    "electronics",
    "accessories",
    "beauty_health",
    "other",
)

WEBSHOP_PRODUCT_DESCRIPTIONS = {
    "apparel": (
        "Clothing items like shirts, dresses, pants, jackets, often requiring size and color selection"
    ),
    "footwear": (
        "Shoes, boots, sandals, and slippers, usually requiring size and sometimes color selection"
    ),
    "home_decor": (
        "Home decoration items like pillows, curtains, rugs, and lamps, often with size or color options"
    ),
    "electronics": "Electronic devices and accessories like phones, chargers, and computer accessories",
    "accessories": "Fashion accessories like bags, wallets, jewelry, hats, and scarves",
    "beauty_health": "Beauty and health products like skincare, cosmetics, and bathing accessories",
    "other": "Miscellaneous products that do not fit into the other categories",
}

WEBSHOP_KEYWORD_GROUPS = {
    "apparel": (
        "shirt",
        "dress",
        "t-shirt",
        "polo",
        "pants",
        "jeans",
        "jacket",
        "coat",
        "sweater",
        "blouse",
        "skirt",
        "shorts",
        "underwear",
        "swimsuit",
        "swimwear",
        "hoodie",
        "vest",
        "cardigan",
        "suit",
        "blazer",
        "tee",
        "top",
    ),
    "footwear": (
        "shoe",
        "boot",
        "sandal",
        "sneaker",
        "slipper",
        "loafer",
        "heel",
        "flat",
        "oxford",
        "pump",
        "moccasin",
        "flip-flop",
        "footwear",
    ),
    "home_decor": (
        "pillow",
        "curtain",
        "rug",
        "mat",
        "blanket",
        "bedding",
        "towel",
        "lamp",
        "decor",
        "furniture",
        "cushion",
        "sheet",
        "tablecloth",
        "vase",
    ),
    "electronics": (
        "phone",
        "laptop",
        "tablet",
        "computer",
        "headphone",
        "earphone",
        "speaker",
        "charger",
        "cable",
        "mouse",
        "keyboard",
        "monitor",
        "camera",
        "watch",
        "smartwatch",
        "electronic",
        "device",
        "gadget",
        "armoires",
    ),
    "accessories": (
        "bag",
        "wallet",
        "belt",
        "hat",
        "cap",
        "scarf",
        "glove",
        "jewelry",
        "necklace",
        "bracelet",
        "ring",
        "earring",
        "sunglasses",
        "glasses",
        "watch",
        "purse",
        "backpack",
        "handbag",
        "tie",
        "bow",
    ),
    "beauty_health": (
        "makeup",
        "cosmetic",
        "skincare",
        "lotion",
        "cream",
        "shampoo",
        "conditioner",
        "perfume",
        "cologne",
        "brush",
        "bathing",
        "soap",
        "body wash",
        "nail",
        "lipstick",
        "mascara",
        "foundation",
        "serum",
        "moisturizer",
    ),
}


class SkillRLSkillExtractionTool(BaseTool):
    @property
    def name(self) -> str:
        return "skillrl_skill_extraction"

    @property
    def description(self) -> str:
        return (
            "Extract Claude-style skill banks from SkillRL-style memory trajectories. "
            "Supports alfworld, search, and webshop domains."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "SkillRL memory objects. Each record should contain tags.outcome, "
                        "content.task_meta.original_goal, and optional refined_trajectory / strategic_guidelines."
                    ),
                },
                "domain": {
                    "type": "string",
                    "enum": list(SUPPORTED_DOMAINS),
                    "description": "Skill extraction strategy to use for the input memories.",
                    "default": "alfworld",
                },
                "llm_model": {
                    "type": "string",
                    "description": "Model name passed to context.llm.generate.",
                    "default": DEFAULT_LLM_MODEL,
                },
                "max_completion_tokens": {
                    "type": "integer",
                    "description": "Max completion tokens for each LLM generation call.",
                    "default": DEFAULT_MAX_COMPLETION_TOKENS,
                },
            },
            "required": ["data"],
        }

    def usage_example(self) -> str:
        return """skill_bank = run_tool(
    "skillrl_skill_extraction",
    data=data,
    domain="alfworld",
    llm_model="o3"
)"""

    def _require_llm(self, context: ToolContext) -> None:
        if context.llm is None:
            raise ValueError(
                "LLM is required for skill extraction. Configure `tool_llm` in config.yaml "
                "or provide context.llm in tests."
            )

    def _parse_json_array(self, text: str) -> tuple[list[dict[str, Any]], bool]:
        if not isinstance(text, str):
            return [], False

        json_start = text.find("[")
        json_end = text.rfind("]") + 1
        if json_start == -1 or json_end <= json_start:
            return [], False

        try:
            parsed = json.loads(text[json_start:json_end])
        except json.JSONDecodeError:
            return [], False

        if not isinstance(parsed, list):
            return [], False

        return parsed, True

    def _log_llm_parse_warning(self, context: ToolContext, prompt_name: str) -> None:
        context.log(
            f"[{self.name}] LLM output for {prompt_name} did not contain a parsable JSON array.",
            "warning",
        )

    def _generate_json_array(
        self,
        context: ToolContext,
        *,
        prompt_name: str,
        prompt: str,
        llm_model: str,
        max_completion_tokens: int,
    ) -> list[dict[str, Any]]:
        context.log(
            f"[{self.name}] LLM generation started: prompt={prompt_name}, model={llm_model}, "
            f"max_completion_tokens={max_completion_tokens}",
            "info",
        )
        started_at = perf_counter()
        response = context.llm.generate(
            model=llm_model,
            prompt=prompt,
            max_completion_tokens=max_completion_tokens,
        )
        items, parsed = self._parse_json_array(response)
        elapsed_s = round(perf_counter() - started_at, 2)
        if not parsed:
            self._log_llm_parse_warning(context, prompt_name)
        context.log(
            f"[{self.name}] LLM generation completed: prompt={prompt_name}, parsed_items={len(items)}, "
            f"parsed={parsed}, elapsed={elapsed_s}s",
            "info",
        )
        return items

    def _count_non_empty_categories(self, buckets: CategoryBuckets) -> int:
        return sum(
            1
            for outcomes in buckets.values()
            if outcomes[SUCCESS_OUTCOME] or outcomes[FAILURE_OUTCOME]
        )

    def _empty_buckets(self, categories: tuple[str, ...]) -> CategoryBuckets:
        return {
            category: {SUCCESS_OUTCOME: [], FAILURE_OUTCOME: []}
            for category in categories
        }

    def _goal_text(self, memory: MemoryRecord) -> str:
        content = memory.get("content")
        if not isinstance(content, dict):
            return ""

        task_meta = content.get("task_meta")
        if not isinstance(task_meta, dict):
            return ""

        goal = task_meta.get("original_goal")
        return goal.strip() if isinstance(goal, str) else ""

    def _goal_text_lower(self, memory: MemoryRecord) -> str:
        return self._goal_text(memory).lower()

    def _tags(self, memory: MemoryRecord) -> dict[str, Any]:
        tags = memory.get("tags")
        return tags if isinstance(tags, dict) else {}

    def _outcome_bucket(self, memory: MemoryRecord) -> str | None:
        outcome = self._tags(memory).get("outcome")
        if not isinstance(outcome, str) or not outcome.strip():
            return None
        return SUCCESS_OUTCOME if outcome.strip().lower() == SUCCESS_OUTCOME else FAILURE_OUTCOME

    def _trajectory_steps(self, memory: MemoryRecord) -> list[dict[str, str]]:
        content = memory.get("content")
        if not isinstance(content, dict):
            return []

        trajectory = content.get("refined_trajectory") or []
        if isinstance(trajectory, dict):
            trajectory = trajectory.get("refined_trajectory", [])
        if not isinstance(trajectory, list):
            return []

        steps: list[dict[str, str]] = []
        for step in trajectory[:5]:
            if not isinstance(step, dict):
                continue
            steps.append(
                {
                    "action": step.get("action", "") if isinstance(step.get("action", ""), str) else "",
                    "reasoning": (
                        step.get("reasoning", "") if isinstance(step.get("reasoning", ""), str) else ""
                    ),
                }
            )
        return steps

    def _strategic_guidelines(self, memory: MemoryRecord) -> dict[str, Any]:
        content = memory.get("content")
        if not isinstance(content, dict):
            return {}

        strategic = content.get("strategic_guidelines") or {}
        if not isinstance(strategic, dict):
            return {}

        nested = strategic.get("strategic_guidelines")
        if isinstance(nested, dict):
            return nested
        return strategic

    def _mistakes_to_avoid(self, memory: MemoryRecord) -> list[str]:
        raw = self._strategic_guidelines(memory).get("mistakes_to_avoid", [])
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, str)][:3]

    def _planning_pattern(self, memory: MemoryRecord) -> str:
        planning = self._strategic_guidelines(memory).get("planning_pattern", "")
        return planning if isinstance(planning, str) else ""

    def _normalize_memories(
        self, context: ToolContext, data: Any
    ) -> tuple[list[MemoryRecord], int]:
        if not isinstance(data, list):
            raise ValueError("`data` must be a list of SkillRL memory objects.")

        valid_memories: list[MemoryRecord] = []
        skipped_records = 0

        for memory in data:
            if not isinstance(memory, dict):
                skipped_records += 1
                continue

            if not self._goal_text(memory) or self._outcome_bucket(memory) is None:
                skipped_records += 1
                continue

            valid_memories.append(memory)

        if skipped_records:
            context.log(
                f"[{self.name}] Skipped {skipped_records} invalid memory record(s) missing required fields.",
                "warning",
            )

        return valid_memories, skipped_records

    def _extract_patterns(
        self,
        memories: list[MemoryRecord],
        *,
        limit: int,
        include_data_source: bool = False,
    ) -> str:
        patterns: list[dict[str, Any]] = []
        for memory in memories[:limit]:
            pattern: dict[str, Any] = {
                "goal": self._goal_text(memory),
                "steps": self._trajectory_steps(memory),
                "planning_pattern": self._planning_pattern(memory),
                "mistakes": self._mistakes_to_avoid(memory),
            }
            if include_data_source:
                data_source = self._tags(memory).get("data_source", "")
                pattern["data_source"] = data_source if isinstance(data_source, str) else ""
            patterns.append(pattern)
        return json.dumps(patterns, indent=2)

    def _distribution_from_buckets(self, buckets: CategoryBuckets) -> dict[str, dict[str, int]]:
        return {
            category: {
                SUCCESS_OUTCOME: len(outcomes[SUCCESS_OUTCOME]),
                FAILURE_OUTCOME: len(outcomes[FAILURE_OUTCOME]),
            }
            for category, outcomes in buckets.items()
        }

    def _top_level_metadata(
        self,
        *,
        domain: str,
        records_received: int,
        records_processed: int,
        records_skipped: int,
        llm_model: str,
    ) -> dict[str, Any]:
        return {
            "tool_name": self.name,
            "domain": domain,
            "records_received": records_received,
            "records_processed": records_processed,
            "records_skipped": records_skipped,
            "llm_model": llm_model,
        }

    def _report_markdown(self, metadata: dict[str, Any], result_metadata: dict[str, Any]) -> str:
        return "\n".join(
            [
                "# Skill Extraction Report",
                "",
                f"- domain: {metadata['domain']}",
                f"- records_received: {metadata['records_received']}",
                f"- records_processed: {metadata['records_processed']}",
                f"- records_skipped: {metadata['records_skipped']}",
                f"- llm_model: {metadata['llm_model']}",
                f"- source: {result_metadata['source']}",
            ]
        )

    def _empty_result(self, *, domain: str, llm_model: str) -> dict[str, Any]:
        if domain == "search":
            return {
                "general_skills": [],
                "query_type_skills": {query_type: [] for query_type in SEARCH_QUERY_TYPES},
                "common_mistakes": [],
                "metadata": {
                    "source": f"generated from Search AI agent trajectories using {llm_model}",
                    "total_memories_analyzed": 0,
                    "query_type_distribution": {
                        query_type: {SUCCESS_OUTCOME: 0, FAILURE_OUTCOME: 0}
                        for query_type in SEARCH_QUERY_TYPES
                    },
                },
            }

        category_key = "product_distribution" if domain == "webshop" else "task_distribution"
        categories = WEBSHOP_PRODUCT_TYPES if domain == "webshop" else ALFWORLD_TASK_TYPES

        return {
            "general_skills": [],
            "task_specific_skills": {category: [] for category in categories},
            "common_mistakes": [],
            "metadata": {
                "source": f"generated from {domain} trajectories using {llm_model}",
                "total_memories_analyzed": 0,
                category_key: {
                    category: {SUCCESS_OUTCOME: 0, FAILURE_OUTCOME: 0}
                    for category in categories
                },
            },
        }

    def _categorize_by_task_type_alfworld(self, memories: list[MemoryRecord]) -> CategoryBuckets:
        categorized = self._empty_buckets(ALFWORLD_TASK_TYPES)
        for memory in memories:
            goal = self._goal_text_lower(memory)
            outcome = self._outcome_bucket(memory)
            if outcome is None:
                continue

            task_type: str | None = None
            if "look at" in goal and "under" in goal:
                task_type = "look_at_obj_in_light"
            elif "clean" in goal:
                task_type = "clean"
            elif "heat" in goal:
                task_type = "heat"
            elif "cool" in goal:
                task_type = "cool"
            elif "examine" in goal or "find" in goal:
                task_type = "examine"
            elif "put" in goal:
                task_type = "pick_and_place"

            if task_type is not None:
                categorized[task_type][outcome].append(memory)
        return categorized

    def _generate_general_skills_alfworld(
        self,
        context: ToolContext,
        categorized_memories: CategoryBuckets,
        llm_model: str,
        max_completion_tokens: int,
    ) -> list[dict[str, Any]]:
        all_successes: list[MemoryRecord] = []
        all_failures: list[MemoryRecord] = []
        for bucket in categorized_memories.values():
            all_successes.extend(bucket[SUCCESS_OUTCOME][:5])
            all_failures.extend(bucket[FAILURE_OUTCOME][:5])

        prompt = f"""You are an expert at distilling agent behavior patterns into concise, actionable skills.

Analyze these successful and failed trajectories from an embodied AI agent operating in household environments (ALFWorld).

SUCCESSFUL TRAJECTORIES:
{self._extract_patterns(all_successes, limit=10)}

FAILED TRAJECTORIES:
{self._extract_patterns(all_failures, limit=10)}

Generate 8-12 GENERAL SKILLS that apply across ALL task types. These should be:
1. **Concise** - Each skill should be 1-2 sentences max
2. **Actionable** - Clear what to do, not vague principles
3. **Transferable** - Apply to pick_and_place, heat, cool, clean, examine, look_at_obj_in_light tasks
4. **Failure-aware** - Derived from what went wrong in failures

Format as JSON array:
[
    {{
        "skill_id": "gen_001",
        "title": "Short title (3-5 words)",
        "principle": "The core actionable insight in 1-2 sentences",
        "when_to_apply": "Specific trigger condition"
    }}
]

Focus on:
- Navigation and exploration strategies
- Object manipulation principles
- State tracking and goal decomposition
- Error recovery patterns
- Container and furniture interaction rules

Return ONLY the JSON array, no other text."""

        return self._generate_json_array(
            context,
            prompt_name="alfworld_general_skills",
            prompt=prompt,
            llm_model=llm_model,
            max_completion_tokens=max_completion_tokens,
        )

    def _generate_task_specific_skills_alfworld(
        self,
        context: ToolContext,
        task_type: str,
        successes: list[MemoryRecord],
        failures: list[MemoryRecord],
        llm_model: str,
        max_completion_tokens: int,
    ) -> list[dict[str, Any]]:
        if not successes and not failures:
            return []

        prompt = f"""You are an expert at distilling agent behavior patterns into concise, actionable skills.

Task Type: {task_type.upper()}
Description: {ALFWORLD_TASK_DESCRIPTIONS.get(task_type, "")}

SUCCESSFUL TRAJECTORIES:
{self._extract_patterns(successes, limit=8)}

FAILED TRAJECTORIES:
{self._extract_patterns(failures, limit=8) if failures else "[]"}

Generate 4-6 TASK-SPECIFIC SKILLS for {task_type} tasks. These should be:
1. **Concise** - 1-2 sentences max per skill
2. **Specific** - Apply specifically to {task_type} tasks
3. **Actionable** - Clear steps or decision rules
4. **Pattern-based** - Identify what makes success vs failure

Format as JSON array:
[
    {{
        "skill_id": "{task_type[:3]}_001",
        "title": "Short title (3-5 words)",
        "principle": "The core actionable insight",
        "when_to_apply": "Specific trigger condition"
    }}
]

Return ONLY the JSON array, no other text."""

        return self._generate_json_array(
            context,
            prompt_name=f"alfworld_task_specific_skills_{task_type}",
            prompt=prompt,
            llm_model=llm_model,
            max_completion_tokens=max_completion_tokens,
        )

    def _generate_common_mistakes_alfworld(
        self,
        context: ToolContext,
        categorized_memories: CategoryBuckets,
        llm_model: str,
        max_completion_tokens: int,
    ) -> list[dict[str, Any]]:
        all_failures: list[dict[str, Any]] = []
        for task_type, bucket in categorized_memories.items():
            for memory in bucket[FAILURE_OUTCOME][:5]:
                mistakes = self._mistakes_to_avoid(memory)
                if not mistakes:
                    continue
                all_failures.append(
                    {
                        "task_type": task_type,
                        "goal": self._goal_text(memory),
                        "mistakes": mistakes,
                    }
                )

        if not all_failures:
            return []

        prompt = f"""You are an expert at analyzing agent failures and distilling them into avoidable mistakes.

Analyze these failure patterns from an embodied AI agent:

{json.dumps(all_failures[:15], indent=2)}

Generate 8-12 COMMON MISTAKES to avoid. Format as JSON array:
[
    {{
        "mistake_id": "err_001",
        "description": "What the mistake is (1 sentence)",
        "why_it_happens": "Why agents make this mistake (1 sentence)",
        "how_to_avoid": "Concrete actionable fix (1-2 sentences)"
    }}
]

Focus on:
- Exploration failures (getting stuck, not finding objects)
- State management errors (forgetting what you are holding)
- Goal misunderstanding (wrong object, incomplete task)
- Inefficient action sequences

Return ONLY the JSON array, no other text."""

        return self._generate_json_array(
            context,
            prompt_name="alfworld_common_mistakes",
            prompt=prompt,
            llm_model=llm_model,
            max_completion_tokens=max_completion_tokens,
        )

    def _extract_skill_bank_alfworld(
        self,
        context: ToolContext,
        memories: list[MemoryRecord],
        llm_model: str,
        max_completion_tokens: int,
    ) -> dict[str, Any]:
        categorized = self._categorize_by_task_type_alfworld(memories)
        non_empty_categories = self._count_non_empty_categories(categorized)
        estimated_llm_calls = 1 + non_empty_categories + 1
        context.log(
            f"[{self.name}] ALFWorld extraction plan: categories={non_empty_categories}, "
            f"estimated_llm_calls={estimated_llm_calls}",
            "info",
        )

        general_skills = self._generate_general_skills_alfworld(
            context=context,
            categorized_memories=categorized,
            llm_model=llm_model,
            max_completion_tokens=max_completion_tokens,
        )
        task_specific_skills = {
            task_type: self._generate_task_specific_skills_alfworld(
                context=context,
                task_type=task_type,
                successes=bucket[SUCCESS_OUTCOME],
                failures=bucket[FAILURE_OUTCOME],
                llm_model=llm_model,
                max_completion_tokens=max_completion_tokens,
            )
            for task_type, bucket in categorized.items()
        }
        common_mistakes = self._generate_common_mistakes_alfworld(
            context=context,
            categorized_memories=categorized,
            llm_model=llm_model,
            max_completion_tokens=max_completion_tokens,
        )

        return {
            "general_skills": general_skills,
            "task_specific_skills": task_specific_skills,
            "common_mistakes": common_mistakes,
            "metadata": {
                "source": f"generated from ALFWorld trajectories using {llm_model}",
                "total_memories_analyzed": len(memories),
                "task_distribution": self._distribution_from_buckets(categorized),
            },
        }

    def _classify_query_type(self, memory: MemoryRecord) -> str:
        data_source = self._tags(memory).get("data_source", "")
        goal = self._goal_text_lower(memory)

        if data_source in ("hotpotqa", "2wikimultihopqa", "musique", "bamboogle"):
            if any(keyword in goal for keyword in SEARCH_COMPARISON_KEYWORDS):
                return "comparison"
            return "multi_hop_reasoning"

        if data_source == "popqa":
            return "entity_attribute_lookup"

        return "direct_retrieval"

    def _categorize_by_query_type_search(self, memories: list[MemoryRecord]) -> CategoryBuckets:
        categorized = self._empty_buckets(SEARCH_QUERY_TYPES)
        for memory in memories:
            query_type = self._classify_query_type(memory)
            outcome = self._outcome_bucket(memory)
            if outcome is not None:
                categorized[query_type][outcome].append(memory)
        return categorized

    def _generate_general_skills_search(
        self,
        context: ToolContext,
        categorized_memories: CategoryBuckets,
        llm_model: str,
        max_completion_tokens: int,
    ) -> list[dict[str, Any]]:
        all_successes: list[MemoryRecord] = []
        all_failures: list[MemoryRecord] = []
        for bucket in categorized_memories.values():
            all_successes.extend(bucket[SUCCESS_OUTCOME][:5])
            all_failures.extend(bucket[FAILURE_OUTCOME][:5])

        prompt = f"""You are an expert at distilling agent behavior patterns into concise, actionable skills.

Analyze these successful and failed trajectories from a Search AI agent that answers questions by issuing search queries and reading retrieved documents.

The agent operates in a search environment where it can:
- Issue search queries to retrieve relevant documents
- Read and analyze retrieved documents
- Formulate answers based on evidence found

The agent handles various question types across datasets:
- Direct factoid retrieval (Natural Questions, TriviaQA)
- Entity attribute lookup (PopQA)
- Multi-hop reasoning (HotpotQA, 2WikiMultiHopQA, MuSiQue, Bamboogle)

SUCCESSFUL TRAJECTORIES:
{self._extract_patterns(all_successes, limit=10, include_data_source=True)}

FAILED TRAJECTORIES:
{self._extract_patterns(all_failures, limit=10, include_data_source=True)}

Generate 8-12 GENERAL SKILLS that apply across ALL question types. These should be:
1. **Concise** - Each skill should be 1-2 sentences max
2. **Actionable** - Clear what to do, not vague principles
3. **Transferable** - Apply to direct retrieval, multi-hop, comparison, and entity lookup tasks
4. **Failure-aware** - Derived from what went wrong in failures

Format as JSON array:
[
    {{
        "skill_id": "gen_001",
        "title": "Short title (3-5 words)",
        "principle": "The core actionable insight in 1-2 sentences",
        "when_to_apply": "Specific trigger condition"
    }}
]

Focus on:
- Query formulation strategies
- Evidence extraction and verification
- Multi-step decomposition for complex questions
- Handling ambiguous entities or questions
- Knowing when to refine versus when to answer
- Avoiding hallucination without evidence

Return ONLY the JSON array, no other text."""

        return self._generate_json_array(
            context,
            prompt_name="search_general_skills",
            prompt=prompt,
            llm_model=llm_model,
            max_completion_tokens=max_completion_tokens,
        )

    def _generate_query_type_skills_search(
        self,
        context: ToolContext,
        query_type: str,
        successes: list[MemoryRecord],
        failures: list[MemoryRecord],
        llm_model: str,
        max_completion_tokens: int,
    ) -> list[dict[str, Any]]:
        if not successes and not failures:
            return []

        prompt = f"""You are an expert at distilling agent behavior patterns into concise, actionable skills.

Query Type: {query_type.upper().replace("_", " ")}
Description: {SEARCH_QUERY_TYPE_DESCRIPTIONS.get(query_type, "")}

SUCCESSFUL TRAJECTORIES:
{self._extract_patterns(successes, limit=8, include_data_source=True)}

FAILED TRAJECTORIES:
{self._extract_patterns(failures, limit=8, include_data_source=True) if failures else "[]"}

Generate 4-6 QUERY-TYPE-SPECIFIC SKILLS for {query_type} tasks. These should be:
1. **Concise** - 1-2 sentences max per skill
2. **Specific** - Apply specifically to {query_type} questions
3. **Actionable** - Clear steps or decision rules
4. **Pattern-based** - Identify what makes success vs failure

Format as JSON array:
[
    {{
        "skill_id": "{query_type[:3]}_001",
        "title": "Short title (3-5 words)",
        "principle": "The core actionable insight",
        "when_to_apply": "Specific trigger condition"
    }}
]

Return ONLY the JSON array, no other text."""

        return self._generate_json_array(
            context,
            prompt_name=f"search_query_type_skills_{query_type}",
            prompt=prompt,
            llm_model=llm_model,
            max_completion_tokens=max_completion_tokens,
        )

    def _generate_common_mistakes_search(
        self,
        context: ToolContext,
        categorized_memories: CategoryBuckets,
        llm_model: str,
        max_completion_tokens: int,
    ) -> list[dict[str, Any]]:
        all_failures: list[dict[str, Any]] = []
        for query_type, bucket in categorized_memories.items():
            for memory in bucket[FAILURE_OUTCOME][:5]:
                mistakes = self._mistakes_to_avoid(memory)
                if not mistakes:
                    continue
                all_failures.append(
                    {
                        "query_type": query_type,
                        "goal": self._goal_text(memory),
                        "data_source": self._tags(memory).get("data_source", ""),
                        "description": (
                            memory.get("contextual_description", "")
                            if isinstance(memory.get("contextual_description", ""), str)
                            else ""
                        )[:200],
                        "mistakes": mistakes,
                    }
                )

        if not all_failures:
            return []

        prompt = f"""You are an expert at analyzing agent failures and distilling them into avoidable mistakes.

Analyze these failure patterns from a Search AI agent that answers questions by issuing search queries and reading retrieved documents:

{json.dumps(all_failures[:20], indent=2)}

Generate 8-12 COMMON MISTAKES to avoid. Format as JSON array:
[
    {{
        "mistake_id": "err_001",
        "description": "What the mistake is (1 sentence)",
        "why_it_happens": "Why agents make this mistake (1 sentence)",
        "how_to_avoid": "Concrete actionable fix (1-2 sentences)"
    }}
]

Focus on:
- Query formulation errors
- Evidence handling failures
- Ambiguous entity resolution failures
- Repeating the same ineffective query
- Failing to decompose complex questions into sub-questions
- Premature answering before gathering sufficient evidence
- Misinterpreting retrieved documents

Return ONLY the JSON array, no other text."""

        return self._generate_json_array(
            context,
            prompt_name="search_common_mistakes",
            prompt=prompt,
            llm_model=llm_model,
            max_completion_tokens=max_completion_tokens,
        )

    def _extract_skill_bank_search(
        self,
        context: ToolContext,
        memories: list[MemoryRecord],
        llm_model: str,
        max_completion_tokens: int,
    ) -> dict[str, Any]:
        categorized = self._categorize_by_query_type_search(memories)
        non_empty_categories = self._count_non_empty_categories(categorized)
        estimated_llm_calls = 1 + non_empty_categories + 1
        context.log(
            f"[{self.name}] Search extraction plan: categories={non_empty_categories}, "
            f"estimated_llm_calls={estimated_llm_calls}",
            "info",
        )

        general_skills = self._generate_general_skills_search(
            context=context,
            categorized_memories=categorized,
            llm_model=llm_model,
            max_completion_tokens=max_completion_tokens,
        )
        query_type_skills = {
            query_type: self._generate_query_type_skills_search(
                context=context,
                query_type=query_type,
                successes=bucket[SUCCESS_OUTCOME],
                failures=bucket[FAILURE_OUTCOME],
                llm_model=llm_model,
                max_completion_tokens=max_completion_tokens,
            )
            for query_type, bucket in categorized.items()
        }
        common_mistakes = self._generate_common_mistakes_search(
            context=context,
            categorized_memories=categorized,
            llm_model=llm_model,
            max_completion_tokens=max_completion_tokens,
        )

        return {
            "general_skills": general_skills,
            "query_type_skills": query_type_skills,
            "common_mistakes": common_mistakes,
            "metadata": {
                "source": f"generated from Search AI agent trajectories using {llm_model}",
                "total_memories_analyzed": len(memories),
                "query_type_distribution": self._distribution_from_buckets(categorized),
            },
        }

    def _categorize_by_product_type_webshop(self, memories: list[MemoryRecord]) -> CategoryBuckets:
        categorized = self._empty_buckets(WEBSHOP_PRODUCT_TYPES)
        for memory in memories:
            goal = self._goal_text_lower(memory)
            outcome = self._outcome_bucket(memory)
            if outcome is None:
                continue

            product_type = "other"
            for category, keywords in WEBSHOP_KEYWORD_GROUPS.items():
                if any(keyword in goal for keyword in keywords):
                    product_type = category
                    break

            categorized[product_type][outcome].append(memory)
        return categorized

    def _generate_general_skills_webshop(
        self,
        context: ToolContext,
        categorized_memories: CategoryBuckets,
        llm_model: str,
        max_completion_tokens: int,
    ) -> list[dict[str, Any]]:
        all_successes: list[MemoryRecord] = []
        all_failures: list[MemoryRecord] = []
        for bucket in categorized_memories.values():
            all_successes.extend(bucket[SUCCESS_OUTCOME][:8])
            all_failures.extend(bucket[FAILURE_OUTCOME][:8])

        prompt = f"""You are an expert at distilling agent behavior patterns into concise, actionable skills.

Analyze these successful and failed trajectories from an AI agent operating in an online shopping environment (WebShop).

SUCCESSFUL TRAJECTORIES:
{self._extract_patterns(all_successes, limit=15)}

FAILED TRAJECTORIES:
{self._extract_patterns(all_failures, limit=15) if all_failures else "[]"}

Generate 10-15 GENERAL SKILLS that apply across ALL product types in web shopping. These should be:
1. **Concise** - Each skill should be 1-2 sentences max
2. **Actionable** - Clear what to do, not vague principles
3. **Transferable** - Apply to apparel, footwear, electronics, home decor, accessories, and similar products
4. **Failure-aware** - Derived from what could go wrong

Format as JSON array:
[
    {{
        "skill_id": "gen_001",
        "title": "Short title (3-5 words)",
        "principle": "The core actionable insight in 1-2 sentences",
        "when_to_apply": "Specific trigger condition"
    }}
]

Focus on:
- Search query formulation strategies
- Product selection heuristics
- Option configuration order
- Constraint verification before purchase
- Navigation and exploration patterns
- Price constraint handling
- Attribute matching strategies

Return ONLY the JSON array, no other text."""

        return self._generate_json_array(
            context,
            prompt_name="webshop_general_skills",
            prompt=prompt,
            llm_model=llm_model,
            max_completion_tokens=max_completion_tokens,
        )

    def _generate_task_specific_skills_webshop(
        self,
        context: ToolContext,
        product_type: str,
        successes: list[MemoryRecord],
        failures: list[MemoryRecord],
        llm_model: str,
        max_completion_tokens: int,
    ) -> list[dict[str, Any]]:
        if not successes and not failures:
            return []

        prompt = f"""You are an expert at distilling agent behavior patterns into concise, actionable skills.

Product Type: {product_type.upper().replace("_", " ")}
Description: {WEBSHOP_PRODUCT_DESCRIPTIONS.get(product_type, "")}

SUCCESSFUL TRAJECTORIES:
{self._extract_patterns(successes, limit=10)}

FAILED TRAJECTORIES:
{self._extract_patterns(failures, limit=10) if failures else "[]"}

Generate 4-6 TASK-SPECIFIC SKILLS for shopping {product_type.replace("_", " ")} products. These should be:
1. **Concise** - 1-2 sentences max per skill
2. **Specific** - Apply specifically to {product_type.replace("_", " ")} shopping
3. **Actionable** - Clear steps or decision rules
4. **Pattern-based** - Identify what makes success vs failure

Format as JSON array:
[
    {{
        "skill_id": "{product_type[:3]}_001",
        "title": "Short title (3-5 words)",
        "principle": "The core actionable insight",
        "when_to_apply": "Specific trigger condition"
    }}
]

Return ONLY the JSON array, no other text."""

        return self._generate_json_array(
            context,
            prompt_name=f"webshop_task_specific_skills_{product_type}",
            prompt=prompt,
            llm_model=llm_model,
            max_completion_tokens=max_completion_tokens,
        )

    def _generate_common_mistakes_webshop(
        self,
        context: ToolContext,
        categorized_memories: CategoryBuckets,
        llm_model: str,
        max_completion_tokens: int,
    ) -> list[dict[str, Any]]:
        failure_examples: list[dict[str, Any]] = []
        success_examples: list[dict[str, Any]] = []

        for product_type, bucket in categorized_memories.items():
            for memory in bucket[FAILURE_OUTCOME][:5]:
                failure_examples.append(
                    {
                        "product_type": product_type,
                        "goal": self._goal_text(memory),
                        "planning_pattern": self._planning_pattern(memory),
                        "mistakes": self._mistakes_to_avoid(memory),
                    }
                )
            for memory in bucket[SUCCESS_OUTCOME][:5]:
                success_examples.append(
                    {
                        "product_type": product_type,
                        "goal": self._goal_text(memory),
                        "planning_pattern": self._planning_pattern(memory),
                        "mistakes": self._mistakes_to_avoid(memory),
                    }
                )

        if failure_examples:
            prompt = f"""You are an expert at analyzing agent failures and distilling them into avoidable mistakes.

Analyze these failure patterns from an AI agent operating in an online shopping environment:

{json.dumps(failure_examples[:20], indent=2)}

Generate 10-15 COMMON MISTAKES to avoid in web shopping. Format as JSON array:
[
    {{
        "mistake_id": "err_001",
        "description": "What the mistake is (1 sentence)",
        "why_it_happens": "Why agents make this mistake (1 sentence)",
        "how_to_avoid": "Concrete actionable fix (1-2 sentences)"
    }}
]

Focus on:
- Search query errors
- Product selection errors
- Option configuration errors
- Constraint verification failures
- Navigation mistakes

Return ONLY the JSON array, no other text."""
        elif success_examples:
            prompt = f"""You are an expert at analyzing agent behaviors and identifying likely failure modes.

Analyze these successful shopping trajectories from an AI agent to infer COMMON MISTAKES that still need to be avoided:

{json.dumps(success_examples[:20], indent=2)}

Generate 10-15 COMMON MISTAKES to avoid in web shopping. Format as JSON array:
[
    {{
        "mistake_id": "err_001",
        "description": "What the mistake is (1 sentence)",
        "why_it_happens": "Why agents make this mistake (1 sentence)",
        "how_to_avoid": "Concrete actionable fix (1-2 sentences)"
    }}
]

Focus on:
- Search query errors
- Product selection errors
- Option configuration errors
- Constraint verification failures
- Navigation mistakes

Return ONLY the JSON array, no other text."""
        else:
            return []

        return self._generate_json_array(
            context,
            prompt_name="webshop_common_mistakes",
            prompt=prompt,
            llm_model=llm_model,
            max_completion_tokens=max_completion_tokens,
        )

    def _extract_skill_bank_webshop(
        self,
        context: ToolContext,
        memories: list[MemoryRecord],
        llm_model: str,
        max_completion_tokens: int,
    ) -> dict[str, Any]:
        categorized = self._categorize_by_product_type_webshop(memories)
        non_empty_categories = self._count_non_empty_categories(categorized)
        estimated_llm_calls = 1 + non_empty_categories + 1
        context.log(
            f"[{self.name}] WebShop extraction plan: categories={non_empty_categories}, "
            f"estimated_llm_calls={estimated_llm_calls}",
            "info",
        )

        general_skills = self._generate_general_skills_webshop(
            context=context,
            categorized_memories=categorized,
            llm_model=llm_model,
            max_completion_tokens=max_completion_tokens,
        )
        task_specific_skills = {
            product_type: self._generate_task_specific_skills_webshop(
                context=context,
                product_type=product_type,
                successes=bucket[SUCCESS_OUTCOME],
                failures=bucket[FAILURE_OUTCOME],
                llm_model=llm_model,
                max_completion_tokens=max_completion_tokens,
            )
            for product_type, bucket in categorized.items()
        }
        common_mistakes = self._generate_common_mistakes_webshop(
            context=context,
            categorized_memories=categorized,
            llm_model=llm_model,
            max_completion_tokens=max_completion_tokens,
        )

        return {
            "general_skills": general_skills,
            "task_specific_skills": task_specific_skills,
            "common_mistakes": common_mistakes,
            "metadata": {
                "source": f"generated from WebShop trajectories using {llm_model}",
                "total_memories_analyzed": len(memories),
                "product_distribution": self._distribution_from_buckets(categorized),
            },
        }

    def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        raw_data = kwargs.get("data", [])
        domain = (kwargs.get("domain") or "alfworld").lower()
        llm_model = kwargs.get("llm_model") or DEFAULT_LLM_MODEL
        max_completion_tokens = kwargs.get("max_completion_tokens") or DEFAULT_MAX_COMPLETION_TOKENS

        if domain not in SUPPORTED_DOMAINS:
            supported = ", ".join(SUPPORTED_DOMAINS)
            raise ValueError(f"Unsupported domain: {domain}. Expected one of: {supported}.")
        if not isinstance(llm_model, str) or not llm_model.strip():
            raise ValueError("`llm_model` must be a non-empty string.")
        if not isinstance(max_completion_tokens, int) or max_completion_tokens <= 0:
            raise ValueError("`max_completion_tokens` must be a positive integer.")

        memories, skipped_records = self._normalize_memories(context, raw_data)

        context.log(
            f"[{self.name}] Start skill extraction. domain={domain}, valid_records={len(memories)}, "
            f"llm_model={llm_model}, max_completion_tokens={max_completion_tokens}",
            "info",
        )

        if not memories:
            output = self._empty_result(domain=domain, llm_model=llm_model)
        elif domain == "alfworld":
            self._require_llm(context)
            output = self._extract_skill_bank_alfworld(
                context=context,
                memories=memories,
                llm_model=llm_model,
                max_completion_tokens=max_completion_tokens,
            )
        elif domain == "search":
            self._require_llm(context)
            output = self._extract_skill_bank_search(
                context=context,
                memories=memories,
                llm_model=llm_model,
                max_completion_tokens=max_completion_tokens,
            )
        else:
            self._require_llm(context)
            output = self._extract_skill_bank_webshop(
                context=context,
                memories=memories,
                llm_model=llm_model,
                max_completion_tokens=max_completion_tokens,
            )

        metadata = self._top_level_metadata(
            domain=domain,
            records_received=len(raw_data) if isinstance(raw_data, list) else 0,
            records_processed=len(memories),
            records_skipped=skipped_records,
            llm_model=llm_model,
        )

        return {
            "result": output,
            "metadata": metadata,
            "artifacts": {
                "report_md": self._report_markdown(metadata, output["metadata"]),
            },
        }
