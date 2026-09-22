from __future__ import annotations

import importlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Callable

from dataelf.discovery.contracts import ArtifactRef, StageResult
from dataelf.domains.finance.config import FinanceSourceConfig


def prepare_finance_source(source: FinanceSourceConfig, workspace: Path, fixture_builder: str | None = None) -> StageResult:
    """Materialize the run's data sources inside the workspace.

    Sources are composable: a run may combine a SQLite database (SQL tools)
    with a files tree (Python tools), be web-only (no sources at all), or run
    offline from fixtures. The workspace always gets a Python code root at
    ``raw/finance/input`` (empty when no files source applies) and an
    auditable ``source_manifest.json``. Benchmark-specific seeding lives
    behind ``fixture_builder`` (a ``module:function`` path declared by the
    benchmark config), never in this module.

    Sources configured with ``link: true`` are symlinked instead of copied
    (zero-copy for corpora too large to replicate per run). Linked targets
    are exported as ``DATAELF_FINANCE_PROTECTED`` so the execute_code
    read-only enforcement layer can refuse writes to the shared originals;
    the manifest records the strategy and, for databases, size/mtime so
    post-run checks can detect accidental mutation.
    """
    raw_dir = workspace / "raw" / "finance"
    table_dir = workspace / "tables" / "finance"
    input_dir = raw_dir / "input"
    raw_dir.mkdir(parents=True, exist_ok=True)
    table_dir.mkdir(parents=True, exist_ok=True)

    files_copied = False
    db_prepared = False
    protected: list[str] = []
    provenance: dict[str, Any] = {"offline": source.offline}

    if source.offline:
        assert source.fixture is not None
        fixture_dir = Path(source.fixture.dir)
        offline_input = fixture_dir / "input"
        if offline_input.is_dir():
            shutil.copytree(offline_input, input_dir, dirs_exist_ok=True)
            files_copied = True
            provenance["files"] = str(offline_input)
        if fixture_builder:
            builder = _load_builder(fixture_builder)
            destination = table_dir / "finance.db"
            try:
                builder(destination, fixture_dir)
            except (OSError, ValueError) as exc:
                return StageResult(status="failed", error_code="FINANCE_FIXTURE_BUILD_FAILED", error_message=f"Finance fixture build failed: {exc}")
            db_prepared = True
            provenance["database"] = {"builder": fixture_builder, "fixture_dir": str(fixture_dir)}
        else:
            offline_db = fixture_dir / "finance.db"
            if offline_db.is_file():
                shutil.copy2(offline_db, table_dir / "finance.db")
                db_prepared = True
                provenance["database"] = str(offline_db)
    else:
        if source.files is not None:
            source_path = Path(source.files.data_path).resolve()
            if not source_path.exists():
                return StageResult(status="failed", error_code="FINANCE_SOURCE_MISSING", error_message=f"Finance data path does not exist: {source_path}")
            if source.files.link:
                if source_path.is_dir() and not source_path.is_symlink():
                    # Leaves that resolve back under the source root are
                    # already covered by the root export below; only symlinks
                    # pointing outside the tree need their own entry.
                    protected.extend(
                        path for path in _mirror_links(source_path, input_dir)
                        if not path.startswith(str(source_path) + os.sep)
                    )
                else:
                    # Single file (or a symlinked dir): keep input_dir a real
                    # directory so it stays usable as the execute_code cwd.
                    input_dir.mkdir(parents=True, exist_ok=True)
                    (input_dir / source_path.name).symlink_to(source_path)
                provenance["files"] = {"strategy": "symlink", "target": str(source_path)}
                protected.append(str(source_path))
            elif source_path.is_dir():
                shutil.copytree(source_path, input_dir, dirs_exist_ok=True)
                provenance["files"] = str(source_path)
            else:
                input_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, input_dir / source_path.name)
                provenance["files"] = str(source_path)
            files_copied = True
        if source.sqlite is not None:
            source_db = Path(source.sqlite.db_path).resolve()
            if not source_db.is_file():
                return StageResult(status="failed", error_code="FINANCE_SOURCE_MISSING", error_message=f"Finance SQLite database does not exist: {source_db}")
            destination_db = table_dir / "finance.db"
            if source.sqlite.link:
                destination_db.symlink_to(source_db)
                stat = source_db.stat()
                provenance["database"] = {"strategy": "symlink", "target": str(source_db), "size": stat.st_size, "mtime": stat.st_mtime}
                protected.append(str(source_db))
            else:
                shutil.copy2(source_db, destination_db)
                provenance["database"] = str(source_db)
            db_prepared = True

    if not files_copied:
        input_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(path.relative_to(workspace).as_posix() for path in input_dir.rglob("*") if path.is_file())
    manifest = {
        "offline": source.offline,
        "source_kinds": ([] + (["sqlite"] if db_prepared else []) + (["files"] if files_copied else [])),
        "files": files,
        "database": db_prepared,
        "linked": bool(protected),
    }
    manifest_path = table_dir / "source_manifest.json"
    manifest_path.write_text(json.dumps({"provenance": provenance, **manifest}, indent=2) + "\n", encoding="utf-8")

    artifacts = [ArtifactRef(artifact_id="finance_source_manifest", kind="source_manifest", path="tables/finance/source_manifest.json", role="evidence", producer_stage="domain_prepare", media_type="application/json")]
    env = {"DATAELF_FINANCE_CODE_ROOT": str(input_dir)}
    if protected:
        # Real paths of linked corpora; consumed by the execute_code
        # read-only enforcement layer (tools/readonly_exec.py).
        env["DATAELF_FINANCE_PROTECTED"] = os.pathsep.join(sorted(set(protected)))
    if files_copied:
        artifacts.append(ArtifactRef(artifact_id="finance_source_files", kind="financial_source_collection", path="raw/finance/input", role="input", producer_stage="domain_prepare", provenance={"source": provenance.get("files")}))
    if db_prepared:
        schema_path = table_dir / "schema.json"
        schema_path.write_text(json.dumps(_inspect_schema(table_dir / "finance.db"), indent=2) + "\n", encoding="utf-8")
        artifacts.append(ArtifactRef(artifact_id="finance_database", kind="sqlite_database", path="tables/finance/finance.db", role="input", producer_stage="domain_prepare", media_type="application/vnd.sqlite3", provenance={"source": provenance.get("database")}))
        artifacts.append(ArtifactRef(artifact_id="finance_schema", kind="table_schema", path="tables/finance/schema.json", role="evidence", producer_stage="domain_prepare", media_type="application/json"))
        env["DATAELF_FINANCE_DB"] = str(table_dir / "finance.db")

    context = {
        "code_root": str(input_dir),
        "db_path": str(table_dir / "finance.db") if db_prepared else None,
        "source_kinds": manifest["source_kinds"],
        "source_mode": "fixture" if source.offline else ("mixed" if db_prepared and files_copied else "sqlite" if db_prepared else "files" if files_copied else "none"),
        "linked": bool(protected),
    }
    return StageResult(
        status="completed",
        artifacts=artifacts,
        context=context,
        env=env,
        authorized_outside=sorted(set(protected)),
    )


def _mirror_links(source: Path, destination: Path) -> set[str]:
    """Mirror a source tree under ``destination`` using symlinks.

    Directories are recreated; every non-directory entry (including the
    source tree's own symlinks, resolved) becomes a symlink to its original.
    Writes through the links are refused by the execute_code read-only
    enforcement layer, so the shared corpus stays pristine.
    """
    if source.is_dir() and not source.is_symlink():
        destination.mkdir(parents=True, exist_ok=True)
        protected: set[str] = set()
        for child in sorted(source.iterdir()):
            protected.update(_mirror_links(child, destination / child.name))
        return protected
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(source)
        return {str(source.resolve())}


def _inspect_schema(path: Path) -> dict[str, list[dict[str, Any]]]:
    schema: dict[str, list[dict[str, Any]]] = {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for name in names:
            schema[name] = [
                {"name": row[1], "type": row[2], "not_null": bool(row[3]), "primary_key": bool(row[5])}
                for row in conn.execute(f'PRAGMA table_info("{name}")')
            ]
    return schema


def _load_builder(spec: str) -> Callable[[Path, Path], Any]:
    module_name, _, function_name = spec.partition(":")
    if not module_name or not function_name:
        raise ValueError(f"Invalid fixture_builder (expected module:function): {spec}")
    module = importlib.import_module(module_name)
    builder = getattr(module, function_name, None)
    if not callable(builder):
        raise ValueError(f"fixture_builder is not callable: {spec}")
    return builder


__all__ = ["prepare_finance_source"]
