from __future__ import annotations

import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from dataelf.config import DataElfConfig
from dataelf.discovery.contracts import AgentResources, DiscoveryContext, DiscoveryJob, DomainManifest, JobSpec, OutputContract, ReviewResult, StageResult
from dataelf.domains.finance.artifacts import BRIEF_ARTIFACT_ID, RESULT_ID, OutputSchema, has_result, output_schema, output_schema_for_spec
from dataelf.domains.finance.benchmarks import ResolvedBenchmark, benchmark_pi_resources, get_benchmark
from dataelf.domains.finance.config import FinanceDomainConfig, FinanceSourceConfig, SQL_TOOLS, CODE_TOOLS, EDGAR_TOOLS, MARKET_TOOLS
from dataelf.domains.finance.connector import prepare_finance_source
from dataelf.domains.finance.prompt import build_finance_prompt, tool_names_for_flags
from dataelf.domains.finance.review import review_finance


# Effective tool flags when neither a benchmark nor the operator says otherwise.
GENERIC_TOOL_FLAGS = {"sql": False, "python": True, "web": False, "edgar": False, "prices": False}
_TOOL_FLAGS = ("sql", "python", "web", "edgar", "prices")

# Optional input-parsing packages from the root `finance` extra (mirrors the
# trajectory_analysis precedent: extra installs on demand, requirements-finance.txt
# records the same specs, preflight validates importability instead of installing).
_EXCEL_SUFFIXES = frozenset({".xlsx", ".xlsm"})
_PDF_SUFFIXES = frozenset({".pdf"})
_FILE_DEP_LABELS = {"openpyxl": "Excel (.xlsx/.xlsm)", "pypdf": "PDF"}


def _probe_import(module: str) -> bool:
    # Import-only probe in isolated Python: no cwd/PYTHONPATH/env leakage,
    # same idiom as the trajectory_analysis tool-interpreter check.
    try:
        probe = subprocess.run(
            [sys.executable, "-I", "-B", "-c", f"import {module}"],
            env={}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


class FinanceDomainPlugin:
    def __init__(self, config: DataElfConfig, manifest: DomainManifest):
        self.manifest = manifest
        self.config = FinanceDomainConfig.from_mapping(config.domain_config(manifest.domain))
        self.config.validate_for_run()
        self._benchmarks: dict[str, ResolvedBenchmark] = {}

    # -- benchmark resolution -------------------------------------------------

    def _benchmark_for(self, name: object) -> ResolvedBenchmark | None:
        if name in (None, ""):
            return None
        key = str(name)
        if key not in self._benchmarks:
            self._benchmarks[key] = get_benchmark(key)
        return self._benchmarks[key]

    def _resolve_benchmark(self, spec: JobSpec) -> ResolvedBenchmark | None:
        # Job parameter > environment (already merged into config) > config.
        requested = spec.parameters.get("benchmark") or self.config.benchmark
        return self._benchmark_for(requested)

    def _resolve_tool_flags(self, spec: JobSpec, benchmark: ResolvedBenchmark | None) -> dict[str, bool]:
        flags = dict(GENERIC_TOOL_FLAGS)
        if benchmark is not None:
            declared = benchmark.config.tools
            flags.update({flag: getattr(declared, flag) for flag in _TOOL_FLAGS})
        # Operator-level overrides from domains.finance.tools.
        for flag in _TOOL_FLAGS:
            value = getattr(self.config.tools, flag)
            if value is not None:
                flags[flag] = value
        # Runtime parameter overrides (finance_tools={sql: false, ...}).
        override = spec.parameters.get("finance_tools")
        if override:
            if not isinstance(override, dict):
                raise ValueError("finance_tools must be an object")
            unknown = sorted(set(override) - set(_TOOL_FLAGS))
            if unknown:
                raise ValueError("Unknown finance_tools flags: " + ", ".join(unknown))
            flags.update({flag: bool(override[flag]) for flag in override})
        return flags

    def _extra_tool_names(self, spec: JobSpec, benchmark: ResolvedBenchmark | None) -> list[str]:
        extra: list[str] = list(benchmark.config.tools.extra) if benchmark is not None else []
        override = spec.parameters.get("finance_extra_tools")
        if override is not None:
            if not isinstance(override, list) or not all(isinstance(item, str) for item in override):
                raise ValueError("finance_extra_tools must be a list of strings")
            extra.extend(override)
        return list(dict.fromkeys(extra))

    def _output_schema(self, spec: JobSpec, benchmark: ResolvedBenchmark | None) -> tuple[OutputSchema, list[dict]]:
        """Resolve the deliverable schema: runtime param > benchmark field overrides.

        Returns the resolved schema plus the effective field overrides,
        stashed into the job parameters so the prompt, output contract, and
        review all re-resolve identically.
        """
        declared = spec.parameters.get("finance_output_fields")
        if declared is not None:
            if not isinstance(declared, list):
                raise ValueError("finance_output_fields must be a list of field overrides")
            return output_schema(declared), declared
        overrides: list[dict] = []
        if benchmark is not None:
            overrides = [field.model_dump(exclude_none=True) for field in benchmark.config.outputs.fields]
        return output_schema(overrides), overrides

    def _resolve_source(self, spec: JobSpec, benchmark: ResolvedBenchmark | None) -> FinanceSourceConfig:
        merged = self.config.source.model_dump(exclude_none=True)
        override = spec.parameters.get("finance_source")
        if override not in (None, ""):
            if not isinstance(override, dict):
                raise ValueError("finance_source must be an object")
            unknown = sorted(set(override) - {"sqlite", "files", "fixture"})
            if unknown:
                raise ValueError("Unknown finance_source sections: " + ", ".join(unknown))
            for key, value in override.items():
                section = dict(merged.get(key) or {})
                if not isinstance(value, dict):
                    raise ValueError(f"finance_source.{key} must be an object")
                section.update(value)
                merged[key] = section
        return FinanceSourceConfig.model_validate(merged)

    # -- DomainPlugin contract ------------------------------------------------

    def normalize_spec(self, spec: JobSpec) -> JobSpec:
        benchmark = self._resolve_benchmark(spec)
        parameters = dict(spec.parameters)
        if benchmark is not None:
            parameters.setdefault("benchmark", benchmark.name)
            parameters.setdefault("prompt_profile", _benchmark_prompt_profile(benchmark))
        schema, field_overrides = self._output_schema(spec, benchmark)
        parameters["finance_output_fields"] = field_overrides
        flags = self._resolve_tool_flags(spec, benchmark)
        extra = self._extra_tool_names(spec, benchmark)
        parameters["finance_tool_flags"] = flags
        parameters["finance_tool_names"] = list(tool_names_for_flags(flags, extra))
        requested = spec.requested_outputs or [schema.artifact_id, BRIEF_ARTIFACT_ID]
        constraints = dict(spec.constraints)
        constraints.setdefault(
            "max_runtime_minutes",
            math.ceil(self.config.analysis.max_runtime_seconds / 60),
        )
        return spec.model_copy(update={
            "parameters": parameters,
            "requested_outputs": requested,
            "constraints": constraints,
        })

    def prepare(self, spec: JobSpec, workspace_path: str, config: DataElfConfig) -> StageResult:
        benchmark = self._resolve_benchmark(spec)
        try:
            source = self._resolve_source(spec, benchmark)
        except ValueError as exc:
            return StageResult(status="failed", error_code="FINANCE_SOURCE_CONFIG_INVALID", error_message=str(exc))
        try:
            self._validate_source(source)
        except ValueError as exc:
            return StageResult(status="failed", error_code="FINANCE_SOURCE_INVALID", error_message=str(exc))
        flags = self._resolve_tool_flags(spec, benchmark)
        db_available = _db_available(source, benchmark)
        files_available = _files_available(source)
        if benchmark is not None:
            missing = [kind for kind in benchmark.config.source.needs if (kind == "sqlite" and not db_available) or (kind == "files" and not files_available)]
            if missing:
                return StageResult(
                    status="failed", error_code="FINANCE_SOURCE_MISSING",
                    error_message=(
                        f"Benchmark {benchmark.name} requires data source(s) {', '.join(missing)}; "
                        "provide source.sqlite/source.files in dataelf.local.yaml (or --param finance_source), "
                        "or configure source.fixture for an offline run."
                    ),
                )
        if flags["sql"] and not db_available:
            return StageResult(
                status="failed", error_code="FINANCE_DB_MISSING",
                error_message="SQL tools are enabled but no database source is available; provide source.sqlite.db_path or a fixture.",
            )
        builder = benchmark.config.source.fixture_builder if benchmark is not None else None
        result = prepare_finance_source(source, Path(workspace_path), builder)
        if result.status != "completed":
            return result
        missing = self._missing_file_deps(Path(workspace_path))
        if missing:
            return StageResult(
                status="failed", error_code="FINANCE_FILE_DEPS_MISSING",
                error_message=(
                    f"Workspace input contains {', '.join(_FILE_DEP_LABELS[name] for name in missing)} files but "
                    f"{', '.join(missing)} is not importable by the job interpreter; install the finance extra "
                    '(uv pip install -e ".[finance]") or provide inputs the default runtime can analyze.'
                ),
            )
        extra = self._extra_tool_names(spec, benchmark)
        registered: list[str] = []
        if flags["sql"]:
            registered.extend(SQL_TOOLS)
        if flags["python"]:
            registered.extend(CODE_TOOLS)
        if flags["edgar"]:
            registered.extend(EDGAR_TOOLS)
        if flags["prices"]:
            registered.extend(MARKET_TOOLS)
        registered.extend(extra)
        allowed = tuple(dict.fromkeys(registered))
        env = dict(result.env)
        env["DATAELF_FINANCE_ALLOWED_TOOLS"] = ",".join(allowed)
        # The finance Pi extensions refuse further tool calls past this
        # budget. The default comes from analysis.max_tool_calls; an explicit
        # runtime parameter can override it per job (kept out of parameters
        # otherwise so the model-visible JobSpec view stays free of budgets).
        budget = spec.parameters.get("max_tool_calls")
        if budget is None:
            budget = self.config.analysis.max_tool_calls
        if isinstance(budget, int) and budget > 0:
            env["DATAELF_FINANCE_MAX_TOOL_CALLS"] = str(budget)
        if self.config.analysis.readonly_code is not None:
            # Explicit operator choice; unset means the tools package
            # auto-enables
            # read-only execution exactly when linked sources are present.
            env["DATAELF_FINANCE_READONLY_CODE"] = "1" if self.config.analysis.readonly_code else "0"
        # Config -> internal env transport for the Node/Python tool
        if self.config.analysis.http_user_agent:
            env["DATAELF_FINANCE_USER_AGENT"] = self.config.analysis.http_user_agent
        if self.config.analysis.retrieve_information_model:
            env["DATAELF_FINANCE_RETRIEVE_MODEL"] = self.config.analysis.retrieve_information_model
        if flags["edgar"] or flags["prices"]:
            # Node's global fetch ignores HTTP(S)_PROXY unless this flag is set
            # before process start (Node >= 24); the edgar/prices tools fetch
            # via global fetch. Preserve an explicit job/config or process
            # value, including "0"; default to enabled only when unset.
            configured_proxy = config.env.get("NODE_USE_ENV_PROXY")
            if configured_proxy is None:
                configured_proxy = os.environ.get("NODE_USE_ENV_PROXY")
            env["NODE_USE_ENV_PROXY"] = "1" if configured_proxy is None else str(configured_proxy)
        return result.model_copy(update={
            "env": env,
            "context": {**result.context, "tool_flags": flags, "tool_names": parameters_tool_names(spec), "benchmark": benchmark.name if benchmark is not None else None},
        })

    def _missing_file_deps(self, workspace: Path) -> list[str]:
        # File kinds in the materialized input decide which optional packages
        # the job will need; stdlib-analyzable kinds (csv/json) probe nothing.
        input_dir = workspace / "raw" / "finance" / "input"
        suffixes = (
            {path.suffix.lower() for path in input_dir.rglob("*") if path.is_file()}
            if input_dir.is_dir() else set()
        )
        needed = []
        if suffixes & _EXCEL_SUFFIXES:
            needed.append("openpyxl")
        if suffixes & _PDF_SUFFIXES:
            needed.append("pypdf")
        return [name for name in needed if not _probe_import(name)]

    def create_modeler(self, spec: JobSpec, config: DataElfConfig):
        if self.config.modeling.enabled:
            raise ValueError('FINANCE_MODELING_UNSUPPORTED')

    def build_prompt(self, job: DiscoveryJob, context: DiscoveryContext) -> str:
        return build_finance_prompt(job, context)

    def output_contract(self, spec: JobSpec) -> OutputContract:
        return output_schema_for_spec(spec.parameters).output_contract()

    def review(self, job: DiscoveryJob, workspace_path: str) -> ReviewResult:
        return review_finance(job, Path(workspace_path))

    def result_ids(self, workspace_path: str) -> list[str]:
        # Harness-assigned: one job produces one result; the model never
        # fabricates ids.
        return [RESULT_ID] if has_result(workspace_path) else []

    def agent_resources(self, spec: JobSpec, config: DataElfConfig) -> AgentResources:
        benchmark = self._resolve_benchmark(spec)
        flags = self._resolve_tool_flags(spec, benchmark)
        extra = self._extra_tool_names(spec, benchmark)
        domain_root = Path(__file__).resolve().parent
        extensions: list[Path] = []
        if flags["sql"] or flags["python"]:
            extensions.append(domain_root / "pi" / "extensions" / "finance_sql_python.ts")
        if flags["edgar"] or flags["prices"]:
            extensions.append(domain_root / "pi" / "extensions" / "finance_edgar_prices.ts")
        skills = [domain_root / "pi" / "skills" / "finance" / "SKILL.md"]
        if benchmark is not None:
            benchmark_extensions, benchmark_skills = benchmark_pi_resources(benchmark)
            extensions.extend(benchmark_extensions)
            skills.extend(benchmark_skills)
        return AgentResources(extensions=extensions, skills=skills)

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _validate_source(source: FinanceSourceConfig) -> None:
        if source.sqlite is not None and not source.sqlite.db_path.is_file():
            raise ValueError(f"Finance SQLite database does not exist: {source.sqlite.db_path}")
        if source.files is not None and not source.files.data_path.exists():
            raise ValueError(f"Finance data path does not exist: {source.files.data_path}")
        if source.fixture is not None and not source.fixture.dir.is_dir():
            raise ValueError(f"Finance fixture directory does not exist: {source.fixture.dir}")


def _db_available(source: FinanceSourceConfig, benchmark: ResolvedBenchmark | None) -> bool:
    if source.offline:
        assert source.fixture is not None
        builder = benchmark.config.source.fixture_builder if benchmark is not None else None
        return bool(builder) or (source.fixture.dir / "finance.db").is_file()
    return source.sqlite is not None


def _files_available(source: FinanceSourceConfig) -> bool:
    if source.offline:
        assert source.fixture is not None
        return (source.fixture.dir / "input").is_dir()
    return source.files is not None


def parameters_tool_names(spec: JobSpec) -> list[str]:
    names = spec.parameters.get("finance_tool_names")
    return list(names) if isinstance(names, list) else []


def _benchmark_prompt_profile(benchmark: ResolvedBenchmark) -> dict[str, Any]:
    prompt = benchmark.config.prompt
    # Identity fields (profile_id/name/version) are deliberately absent: the
    # profile dict reaches the model through the runtime's JobSpec view, and
    # benchmark identity (e.g. a "DDR-Bench" display name) is orchestration
    # metadata the agent must not see. Only the guidance content travels.
    # Absent fields are omitted (not passed as None): normalize_prompt_profile
    # rejects non-string values, and the generic fallbacks in prompt.py apply
    # per missing key.
    profile: dict[str, Any] = {}
    for key, value in (
        ("source_guidance", prompt.source_guidance),
        ("tool_guidance", prompt.tool_guidance),
        ("output_guidance", prompt.output_guidance),
        ("stopping_policy", prompt.stopping_policy),
    ):
        if value is not None:
            profile[key] = value
    if prompt.instructions:
        profile["instructions"] = list(prompt.instructions)
    if prompt.requirements:
        profile["insight_requirements"] = list(prompt.requirements)
    return profile


def create_plugin(config: DataElfConfig, manifest: DomainManifest) -> FinanceDomainPlugin:
    return FinanceDomainPlugin(config, manifest)


__all__ = ["FinanceDomainPlugin", "create_plugin"]
