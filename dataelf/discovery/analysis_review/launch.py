"""Prepare reviewed-role resources before invoking the generic Pi executor."""

from pathlib import Path
from dataelf.discovery.contracts import AgentResources
from dataelf.discovery.domain_services import environment
from dataelf.discovery.policies import network_environment


def prepare_actor(job, context):
    workspace = Path(context.workspace_path)
    path = Path(context.env["DATAELF_ANALYSIS_CONTEXT"])
    role = context.env.get("DATAELF_ANALYSIS_ROLE", "analyst")
    env = {**network_environment(job.spec), **context.env}
    env.update(environment(job.spec.model_dump(), path))
    env["DATAELF_REVIEWED_WORKFLOW"] = "1"
    env["DATAELF_WORKFLOW_POLICY"] = job.spec.workflow_policy
    package = Path(__file__).resolve().parents[2]
    extensions = [
        package / "discovery/runtime" / name
        for name in ("compat.ts", "outputs.ts", "analysis.ts")
    ]
    extensions.append(package / "ontology/runtime/inputs.ts")
    graph = context.domain_context.get("graph_ref")
    root = context.domain_context.get("graph_root")
    if role != "reviewer" and graph and root:
        from dataelf.ontology.graph.tools import analysis_context

        env["DATAELF_GRAPH_CONTEXT"] = str(
            analysis_context(
                workspace,
                graph,
                root,
                job.job_id,
                {
                    "workflow_policy": job.spec.workflow_policy,
                    "original_spec": job.spec.model_dump(mode="json"),
                    "analysis_context_path": str(path),
                },
            )
        )
        extensions.append(package / "ontology/runtime/graph.ts")
    else:
        env.pop("DATAELF_GRAPH_CONTEXT", None)
    env.pop("DATAELF_ONTOLOGY_CONTEXT", None)
    resources = AgentResources(
        extensions=list(
            dict.fromkeys([*context.agent_resources.extensions, *extensions])
        ),
        skills=context.agent_resources.skills,
    )
    return context.model_copy(
        update={
            "env": env,
            "agent_resources": resources,
            "execution": {
                "cwd": str(workspace.resolve()),
                "stop_files": [
                    str(path.parent / name)
                    for name in (
                        "analysis_submission.json",
                        "analysis_review.json",
                        "analysis_transport.json",
                        "handoff.json",
                    )
                ],
                "compact_json_events": True,
                "tools": (
                    env.get("DATAELF_ANALYSIS_TOOLS") if role == "reviewer" else None
                ),
                "suffix": (
                    "执行当前独立审核，通过实际工具交接反馈。"
                    if role == "reviewer"
                    else "执行当前研究任务，通过实际工具保存并交接产物。"
                ),
            },
        }
    )
