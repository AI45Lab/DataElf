from agentic import AssetManager
from tools import BaseTool, ToolContext
from tools.tool_registry import ToolRegistry


class DummyAuditTool(BaseTool):
    @property
    def name(self) -> str:
        return "dummy_audit"

    @property
    def description(self) -> str:
        return "Dummy audit tool for asset tests."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "data": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["data"],
        }

    def run(self, context: ToolContext, **kwargs):
        return {"result": {"count": len(kwargs.get("data", []))}}


def test_asset_manager_approves_and_registers_composite_tool(tmp_path):
    asset_manager = AssetManager(root=tmp_path / ".elf")
    candidate = {
        "candidate_id": "cand_comp_demo",
        "candidate_type": "composite_tool",
        "name": "privacy_guard",
        "description": "Composite privacy guard tool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "data": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["data"],
        },
        "steps": [
            {
                "type": "run_tool",
                "tool_name": "dummy_audit",
                "kwargs": {
                    "data": "$input.data",
                },
                "output": "audit",
            }
        ],
        "result": {"audit": "$audit"},
        "validation_criteria": ["Must run on mock data."],
        "source_attempts": ["attempt_01"],
        "status": "draft",
        "pipeline_template": 'save_result({"ok": True})',
    }

    asset_manager.save_candidate(candidate)
    approved = asset_manager.approve_candidate(candidate["candidate_id"])
    stable_asset = asset_manager.get_stable_asset(approved["asset_id"])
    updated_candidate = asset_manager.get_candidate(candidate["candidate_id"])

    registry = ToolRegistry()
    registry.register(DummyAuditTool())
    loaded = asset_manager.register_stable_tools(registry)

    assert approved["status"] == "approved"
    assert approved["asset_type"] == "tool"
    assert approved["asset_id"].startswith("asset_")
    assert approved["source_candidate_id"] == candidate["candidate_id"]
    assert "privacy_guard" in loaded
    assert registry.get("privacy_guard") is not None
    assert stable_asset is not None
    assert updated_candidate is not None
    assert updated_candidate["status"] == "approved"


def test_asset_manager_approves_pipeline_candidate_as_submit_asset(tmp_path):
    asset_manager = AssetManager(root=tmp_path / ".elf")
    candidate = {
        "candidate_id": "cand_pipe_demo",
        "candidate_type": "pipeline",
        "name": "security_pipeline_candidate",
        "description": "Pipeline candidate for submit flow.",
        "pipeline": 'save_result({"ok": True})',
        "source_attempts": ["attempt_01"],
        "status": "draft",
        "validation_criteria": ["Manual review required."],
    }

    asset_manager.save_candidate(candidate)
    approved = asset_manager.approve_candidate(candidate["candidate_id"])
    stable_asset = asset_manager.get_stable_asset(approved["asset_id"])
    updated_candidate = asset_manager.get_candidate(candidate["candidate_id"])

    assert approved["status"] == "approved"
    assert approved["asset_type"] == "pipeline"
    assert approved["asset_id"].startswith("asset_pipe_")
    assert approved["source_candidate_id"] == candidate["candidate_id"]
    assert stable_asset is not None
    assert stable_asset["asset_type"] == "pipeline"
    assert updated_candidate is not None
    assert updated_candidate["status"] == "approved"
