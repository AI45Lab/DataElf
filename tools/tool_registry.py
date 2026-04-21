import inspect
from typing import Any

from .base_tool import BaseTool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        name = tool.name
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")

        self._tools[name] = tool

    def register_class(self, tool_class: type[BaseTool]) -> BaseTool:
        if not inspect.isclass(tool_class):
            raise TypeError(f"Expected a class, got {type(tool_class)}")

        tool = tool_class()
        self.register(tool)
        return tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def list_schemas(self) -> list[dict[str, Any]]:
        return [tool.get_schema() for tool in self._tools.values()]

    def clear(self) -> None:
        self._tools.clear()

    @property
    def tools(self) -> dict[str, BaseTool]:
        return self._tools.copy()

_global_registry: ToolRegistry | None = None


def get_global_registry() -> ToolRegistry:
    global _global_registry
    if _global_registry is None:
        _global_registry = ToolRegistry()
    return _global_registry


def register_tool(tool: BaseTool) -> None:
    get_global_registry().register(tool)


def get_tool(name: str) -> BaseTool | None:
    return get_global_registry().get(name)


def list_tool_schemas() -> list[dict[str, Any]]:
    return get_global_registry().list_schemas()
