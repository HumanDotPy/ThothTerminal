from __future__ import annotations

import re
import math
from collections import defaultdict
from datetime import timedelta
from typing import Any

from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session, joinedload, load_only

from thoth_terminal.config import settings
from thoth_terminal.integrations.ibkr.gateway import gateway_browser_login_url
from thoth_terminal.db import utcnow
from thoth_terminal.integrations.ibkr.routes import ibkr_service
from thoth_terminal.models import (
    AnalystEvidence,
    Dataset,
    Document,
    IntelligenceEvent,
    InstrumentRequest,
    LocalAgentRun,
    Note,
    PortfolioMoodSnapshot,
    ResearchInstrument,
    ResearchReport,
    ResearchUniverse,
    ResearchUniverseRow,
    ResearchUniverseSection,
    Source,
    SourceState,
    Thesis,
)
from thoth_terminal.universe_service import serialize_universe


def build_home_intelligence(session: Session, *, user_id: str = "local", live_ibkr: bool = False) -> dict[str, Any]:
    now = utcnow()
    since = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    document_counts = _document_count_snapshot(session, since=since)
    counts = {
        "new_documents": document_counts["new_documents"],
        "opened": document_counts["opened"],
        "saved": document_counts["saved"],
        "actioned": document_counts["actioned"],
        "recent_documents": document_counts["recent_documents"],
    }
    recent_events = _recent_intelligence_events(session, limit=8)
    top_changes = recent_events or _recent_document_events(session, limit=5)
    datasets = _dataset_workbench(session, since=week_ago)
    family_lanes = _family_lanes(session)
    notebook = _notebook(session)
    signal_board = _signal_board(session)
    system_status = _system_status(session, since=since, document_counts=document_counts)
    ibkr_status = ibkr_service().status(live=live_ibkr)
    portfolio_mood = _portfolio_mood(session, allow_live_refresh=bool(live_ibkr and ibkr_status.get("connected")))
    portfolio_state = _portfolio_state(portfolio_mood)
    market_watchlist = build_market_watchlist(
        session,
        allow_live_refresh=bool(live_ibkr and ibkr_status.get("connected")),
        portfolio_mood=portfolio_mood,
    )
    ibkr_label = "disabled"
    if ibkr_status.get("connected"):
        ibkr_label = "connected"
    elif ibkr_status.get("auth_required"):
        ibkr_label = "auth_required"
    elif ibkr_status.get("enabled"):
        ibkr_label = "gateway_check"
    system_status["ibkr_connection_status"] = ibkr_label
    system_status["ibkr"] = {
        "connected": bool(ibkr_status.get("connected")),
        "auth_required": bool(ibkr_status.get("auth_required")),
        "enabled": bool(ibkr_status.get("enabled")),
        "iteration_connected": bool(ibkr_status.get("iteration_connected")),
        "gateway_present": bool(ibkr_status.get("gateway_present")),
        "connection_status": ibkr_label,
        "login_url": "/ibkr/auth",
        "gateway_login_url": gateway_browser_login_url(),
        "reason_badges": ["IBKR disconnected/auth required"]
        if ibkr_status.get("auth_required")
        else ["IBKR disabled" if not ibkr_status.get("enabled") else "portfolio link healthy"],
    }
    command_center = _research_command_center(session, recent_events=recent_events, now=now)

    return {
        "generated_at": now.isoformat(),
        "user_id": user_id,
        "hero": {
            "title": "Session briefing",
            "material_updates_count": len(top_changes),
            "dataset_changes": len(datasets),
            "thesis_linked_signals": sum(1 for item in top_changes if "touches active thesis" in item["reason_badges"]),
            "portfolio_mood": portfolio_mood,
            "degraded_sources": system_status["degraded_sources_count"],
            "stale_notes": notebook["stale_count"],
            "top_priority_changes": top_changes[:3],
            "reason_badges": _hero_badges(counts, system_status, portfolio_mood),
        },
        "dataset_workbench": {"items": datasets, "reason": "changed, stale, or thesis-linked datasets"},
        "family_lanes": {"items": family_lanes},
        "notebook": notebook,
        "signal_board": {"clusters": signal_board},
        "system_status": system_status,
        "link_status": _link_status(session, ibkr_status=ibkr_status),
        "portfolio_mood": portfolio_mood,
        "portfolio_state": portfolio_state,
        "market_watchlist": market_watchlist,
        "session_digest": {
            "counts": counts,
            "items": top_changes[:6],
            "next_routes": _next_routes(top_changes, datasets, notebook, system_status),
        },
        "command_center": command_center,
    }


def _research_command_center(session: Session, *, recent_events: list[dict[str, Any]], now) -> dict[str, Any]:
    attention = [
        row
        for row in recent_events
        if row.get("event_type") not in {"filing_ingested", "filing"}
        and not re.search(r"\b(?:10-[QK]|8-K|6-K|20-F)\b", str(row.get("title") or ""), flags=re.IGNORECASE)
    ][:6]
    requests = (
        session.query(InstrumentRequest)
        .filter(~func.lower(InstrumentRequest.status).in_(["completed", "fulfilled", "closed", "cancelled", "rejected", "archived"]))
        .order_by(InstrumentRequest.priority.desc(), InstrumentRequest.updated_at.desc(), InstrumentRequest.id.desc())
        .limit(8)
        .all()
    )
    gaps = [
        {
            "id": int(row.id),
            "title": row.title,
            "summary": row.summary or "",
            "priority": int(row.priority or 0),
            "status": row.status,
            "href": f"/instruments/requests/{int(row.id)}",
            "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        }
        for row in requests
    ]
    if len(attention) < 6:
        attention.extend(
            {
                "id": f"request:{row['id']}",
                "event_type": "research_gap",
                "title": row["title"],
                "summary": row["summary"],
                "href": row["href"],
                "importance": min(1.0, float(row["priority"]) / 10.0),
                "event_time": row["updated_at"],
                "reason_badges": ["open research gap", f"priority {row['priority']}"],
            }
            for row in gaps[: 6 - len(attention)]
        )

    evidence_rows = (
        session.query(AnalystEvidence)
        .options(
            joinedload(AnalystEvidence.company),
            joinedload(AnalystEvidence.issuer),
            joinedload(AnalystEvidence.subject),
            joinedload(AnalystEvidence.underlying_source_actor),
        )
        .filter(AnalystEvidence.status != "archived")
        .filter(or_(AnalystEvidence.estimate_value.isnot(None), AnalystEvidence.price_target.isnot(None)))
        .filter(or_(AnalystEvidence.company_id.isnot(None), AnalystEvidence.issuer_id.isnot(None), AnalystEvidence.subject_id.isnot(None)))
        .order_by(AnalystEvidence.published_at.desc().nullslast(), AnalystEvidence.updated_at.desc(), AnalystEvidence.id.desc())
        .limit(10)
        .all()
    )
    revisions = []
    for row in evidence_rows:
        forecast = (row.payload or {}).get("forecast") if isinstance(row.payload, dict) else {}
        forecast = forecast if isinstance(forecast, dict) else {}
        subject = row.company.name if row.company is not None else row.issuer.display_name if row.issuer is not None else row.subject.display_name if row.subject is not None else "Unscoped"
        owner = row.underlying_source_actor.display_name if row.underlying_source_actor is not None else str((row.payload or {}).get("estimate_owner") or row.source_name or row.outlet or "Unknown")
        value = forecast.get("value") if forecast.get("value") is not None else row.estimate_value if row.estimate_value is not None else row.price_target
        unit = str(forecast.get("currency") or row.currency or forecast.get("unit") or row.estimate_unit or "")
        revisions.append(
            {
                "id": int(row.id),
                "subject": subject,
                "owner": owner,
                "metric": str(forecast.get("metric_key") or row.estimate_metric or "price_target"),
                "period": str(forecast.get("period") or row.estimate_period or row.target_horizon or "current"),
                "value": value,
                "unit": unit,
                "summary": row.summary or "",
                "published_at": (row.published_at or row.updated_at).isoformat() if (row.published_at or row.updated_at) else "",
                "href": f"/consensus?subject={subject}",
            }
        )

    runs = session.query(LocalAgentRun).order_by(LocalAgentRun.started_at.desc(), LocalAgentRun.id.desc()).limit(6).all()
    agent_runs = []
    for row in runs:
        stats = dict(row.stats_json or {})
        telemetry = stats.get("telemetry") if isinstance(stats.get("telemetry"), dict) else {}
        tools = telemetry.get("tools") if isinstance(telemetry.get("tools"), dict) else {}
        model = telemetry.get("model") if isinstance(telemetry.get("model"), dict) else {}
        agent_runs.append(
            {
                "id": int(row.id),
                "run_type": row.run_type,
                "status": row.status,
                "model": row.model_name or row.model_profile or "",
                "summary": row.summary or row.error or "",
                "started_at": row.started_at.isoformat() if row.started_at else "",
                "duration_ms": (telemetry.get("run") or {}).get("duration_ms") if isinstance(telemetry.get("run"), dict) else None,
                "tool_calls": tools.get("calls"),
                "tool_error_rate": tools.get("error_rate"),
                "generation_tps": model.get("generation_tokens_per_second"),
                "href": "/agents",
            }
        )

    reports = session.query(ResearchReport).filter(ResearchReport.status != "archived").order_by(ResearchReport.updated_at.desc(), ResearchReport.id.desc()).limit(5).all()
    report_rows = [
        {"id": int(row.id), "title": row.title, "status": row.status, "updated_at": row.updated_at.isoformat() if row.updated_at else "", "href": f"/reports/{int(row.id)}"}
        for row in reports
    ]
    theses = session.query(Thesis).filter(~func.lower(Thesis.status).in_(["archived", "rejected", "duplicate"])).order_by(Thesis.updated_at.desc(), Thesis.importance.desc(), Thesis.id.desc()).limit(5).all()
    thesis_rows = [
        {"id": int(row.id), "title": row.title, "status": row.status, "importance": int(row.importance or 0), "updated_at": row.updated_at.isoformat() if row.updated_at else "", "href": f"/theses/{int(row.id)}"}
        for row in theses
    ]
    active_runs = sum(1 for row in agent_runs if row["status"] in {"running", "queued"})
    failed_runs = sum(1 for row in agent_runs if row["status"] == "error")
    return {
        "generated_at": now.isoformat(),
        "attention": attention,
        "model_revisions": revisions,
        "agent_runs": agent_runs,
        "research_gaps": gaps,
        "reports": report_rows,
        "theses": thesis_rows,
        "metrics": {
            "attention": len(attention),
            "open_gaps": len(gaps),
            "model_revisions": len(revisions),
            "active_runs": active_runs,
            "failed_recent_runs": failed_runs,
        },
    }


def _recent_intelligence_events(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = (
        session.query(IntelligenceEvent)
        .order_by(
            IntelligenceEvent.importance_score.desc(),
            IntelligenceEvent.event_time.desc(),
            IntelligenceEvent.id.desc(),
        )
        .limit(limit)
        .all()
    )
    return [
        {
            "id": f"event:{row.id}",
            "event_type": row.event_type,
            "title": row.title,
            "summary": row.summary or "",
            "href": _event_href(row),
            "importance": row.importance_score,
            "novelty": row.novelty_score,
            "confidence": row.confidence_score,
            "event_time": row.event_time.isoformat() if row.event_time else None,
            "reason_badges": _event_reason_badges(row),
        }
        for row in rows
    ]


def _document_count_snapshot(session: Session, *, since) -> dict[str, int]:
    row = (
        session.query(
            func.sum(case((Document.triage_state == "NEW", 1), else_=0)).label("new_documents"),
            func.sum(case((Document.triage_state == "OPENED", 1), else_=0)).label("opened"),
            func.sum(case((Document.saved.is_(True), 1), else_=0)).label("saved"),
            func.sum(case((Document.triage_state == "ACTIONED", 1), else_=0)).label("actioned"),
            func.sum(case((Document.ingested_at >= since, 1), else_=0)).label("recent_documents"),
            func.sum(
                case(
                    ((Document.ingested_at >= since) & (Document.triage_state == "IGNORED"), 1),
                    else_=0,
                )
            ).label("failed_ingestions"),
            func.sum(
                case(
                    ((Document.triage_state == "NEW") & (Document.importance >= 3), 1),
                    else_=0,
                )
            ).label("unreviewed_high_importance_signals"),
        ).one()
    )
    return {
        key: int(getattr(row, key, 0) or 0)
        for key in (
            "new_documents",
            "opened",
            "saved",
            "actioned",
            "recent_documents",
            "failed_ingestions",
            "unreviewed_high_importance_signals",
        )
    }


def _recent_document_events(session: Session, *, limit: int) -> list[dict[str, Any]]:
    rows = (
        session.query(
            Document.id.label("document_id"),
            Document.title.label("document_title"),
            Document.url.label("document_url"),
            Document.triage_state.label("triage_state"),
            Document.importance.label("importance"),
            Document.ingested_at.label("ingested_at"),
            Source.name.label("source_name"),
        )
        .join(Source, Source.id == Document.source_id)
        .order_by(Document.importance.desc(), Document.ingested_at.desc(), Document.id.desc())
        .limit(limit)
        .all()
    )
    out = []
    for row in rows:
        badges = ["new since last session"] if row.triage_state == "NEW" else ["unreviewed"]
        importance = int(row.importance or 1)
        if importance >= 3:
            badges.append("stale but important" if row.ingested_at < utcnow() - timedelta(days=7) else "important")
        out.append(
            {
                "id": f"doc:{row.document_id}",
                "event_type": "news_ingested",
                "title": row.document_title or row.document_url or f"Document {row.document_id}",
                "summary": "",
                "href": f"/docs/{row.document_id}",
                "source": row.source_name,
                "importance": float(importance),
                "novelty": 0.5 if row.triage_state == "NEW" else 0.2,
                "confidence": 0.8,
                "event_time": row.ingested_at.isoformat() if row.ingested_at else None,
                "reason_badges": badges,
            }
        )
    return out


def _dataset_workbench(session: Session, *, since) -> list[dict[str, Any]]:
    rows = (
        session.query(Dataset)
        .options(
            load_only(
                Dataset.id,
                Dataset.slug,
                Dataset.title,
                Dataset.category,
                Dataset.family,
                Dataset.status,
                Dataset.source_kind,
                Dataset.updated_at,
                Dataset.extra,
            )
        )
        .filter(Dataset.status != "archived")
        .order_by(Dataset.updated_at.desc(), Dataset.id.desc())
        .limit(32)
        .all()
    )
    out = []
    for row in _dedupe_dataset_workbench_rows(rows, limit=8):
        badges = []
        if row.updated_at and row.updated_at >= since:
            badges.append("dataset changed")
        if row.family:
            badges.append(f"family: {row.family}")
        if not badges:
            badges.append("stale but important")
        out.append(
            {
                "id": row.id,
                "title": row.title,
                "family": row.family,
                "category": row.category,
                "href": f"/visualizations/datasets/{row.id}",
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "importance_score": 0.65 + (0.2 if "dataset changed" in badges else 0),
                "reason_badges": badges[:3],
            }
        )
    return out


def _dedupe_dataset_workbench_rows(rows: list[Dataset], *, limit: int = 8) -> list[Dataset]:
    out: list[Dataset] = []
    seen: set[tuple[str, ...]] = set()
    for row in rows:
        keys = _dataset_workbench_identity_keys(row)
        if any(key in seen for key in keys):
            continue
        seen.update(keys)
        out.append(row)
        if len(out) >= max(1, int(limit)):
            break
    return out


def _dataset_workbench_identity_keys(dataset: Dataset) -> list[tuple[str, ...]]:
    extra = dataset.extra if isinstance(dataset.extra, dict) else {}
    family = _home_text_key(dataset.family) or "general"
    category = _home_text_key(dataset.category) or "dataset"
    source = _home_text_key(dataset.source_kind) or category
    title = _home_text_key(dataset.title) or _home_text_key(dataset.slug)

    identity = ""
    company_id = _home_safe_int(extra.get("company_id"))
    issuer_id = _home_safe_int(extra.get("issuer_id"))
    if company_id is not None:
        identity = f"company:{company_id}"
    elif issuer_id is not None:
        identity = f"issuer:{issuer_id}"
    else:
        for key in ("company_slug", "company_name", "issuer_name", "ticker", "symbol"):
            identity = _home_text_key(extra.get(key))
            if identity:
                break
    keys: list[tuple[str, ...]] = []
    if identity:
        keys.append(("entity", family, category, source, identity))
    if title:
        keys.append(("title", title))
        keys.append(("title", family, title))
        keys.append(("title", family, category, source, title))
    if not keys:
        keys.append(("dataset", family, category, source, _home_text_key(dataset.slug) or str(dataset.id)))
    return keys


def _home_text_key(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9가-힣]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _home_safe_int(value: object) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(value)
    except Exception:
        return None


def _family_lanes(session: Session) -> list[dict[str, Any]]:
    rows = (
        session.query(Dataset.family, func.count(Dataset.id), func.max(Dataset.updated_at))
        .group_by(Dataset.family)
        .order_by(func.max(Dataset.updated_at).desc())
        .limit(6)
        .all()
    )
    return [
        {
            "family": family or "general",
            "pressure_score": min(1.0, 0.25 + (count or 0) * 0.08),
            "dataset_count": int(count or 0),
            "last_update": last_update.isoformat() if last_update else None,
            "reason_badges": ["dataset changed"] if last_update else ["unreviewed"],
            "href": f"/visualizations/datasets?family={family or 'general'}",
        }
        for family, count, last_update in rows
    ]


def _notebook(session: Session) -> dict[str, Any]:
    rows = (
        session.query(Note)
        .options(load_only(Note.id, Note.title, Note.status, Note.pinned, Note.updated_at))
        .filter(Note.status == "active")
        .order_by(Note.pinned.desc(), Note.updated_at.desc())
        .limit(6)
        .all()
    )
    stale_count = _count(
        session.query(Note).filter(
            Note.status == "active",
            Note.stale_after_days.isnot(None),
            Note.last_reviewed_at.is_(None),
        )
    )
    return {
        "stale_count": stale_count,
        "items": [
            {
                "id": row.id,
                "title": row.title,
                "href": f"/notes/{row.id}",
                "pinned": bool(row.pinned),
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "reason_badges": ["pinned"] if row.pinned else ["recently updated"],
            }
            for row in rows
        ],
    }


def _signal_board(session: Session) -> list[dict[str, Any]]:
    rows = (
        session.query(
            Document.id.label("document_id"),
            Document.title.label("document_title"),
            Document.url.label("document_url"),
            Document.kind.label("document_kind"),
            Document.triage_state.label("triage_state"),
            Document.importance.label("importance"),
            Document.ingested_at.label("ingested_at"),
            Source.name.label("source_name"),
            Source.config.label("source_config"),
        )
        .join(Source, Source.id == Document.source_id)
        .filter(Document.triage_state.in_(["NEW", "OPENED"]))
        .order_by(Document.importance.desc(), Document.ingested_at.desc())
        .limit(32)
        .all()
    )
    clusters = []
    for row in rows:
        is_filing = _is_filing_signal_values(
            title=row.document_title,
            kind=row.document_kind,
            source_name=row.source_name,
            source_config=row.source_config,
        )
        badges = ["unreviewed", "source cluster expanding"]
        if is_filing:
            badges.append("filing")
        clusters.append(
            {
                "id": f"cluster:{row.document_id}",
                "title": row.document_title or row.document_url or f"Document {row.document_id}",
                "entities": [],
                "source_count": 1,
                "document_count": 1,
                "first_seen": row.ingested_at.isoformat() if row.ingested_at else None,
                "last_seen": row.ingested_at.isoformat() if row.ingested_at else None,
                "novelty": 0.7 if row.triage_state == "NEW" else 0.35,
                "importance": float(row.importance or 1),
                "triage_state": row.triage_state,
                "is_filing": is_filing,
                "representative_documents": [
                    {"id": row.document_id, "title": row.document_title, "href": f"/docs/{row.document_id}"}
                ],
                "reason": f"{row.source_name} has an unreviewed item.",
                "reason_badges": badges,
            }
        )
    return clusters


def _is_filing_signal(doc: Document, source: Source) -> bool:
    return _is_filing_signal_values(
        title=doc.title,
        kind=doc.kind,
        source_name=source.name,
        source_config=source.config,
    )


def _is_filing_signal_values(
    *,
    title: Any,
    kind: Any,
    source_name: Any,
    source_config: Any,
) -> bool:
    title = (title or "").strip().lower()
    kind = (kind or "").strip().lower()
    source_name = (source_name or "").strip().lower()
    source_config = source_config if isinstance(source_config, dict) else {}
    source_url = str(source_config.get("url") or source_config.get("feed_url") or "").strip().lower()
    return (
        "filing" in kind
        or title.startswith("sec current filing:")
        or source_name in {"sec edgar current", "sec edgar", "edgar"}
        or "sec edgar" in source_name
        or "sec.gov" in source_url
    )


def _system_status(session: Session, *, since, document_counts: dict[str, int] | None = None) -> dict[str, Any]:
    document_counts = document_counts or {}
    degraded = (
        session.query(
            Source.id.label("source_id"),
            Source.name.label("source_name"),
            SourceState.consecutive_failures.label("consecutive_failures"),
        )
        .join(SourceState, SourceState.source_id == Source.id)
        .filter(SourceState.consecutive_failures > 0)
        .order_by(SourceState.consecutive_failures.desc())
        .limit(6)
        .all()
    )
    failed_ingestions = int(document_counts.get("failed_ingestions", 0))
    return {
        "source_coverage": _count(session.query(Source)),
        "degraded_sources_count": len(degraded),
        "degraded_sources": [
            {
                "id": row.source_id,
                "name": row.source_name,
                "failures": row.consecutive_failures,
                "reason_badges": ["pipeline degraded"],
            }
            for row in degraded
        ],
        "failed_ingestions": failed_ingestions,
        "stale_datasets": 0,
        "unresolved_conflicts": 0,
        "unreviewed_high_importance_signals": int(document_counts.get("unreviewed_high_importance_signals", 0)),
        "stale_active_theses": _count(
            session.query(Thesis).filter(Thesis.status.in_(["watching", "active"]), Thesis.stale_check_state == "stale")
        ),
    }


def _portfolio_mood(session: Session, *, allow_live_refresh: bool = False) -> dict[str, Any]:
    service = ibkr_service()
    if service.config.active:
        return service.portfolio_mood(
            session,
            settings=settings,
            allow_live_refresh=allow_live_refresh,
            persist_mood=bool(allow_live_refresh),
        )
    return _latest_portfolio_mood(session)


def _portfolio_state(portfolio_mood: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(portfolio_mood.get("metrics") or {})
    positions = [item for item in (portfolio_mood.get("positions") or []) if isinstance(item, dict)]
    cost_basis, cost_basis_coverage = _estimated_position_cost_basis(positions)
    if cost_basis is not None:
        metrics["estimated_position_cost_basis"] = cost_basis
        metrics["estimated_position_cost_basis_coverage_pct"] = cost_basis_coverage
    sorted_positions = sorted(
        positions,
        key=lambda item: abs(float(item.get("market_value") or 0.0)),
        reverse=True,
    )
    top_positions = sorted_positions[:5]
    strip_positions = sorted_positions[:18]
    snapshots = [item for item in (portfolio_mood.get("market_snapshots") or []) if isinstance(item, dict)]
    by_conid = {str(item.get("conid") or ""): item for item in snapshots if item.get("conid")}
    fallback_market_status = "portfolio" if any(_position_last(pos) is not None for pos in positions) else "unavailable"

    def quote_row(pos: dict[str, Any]) -> dict[str, Any]:
        conid = str(pos.get("conid") or "")
        quote = by_conid.get(conid, {})
        return {
            "symbol": pos.get("symbol") or quote.get("symbol") or conid or "IBKR",
            "conid": conid or None,
            "market_value": pos.get("market_value"),
            "weight": pos.get("weight"),
            "unrealized_pnl": pos.get("unrealized_pnl"),
            "last": quote.get("last") or quote.get("mark") or _position_last(pos),
            "change_pct": quote.get("change_pct"),
            "market_data": quote.get("availability_label") or portfolio_mood.get("market_data_status") or "portfolio",
        }

    quote_rows = [quote_row(pos) for pos in top_positions]
    strip_quote_rows = [quote_row(pos) for pos in strip_positions]
    raw_market_status = portfolio_mood.get("market_data_status")
    market_status = (
        fallback_market_status
        if (not raw_market_status or raw_market_status == "unavailable") and fallback_market_status != "unavailable"
        else (raw_market_status or fallback_market_status)
    )
    return {
        "account_id": portfolio_mood.get("account_id"),
        "metrics": metrics,
        "top_positions": quote_rows,
        "strip_positions": strip_quote_rows,
        "position_count": len(positions),
        "market_data_status": market_status,
        "captured_at": portfolio_mood.get("captured_at"),
        "live_error": portfolio_mood.get("live_error"),
    }


def derive_home_portfolio_state(portfolio_mood: dict[str, Any]) -> dict[str, Any]:
    return _portfolio_state(portfolio_mood)


def _estimated_position_cost_basis(positions: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    total = 0.0
    covered = 0
    eligible = 0
    for pos in positions:
        quantity = _number_or_none(pos.get("quantity"))
        average_cost = _number_or_none(pos.get("average_cost"))
        if quantity is None or quantity == 0:
            continue
        eligible += 1
        if average_cost is None or average_cost <= 0:
            continue
        total += abs(float(quantity) * float(average_cost))
        covered += 1
    if not covered:
        return None, None
    return total, covered / max(eligible, 1)


def _number_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def build_market_watchlist(
    session: Session | None = None,
    *,
    allow_live_refresh: bool = False,
    portfolio_mood: dict[str, Any] | None = None,
) -> dict[str, Any]:
    universe_strip = _universe_market_strip(session, include_quotes=bool(allow_live_refresh)) if session is not None else {}
    if universe_strip:
        return universe_strip
    return {
        "items": [],
        "groups": [],
        "name": "Universes",
        "status": "unconfigured",
        "status_counts": {},
        "source": "universes",
        "captured_at": utcnow().isoformat(),
    }


def _universe_market_strip(
    session: Session,
    *,
    include_quotes: bool,
    max_groups: int = 5,
    max_rows_per_group: int = 18,
) -> dict[str, Any]:
    if not include_quotes:
        return _universe_market_strip_without_quotes(
            session,
            max_groups=max_groups,
            max_rows_per_group=max_rows_per_group,
        )

    universes = (
        session.query(ResearchUniverse)
        .filter(ResearchUniverse.status == "active")
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverse.name.asc(), ResearchUniverse.id.asc())
        .limit(max(1, int(max_groups)) * 2)
        .all()
    )
    groups: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    for universe in universes:
        detail = serialize_universe(session, universe, include_rows=True, include_quotes=include_quotes)
        group_items: list[dict[str, Any]] = []
        group_counts: dict[str, int] = {}
        for section in detail.get("sections") or []:
            section_name = section.get("name")
            for row in section.get("rows") or []:
                inst = row.get("instrument") or {}
                quote = inst.get("quote") or {}
                symbol = row.get("display_label") or inst.get("ticker") or inst.get("display_name") or inst.get("ibkr_conid")
                if not symbol:
                    continue
                is_private = inst.get("is_public") is False
                has_pricing = any(
                    quote.get(key) is not None for key in ("last", "mark", "change", "change_pct")
                )
                if include_quotes and not is_private and not has_pricing:
                    continue
                market_data = str(
                    quote.get("market_data")
                    or quote.get("provider")
                    or ("private" if is_private else "unavailable")
                )
                status_counts[market_data] = status_counts.get(market_data, 0) + 1
                group_counts[market_data] = group_counts.get(market_data, 0) + 1
                item = {
                    "symbol": _market_strip_symbol(symbol, is_private=is_private),
                    "conid": inst.get("ibkr_conid"),
                    "last": None if is_private else quote.get("last"),
                    "change": None if is_private else quote.get("change"),
                    "change_pct": None if is_private else quote.get("change_pct"),
                    "company_name": inst.get("display_name"),
                    "market_data": market_data,
                    "is_private": is_private,
                    "universe_id": detail.get("id"),
                    "universe_slug": detail.get("slug"),
                    "universe_name": detail.get("name"),
                    "section_name": section_name,
                }
                group_items.append(item)
                items.append(item)
                if len(group_items) >= max(1, int(max_rows_per_group)):
                    break
            if len(group_items) >= max(1, int(max_rows_per_group)):
                break
        if not group_items:
            continue
        groups.append(
            {
                "id": detail.get("slug") or detail.get("id"),
                "name": detail.get("name") or "Universe",
                "items": group_items,
                "status": _preferred_market_status(group_counts),
                "status_counts": group_counts,
            }
        )
        if len(groups) >= max(1, int(max_groups)):
            break
    if not groups:
        return {}
    return {
        "items": items,
        "groups": groups,
        "name": groups[0]["name"],
        "status": _preferred_market_status(status_counts),
        "status_counts": status_counts,
        "source": "universes",
        "captured_at": utcnow().isoformat(),
    }


def _universe_market_strip_without_quotes(
    session: Session,
    *,
    max_groups: int,
    max_rows_per_group: int,
) -> dict[str, Any]:
    group_limit = max(1, int(max_groups))
    row_limit = max(1, int(max_rows_per_group))
    universes = (
        session.query(ResearchUniverse)
        .options(
            load_only(
                ResearchUniverse.id,
                ResearchUniverse.slug,
                ResearchUniverse.name,
                ResearchUniverse.status,
                ResearchUniverse.sort_order,
            )
        )
        .filter(ResearchUniverse.status == "active")
        .order_by(ResearchUniverse.sort_order.asc(), ResearchUniverse.name.asc(), ResearchUniverse.id.asc())
        .limit(group_limit * 2)
        .all()
    )
    if not universes:
        return {}

    universe_ids = [int(row.id) for row in universes]
    sections_by_id = {
        int(section.id): section
        for section in (
            session.query(ResearchUniverseSection)
            .options(
                load_only(
                    ResearchUniverseSection.id,
                    ResearchUniverseSection.universe_id,
                    ResearchUniverseSection.name,
                    ResearchUniverseSection.sort_order,
                )
            )
            .filter(ResearchUniverseSection.universe_id.in_(universe_ids))
            .order_by(
                ResearchUniverseSection.universe_id.asc(),
                ResearchUniverseSection.sort_order.asc(),
                ResearchUniverseSection.id.asc(),
            )
            .all()
        )
    }

    row_rank = (
        func.row_number()
        .over(
            partition_by=ResearchUniverseRow.universe_id,
            order_by=(
                ResearchUniverseSection.sort_order.asc(),
                ResearchUniverseRow.sort_order.asc(),
                ResearchUniverseRow.id.asc(),
            ),
        )
        .label("row_rank")
    )
    ranked_rows = (
        session.query(ResearchUniverseRow.id.label("row_id"), row_rank)
        .join(ResearchUniverseSection, ResearchUniverseSection.id == ResearchUniverseRow.section_id)
        .filter(ResearchUniverseRow.universe_id.in_(universe_ids), ResearchUniverseRow.archived_at.is_(None))
        .subquery()
    )
    rows_by_universe: dict[int, list[ResearchUniverseRow]] = defaultdict(list)
    rows = (
        session.query(ResearchUniverseRow)
        .join(ranked_rows, ResearchUniverseRow.id == ranked_rows.c.row_id)
        .options(
            load_only(
                ResearchUniverseRow.id,
                ResearchUniverseRow.universe_id,
                ResearchUniverseRow.section_id,
                ResearchUniverseRow.instrument_id,
                ResearchUniverseRow.display_label,
                ResearchUniverseRow.sort_order,
            ),
            joinedload(ResearchUniverseRow.instrument).load_only(
                ResearchInstrument.id,
                ResearchInstrument.display_name,
                ResearchInstrument.ticker,
                ResearchInstrument.ibkr_conid,
                ResearchInstrument.is_public,
            ),
        )
        .filter(ranked_rows.c.row_rank <= row_limit)
        .order_by(ResearchUniverseRow.universe_id.asc(), ranked_rows.c.row_rank.asc(), ResearchUniverseRow.id.asc())
        .all()
    )
    for row in rows:
        rows_by_universe[int(row.universe_id)].append(row)

    groups: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    for universe in universes:
        group_items: list[dict[str, Any]] = []
        group_counts: dict[str, int] = {}
        for row in rows_by_universe.get(int(universe.id), []):
            inst = row.instrument
            symbol = row.display_label
            if inst is not None:
                symbol = symbol or inst.ticker or inst.display_name or inst.ibkr_conid
            if not symbol:
                continue
            is_private = bool(inst is not None and inst.is_public is False)
            market_data = "private" if is_private else "unavailable"
            status_counts[market_data] = status_counts.get(market_data, 0) + 1
            group_counts[market_data] = group_counts.get(market_data, 0) + 1
            section = sections_by_id.get(int(row.section_id))
            item = {
                "symbol": _market_strip_symbol(symbol, is_private=is_private),
                "conid": inst.ibkr_conid if inst is not None else None,
                "last": None,
                "change": None,
                "change_pct": None,
                "company_name": inst.display_name if inst is not None else None,
                "market_data": market_data,
                "is_private": is_private,
                "universe_id": int(universe.id),
                "universe_slug": universe.slug,
                "universe_name": universe.name,
                "section_name": section.name if section is not None else None,
            }
            group_items.append(item)
            items.append(item)
            if len(group_items) >= row_limit:
                break
        if not group_items:
            continue
        groups.append(
            {
                "id": universe.slug or int(universe.id),
                "name": universe.name or "Universe",
                "items": group_items,
                "status": _preferred_market_status(group_counts),
                "status_counts": group_counts,
            }
        )
        if len(groups) >= group_limit:
            break
    if not groups:
        return {}
    return {
        "items": items,
        "groups": groups,
        "name": groups[0]["name"],
        "status": _preferred_market_status(status_counts),
        "status_counts": status_counts,
        "source": "universes",
        "captured_at": utcnow().isoformat(),
    }


def _market_strip_symbol(symbol: Any, *, is_private: bool) -> str:
    label = str(symbol or "").strip()
    if is_private and label.lower().startswith("ibkr conid"):
        return "PRIVATE"
    return label


def _preferred_market_status(status_counts: dict[str, int]) -> str:
    for status in ("real-time", "delayed", "frozen-delayed", "frozen", "unknown", "unavailable", "standby"):
        if status_counts.get(status):
            return status
    return "unavailable"


def _position_last(pos: dict[str, Any]) -> float | None:
    metadata = pos.get("metadata") if isinstance(pos.get("metadata"), dict) else {}
    for key in ("marketPrice", "market_price", "last", "mark"):
        try:
            value = metadata.get(key) if metadata else pos.get(key)
            if value is not None:
                return float(value)
        except Exception:
            continue
    return None


def _latest_portfolio_mood(session: Session) -> dict[str, Any]:
    row = (
        session.query(PortfolioMoodSnapshot)
        .order_by(PortfolioMoodSnapshot.captured_at.desc(), PortfolioMoodSnapshot.id.desc())
        .first()
    )
    if row is None:
        return {
            "primary_mood": "neutral",
            "secondary_mood": None,
            "intensity": 0,
            "caption": "No portfolio snapshot yet.",
            "reason_badges": ["market data delayed"],
            "metrics": {},
        }
    return {
        "account_id": row.account_id,
        "primary_mood": row.primary_mood,
        "secondary_mood": row.secondary_mood,
        "intensity": row.intensity,
        "caption": row.caption,
        "reasons": list(row.reasons_json or []),
        "metrics": dict(row.metrics_json or {}),
        "captured_at": row.captured_at.isoformat() if row.captured_at else None,
        "reason_badges": ["portfolio mood changed"],
    }


def _link_status(session: Session, *, ibkr_status: dict[str, Any] | None = None) -> dict[str, Any]:
    active_sources = _count(session.query(Source).filter(Source.category.isnot(None)))
    ibkr = ibkr_status or {}
    ibkr_label = "disabled"
    ibkr_badges = ["broker disabled"]
    if ibkr.get("connected"):
        ibkr_label = "online"
        ibkr_badges = ["portfolio link healthy"]
    elif ibkr.get("auth_required"):
        ibkr_label = "auth required"
        ibkr_badges = ["IBKR disconnected/auth required"]
    elif ibkr.get("enabled"):
        ibkr_label = "standby"
        ibkr_badges = ["broker local-only"]
    return {
        "items": [
            {"name": "AEUG NET", "status": "online", "reason_badges": ["source coverage"]},
            {"name": "MAGI CORE", "status": "online", "reason_badges": ["local graph"]},
            {"name": "DATA LAKE", "status": "online" if active_sources else "standby", "reason_badges": ["dataset changed"]},
            {"name": "IBKR", "status": ibkr_label, "reason_badges": ibkr_badges},
        ]
    }


def _hero_badges(counts: dict[str, int], system_status: dict[str, Any], portfolio_mood: dict[str, Any]) -> list[str]:
    badges = []
    if counts["recent_documents"]:
        badges.append("new since last session")
    if system_status["degraded_sources_count"]:
        badges.append("pipeline degraded")
    if portfolio_mood.get("primary_mood") != "neutral":
        badges.append("portfolio mood changed")
    if not badges:
        badges.append("system nominal")
    return badges


def _next_routes(
    changes: list[dict[str, Any]],
    datasets: list[dict[str, Any]],
    notebook: dict[str, Any],
    system_status: dict[str, Any],
) -> list[dict[str, str]]:
    routes = []
    if changes:
        routes.append({"label": "Open Signal Board", "href": "/feed"})
    if datasets:
        routes.append({"label": "Open Dataset Workbench", "href": "/visualizations/datasets"})
    if notebook.get("stale_count"):
        routes.append({"label": "Review Notebook", "href": "/notes"})
    if system_status.get("degraded_sources_count"):
        routes.append({"label": "Check Ops", "href": "/ops"})
    return routes[:4]


def _event_href(row: IntelligenceEvent) -> str:
    if row.document_id:
        return f"/docs/{row.document_id}"
    if row.note_id:
        return f"/notes/{row.note_id}"
    if row.thesis_id:
        return f"/theses/{row.thesis_id}"
    if row.dataset_id:
        return f"/visualizations/datasets/{row.dataset_id}"
    return "/feed"


def _event_reason_badges(row: IntelligenceEvent) -> list[str]:
    meta = row.metadata_json or {}
    badges = list(meta.get("reason_badges") or [])
    if row.dataset_id:
        badges.append("dataset changed")
    if row.thesis_id:
        badges.append("touches active thesis")
    if row.event_type == "portfolio_mood_changed":
        badges.append("portfolio mood changed")
    if row.confidence_score < 0.5:
        badges.append("low confidence")
    return badges[:4] or ["new since last session"]


def _count(query) -> int:
    try:
        statement = (
            query.order_by(None)
            .limit(None)
            .offset(None)
            .statement.with_only_columns(func.count(), maintain_column_froms=True)
        )
        return int(query.session.execute(statement).scalar() or 0)
    except Exception:
        try:
            return int(query.count())
        except Exception:
            return 0
