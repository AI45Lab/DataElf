from .base_tool import BaseTool, ToolContext
from .tool_registry import ToolRegistry, get_global_registry

# Lazy imports — avoid pulling in heavy ML deps (torch, transformers, etc.)
# unless the specific tool class is actually accessed.
_LAZY_SUBMODULES = {
    "DataScoringTool",
    "DataSelectTool",
    "EnzymeAcquireTool",
    "ProteinAnalyzerTool",
    "SecurityAuditTool",
    "SkillRLSkillExtractionTool",
}
_lazy_cache: dict = {}


def __getattr__(name: str):
    if name in _lazy_cache:
        return _lazy_cache[name]
    if name in _LAZY_SUBMODULES:
        if name == "DataScoringTool":
            from .scoring import DataScoringTool
        elif name == "DataSelectTool":
            from .select import DataSelectTool
        elif name == "EnzymeAcquireTool":
            from .scitools.bio.enzyme_acquire_tool import EnzymeAcquireTool
        elif name == "ProteinAnalyzerTool":
            from .scitools.bio.protein_analyzer_tool import ProteinAnalyzerTool
        elif name == "SecurityAuditTool":
            from .security_audit import SecurityAuditTool
        elif name == "SkillRLSkillExtractionTool":
            from .trajectory_skill_extraction.skillrl_skill_extraction_tool import SkillRLSkillExtractionTool
        else:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        _lazy_cache[name] = locals()[name]
        return _lazy_cache[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BaseTool",
    "ToolContext",
    "ToolRegistry",
    "get_global_registry",
    "DataScoringTool",
    "DataSelectTool",
    "SkillRLSkillExtractionTool",
    "EnzymeAcquireTool",
    "ProteinAnalyzerTool",
    "SecurityAuditTool",
]