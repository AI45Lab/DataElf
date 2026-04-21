from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from typing import Any

from .composite_tool import CompositeDerivedTool


class AssetManager:
    def __init__(self, root: str | Path = ".elf") -> None:
        self.root = Path(root)
        self.stable_root = self.root / "stable"
        self.candidate_root = self.root / "candidates"
        self.attempt_root = self.root / "attempts"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for path in [
            self.root,
            self.stable_root / "pipeline_assets",
            self.stable_root / "tool_assets",
            self.stable_root / "tool_code" / "experimental",
            self.candidate_root / "pipeline_candidates",
            self.candidate_root / "tool_candidates" / "composite",
            self.candidate_root / "tool_candidates" / "experimental",
            self.candidate_root / "tool_code" / "experimental",
            self.attempt_root,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def save_attempt(self, job_id: str, attempt_id: str, record: dict[str, Any]) -> Path:
        attempt_dir = self.attempt_root / job_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        path = attempt_dir / f"{attempt_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
        return path

    def list_attempts(self, job_id: str) -> list[dict[str, Any]]:
        attempt_dir = self.attempt_root / job_id
        if not attempt_dir.exists():
            return []
        records = []
        for path in sorted(attempt_dir.glob("*.json")):
            records.append(self._read_json(path))
        return records

    def save_candidate(self, candidate: dict[str, Any], python_code: str | None = None) -> Path:
        candidate_type = candidate.get("candidate_type", "composite_tool")
        if candidate_type == "pipeline":
            candidate_path = self.candidate_root / "pipeline_candidates" / f"{candidate['candidate_id']}.json"
        elif candidate_type == "experimental_python_tool":
            candidate_path = self.candidate_root / "tool_candidates" / "experimental" / f"{candidate['candidate_id']}.json"
            if python_code is not None:
                code_path = self.candidate_root / "tool_code" / "experimental" / f"{candidate['candidate_id']}.py"
                code_path.write_text(python_code, encoding="utf-8")
                candidate["code_path"] = str(code_path)
        else:
            candidate_path = self.candidate_root / "tool_candidates" / "composite" / f"{candidate['candidate_id']}.json"

        with open(candidate_path, "w", encoding="utf-8") as f:
            json.dump(candidate, f, indent=2, ensure_ascii=False)
        return candidate_path

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        path = self._candidate_path(candidate_id)
        if path is not None and path.exists():
            return self._read_json(path)
        for legacy_path in [
            self.candidate_root / "pipelines" / f"{candidate_id}.json",
            self.candidate_root / "composite_tools" / f"{candidate_id}.json",
            self.candidate_root / "experimental_tools" / f"{candidate_id}.json",
        ]:
            if legacy_path.exists():
                return self._read_json(legacy_path)
        return None

    def update_candidate(self, candidate_id: str, **updates: Any) -> dict[str, Any]:
        path = self._candidate_path(candidate_id)
        if path is None or not path.exists():
            raise ValueError(f"Candidate not found: {candidate_id}")

        candidate = self._read_json(path)
        candidate.update(updates)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(candidate, f, indent=2, ensure_ascii=False)
        return candidate

    def reject_candidate(self, candidate_id: str, reason: str = "") -> dict[str, Any]:
        payload = {"status": "rejected"}
        if reason:
            payload["rejection_reason"] = reason
        return self.update_candidate(candidate_id, **payload)

    def list_candidates(self) -> list[dict[str, Any]]:
        paths = sorted((self.candidate_root / "pipeline_candidates").glob("*.json"))
        paths += sorted((self.candidate_root / "tool_candidates" / "composite").glob("*.json"))
        paths += sorted((self.candidate_root / "tool_candidates" / "experimental").glob("*.json"))
        return [self._read_json(path) for path in paths]

    def save_stable_pipeline_asset(
        self,
        asset_id: str,
        name: str,
        pipeline: str,
        metadata: dict[str, Any] | None = None,
    ) -> Path:
        path = self.stable_root / "pipeline_assets" / f"{asset_id}.json"
        payload = {
            "asset_id": asset_id,
            "name": name,
            "asset_type": "pipeline",
            "pipeline": pipeline,
            "metadata": metadata or {},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        return path

    def get_stable_asset(self, asset_id: str) -> dict[str, Any] | None:
        for path in [
            self.stable_root / "pipeline_assets" / f"{asset_id}.json",
            self.stable_root / "tool_assets" / f"{asset_id}.json",
            self.stable_root / "pipelines" / f"{asset_id}.json",
            self.stable_root / "tools" / f"{asset_id}.json",
        ]:
            if path.exists():
                return self._read_json(path)
        return None

    def list_stable_assets(self) -> list[dict[str, Any]]:
        paths = sorted((self.stable_root / "pipeline_assets").glob("*.json"))
        paths += sorted((self.stable_root / "tool_assets").glob("*.json"))
        return [self._read_json(path) for path in paths]

    def approve_candidate(self, candidate_id: str) -> dict[str, Any]:
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise ValueError(f"Candidate not found: {candidate_id}")

        candidate_type = candidate.get("candidate_type")
        if candidate_type == "pipeline":
            asset = {
                "asset_id": self._make_asset_id(candidate),
                "asset_type": "pipeline",
                "name": candidate.get("name", candidate_id),
                "description": candidate.get("description", ""),
                "pipeline": candidate["pipeline"],
                "source_candidate_id": candidate_id,
                "source_attempts": candidate.get("source_attempts", []),
                "validation_criteria": candidate.get("validation_criteria", []),
                "tool_domains": candidate.get("tool_domains", []),
                "status": "approved",
                "metadata": candidate.get("metadata", {}),
            }
            target_json = self.stable_root / "pipeline_assets" / f"{asset['asset_id']}.json"
        elif candidate_type == "experimental_python_tool":
            asset = {
                "asset_id": self._make_asset_id(candidate),
                "asset_type": "tool",
                "tool_kind": "experimental_python_tool",
                "name": candidate.get("name", candidate_id),
                "description": candidate.get("description", ""),
                "validation_criteria": candidate.get("validation_criteria", []),
                "tool_domains": candidate.get("tool_domains", []),
                "source_candidate_id": candidate_id,
                "source_attempts": candidate.get("source_attempts", []),
                "status": "approved",
            }
            target_json = self.stable_root / "tool_assets" / f"{asset['asset_id']}.json"
            target_py = self.stable_root / "tool_code" / "experimental" / f"{asset['asset_id']}.py"
            shutil.copy2(candidate["code_path"], target_py)
            asset["code_path"] = str(target_py)
        else:
            asset = {
                "asset_id": self._make_asset_id(candidate),
                "asset_type": "tool",
                "tool_kind": "composite_tool",
                "name": candidate.get("name", candidate_id),
                "description": candidate.get("description", ""),
                "input_schema": candidate.get("input_schema", {}),
                "steps": candidate.get("steps", []),
                "result": candidate.get("result"),
                "usage_example": candidate.get("usage_example"),
                "validation_criteria": candidate.get("validation_criteria", []),
                "tool_domains": candidate.get("tool_domains", []),
                "source_candidate_id": candidate_id,
                "source_attempts": candidate.get("source_attempts", []),
                "status": "approved",
            }
            target_json = self.stable_root / "tool_assets" / f"{asset['asset_id']}.json"

        with open(target_json, "w", encoding="utf-8") as f:
            json.dump(asset, f, indent=2, ensure_ascii=False)

        self.update_candidate(candidate_id, status="approved", asset_id=asset["asset_id"])

        return asset

    def register_stable_tools(self, registry: Any) -> list[str]:
        return self._register_tool_manifests(
            registry,
            paths=sorted((self.stable_root / "tool_assets").glob("*.json")),
            allow_experimental=True,
        )

    def register_candidate_tools(
        self,
        registry: Any,
        allow_experimental: bool = False,
    ) -> list[str]:
        paths = sorted((self.candidate_root / "tool_candidates" / "composite").glob("*.json"))
        if allow_experimental:
            paths += sorted((self.candidate_root / "tool_candidates" / "experimental").glob("*.json"))
        return self._register_tool_manifests(
            registry,
            paths=paths,
            allow_experimental=allow_experimental,
        )

    def _register_tool_manifests(
        self,
        registry: Any,
        paths: list[Path],
        allow_experimental: bool,
    ) -> list[str]:
        loaded: list[str] = []
        for path in paths:
            manifest = self._read_json(path)
            tool_kind = manifest.get("tool_kind") or manifest.get("candidate_type")
            manifest_status = str(manifest.get("status", "") or "").lower()
            validation_status = str(manifest.get("validation_status", "") or "").lower()
            if manifest_status in {"rejected", "smoke_failed"} or validation_status == "smoke_failed":
                continue
            if tool_kind == "experimental_python_tool":
                if not allow_experimental:
                    continue
                tool = self._load_python_tool(manifest["code_path"])
            else:
                tool = CompositeDerivedTool(manifest)

            if registry.get(tool.name) is None:
                registry.register(tool)
                loaded.append(tool.name)
        return loaded

    def _load_python_tool(self, file_path: str):
        module_path = Path(file_path)
        spec = importlib.util.spec_from_file_location(module_path.stem, module_path)
        if spec is None or spec.loader is None:
            raise ValueError(f"Cannot load experimental tool module: {file_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        if hasattr(module, "build_tool"):
            return module.build_tool()

        for value in module.__dict__.values():
            if isinstance(value, type) and value.__name__.endswith("Tool"):
                return value()

        raise ValueError(f"No tool class found in experimental tool module: {file_path}")

    def _read_json(self, path: Path) -> dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _candidate_path(self, candidate_id: str) -> Path | None:
        for path in [
            self.candidate_root / "pipeline_candidates" / f"{candidate_id}.json",
            self.candidate_root / "tool_candidates" / "composite" / f"{candidate_id}.json",
            self.candidate_root / "tool_candidates" / "experimental" / f"{candidate_id}.json",
        ]:
            if path.exists():
                return path
        return None

    def _make_asset_id(self, candidate: dict[str, Any]) -> str:
        candidate_id = candidate.get("candidate_id", "")
        candidate_type = candidate.get("candidate_type", "")
        if candidate_type == "pipeline":
            return candidate_id.replace("cand_pipe_", "asset_pipe_", 1)
        if candidate_type in {"composite_tool", "experimental_python_tool"}:
            return candidate_id.replace("cand_", "asset_", 1)
        return f"asset_{candidate_id}" if candidate_id else "asset_unknown"
