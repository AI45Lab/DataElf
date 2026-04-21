import pytest
from tools import ToolContext, BaseTool
from tools.tool_registry import ToolRegistry, get_global_registry, register_tool, get_tool


# Minimal test tool — no heavy dependencies
class DummyTool(BaseTool):
    @property
    def name(self) -> str:
        return "dummy_tool"

    @property
    def description(self) -> str:
        return "A dummy tool for testing"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    def usage_example(self) -> str:
        return 'run_tool("dummy_tool")'

    def run(self, context: ToolContext, **kwargs):
        return {"result": "ok"}


class TestToolRegistry:
    def setup_method(self):
        self.registry = ToolRegistry()

    def test_register_tool(self):
        tool = DummyTool()
        self.registry.register(tool)

        assert "dummy_tool" in self.registry.list_tools()
        assert self.registry.get("dummy_tool") is tool

    def test_register_duplicate_raises_error(self):
        tool1 = DummyTool()
        tool2 = DummyTool()

        self.registry.register(tool1)

        with pytest.raises(ValueError, match="Tool already registered"):
            self.registry.register(tool2)

    def test_register_class(self):
        tool = self.registry.register_class(DummyTool)

        assert isinstance(tool, DummyTool)
        assert "dummy_tool" in self.registry.list_tools()

    def test_register_class_with_non_class_raises_error(self):
        tool_instance = DummyTool()

        with pytest.raises(TypeError, match="Expected a class"):
            self.registry.register_class(tool_instance)

    def test_get_existing_tool(self):
        tool = DummyTool()
        self.registry.register(tool)

        result = self.registry.get("dummy_tool")

        assert result is tool

    def test_get_nonexistent_tool_returns_none(self):
        result = self.registry.get("nonexistent_tool")
        assert result is None

    def test_list_tools(self):
        self.registry.register(DummyTool())

        tools = self.registry.list_tools()

        assert "dummy_tool" in tools

    def test_list_schemas(self):
        self.registry.register(DummyTool())

        schemas = self.registry.list_schemas()

        assert len(schemas) == 1
        assert schemas[0]["name"] == "dummy_tool"

        # Verify schema contains all required fields for PromptBuilder
        for schema in schemas:
            assert "name" in schema
            assert "description" in schema
            assert "parameters" in schema
            assert "usage_example" in schema
            assert isinstance(schema["usage_example"], str)
            assert len(schema["usage_example"]) > 0

    def test_clear(self):
        self.registry.register(DummyTool())
        self.registry.clear()

        assert self.registry.list_tools() == []

    def test_tools_property_returns_copy(self):
        tool = DummyTool()
        self.registry.register(tool)

        tools = self.registry.tools
        tools.clear()

        assert self.registry.get("dummy_tool") is not None


class TestGlobalRegistry:
    def setup_method(self):
        registry = get_global_registry()
        registry.clear()

    def test_get_global_registry_returns_singleton(self):
        registry1 = get_global_registry()
        registry2 = get_global_registry()

        assert registry1 is registry2

    def test_register_tool_function(self):
        tool = DummyTool()
        register_tool(tool)

        assert get_tool("dummy_tool") is tool

    def test_get_tool_function(self):
        tool = DummyTool()
        register_tool(tool)

        result = get_tool("dummy_tool")

        assert result is tool

    def test_get_tool_nonexistent_returns_none(self):
        result = get_tool("nonexistent")
        assert result is None
