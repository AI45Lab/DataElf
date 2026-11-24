"""Manifest-selected services at the source/prompt boundary.

The runtime never guesses a domain. Persisted task identity is used by tool
subprocesses, which do not share the caller's Python objects or registries.
"""

import json
from importlib import import_module
from pathlib import Path
from .domain_registry import DomainRegistry


def bind(workspace, domain):
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / "domain_services.json"
    value = {"domain": domain}
    if path.exists():
        if json.loads(path.read_text()) != value:
            raise ValueError("Workspace service identity cannot change")
    else:
        path.write_text(json.dumps(value) + "\n")


def identity(context):
    if isinstance(context, (str, Path)):
        workspace = Path(context)
        for name in ("domain_services.json", "job_spec.json"):
            path = workspace / name
            if path.is_file():
                return json.loads(path.read_text())["domain"]
        raise ValueError("No domain service identity bound to workspace")
    for part in (
        context,
        context.get("request", {}),
        context.get("spec", {}),
        context.get("original_spec", {}),
    ):
        if part.get("domain"):
            return part["domain"]
    workspace = context.get("workspace") or context.get("request", {}).get("workspace")
    return identity(workspace) if workspace else None


def services(context):
    domain = identity(context)
    if not domain:
        raise ValueError("Domain identity is required for source/prompt services")
    manifest = DomainRegistry().load_manifest(domain)
    entry = manifest.agent_services
    if not entry:
        raise ValueError(f"Domain {domain} has not declared agent_services")
    module, separator, name = entry.partition(":")
    if not separator:
        raise ValueError("agent_services must use module:object syntax")
    return getattr(import_module(module), name)


def evidence(workspace):
    return services(workspace).evidence(workspace)


def source_schemas(role, context):
    return services(context).source_schemas(role)


def source_dispatch(path, name, arguments):
    context = json.loads(Path(path).read_text())
    if name not in source_schemas(context["role"], context):
        raise ValueError("Source tool is not permitted for this role")
    return services(context).source_dispatch(path, name, arguments)


def descriptions(context):
    return services(context).descriptions()


def compose(role, context, tools):
    return services(context).compose(role, context, tools)


def input_manifest(role, system, task, tools, context):
    return services(context).input_manifest(role, system, task, tools, context)


def skills(context):
    return services(context).skills


def environment(context, path):
    return services(context).environment(path)
