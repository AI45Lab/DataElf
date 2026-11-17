"""AI Index source and prompt services injected into reviewed components."""

from .evidence import EvidenceCatalog, SKILL_ROOT
from . import skill_tools, role_prompts


class AIIndexServices:
    evidence = staticmethod(EvidenceCatalog)
    source_schemas = staticmethod(skill_tools.schemas)
    source_dispatch = staticmethod(skill_tools.dispatch)
    descriptions = staticmethod(role_prompts.descriptions)
    compose = staticmethod(role_prompts.compose)
    input_manifest = staticmethod(role_prompts.input_manifest)
    from .analysis_contract import validate_submission

    validate_submission = staticmethod(validate_submission)
    submission_dirs = ("raw", "tables", "scripts", "notes", "deep_dives", "insights")
    writable_dirs = ("tables", "scripts", "notes", "deep_dives", "insights")
    skills = (SKILL_ROOT / "SKILL.md",)

    @staticmethod
    def environment(path):
        return {"DATAELF_AI_INDEX_QUERY_CONTEXT": str(path)}


services = AIIndexServices()
