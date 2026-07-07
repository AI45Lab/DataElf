from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, TypedDict

from dataelf.config import DataElfConfig
from dataelf.discovery.base import DiscoveryContext
from dataelf.discovery.domain_registry import DomainRegistry
from dataelf.discovery.explorer_factory import create_insights_explorer, normalize_insights_explorer_name
from dataelf.discovery.quality_review import review_workspace
from dataelf.discovery.result_parser import load_insight_candidate_ids
from dataelf.discovery.workspace import prepare_workspace
from dataelf.domains.ai_index.client import AIIndexClient
from dataelf.domains.ai_index.connector import AIIndexConnector
from dataelf.schemas import DiscoveryJob, new_id, now_utc
from dataelf.stores.sqlite_store import SQLiteStore

logger = logging.getLogger("dataelf.discovery")


class StoreLike:
    def save_discovery_job(self, job: DiscoveryJob) -> None:
        ...

    def add_trace_event(self, job_id: str, event_type: str, payload: dict[str, Any]) -> str:
        ...

    def save_quality_review(self, review: Any) -> None:
        ...


class NullStore:
    def save_discovery_job(self, job: DiscoveryJob) -> None:
        return None

    def add_trace_event(self, job_id: str, event_type: str, payload: dict[str, Any]) -> str:
        logger.debug("trace_event skipped because sqlite is disabled: %s %s %s", job_id, event_type, payload)
        return ""

    def save_quality_review(self, review: Any) -> None:
        return None


class DiscoveryWorkflowState(TypedDict, total=False):
    user_query: str
    config: DataElfConfig
    store: StoreLike
    domain_registry: DomainRegistry
    domain_pack: dict[str, Any]
    client: AIIndexClient
    job: DiscoveryJob
    quality_review: dict[str, Any]


def run_discovery(user_query: str, config: DataElfConfig) -> DiscoveryJob:
    config.ensure_dirs()
    logger.info("Starting discovery workflow.")
    graph = build_discovery_workflow()
    result = graph.invoke({"user_query": user_query, "config": config})
    logger.info("Discovery workflow finished with status=%s.", result["job"].status)
    return result["job"]


def initialize_job(user_query: str, config: DataElfConfig, store: StoreLike) -> DiscoveryJob:
    job_id = new_id("job")
    workspace_path = config.workspaces_dir / job_id
    logger.info("Initializing discovery job %s.", job_id)
    job = DiscoveryJob(
        job_id=job_id,
        trigger_type="user",
        job_type="user_requested_discovery",
        seed_query=user_query,
        status="running",
        workspace_path=str(workspace_path),
        constraints={"max_api_calls": 80, "max_web_searches": 20, "max_runtime_minutes": 30},
    )
    store.save_discovery_job(job)
    store.add_trace_event(job.job_id, "job_initialized", {"seed_query": user_query, "workspace_path": str(workspace_path)})
    return job


def parse_discovery_intent(job: DiscoveryJob, store: StoreLike) -> DiscoveryJob:
    query = job.seed_query or ""
    logger.info("Parsing discovery intent for job %s.", job.job_id)
    topic = _extract_topic(query)
    scope: dict[str, Any] = {
        "domain": "ai_index",
        "topic": topic,
        "goal": "discover_insights",
        "domains": ["LLMs"] if "llm" in query.lower() or "Agentic" in topic else [],
        "sub_domains": ["Agentic LLMs"] if "agent" in query.lower() or "智能体" in query else [],
        "time_window": "last_6_months" if "半年" in query or "最近" in query else "last_6_months",
        "expected_outputs": _extract_expected_outputs(query),
        "need_web_search": "联网" in query or "web" in query.lower() or "search" in query.lower(),
        "need_code_analysis": True,
    }
    if not scope["domains"]:
        scope["domains"] = ["LLMs"]
    if not scope["sub_domains"]:
        scope["sub_domains"] = ["Agentic LLMs"]
    job.scope = scope
    job.updated_at = now_utc()
    store.save_discovery_job(job)
    store.add_trace_event(job.job_id, "intent_parse", {"scope": scope})
    return job


def build_discovery_workflow():
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:
        raise RuntimeError("langgraph is not installed. Run `uv pip install -e .` or `pip install -e .`.") from exc

    def prepare(state: dict[str, Any]) -> dict[str, Any]:
        config: DataElfConfig = state["config"]
        if config.enable_sqlite:
            store: StoreLike = SQLiteStore(config.sqlite_path)
            store.init_schema()
        else:
            store = NullStore()
        return {"store": store, "domain_registry": DomainRegistry()}

    def init_job_node(state: dict[str, Any]) -> dict[str, Any]:
        return {"job": initialize_job(state["user_query"], state["config"], state["store"])}

    def intent_node(state: dict[str, Any]) -> dict[str, Any]:
        return {"job": parse_discovery_intent(state["job"], state["store"])}

    def load_domain_pack_node(state: dict[str, Any]) -> dict[str, Any]:
        job: DiscoveryJob = state["job"]
        domain = job.scope.get("domain", "ai_index")
        logger.info("Loading domain pack: %s.", domain)
        pack = state["domain_registry"].load_domain_pack(domain)
        state["store"].add_trace_event(job.job_id, "domain_pack_loaded", {"domain": domain, "tools": pack.get("tools", [])})
        return {"domain_pack": pack}

    def prepare_workspace_node(state: dict[str, Any]) -> dict[str, Any]:
        job: DiscoveryJob = state["job"]
        logger.info("Preparing workspace: %s.", job.workspace_path)
        workspace = prepare_workspace(Path(job.workspace_path), domain=job.scope.get("domain", "ai_index"))
        state["store"].add_trace_event(job.job_id, "workspace_prepared", {"workspace_path": str(workspace)})
        return {}

    def insights_explore_node(state: dict[str, Any]) -> dict[str, Any]:
        config: DataElfConfig = state["config"]
        job: DiscoveryJob = state["job"]
        logger.info("Starting insights_explore for job %s.", job.job_id)
        connector = AIIndexConnector(
            mode=config.ai_index_mode,
            base_url=config.ai_index_base_url,
            api_key=config.ai_index_api_key,
            fixtures_dir=config.fixtures_dir,
            workspace_path=Path(job.workspace_path),
        )
        client = AIIndexClient(connector=connector, workspace_path=Path(job.workspace_path))
        explorer_name = normalize_insights_explorer_name(config.insights_explorer)
        explorer = create_insights_explorer(config)
        explorer_model = config.pi_model if explorer_name == "pi" else config.model
        context = DiscoveryContext(
            workspace_path=job.workspace_path,
            domain=job.scope.get("domain", "ai_index"),
            model=explorer_model,
            env={
                "DATAELF_AI_INDEX_MODE": config.ai_index_mode,
                "AI_INDEX_BASE_URL": config.ai_index_base_url,
                "AI_INDEX_API_KEY": config.ai_index_api_key,
            },
            domain_pack=state["domain_pack"],
            config={**config.model_dump(mode="json"), "insights_explorer": explorer_name},
        )
        state["store"].add_trace_event(job.job_id, "insights_explore_start", {"mode": config.ai_index_mode, "explorer": explorer_name})
        result = explorer.run(job, context)
        logger.info("insights_explore finished with status=%s for job %s.", result.status, job.job_id)
        job.insight_candidate_ids = load_insight_candidate_ids(Path(job.workspace_path))
        job.updated_at = now_utc()
        if result.error:
            job.error = result.error or "insights_explore_failed"
            job.status = "failed"
        state["store"].save_discovery_job(job)
        state["store"].add_trace_event(
            job.job_id,
            "insights_explore_completed",
            {"result": result.model_dump(mode="json"), "insight_candidate_ids": job.insight_candidate_ids},
        )
        return {"job": job, "client": client}

    def route_after_insights_explore(state: dict[str, Any]) -> str:
        job: DiscoveryJob = state["job"]
        if job.status == "failed" or job.error:
            logger.info("Skipping quality review because insights_explore failed for job %s.", job.job_id)
            return "finalize"
        return "quality_review"

    def quality_review_node(state: dict[str, Any]) -> dict[str, Any]:
        job: DiscoveryJob = state["job"]
        logger.info("Running quality review for job %s.", job.job_id)
        review = review_workspace(job.job_id, Path(job.workspace_path))
        state["store"].save_quality_review(review)
        (Path(job.workspace_path) / "reviews" / "quality_review.json").write_text(
            review.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        state["store"].add_trace_event(
            job.job_id,
            "quality_review_completed",
            {"review_status": review.review_status, "warnings": review.warnings},
        )
        return {"quality_review": review.model_dump(mode="json")}

    def finalize_node(state: dict[str, Any]) -> dict[str, Any]:
        job: DiscoveryJob = state["job"]
        review = state.get("quality_review", {})
        logger.info("Finalizing job %s.", job.job_id)
        if job.status != "failed":
            job.status = "completed" if review.get("review_status") != "failed" else "failed"
        job.updated_at = now_utc()
        if job.status == "failed" and not job.error:
            job.error = "quality_review_failed"
        if job.status == "failed" and not review:
            review = _write_skipped_quality_review(job, "Skipped because insights_explore failed.")
        state["store"].save_discovery_job(job)
        _write_workspace_index(job, review)
        state["store"].add_trace_event(job.job_id, "job_finalized", {"status": job.status})
        return {"job": job}

    workflow = StateGraph(DiscoveryWorkflowState)
    workflow.add_node("prepare", prepare)
    workflow.add_node("init_job", init_job_node)
    workflow.add_node("intent_parse", intent_node)
    workflow.add_node("load_domain_pack", load_domain_pack_node)
    workflow.add_node("prepare_workspace", prepare_workspace_node)
    workflow.add_node("insights_explore", insights_explore_node)
    workflow.add_node("quality_review", quality_review_node)
    workflow.add_node("finalize", finalize_node)
    workflow.set_entry_point("prepare")
    workflow.add_edge("prepare", "init_job")
    workflow.add_edge("init_job", "intent_parse")
    workflow.add_edge("intent_parse", "load_domain_pack")
    workflow.add_edge("load_domain_pack", "prepare_workspace")
    workflow.add_edge("prepare_workspace", "insights_explore")
    workflow.add_conditional_edges(
        "insights_explore",
        route_after_insights_explore,
        {"quality_review": "quality_review", "finalize": "finalize"},
    )
    workflow.add_edge("quality_review", "finalize")
    workflow.add_edge("finalize", END)
    return workflow.compile()


def _extract_topic(query: str) -> str:
    match = re.search(r"围绕\s*([^，,]+)", query)
    if match:
        return match.group(1).strip()
    if "Agentic" in query or "agent" in query.lower():
        return "Agentic LLMs"
    return "AI science intelligence"


def _extract_expected_outputs(query: str) -> int:
    digit = re.search(r"(\d+)\s*个", query)
    if digit:
        return max(1, min(int(digit.group(1)), 5))
    chinese = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}
    for token, value in chinese.items():
        if f"{token}个" in query:
            return value
    return 3


def _write_workspace_index(job: DiscoveryJob, review: dict[str, Any]) -> None:
    path = Path(job.workspace_path) / "workspace_index.json"
    payload = {
        "job_id": job.job_id,
        "status": job.status,
        "seed_query": job.seed_query,
        "scope": job.scope,
        "insight_candidate_ids": job.insight_candidate_ids,
        "quality_review": review,
        "key_files": {
            "candidate_signals": "insights/candidate_signals.json",
            "insight_candidates": "insights/insight_candidates.json",
            "final_brief": "insights/final_brief.md",
            "quality_review": "reviews/quality_review.json",
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_skipped_quality_review(job: DiscoveryJob, reason: str) -> dict[str, Any]:
    payload = {
        "review_id": None,
        "job_id": job.job_id,
        "review_status": "skipped",
        "warnings": [reason],
        "recommended_revision": False,
        "payload": {"reason": reason},
        "created_at": now_utc().isoformat(),
    }
    path = Path(job.workspace_path) / "reviews" / "quality_review.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload
