from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


DEFAULT_MAX_TOOL_CALLS = 100
DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "finance"

# Generic Finance tool names.
SQL_TOOLS: tuple[str, ...] = ("get_database_info", "describe_table", "execute_query")
CODE_TOOLS: tuple[str, ...] = ("execute_code", "list_files", "get_field_description")
EDGAR_TOOLS: tuple[str, ...] = (
    "edgar_search", "company_profile", "company_facts", "parse_html_page",
    "retrieve_information",
)
MARKET_TOOLS: tuple[str, ...] = ("price_history",)
WEB_TOOLS: tuple[str, ...] = ("web_search", "fetch_content", "source_check", "get_search_content")
TERMINAL_TOOLS: tuple[str, ...] = ("read", "bash", "edit", "write", "grep", "find", "ls")

# Names statically known to be registrable without benchmark-local Pi
# extensions: the finance extension files above plus the common web tools.
_STATIC_TOOL_NAMES = frozenset(
    SQL_TOOLS + CODE_TOOLS + EDGAR_TOOLS + MARKET_TOOLS + WEB_TOOLS + TERMINAL_TOOLS
)


class SqliteSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    db_path: Path
    # Zero-copy for large corpora: symlink the database into the workspace
    # instead of copying it. Read-only enforcement for execute_code turns on
    # automatically while any source is linked (analysis.readonly_code).
    link: bool = False


class FilesSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_path: Path
    # Same as sqlite.link: mirror the tree with symlinks instead of copying.
    link: bool = False


class FixtureSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Resolve the built-in fixture relative to the repository/package root so
    # CLI invocations do not depend on their current working directory.
    dir: Path = DEFAULT_FIXTURE_DIR


class FinanceSourceConfig(BaseModel):
    """Composable data sources: all sections optional.

    ``sqlite`` and ``files`` may be combined (e.g. a database plus CSV or
    document inputs). A web-only run omits every section. Configuring
    ``fixture`` switches the run to offline mode and takes precedence over
    the real sources: the directory may contain a seeded ``finance.db`` (or
    be built by the benchmark's ``fixture_builder``) and/or an ``input/``
    directory standing in for the files source.
    """

    model_config = ConfigDict(extra="forbid")

    sqlite: SqliteSourceConfig | None = None
    files: FilesSourceConfig | None = None
    fixture: FixtureSourceConfig | None = None

    @property
    def offline(self) -> bool:
        return self.fixture is not None


class FinanceAnalysisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tool_calls: int = Field(default=DEFAULT_MAX_TOOL_CALLS, ge=1, le=2000)
    max_runtime_seconds: int = Field(default=1800, ge=30, le=86400)
    # Read-only enforcement for execute_code. ``None`` means automatic: on
    # whenever a source is linked (a stray write would corrupt the shared
    # corpus instead of a disposable workspace copy), off otherwise.
    # True/False forces it for every run.
    readonly_code: bool | None = None
    # User-Agent header for every outbound finance HTTP request (EDGAR,
    # market data, page fetches), not just EDGAR.
    http_user_agent: str | None = Field(default=None, min_length=1)
    # Helper model for the retrieve_information tool's extra extraction
    # call; None falls back to the explorer's main model.
    retrieve_information_model: str | None = Field(default=None, min_length=3)


class FinanceToolsConfig(BaseModel):
    """Operator-level tool overrides.

    ``None`` means "no opinion": the selected benchmark's declaration (or the
    generic defaults) applies. Setting a flag forces it for every run,
    benchmark or not, unless job parameters override it again.

    Pi's built-in terminal tools (``TERMINAL_TOOLS``) ship with the explorer
    and are always available; they are not a switch.
    """

    model_config = ConfigDict(extra="forbid")

    sql: bool | None = None
    python: bool | None = None
    web: bool | None = None
    edgar: bool | None = None
    prices: bool | None = None


class FinanceModelingConfig(BaseModel):
    """Placeholder for a future Finance modeling stage.

    Finance has no modeling stage yet; the section only reserves the config
    keys the generic CLI can inject (``--modeling/--no-modeling`` writes
    ``enabled``, ``--ontology-template`` writes ``ontology_template``) so the
    flags stay valid, mirroring the trajectory_analysis precedent. Enabling
    it fails fast in ``create_modeler`` instead of silently skipping.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    ontology_template: str | None = None


class FinanceDomainConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    benchmark: str | None = None
    source: FinanceSourceConfig = Field(default_factory=FinanceSourceConfig)
    analysis: FinanceAnalysisConfig = Field(default_factory=FinanceAnalysisConfig)
    tools: FinanceToolsConfig = Field(default_factory=FinanceToolsConfig)
    modeling: FinanceModelingConfig = Field(default_factory=FinanceModelingConfig)

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "FinanceDomainConfig":
        if not isinstance(values, dict):
            raise ValueError("domains.finance must be a mapping")
        allowed = {"benchmark", "source", "analysis", "tools", "modeling"}
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ValueError("Unknown Finance config keys: " + ", ".join(unknown))
        benchmark = os.getenv("DATAELF_FINANCE_BENCHMARK") or values.get("benchmark") or None
        source = dict(values.get("source") or {})
        analysis = dict(values.get("analysis") or {})
        tools = dict(values.get("tools") or {})
        modeling = dict(values.get("modeling") or {})
        if os.getenv("DATAELF_FINANCE_DB_PATH"):
            source["sqlite"] = {"db_path": os.environ["DATAELF_FINANCE_DB_PATH"]}
        if os.getenv("DATAELF_FINANCE_DATA_PATH"):
            source["files"] = {"data_path": os.environ["DATAELF_FINANCE_DATA_PATH"]}
        if os.getenv("DATAELF_FINANCE_FIXTURE_DIR"):
            source["fixture"] = {"dir": os.environ["DATAELF_FINANCE_FIXTURE_DIR"]}
        if "sqlite" in source and os.getenv("DATAELF_FINANCE_DB_LINK") not in (None, ""):
            source["sqlite"] = {**source["sqlite"], "link": _env_flag("DATAELF_FINANCE_DB_LINK")}
        if "files" in source and os.getenv("DATAELF_FINANCE_FILES_LINK") not in (None, ""):
            source["files"] = {**source["files"], "link": _env_flag("DATAELF_FINANCE_FILES_LINK")}
        if os.getenv("DATAELF_FINANCE_READONLY_CODE") not in (None, ""):
            analysis["readonly_code"] = _env_flag("DATAELF_FINANCE_READONLY_CODE")
        if os.getenv("DATAELF_FINANCE_TOOLS"):
            tools.update(_tools_from_env(os.environ["DATAELF_FINANCE_TOOLS"]))
        for field, env_name in {
            "max_tool_calls": "DATAELF_FINANCE_MAX_TOOL_CALLS",
            "max_runtime_seconds": "DATAELF_FINANCE_MAX_RUNTIME_SECONDS",
        }.items():
            if os.getenv(env_name) not in (None, ""):
                analysis[field] = os.environ[env_name]
        resolved = cls.model_validate({
            "benchmark": benchmark,
            "source": source,
            "analysis": analysis,
            "tools": tools,
            "modeling": modeling,
        })
        if resolved.benchmark is not None:
            from dataelf.domains.finance.benchmarks import get_benchmark

            get_benchmark(resolved.benchmark)
        return resolved

    def validate_for_run(self) -> None:
        if self.benchmark is not None:
            from dataelf.domains.finance.benchmarks import list_benchmarks

            if self.benchmark not in list_benchmarks():
                raise ValueError(
                    f"Unknown Finance benchmark {self.benchmark!r}; "
                    f"available: {', '.join(list_benchmarks())}"
                )


def _env_flag(name: str) -> bool:
    return os.environ[name].strip().lower() in ("1", "true", "yes", "on")


def _tools_from_env(value: str) -> dict[str, bool]:
    """Parse DATAELF_FINANCE_TOOLS as an explicit capability list."""
    flags = {"sql", "python", "web", "edgar", "prices"}
    requested = {item.strip().lower() for item in value.split(",") if item.strip()}
    unknown = sorted(requested - flags)
    if unknown:
        raise ValueError(f"Unknown Finance tool flags in DATAELF_FINANCE_TOOLS: {', '.join(unknown)}")
    return {flag: flag in requested for flag in flags}


class FinanceBenchmarkTools(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sql: bool = False
    python: bool = False
    web: bool = False
    edgar: bool = False
    prices: bool = False
    extra: list[str] = Field(default_factory=list)


class FinanceBenchmarkSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Data kinds the benchmark expects to be provided (preflight requirement,
    # not an exclusion: operator-provided extras are prepared too). Empty for
    # web-only benchmarks.
    needs: list[Literal["sqlite", "files"]] = Field(default_factory=list)
    # ``module:function`` executed as ``builder(destination, fixture_dir)``.
    # Only benchmarks that need a custom seeded database declare this.
    fixture_builder: str | None = None


class FinanceBenchmarkPrompt(BaseModel):
    """Benchmark prompt overrides — every field maps original-benchmark text.

    Per the benchmark development plan (§4 principle 7), each field must trace
    back to a concrete statement in the original benchmark's prompts; a
    benchmark whose original has no counterpart for a field omits it and the
    generic ``prompt.py`` fallbacks apply at render time. Values, when present,
    must be non-empty — filler text is worse than omission.
    """

    model_config = ConfigDict(extra="forbid")

    source_guidance: str | None = Field(default=None, min_length=1)
    tool_guidance: str | None = Field(default=None, min_length=1)
    instructions: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    output_guidance: str | None = Field(default=None, min_length=1)
    stopping_policy: str | None = Field(default=None, min_length=1)


class FinanceOutputField(BaseModel):
    """Per-benchmark override or extension of one payload field.

    Entries patch the default schema's field of the same name (``None``
    leaves the default untouched) or append a new field when the name is
    unknown. See ``artifacts.py`` for the single source of truth.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    required: bool | None = None
    kind: Literal["string", "number", "list", "str_or_list"] | None = None
    prompt_hint: str | None = None


class FinanceBenchmarkOutputs(BaseModel):
    """Per-benchmark payload field overrides on the default schema."""

    model_config = ConfigDict(extra="forbid")

    fields: list[FinanceOutputField] = Field(default_factory=list)


class FinanceBenchmarkPi(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Paths relative to the benchmark folder; resolved and validated on load.
    extensions: list[Path] = Field(default_factory=list)
    skills: list[Path] = Field(default_factory=list)


class BenchmarkConfig(BaseModel):
    """Validated contents of one benchmark folder's ``config.yaml``."""

    model_config = ConfigDict(extra="forbid")

    benchmark: str
    version: str = "1"
    display_name: str = Field(min_length=1)
    tools: FinanceBenchmarkTools = Field(default_factory=FinanceBenchmarkTools)
    source: FinanceBenchmarkSource = Field(default_factory=FinanceBenchmarkSource)
    prompt: FinanceBenchmarkPrompt = Field(default_factory=FinanceBenchmarkPrompt)
    outputs: FinanceBenchmarkOutputs = Field(default_factory=FinanceBenchmarkOutputs)
    pi: FinanceBenchmarkPi = Field(default_factory=FinanceBenchmarkPi)

    @model_validator(mode="after")
    def _extra_tools_must_be_registrable(self) -> "BenchmarkConfig":
        # Extra names outside the known tool groups can only come from a
        # benchmark-local Pi extension; without one the prompt would advertise
        # tools that are never registered.
        if self.tools.extra and not self.pi.extensions:
            unknown = [name for name in self.tools.extra if name not in _STATIC_TOOL_NAMES]
            if unknown:
                raise ValueError(
                    "tools.extra declares names no registered extension can provide ("
                    + ", ".join(unknown)
                    + "); add pi.extensions registering them or use known tool names"
                )
        return self


__all__ = [
    "BenchmarkConfig",
    "CODE_TOOLS",
    "EDGAR_TOOLS",
    "FinanceAnalysisConfig",
    "FinanceBenchmarkOutputs",
    "FinanceOutputField",
    "FinanceBenchmarkPi",
    "FinanceBenchmarkPrompt",
    "FinanceBenchmarkSource",
    "FinanceBenchmarkTools",
    "FinanceDomainConfig",
    "FinanceSourceConfig",
    "FinanceToolsConfig",
    "MARKET_TOOLS",
    "SQL_TOOLS",
    "TERMINAL_TOOLS",
    "WEB_TOOLS",
    "FilesSourceConfig",
    "FixtureSourceConfig",
    "SqliteSourceConfig",
]
