from __future__ import annotations

import functools
import time
from pathlib import Path
from typing import Any, Callable

from tools.base_tool import BaseTool, ToolContext


def timed(method: Callable) -> Callable:
    @functools.wraps(method)
    def wrapper(self, context: ToolContext, **kwargs):
        t0 = time.time()
        result = method(self, context, **kwargs)
        elapsed = int((time.time() - t0) * 1000)
        if isinstance(result, dict) and "metadata" in result:
            result["metadata"]["duration_ms"] = elapsed
        return result
    return wrapper


class ScientificBaseTool(BaseTool):

    domain: str = ""

    def get_output_dir(self, context: ToolContext) -> Path:
        base = Path(context.config.get("output_dir", "./artifacts"))
        output_dir = base / self.domain
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def require_data(self, kwargs: dict, context: ToolContext) -> list | None:
        data = kwargs.get("data", [])
        if not data:
            context.log("data parameter is empty", "error")
        return data or None

    @staticmethod
    def ok(result: Any, metadata: dict | None = None, artifacts: dict | None = None) -> dict:
        return {
            "result":    result,
            "metadata":  metadata or {},
            "artifacts": artifacts or {},
        }

    @staticmethod
    def err(message: str, context: ToolContext, **extra) -> dict:
        context.log(message, "error")
        return {
            "result":   None,
            "metadata": {"error": message, **extra},
        }
