"""Read-only context capacity and recent measured inference usage."""
from sqlalchemy import func

from thoth_terminal.cloud_agent_memory_service import render_cloud_agent_memory_context
from thoth_terminal.config import settings
from thoth_terminal.models import CloudAgentMemorySegment, CloudGoalRun


def research_context_health(session) -> dict:
    budget = int(settings.cloud_agent_memory_context_chars)
    text, metadata = render_cloud_agent_memory_context(session, max_chars=budget, mark_accessed=False)
    count = session.query(func.count(CloudAgentMemorySegment.id)).filter_by(
        agent_key=metadata["agent_key"], status="active").scalar() or 0
    runs = session.query(CloudGoalRun.id, CloudGoalRun.task_kind, CloudGoalRun.model,
                         CloudGoalRun.created_at, CloudGoalRun.stats_json).filter(
        CloudGoalRun.task_kind.in_(["cloud_research_heartbeat", "cloud_research_nightly"]),
        CloudGoalRun.status == "complete").order_by(CloudGoalRun.id.desc()).limit(12).all()
    return {
        "memory": {**metadata, "active_segments_total": count, "budget_chars": budget,
                   "rendered_chars": len(text), "capacity_percent": round(len(text) * 100 / budget, 2) if budget > 0 else None,
                   "selection_limit_reached": count > metadata["segments"],
                   "scope": "Configured legacy memory renderer; immutable stable snapshot has a separate representation."},
        "recent_runs": [{"id": id, "kind": kind, "model": model, "created_at": str(created),
                         **{key: (stats or {}).get(key) for key in
                            ("input_tokens", "output_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cache_hit_rate")}}
                        for id, kind, model, created, stats in runs],
        "action": "Review and consolidate memory with evidence retention when omissions or selection limits occur; do not silently discard source claims.",
    }
