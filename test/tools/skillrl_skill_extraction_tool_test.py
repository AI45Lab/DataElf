import json
from pathlib import Path

import pytest

from config import load_config
from llm import OpenAIProvider
from tools import SkillRLSkillExtractionTool, ToolContext


TEST_DATA_DIR = Path(__file__).resolve().parents[2] / "test_data" / "skillrl"
ALFWORLD_TASK_TYPES = (
    "pick_and_place",
    "look_at_obj_in_light",
    "clean",
    "heat",
    "cool",
    "examine",
)
SEARCH_QUERY_TYPES = (
    "direct_retrieval",
    "multi_hop_reasoning",
    "entity_attribute_lookup",
    "comparison",
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


def _load_fixture(filename: str) -> list[dict]:
    with (TEST_DATA_DIR / filename).open() as f:
        return json.load(f)


def _save_result(filename: str, payload: dict) -> None:
    TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (TEST_DATA_DIR / filename).open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


class MockLogger:
    def __init__(self):
        self.logs = []

    def info(self, msg):
        self.logs.append(("info", msg))

    def error(self, msg):
        self.logs.append(("error", msg))

    def warning(self, msg):
        self.logs.append(("warning", msg))


class BadJSONLLM:
    def generate(self, model: str, prompt: str, **kwargs):
        return "not-json"


class GoodJSONLLM:
    def generate(self, model: str, prompt: str, **kwargs):
        return '[{"skill_id":"gen_001","title":"Test","principle":"Do the thing.","when_to_apply":"When needed"}]'


def _load_test_config():
    return load_config(Path(__file__).resolve().parents[2] / "config.yaml")


def _has_tool_llm_config() -> bool:
    cfg = _load_test_config()
    llm_cfg = getattr(cfg, "tool_llm", None)
    return bool(llm_cfg and getattr(llm_cfg, "model", None) and getattr(llm_cfg, "api_key", None))


def _real_llm():
    """Load a real tool-side LLM from the repository config, if configured."""
    cfg = _load_test_config()
    llm_cfg = cfg.tool_llm
    return OpenAIProvider(
        api_key=llm_cfg.api_key,
        base_url=llm_cfg.base_url,
        max_retries=llm_cfg.max_retries,
        retry_delay=llm_cfg.retry_delay,
    )


requires_llm = pytest.mark.skipif(
    not _has_tool_llm_config(),
    reason="No LLM configured in config.yaml (tool_llm.model/api_key missing)",
)


@pytest.fixture
def tool():
    return SkillRLSkillExtractionTool()


@pytest.fixture
def logger():
    return MockLogger()


@pytest.fixture
def alfworld_data():
    return _load_fixture("alfworld_sample.json")


@pytest.fixture
def search_data():
    return _load_fixture("search_sample.json")


@pytest.fixture
def webshop_data():
    return _load_fixture("webshop_sample.json")


def _make_context(logger: MockLogger, llm=None) -> ToolContext:
    return ToolContext(job_id="test_job", logger=logger, llm=llm)


def _make_real_context(logger: MockLogger) -> ToolContext:
    cfg = _load_test_config()
    return ToolContext(
        job_id="test_job",
        logger=logger,
        llm=_real_llm(),
        config={"tool_llm": cfg.tool_llm.__dict__},
    )


@pytest.fixture
def real_llm_model():
    return _load_test_config().tool_llm.model


@requires_llm
def test_skillrl_skill_extraction_alfworld_structure_with_real_data(
    tool, logger, alfworld_data, real_llm_model
):
    context = _make_real_context(logger)

    output = tool.run(
        context,
        data=alfworld_data,
        domain="alfworld",
        llm_model=real_llm_model,
        max_completion_tokens=4096,
    )
    _save_result("./output/alfworld_result.json", output)

    assert "result" in output
    assert "metadata" in output
    assert "artifacts" in output
    assert "report_md" in output["artifacts"]
    assert output["metadata"]["domain"] == "alfworld"
    assert output["metadata"]["records_received"] == len(alfworld_data)
    assert output["metadata"]["records_processed"] == len(alfworld_data)
    assert output["metadata"]["records_skipped"] == 0
    assert output["metadata"]["llm_model"] == real_llm_model

    result = output["result"]
    assert isinstance(result["general_skills"], list)
    assert isinstance(result["task_specific_skills"], dict)
    assert isinstance(result["common_mistakes"], list)
    assert result["metadata"]["total_memories_analyzed"] == len(alfworld_data)
    assert result["metadata"]["source"].endswith(f"using {real_llm_model}")

    dist = result["metadata"]["task_distribution"]
    for task_type in ALFWORLD_TASK_TYPES:
        assert dist[task_type]["success"] == 1
        assert dist[task_type]["failure"] == 1
        assert 1 <= len(result["task_specific_skills"][task_type]) <= 6


@requires_llm
def test_skillrl_skill_extraction_search_structure_with_real_data(
    tool, logger, search_data, real_llm_model
):
    context = _make_real_context(logger)

    output = tool.run(
        context,
        data=search_data,
        domain="search",
        llm_model=real_llm_model,
        max_completion_tokens=4096,
    )
    _save_result("./output/search_result.json", output)

    assert output["metadata"]["domain"] == "search"
    assert output["metadata"]["records_received"] == len(search_data)
    assert output["metadata"]["records_processed"] == len(search_data)
    assert output["metadata"]["records_skipped"] == 0
    assert output["metadata"]["llm_model"] == real_llm_model

    result = output["result"]
    assert isinstance(result["general_skills"], list)
    assert isinstance(result["query_type_skills"], dict)
    assert isinstance(result["common_mistakes"], list)
    assert result["metadata"]["total_memories_analyzed"] == len(search_data)
    assert result["metadata"]["source"].endswith(f"using {real_llm_model}")

    dist = result["metadata"]["query_type_distribution"]
    for query_type in SEARCH_QUERY_TYPES:
        assert dist[query_type]["success"] == 1
        assert dist[query_type]["failure"] == 1
        assert 1 <= len(result["query_type_skills"][query_type]) <= 6


@requires_llm
def test_skillrl_skill_extraction_webshop_structure_with_real_data(
    tool, logger, webshop_data, real_llm_model
):
    context = _make_real_context(logger)

    output = tool.run(
        context,
        data=webshop_data,
        domain="webshop",
        llm_model=real_llm_model,
        max_completion_tokens=4096,
    )
    _save_result("./output/webshop_result.json", output)

    assert output["metadata"]["domain"] == "webshop"
    assert output["metadata"]["records_received"] == len(webshop_data)
    assert output["metadata"]["records_processed"] == len(webshop_data)
    assert output["metadata"]["records_skipped"] == 0
    assert output["metadata"]["llm_model"] == real_llm_model

    result = output["result"]
    assert isinstance(result["general_skills"], list)
    assert isinstance(result["task_specific_skills"], dict)
    assert isinstance(result["common_mistakes"], list)
    assert result["metadata"]["total_memories_analyzed"] == len(webshop_data)
    assert result["metadata"]["source"].endswith(f"using {real_llm_model}")

    dist = result["metadata"]["product_distribution"]
    assert dist["apparel"] == {"success": 1, "failure": 1}
    assert dist["footwear"] == {"success": 1, "failure": 1}
    assert dist["home_decor"] == {"success": 1, "failure": 1}
    assert dist["electronics"] == {"success": 1, "failure": 1}
    assert dist["accessories"] == {"success": 1, "failure": 1}
    assert dist["other"] == {"success": 1, "failure": 1}
    assert dist["beauty_health"] == {"success": 1, "failure": 0}

    for product_type in WEBSHOP_PRODUCT_TYPES:
        assert 1 <= len(result["task_specific_skills"][product_type]) <= 6


def test_skillrl_skill_extraction_empty_data_without_llm(tool, logger):
    context = _make_context(logger, llm=None)

    output = tool.run(context, data=[], domain="search", llm_model="o3", max_completion_tokens=4096)

    assert output["metadata"]["records_received"] == 0
    assert output["metadata"]["records_processed"] == 0


def test_skillrl_skill_extraction_logs_llm_substages(tool, logger):
    context = _make_context(logger, llm=GoodJSONLLM())
    data = [
        {
            "tags": {"outcome": "success"},
            "content": {
                "task_meta": {"original_goal": "examine the apple"},
                "refined_trajectory": [{"action": "go to kitchen", "reasoning": "apple is often there"}],
                "strategic_guidelines": {"mistakes_to_avoid": ["forgetting the target object"]},
            },
        },
        {
            "tags": {"outcome": "failure"},
            "content": {
                "task_meta": {"original_goal": "examine the apple"},
                "refined_trajectory": [{"action": "open fridge", "reasoning": "searching blindly"}],
                "strategic_guidelines": {"mistakes_to_avoid": ["searching random containers first"]},
            },
        },
    ]

    output = tool.run(
        context,
        data=data,
        domain="alfworld",
        llm_model="gpt-4o-mini",
        max_completion_tokens=512,
    )

    info_logs = [message for level, message in logger.logs if level == "info"]

    assert output["metadata"]["records_processed"] == 2
    assert any("Start skill extraction. domain=alfworld" in message for message in info_logs)
    assert any("ALFWorld extraction plan:" in message for message in info_logs)
    assert any("LLM generation started: prompt=alfworld_general_skills" in message for message in info_logs)
    assert any("LLM generation completed: prompt=alfworld_general_skills" in message for message in info_logs)
    assert output["metadata"]["records_skipped"] == 0
    assert output["result"]["general_skills"]


@requires_llm
def test_skillrl_skill_extraction_invalid_records_are_skipped(
    tool, logger, alfworld_data, real_llm_model
):
    context = _make_real_context(logger)
    mixed_data = [
        alfworld_data[0],
        "not-a-dict",
        {"tags": {"outcome": "Success"}, "content": {"task_meta": {}}},
        {"content": {"task_meta": {"original_goal": "clean the plate."}}},
    ]

    output = tool.run(
        context,
        data=mixed_data,
        domain="alfworld",
        llm_model=real_llm_model,
        max_completion_tokens=4096,
    )

    assert output["metadata"]["records_received"] == 4
    assert output["metadata"]["records_processed"] == 1
    assert output["metadata"]["records_skipped"] == 3
    assert any(
        level == "warning" and "Skipped 3 invalid memory record(s)" in message
        for level, message in logger.logs
    )


def test_skillrl_skill_extraction_requires_llm_for_non_empty_data(tool, logger, alfworld_data):
    context = _make_context(logger, llm=None)

    with pytest.raises(ValueError, match="LLM is required for skill extraction"):
        tool.run(
            context,
            data=alfworld_data[:1],
            domain="alfworld",
            llm_model="o3",
            max_completion_tokens=4096,
        )


@pytest.mark.parametrize(
    ("kwargs", "error_message"),
    [
        ({"data": {}}, "`data` must be a list of SkillRL memory objects."),
        ({"data": [], "domain": "unknown"}, "Unsupported domain"),
        ({"data": [], "llm_model": ""}, "`llm_model` must be a non-empty string."),
        (
            {"data": [], "max_completion_tokens": 0},
            "`max_completion_tokens` must be a positive integer.",
        ),
    ],
)
def test_skillrl_skill_extraction_rejects_invalid_parameters(
    tool, logger, kwargs, error_message
):
    context = _make_context(logger)

    with pytest.raises(ValueError, match=error_message):
        tool.run(context, **kwargs)


def test_skillrl_skill_extraction_warns_on_invalid_llm_json(tool, logger, search_data):
    context = _make_context(logger, BadJSONLLM())

    output = tool.run(
        context,
        data=search_data[:2],
        domain="search",
        llm_model="o3",
        max_completion_tokens=4096,
    )

    assert output["result"]["general_skills"] == []
    assert output["result"]["query_type_skills"]["direct_retrieval"] == []
    assert any(
        level == "warning" and "did not contain a parsable JSON array" in message
        for level, message in logger.logs
    )
