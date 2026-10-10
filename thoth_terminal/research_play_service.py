from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func
from sqlalchemy.orm import Session, joinedload

from thoth_terminal.db import utcnow
from thoth_terminal.models import (
    AlignmentFeedback,
    Document,
    Domain,
    ResearchPlay,
    ResearchPlayRun,
    ResearchPlaySignal,
    ResearchPlaySkill,
    ResearchSkill,
)
from thoth_terminal.query import search_document_ids


def _text(value: object | None) -> str | None:
    raw = str(value or "").strip()
    return raw or None


def _coerce_int(value: object | None, *, default: int, min_v: int = 0, max_v: int = 10_000) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except Exception:
        parsed = int(default)
    return max(min_v, min(max_v, parsed))


def _coerce_str_list(value: object | None) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _coerce_int_list(value: object | None) -> list[int]:
    out: list[int] = []
    for item in _coerce_str_list(value) if isinstance(value, str) else (value if isinstance(value, list) else []):
        try:
            out.append(int(item))  # type: ignore[arg-type]
        except Exception:
            continue
    return out


def _merge_dicts(*parts: object) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for part in parts:
        if isinstance(part, dict):
            out.update(part)
    return out


def _effective_doc_ts() -> Any:
    now = utcnow()
    return case(
        (
            (Document.published_at.isnot(None)) & (Document.published_at > now),
            func.coalesce(Document.ingested_at, now),
        ),
        else_=func.coalesce(Document.published_at, Document.ingested_at),
    )


def serialize_research_skill(skill: ResearchSkill) -> dict[str, Any]:
    return {
        "id": int(skill.id),
        "slug": str(skill.slug),
        "name": str(skill.name),
        "status": str(skill.status),
        "owner": skill.owner,
        "description": skill.description,
        "universe_json": dict(skill.universe_json or {}),
        "inputs_json": dict(skill.inputs_json or {}),
        "trigger_json": dict(skill.trigger_json or {}),
        "scoring_json": dict(skill.scoring_json or {}),
        "output_json": dict(skill.output_json or {}),
        "extra": dict(skill.extra or {}),
        "created_at": skill.created_at.isoformat() if skill.created_at else None,
        "updated_at": skill.updated_at.isoformat() if skill.updated_at else None,
    }


def serialize_research_play_skill(link: ResearchPlaySkill) -> dict[str, Any]:
    skill = link.skill
    return {
        "id": int(link.id),
        "play_id": int(link.play_id),
        "skill_id": int(link.skill_id),
        "position": int(link.position or 0),
        "required": bool(link.required),
        "config_json": dict(link.config_json or {}),
        "created_at": link.created_at.isoformat() if link.created_at else None,
        "updated_at": link.updated_at.isoformat() if link.updated_at else None,
        "skill": serialize_research_skill(skill) if skill is not None else None,
    }


def serialize_research_play_run(run: ResearchPlayRun) -> dict[str, Any]:
    return {
        "id": int(run.id),
        "play_id": int(run.play_id),
        "trigger_job_id": int(run.trigger_job_id) if run.trigger_job_id is not None else None,
        "trigger_kind": str(run.trigger_kind),
        "status": str(run.status),
        "summary": run.summary,
        "metrics_json": dict(run.metrics_json or {}),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def serialize_research_play_signal(signal: ResearchPlaySignal) -> dict[str, Any]:
    return {
        "id": int(signal.id),
        "play_id": int(signal.play_id),
        "play_run_id": int(signal.play_run_id) if signal.play_run_id is not None else None,
        "skill_id": int(signal.skill_id) if signal.skill_id is not None else None,
        "subject_id": int(signal.subject_id) if signal.subject_id is not None else None,
        "signal_key": str(signal.signal_key),
        "signal_kind": str(signal.signal_kind),
        "score": float(signal.score) if signal.score is not None else None,
        "delta_score": float(signal.delta_score) if signal.delta_score is not None else None,
        "baseline_json": dict(signal.baseline_json or {}),
        "observed_json": dict(signal.observed_json or {}),
        "summary": signal.summary,
        "created_at": signal.created_at.isoformat() if signal.created_at else None,
    }


def serialize_alignment_feedback(row: AlignmentFeedback) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "target_type": str(row.target_type),
        "target_id": int(row.target_id) if row.target_id is not None else None,
        "feedback_kind": str(row.feedback_kind),
        "actor": row.actor,
        "note": row.note,
        "payload": dict(row.payload or {}),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def serialize_research_play(
    play: ResearchPlay,
    *,
    include_skills: bool = True,
    latest_run: ResearchPlayRun | None = None,
) -> dict[str, Any]:
    skills = sorted(play.skills or [], key=lambda row: (int(row.position or 0), int(row.id or 0)))
    return {
        "id": int(play.id),
        "slug": str(play.slug),
        "name": str(play.name),
        "status": str(play.status),
        "owner": play.owner,
        "description": play.description,
        "universe_json": dict(play.universe_json or {}),
        "schedule_json": dict(play.schedule_json or {}),
        "alert_json": dict(play.alert_json or {}),
        "extra": dict(play.extra or {}),
        "skill_count": len(skills),
        "skills": [serialize_research_play_skill(link) for link in skills] if include_skills else [],
        "latest_run": serialize_research_play_run(latest_run) if latest_run is not None else None,
        "created_at": play.created_at.isoformat() if play.created_at else None,
        "updated_at": play.updated_at.isoformat() if play.updated_at else None,
    }


def list_research_skills(
    session: Session,
    *,
    query: str | None = None,
    owner: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[ResearchSkill]:
    q = session.query(ResearchSkill).order_by(ResearchSkill.updated_at.desc(), ResearchSkill.id.desc())
    if status:
        q = q.filter(ResearchSkill.status == str(status).strip())
    if owner:
        q = q.filter(ResearchSkill.owner.ilike(f"%{str(owner).strip()}%"))
    if query:
        like = f"%{str(query).strip()}%"
        q = q.filter(
            (ResearchSkill.slug.ilike(like))
            | (ResearchSkill.name.ilike(like))
            | (ResearchSkill.description.ilike(like))
        )
    return q.limit(max(1, min(int(limit or 100), 200))).all()


def list_research_plays(
    session: Session,
    *,
    query: str | None = None,
    owner: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[ResearchPlay]:
    q = (
        session.query(ResearchPlay)
        .options(joinedload(ResearchPlay.skills).joinedload(ResearchPlaySkill.skill))
        .order_by(ResearchPlay.updated_at.desc(), ResearchPlay.id.desc())
    )
    if status:
        q = q.filter(ResearchPlay.status == str(status).strip())
    if owner:
        q = q.filter(ResearchPlay.owner.ilike(f"%{str(owner).strip()}%"))
    if query:
        like = f"%{str(query).strip()}%"
        q = q.filter(
            (ResearchPlay.slug.ilike(like))
            | (ResearchPlay.name.ilike(like))
            | (ResearchPlay.description.ilike(like))
        )
    return q.limit(max(1, min(int(limit or 100), 200))).all()


def get_research_skill(session: Session, identifier: int | str) -> ResearchSkill | None:
    if isinstance(identifier, int) or str(identifier).isdigit():
        return session.query(ResearchSkill).filter_by(id=int(identifier)).first()
    return session.query(ResearchSkill).filter_by(slug=str(identifier).strip()).first()


def get_research_play(session: Session, identifier: int | str) -> ResearchPlay | None:
    q = session.query(ResearchPlay).options(joinedload(ResearchPlay.skills).joinedload(ResearchPlaySkill.skill))
    if isinstance(identifier, int) or str(identifier).isdigit():
        return q.filter_by(id=int(identifier)).first()
    return q.filter_by(slug=str(identifier).strip()).first()


def list_recent_play_runs(session: Session, *, play_id: int, limit: int = 20) -> list[ResearchPlayRun]:
    return (
        session.query(ResearchPlayRun)
        .filter(ResearchPlayRun.play_id == int(play_id))
        .order_by(ResearchPlayRun.started_at.desc(), ResearchPlayRun.id.desc())
        .limit(max(1, min(int(limit or 20), 100)))
        .all()
    )


def list_recent_play_signals(
    session: Session,
    *,
    play_id: int,
    play_run_id: int | None = None,
    limit: int = 50,
) -> list[ResearchPlaySignal]:
    q = (
        session.query(ResearchPlaySignal)
        .options(joinedload(ResearchPlaySignal.skill))
        .filter(ResearchPlaySignal.play_id == int(play_id))
    )
    if play_run_id is not None:
        q = q.filter(ResearchPlaySignal.play_run_id == int(play_run_id))
    return (
        q.order_by(ResearchPlaySignal.created_at.desc(), ResearchPlaySignal.id.desc())
        .limit(max(1, min(int(limit or 50), 200)))
        .all()
    )


def create_research_skill(
    session: Session,
    *,
    slug: str,
    name: str,
    owner: str | None = None,
    description: str | None = None,
    universe_json: dict[str, Any] | None = None,
    inputs_json: dict[str, Any] | None = None,
    trigger_json: dict[str, Any] | None = None,
    scoring_json: dict[str, Any] | None = None,
    output_json: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> ResearchSkill:
    now = utcnow()
    row = session.query(ResearchSkill).filter_by(slug=str(slug)).first()
    if row is None:
        row = ResearchSkill(
            slug=str(slug),
            name=str(name),
            status="active",
            owner=owner,
            description=description,
            universe_json=universe_json or {},
            inputs_json=inputs_json or {},
            trigger_json=trigger_json or {},
            scoring_json=scoring_json or {},
            output_json=output_json or {},
            extra=extra or {},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.name = str(name)
        row.owner = owner
        row.description = description
        row.universe_json = universe_json or {}
        row.inputs_json = inputs_json or {}
        row.trigger_json = trigger_json or {}
        row.scoring_json = scoring_json or {}
        row.output_json = output_json or {}
        row.extra = extra or {}
        row.updated_at = now
    session.flush()
    return row


def create_research_play(
    session: Session,
    *,
    slug: str,
    name: str,
    owner: str | None = None,
    description: str | None = None,
    universe_json: dict[str, Any] | None = None,
    schedule_json: dict[str, Any] | None = None,
    alert_json: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> ResearchPlay:
    now = utcnow()
    row = session.query(ResearchPlay).filter_by(slug=str(slug)).first()
    if row is None:
        row = ResearchPlay(
            slug=str(slug),
            name=str(name),
            status="active",
            owner=owner,
            description=description,
            universe_json=universe_json or {},
            schedule_json=schedule_json or {},
            alert_json=alert_json or {},
            extra=extra or {},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.name = str(name)
        row.owner = owner
        row.description = description
        row.universe_json = universe_json or {}
        row.schedule_json = schedule_json or {}
        row.alert_json = alert_json or {}
        row.extra = extra or {}
        row.updated_at = now
    session.flush()
    return row


def bind_skill_to_play(
    session: Session,
    *,
    play_id: int,
    skill_id: int,
    position: int = 0,
    required: bool = True,
    config_json: dict[str, Any] | None = None,
) -> ResearchPlaySkill:
    now = utcnow()
    row = (
        session.query(ResearchPlaySkill)
        .filter_by(play_id=int(play_id), skill_id=int(skill_id))
        .first()
    )
    if row is None:
        row = ResearchPlaySkill(
            play_id=int(play_id),
            skill_id=int(skill_id),
            position=int(position),
            required=bool(required),
            config_json=config_json or {},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.position = int(position)
        row.required = bool(required)
        row.config_json = config_json or {}
        row.updated_at = now
    session.flush()
    return row


def start_research_play_run(
    session: Session,
    *,
    play_id: int,
    trigger_kind: str = "manual",
    trigger_job_id: int | None = None,
    metrics_json: dict[str, Any] | None = None,
) -> ResearchPlayRun:
    row = ResearchPlayRun(
        play_id=int(play_id),
        trigger_job_id=int(trigger_job_id) if trigger_job_id is not None else None,
        trigger_kind=str(trigger_kind or "manual"),
        status="running",
        summary=None,
        metrics_json=metrics_json or {},
        started_at=utcnow(),
        finished_at=None,
    )
    session.add(row)
    session.flush()
    return row


def finish_research_play_run(
    session: Session,
    *,
    play_run_id: int,
    status: str,
    summary: str | None = None,
    metrics_json: dict[str, Any] | None = None,
) -> ResearchPlayRun | None:
    row = session.query(ResearchPlayRun).filter_by(id=int(play_run_id)).first()
    if row is None:
        return None
    row.status = str(status)
    row.summary = summary
    if metrics_json is not None:
        row.metrics_json = metrics_json
    row.finished_at = utcnow()
    session.flush()
    return row


def record_research_play_signal(
    session: Session,
    *,
    play_id: int,
    signal_key: str,
    signal_kind: str,
    play_run_id: int | None = None,
    skill_id: int | None = None,
    subject_id: int | None = None,
    score: float | None = None,
    delta_score: float | None = None,
    baseline_json: dict[str, Any] | None = None,
    observed_json: dict[str, Any] | None = None,
    summary: str | None = None,
) -> ResearchPlaySignal:
    row = ResearchPlaySignal(
        play_id=int(play_id),
        play_run_id=int(play_run_id) if play_run_id is not None else None,
        skill_id=int(skill_id) if skill_id is not None else None,
        subject_id=int(subject_id) if subject_id is not None else None,
        signal_key=str(signal_key),
        signal_kind=str(signal_kind),
        score=score,
        delta_score=delta_score,
        baseline_json=baseline_json or {},
        observed_json=observed_json or {},
        summary=summary,
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def record_alignment_feedback(
    session: Session,
    *,
    target_type: str,
    target_id: int | None,
    feedback_kind: str,
    actor: str | None = None,
    note: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AlignmentFeedback:
    row = AlignmentFeedback(
        target_type=str(target_type),
        target_id=int(target_id) if target_id is not None else None,
        feedback_kind=str(feedback_kind),
        actor=actor,
        note=note,
        payload=payload or {},
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def _signal_summary_lines(signals: list[ResearchPlaySignal]) -> list[str]:
    lines: list[str] = []
    for signal in signals[:8]:
        observed = signal.observed_json if isinstance(signal.observed_json, dict) else {}
        skill_slug = None
        if signal.skill is not None:
            skill_slug = str(signal.skill.slug or "").strip() or None
        subject = str(observed.get("title") or observed.get("url") or signal.signal_key).strip()
        prefix = f"[{skill_slug}]" if skill_slug else "[signal]"
        lines.append(f"{prefix} {subject}")
    return lines


def _maybe_send_play_alert(play: ResearchPlay, run: ResearchPlayRun, signals: list[ResearchPlaySignal]) -> bool:
    if not signals:
        return False
    alert_cfg = play.alert_json if isinstance(play.alert_json, dict) else {}
    guild_id = alert_cfg.get("guild_id")
    channel_id = alert_cfg.get("channel_id")
    if not str(guild_id or "").isdigit() or not str(channel_id or "").isdigit():
        return False
    from thoth_terminal.discord.outbox import create_outbox_item

    thread_id = int(alert_cfg["thread_id"]) if str(alert_cfg.get("thread_id") or "").isdigit() else None
    header = f"{play.name}: {len(signals)} new signal(s)"
    body = "\n".join(_signal_summary_lines(signals))
    content = header if not body else f"{header}\n{body}"
    create_outbox_item(
        guild_id=int(guild_id),
        channel_id=int(channel_id),
        thread_id=thread_id,
        content=content[:1800],
        dedupe_key=f"research_play:{int(play.id)}:run:{int(run.id)}",
        created_by_kind="system",
    )
    return True


def execute_research_play(
    session: Session,
    *,
    play_id: int,
    trigger_kind: str = "manual",
    trigger_job_id: int | None = None,
) -> dict[str, Any]:
    play = get_research_play(session, int(play_id))
    if play is None:
        raise ValueError("Research play not found.")

    previous_run = (
        session.query(ResearchPlayRun)
        .filter(ResearchPlayRun.play_id == int(play_id))
        .filter(ResearchPlayRun.finished_at.isnot(None))
        .order_by(ResearchPlayRun.finished_at.desc(), ResearchPlayRun.id.desc())
        .first()
    )
    previous_finished_at = previous_run.finished_at if previous_run is not None else None
    run = start_research_play_run(
        session,
        play_id=int(play.id),
        trigger_kind=str(trigger_kind or "manual"),
        trigger_job_id=(int(trigger_job_id) if trigger_job_id is not None else None),
        metrics_json={},
    )

    active_links = [
        link
        for link in sorted(play.skills or [], key=lambda row: (int(row.position or 0), int(row.id or 0)))
        if link.skill is not None and str(link.skill.status or "active").lower() == "active"
    ]
    created_signals: list[ResearchPlaySignal] = []
    docs_considered = 0
    now = utcnow()
    doc_ts = _effective_doc_ts()

    for link in active_links:
        skill = link.skill
        assert skill is not None
        merged_universe = _merge_dicts(play.universe_json, skill.universe_json, link.config_json)
        merged_inputs = _merge_dicts(skill.inputs_json, link.config_json)
        merged_trigger = _merge_dicts(play.schedule_json, skill.trigger_json, link.config_json)
        merged_output = _merge_dicts(skill.output_json, link.config_json)

        query_text = _text(merged_inputs.get("query")) or _text(merged_universe.get("query"))
        source_ids = _coerce_int_list(merged_universe.get("source_ids"))
        domains = _coerce_str_list(merged_universe.get("domains"))
        language = _text(merged_universe.get("language"))
        lookback_hours = _coerce_int(
            merged_trigger.get("lookback_hours"),
            default=_coerce_int((play.schedule_json or {}).get("lookback_hours"), default=24, min_v=1, max_v=24 * 30),
            min_v=1,
            max_v=24 * 30,
        )
        max_docs = _coerce_int(merged_output.get("max_docs"), default=8, min_v=1, max_v=50)
        signal_kind = _text(merged_trigger.get("signal_kind")) or "document_match"
        window_start = now - timedelta(hours=lookback_hours)
        if previous_finished_at is not None and previous_finished_at > window_start:
            window_start = previous_finished_at

        q = session.query(Document).options(joinedload(Document.source)).filter(doc_ts >= window_start)
        if source_ids:
            q = q.filter(Document.source_id.in_(source_ids))
        if language:
            q = q.filter(Document.language == language)
        if domains:
            q = q.join(Document.domains).filter(Domain.slug.in_(domains)).distinct()
        if query_text:
            ids = search_document_ids(session, query_text, limit=5000)
            if ids:
                q = q.filter(Document.id.in_(ids))
            else:
                like = f"%{query_text}%"
                q = q.filter(
                    (Document.title.ilike(like))
                    | (Document.normalized_text.ilike(like))
                    | (Document.translation_en.ilike(like))
                )

        docs = q.order_by(doc_ts.desc(), Document.id.desc()).limit(max_docs).all()
        docs_considered += len(docs)
        for doc in docs:
            signal_key = f"{skill.slug}:{int(doc.id)}"
            exists = (
                session.query(ResearchPlaySignal.id)
                .filter(ResearchPlaySignal.play_id == int(play.id))
                .filter(ResearchPlaySignal.signal_key == signal_key)
                .first()
            )
            if exists:
                continue
            observed_json = {
                "document_id": int(doc.id),
                "title": _text(doc.title) or _text(doc.url) or f"doc {int(doc.id)}",
                "url": _text(doc.url) or _text(doc.canonical_url),
                "source": _text(doc.source.name) if doc.source is not None else None,
                "published_at": doc.published_at.isoformat() if doc.published_at else None,
                "ingested_at": doc.ingested_at.isoformat() if doc.ingested_at else None,
            }
            baseline_json = {
                "previous_run_finished_at": previous_finished_at.isoformat() if previous_finished_at else None,
                "window_start": window_start.isoformat(),
                "lookback_hours": int(lookback_hours),
                "query": query_text,
                "domains": domains,
                "source_ids": source_ids,
            }
            signal = record_research_play_signal(
                session,
                play_id=int(play.id),
                play_run_id=int(run.id),
                skill_id=int(skill.id),
                signal_key=signal_key,
                signal_kind=signal_kind,
                score=float(doc.importance or 0),
                delta_score=None,
                baseline_json=baseline_json,
                observed_json=observed_json,
                summary=_text(doc.title) or _text(doc.url) or f"Document {int(doc.id)}",
            )
            session.flush()
            signal.skill = skill
            created_signals.append(signal)

    metrics_json = {
        "skills_total": len(active_links),
        "signals_new": len(created_signals),
        "documents_considered": docs_considered,
        "previous_run_finished_at": previous_finished_at.isoformat() if previous_finished_at else None,
    }
    summary = (
        f"{len(created_signals)} new signal(s) across {len(active_links)} skill(s)."
        if created_signals
        else f"No new signals across {len(active_links)} skill(s)."
    )
    finished = finish_research_play_run(
        session,
        play_run_id=int(run.id),
        status="success",
        summary=summary,
        metrics_json=metrics_json,
    )
    session.flush()
    if finished is not None:
        run = finished
    alerted = _maybe_send_play_alert(play, run, created_signals)
    return {
        "play": serialize_research_play(play, include_skills=True, latest_run=run),
        "run": serialize_research_play_run(run),
        "signals": [serialize_research_play_signal(signal) for signal in created_signals],
        "alerted": bool(alerted),
    }
