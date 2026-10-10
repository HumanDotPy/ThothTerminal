from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.cloud_goal_run_service import (
    CLOUD_GOAL_COMPLETE,
    CLOUD_GOAL_FAILED,
    CLOUD_GOAL_RUNNING,
    append_cloud_goal_event,
    get_cloud_goal_run,
    list_cloud_goal_events,
    serialize_cloud_goal_run,
    utcnow,
)
from thoth_terminal.cloud_orchestrator_service import (
    continue_cloud_orchestrator_handoffs,
    continue_cloud_orchestrator_run,
    start_cloud_orchestrator_run,
)
from thoth_terminal.cloud_skill_runtime_service import cloud_skill_snapshot, render_cloud_skill_instructions, resolve_cloud_skills
from thoth_terminal.models import CloudGoalRun, ResearchSkill


CLOUD_OVERLORD_SKILL_SLUG = "thoth-cloud-analyst-overlord"

CLOUD_OVERLORD_SKILL_PROMPT = """Act as Thoth's autonomous lead analyst.

Own the active goal instead of waiting for a fully specified task list. Search across Thoth's documents, reports, knowledge assets, datasets, supply chains, theses, product stages, instrument requests, and recent agent state. Ask what is missing, decide what matters, and delegate bounded work to the available cloud task capabilities.

Use Flash-tier agents for broad reading, source packs, extraction, and gap discovery. Use strong-tier agents for substantive synthesis, modeling, adversarial review, and final quality judgment. Build dependency chains so downstream agents receive upstream results. Prefer an independently challenged best-effort first version over leaving high-quality source material untouched.

Treat trusted Substack and specialist sources as admissible evidence when claims are explicitly attributed to the author and source document. A single source is a verification gap, not an automatic rejection. Never silently convert an attributed estimate into a verified fact.

Use existing durable rails. Search for a destination before creating one. Write reports, addenda, knowledge assets, datasets, supply-chain state, product stages, evidence, or precise requests only when the evidence fits. Keep minimum-viable state distinct from canon-ready state. Preserve source IDs, run IDs, assumptions, contradictions, and unresolved checks.

Revise the goal when evidence shows it is too narrow, duplicated, incorrectly framed, or already satisfied. Record why the goal changed. Stop only when the goal is achieved, explicitly paused or cleared, or genuinely blocked by missing authority or inaccessible evidence."""


def _text(value: object) -> str:
    return str(value or "").strip()


def ensure_cloud_overlord_skill(session: Session) -> ResearchSkill:
    row = session.query(ResearchSkill).filter_by(slug=CLOUD_OVERLORD_SKILL_SLUG).first()
    if row is not None:
        extra = dict(row.extra or {})
        changed = False
        if not _text(extra.get("instructions_md")):
            extra["instructions_md"] = _text(extra.get("system_prompt")) or CLOUD_OVERLORD_SKILL_PROMPT
            changed = True
        if not extra.get("runtime"):
            extra["runtime"] = {"cloud_enabled": True, "progressive_disclosure": True}
            changed = True
        if changed:
            row.extra = extra
            row.updated_at = utcnow()
            session.flush()
        return row
    now = utcnow()
    row = ResearchSkill(
        slug=CLOUD_OVERLORD_SKILL_SLUG,
        name="Thoth Cloud Analyst Overlord",
        status="active",
        owner="operator",
        description="Autonomous goal-driven lead analyst that orchestrates model-selected cloud research agents.",
        universe_json={"scope": "terminal_wide"},
        inputs_json={"required": ["goal"], "optional": ["model_roles", "permission_profile", "subagent_prompts"]},
        trigger_json={"commands": ["/goal"], "implicit": False},
        scoring_json={"quality_gate": "independent_challenge", "default_state": "minimum_viable"},
        output_json={"durable_destinations": ["report", "knowledge_asset", "dataset", "supply_chain", "product_stage", "instrument_request"]},
        extra={
            "system_prompt": CLOUD_OVERLORD_SKILL_PROMPT,
            "instructions_md": CLOUD_OVERLORD_SKILL_PROMPT,
            "runtime": {"cloud_enabled": True, "progressive_disclosure": True},
            "subagent_prompts": {},
            "best_effort_first": True,
            "self_edit_goal": True,
        },
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def serialize_cloud_overlord_skill(row: ResearchSkill) -> dict[str, Any]:
    extra = dict(row.extra or {})
    return {
        "id": int(row.id),
        "slug": row.slug,
        "name": row.name,
        "status": row.status,
        "description": row.description,
        "system_prompt": _text(extra.get("system_prompt")) or CLOUD_OVERLORD_SKILL_PROMPT,
        "subagent_prompts": dict(extra.get("subagent_prompts") or {}),
        "best_effort_first": bool(extra.get("best_effort_first", True)),
        "self_edit_goal": bool(extra.get("self_edit_goal", True)),
        "updated_at": row.updated_at.isoformat(),
    }


def update_cloud_overlord_skill(
    session: Session,
    *,
    system_prompt: str | None = None,
    subagent_prompts: dict[str, str] | None = None,
) -> dict[str, Any]:
    row = ensure_cloud_overlord_skill(session)
    extra = dict(row.extra or {})
    if system_prompt is not None:
        prompt = _text(system_prompt)
        if not prompt:
            raise ValueError("system_prompt cannot be empty.")
        extra["system_prompt"] = prompt
        extra["instructions_md"] = prompt
    if subagent_prompts is not None:
        extra["subagent_prompts"] = {
            _text(key): _text(value)
            for key, value in subagent_prompts.items()
            if _text(key) and _text(value)
        }
    row.extra = extra
    row.updated_at = utcnow()
    session.flush()
    return serialize_cloud_overlord_skill(row)


def _goal_state(run: CloudGoalRun) -> str:
    return _text((run.stats_json or {}).get("goal_state")) or "active"


def list_cloud_goals(session: Session, *, limit: int = 30) -> list[dict[str, Any]]:
    rows = (
        session.query(CloudGoalRun)
        .filter(CloudGoalRun.task_kind == "cloud_orchestrator")
        .order_by(CloudGoalRun.updated_at.desc(), CloudGoalRun.id.desc())
        .limit(max(1, min(int(limit or 30), 100)))
        .all()
    )
    return [
        {
            "id": int(row.id),
            "objective": row.objective,
            "goal_state": _goal_state(row),
            "run_status": row.status,
            "provider": row.provider,
            "model": row.model,
            "routed_model": row.routed_model,
            "plan_title": _text((row.final_result or {}).get("plan_title")),
            "pending_steps": max(
                0,
                len((row.final_result or {}).get("steps") or [])
                - len((row.stats_json or {}).get("executed_step_ids") or []),
            ),
            "updated_at": row.updated_at.isoformat(),
        }
        for row in rows
    ]


def _resolve_goal(session: Session, goal_id: int | None = None) -> CloudGoalRun:
    if goal_id is not None:
        row = get_cloud_goal_run(session, int(goal_id))
    else:
        row = (
            session.query(CloudGoalRun)
            .filter(CloudGoalRun.task_kind == "cloud_orchestrator")
            .order_by(CloudGoalRun.updated_at.desc(), CloudGoalRun.id.desc())
            .first()
        )
    if row is None or row.task_kind != "cloud_orchestrator":
        raise ValueError("Cloud goal not found.")
    return row


def revise_cloud_goal(session: Session, run: CloudGoalRun, *, objective: str, reason: str) -> dict[str, Any]:
    updated = _text(objective)
    if not updated:
        raise ValueError("Goal objective cannot be empty.")
    if len(updated) > 4000:
        raise ValueError("Goal objective must be at most 4,000 characters.")
    stats = dict(run.stats_json or {})
    revisions = list(stats.get("goal_revisions") or [])
    revisions.append({"from": run.objective, "to": updated, "reason": _text(reason), "at": utcnow().isoformat()})
    run.objective = updated
    stats["goal_revisions"] = revisions
    stats["goal_state"] = "active"
    run.stats_json = stats
    run.updated_at = utcnow()
    append_cloud_goal_event(session, run, event_kind="goal_revised", summary=_text(reason), content_md=updated)
    session.flush()
    return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=1000))


def _set_goal_state(session: Session, run: CloudGoalRun, state: str, *, reason: str) -> dict[str, Any]:
    stats = dict(run.stats_json or {})
    stats["goal_state"] = state
    run.stats_json = stats
    run.updated_at = utcnow()
    if state == "cleared" and run.status == CLOUD_GOAL_RUNNING:
        run.status = CLOUD_GOAL_FAILED
        run.completed_at = utcnow()
        run.error = "Goal cleared by operator."
    append_cloud_goal_event(session, run, event_kind=f"goal_{state}", summary=reason)
    session.flush()
    return serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=1000))


def _command_goal_id(tokens: list[str]) -> tuple[int | None, list[str]]:
    if tokens and tokens[0].isdigit():
        return int(tokens[0]), tokens[1:]
    return None, tokens


def execute_cloud_goal_command(
    session: Session,
    *,
    command: str,
    provider: str,
    model: str,
    flash_provider: str | None = None,
    flash_model: str | None = None,
    permission_profile: dict[str, Any] | None = None,
    skill_slugs: list[str] | None = None,
) -> dict[str, Any]:
    raw = _text(command)
    if not raw.startswith("/"):
        raw = f"/goal {raw}"
    head, _, tail = raw.partition(" ")
    cmd = head.lower()
    args = tail.strip()
    skill = ensure_cloud_overlord_skill(session)
    skill_payload = serialize_cloud_overlord_skill(skill)

    if cmd != "/goal":
        raise ValueError("Supported command is /goal.")
    if not args or args.lower() in {"list", "status", "show"}:
        return {"action": "list", "goals": list_cloud_goals(session), "skill": skill_payload}

    tokens = args.split()
    action = tokens[0].lower()
    rest = tokens[1:]
    if action in {"pause", "resume", "clear", "pursue", "edit", "replan"}:
        goal_id, rest = _command_goal_id(rest)
        run = _resolve_goal(session, goal_id)
        if action == "pause":
            return {"action": action, "run": _set_goal_state(session, run, "paused", reason="Goal paused by operator.")}
        if action == "resume":
            return {"action": action, "run": _set_goal_state(session, run, "active", reason="Goal resumed by operator.")}
        if action == "clear":
            return {"action": action, "run": _set_goal_state(session, run, "cleared", reason="Goal cleared by operator.")}
        if action in {"edit", "replan"}:
            objective = " ".join(rest).strip()
            payload = revise_cloud_goal(session, run, objective=objective, reason=f"Goal {action} requested by operator.")
            if action == "replan":
                stats = dict(run.stats_json or {})
                stats.pop("executed_step_ids", None)
                stats.pop("handoffs", None)
                run.stats_json = stats
                run.final_result = {}
                run.status = CLOUD_GOAL_RUNNING
                run.completed_at = None
                run.error = None
                session.flush()
                payload = serialize_cloud_goal_run(run, events=list_cloud_goal_events(session, int(run.id), limit=1000))
            return {"action": action, "run": payload}
        if _goal_state(run) != "active":
            raise ValueError("Goal must be active before it can be pursued.")
        if run.status == CLOUD_GOAL_RUNNING:
            continue_cloud_orchestrator_run(session, int(run.id), max_steps=1)
        profile = dict((run.stats_json or {}).get("permission_profile") or {})
        result = continue_cloud_orchestrator_handoffs(
            session,
            int(run.id),
            max_handoffs=24,
            execute_child=True,
            child_max_steps=int(profile.get("child_max_steps") or 3),
            compact_if_needed=True,
            allow_durable_writes=bool(profile.get("allow_durable_writes", False)),
            operator_approved=bool(profile.get("operator_approved", False)),
        )
        return {"action": action, "run": result}

    objective = args
    if len(objective) > 4000:
        raise ValueError("Goal objective must be at most 4,000 characters.")
    profile = dict(permission_profile or {})
    requested_skills = [CLOUD_OVERLORD_SKILL_SLUG, *(skill_slugs or [])]
    active_skills = resolve_cloud_skills(session, prompt=raw, explicit_slugs=requested_skills)
    skill_instructions = render_cloud_skill_instructions(active_skills, session=session)
    run = start_cloud_orchestrator_run(
        session,
        objective=objective,
        provider=provider,
        model=model,
        skill_prompt=skill_instructions,
        strong_provider=provider,
        strong_model=model,
        flash_provider=flash_provider or provider,
        flash_model=flash_model or model,
        permission_profile=profile,
        subagent_prompts=skill_payload["subagent_prompts"],
    )
    row = get_cloud_goal_run(session, int(run["id"]))
    if row is not None:
        stats = dict(row.stats_json or {})
        stats["active_skills"] = cloud_skill_snapshot(active_skills)
        row.stats_json = stats
        session.flush()
        run = serialize_cloud_goal_run(row, events=list_cloud_goal_events(session, int(row.id), limit=1000))
    return {"action": "create", "run": run, "skill": skill_payload}
