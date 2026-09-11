"""Ephemeral Pi configuration snapshots, with no writes to research settings."""
from __future__ import annotations
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


@contextmanager
def isolated_agent(settings, env):
    configured = env.get("PI_CODING_AGENT_DIR") or os.getenv("PI_CODING_AGENT_DIR")
    source = Path(configured).expanduser() if configured else Path.home() / ".pi/agent"
    if not source.is_absolute():
        source = settings.project_root / source
    with TemporaryDirectory(prefix="dataelf-server-pi-") as directory:
        root = Path(directory)
        for name in ("models.json", "auth.json", "settings.json"):
            path = source / name
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if name == "models.json":
                for provider in data.get("providers", {}).values():
                    for definition in [provider, *provider.get("models", [])]:
                        value = definition.get("baseUrl", "")
                        match = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", value)
                        if match:
                            resolved = env.get(match[1]) or os.getenv(match[1])
                            if not resolved:
                                raise ValueError(f"Missing provider endpoint variable: {match[1]}")
                            definition["baseUrl"] = resolved.rstrip("/").removesuffix("/chat/completions")
            if name == "settings.json":
                data = {k: v for k, v in data.items() if k not in {"packages", "extensions", "skills", "prompts", "themes"}}
            target = root / name
            target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            target.chmod(0o600)
        yield {**env, "PI_CODING_AGENT_DIR": str(root)}
