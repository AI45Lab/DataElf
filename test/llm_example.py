from typing import Any
from tools import BaseTool, ToolContext


class DataQualityJudgeTool(BaseTool):

    @property
    def name(self) -> str:
        return "data_quality_judge"

    @property
    def description(self) -> str:
        return "Judge data quality using LLM"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Data records to judge"
                }
            },
            "required": ["data"]
        }

    def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        data = kwargs.get("data", [])

        if not context.llm:
            return {"result": {"error": "LLM not available"}}
        # 构造 prompt
        prompt = f"""Evaluate the quality of this dataset:
{len(data)} records
Sample: {data[:3]}

Score from 0-100 and provide feedback."""
        
        # 使用context.llm调用 LLM
        tool_llm_config = context.config.get("tool_llm", {})
        model = tool_llm_config.get("model", "gpt-4o-mini")

        response = context.llm.generate(
            model=model,
            prompt=prompt
        )

        return {
            "result": {"judgment": response},
            "metadata": {"records_count": len(data)}
        }


"""
data = load_dataset("companies")
result = run_tool("data_quality_judge", data=data)
save_result(result)
"""
