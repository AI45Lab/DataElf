"""Finance benchmark adaptations.

Each benchmark owns one folder under ``dataelf/domains/finance/benchmarks/``
with a validated ``config.yaml`` plus optional Pi resources and fixture
builders. The folder name is the benchmark id used by
``domains.finance.benchmark`` in ``dataelf.local.yaml``, the
``DATAELF_FINANCE_BENCHMARK`` environment variable, and the ``benchmark``
job parameter. Selection precedence: job parameter > environment > config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from dataelf.domains.finance.config import BenchmarkConfig

BENCHMARKS_DIR = Path(__file__).resolve().parent
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class ResolvedBenchmark:
    config: BenchmarkConfig
    folder: Path

    @property
    def name(self) -> str:
        return self.config.benchmark


def list_benchmarks() -> list[str]:
    """List available benchmark ids (folders containing a config.yaml)."""
    names = [
        path.parent.name
        for path in sorted(BENCHMARKS_DIR.glob("*/config.yaml"))
        if _NAME_PATTERN.match(path.parent.name)
    ]
    return names


def get_benchmark(name: str) -> ResolvedBenchmark:
    """Load and validate one benchmark, including its Pi resource paths."""
    key = str(name).strip()
    if not _NAME_PATTERN.match(key):
        raise ValueError(f"Invalid Finance benchmark name: {name!r}")
    folder = BENCHMARKS_DIR / key
    config_path = folder / "config.yaml"
    if not config_path.is_file():
        raise ValueError(
            f"Unknown Finance benchmark {name!r}; available: {', '.join(list_benchmarks())}"
        )
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Finance benchmark config must be a mapping: {config_path}")
    payload.setdefault("benchmark", key)
    config = BenchmarkConfig.model_validate(payload)
    if config.benchmark != key:
        raise ValueError(
            f"Finance benchmark folder {key!r} declares benchmark {config.benchmark!r}"
        )
    return ResolvedBenchmark(config=config, folder=folder)


def benchmark_pi_resources(benchmark: ResolvedBenchmark) -> tuple[list[Path], list[Path]]:
    """Resolve declared Pi resources, ensuring they exist inside the folder."""
    extensions: list[Path] = []
    for relative in benchmark.config.pi.extensions:
        path = (benchmark.folder / relative).resolve()
        _assert_resource(benchmark.folder, path, "extension")
        extensions.append(path)
    skills: list[Path] = []
    for relative in benchmark.config.pi.skills:
        path = (benchmark.folder / relative).resolve()
        _assert_resource(benchmark.folder, path, "skill")
        skills.append(path)
    return extensions, skills


def _assert_resource(folder: Path, path: Path, kind: str) -> None:
    if not path.is_relative_to(folder):
        raise ValueError(f"Finance benchmark {kind} escapes benchmark folder: {path}")
    if not path.is_file():
        raise ValueError(f"Finance benchmark {kind} does not exist: {path}")


__all__ = [
    "BENCHMARKS_DIR",
    "ResolvedBenchmark",
    "benchmark_pi_resources",
    "get_benchmark",
    "list_benchmarks",
]
