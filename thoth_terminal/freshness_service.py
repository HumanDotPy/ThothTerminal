from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import requests
from sqlalchemy.orm import Session

from thoth_terminal.agent.llm_client import AgentLlmError, ChatMessage, active_llm_model, llm_available
from thoth_terminal.agent_request_manifest_service import manifested_chat_json
from thoth_terminal.config import settings
from thoth_terminal.db import utcnow
from thoth_terminal.instrument_service import create_instrument_request
from thoth_terminal.models import (
    FreshnessReview,
    KnowledgeAsset,
    KnowledgeAssetVersion,
    Note,
    ResearchSubject,
    Thesis,
    ThesisEvidence,
)
from thoth_terminal.research_report_service import assemble_report_payload, resolve_report_subject


REVIEWABLE_OBJECT_TYPES = {"thesis", "note", "knowledge_asset"}


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _dt_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def serialize_freshness_review(row: FreshnessReview) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "object_type": row.object_type,
        "object_id": int(row.object_id),
        "subject_id": int(row.subject_id) if row.subject_id is not None else None,
        "disposition": row.disposition,
        "trigger_kind": row.trigger_kind,
        "rationale": row.rationale,
        "summary": row.summary,
        "evidence_bundle": list(row.evidence_bundle or []),
        "payload": dict(row.payload or {}),
        "model": row.model,
        "live_web_used": bool(row.live_web_used),
        "created_at": _dt_iso(row.created_at),
        "checked_at": _dt_iso(row.checked_at),
    }


def list_freshness_reviews(
    session: Session,
    *,
    object_type: str | None = None,
    object_id: int | None = None,
    subject_id: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    q = session.query(FreshnessReview)
    if object_type:
        q = q.filter(FreshnessReview.object_type == str(object_type).strip().lower())
    if object_id is not None:
        q = q.filter(FreshnessReview.object_id == int(object_id))
    if subject_id is not None:
        q = q.filter(FreshnessReview.subject_id == int(subject_id))
    rows = q.order_by(FreshnessReview.checked_at.desc(), FreshnessReview.id.desc()).limit(max(1, min(int(limit or 50), 200))).all()
    return [serialize_freshness_review(row) for row in rows]


def get_freshness_review(session: Session, *, review_id: int) -> dict[str, Any]:
    row = session.query(FreshnessReview).filter_by(id=int(review_id)).first()
    if row is None:
        raise ValueError("Freshness review not found.")
    return serialize_freshness_review(row)


def _resolve_target(session: Session, *, object_type: str, object_id: int) -> tuple[str, Thesis | Note | KnowledgeAsset]:
    kind = str(object_type or "").strip().lower()
    if kind == "thesis":
        row = session.query(Thesis).filter_by(id=int(object_id)).first()
    elif kind == "note":
        row = session.query(Note).filter_by(id=int(object_id)).first()
    elif kind == "knowledge_asset":
        row = session.query(KnowledgeAsset).filter_by(id=int(object_id)).first()
    else:
        raise ValueError("Unsupported object_type.")
    if row is None:
        raise ValueError(f"{kind} not found.")
    return kind, row


def _target_subject(session: Session, *, object_type: str, row: Thesis | Note | KnowledgeAsset) -> ResearchSubject | None:
    if object_type == "knowledge_asset":
        return session.query(ResearchSubject).filter_by(id=int(row.subject_id)).first()
    entities = []
    if object_type == "thesis":
        entities = [entity.canonical_name for entity in (row.entities or []) if entity and entity.canonical_name]
    elif object_type == "note":
        entities = [entity.canonical_name for entity in (row.entities or []) if entity and entity.canonical_name]
    for token in entities:
        try:
            resolved = resolve_report_subject(session, token)
        except Exception:
            continue
        if resolved.subject_row is not None:
            return resolved.subject_row
    return None


def _target_policy_days(row: Thesis | Note | KnowledgeAsset) -> int | None:
    value = getattr(row, "stale_after_days", None)
    return int(value) if value is not None else None


def _target_updated_at(row: Thesis | Note | KnowledgeAsset) -> datetime:
    if isinstance(row, KnowledgeAsset):
        return row.updated_at or row.created_at
    return row.updated_at or row.created_at


def _target_body(session: Session, *, object_type: str, row: Thesis | Note | KnowledgeAsset) -> str:
    if object_type == "thesis":
        evidence_rows = session.query(ThesisEvidence).filter(ThesisEvidence.thesis_id == int(row.id)).order_by(ThesisEvidence.id.desc()).limit(5).all()
        evidence_lines = [str(ev.snippet or ev.note or "").strip() for ev in evidence_rows if str(ev.snippet or ev.note or "").strip()]
        return "\n".join(
            [
                f"Title: {row.title}",
                f"Statement: {row.statement}",
                *[f"Evidence: {line}" for line in evidence_lines],
            ]
        ).strip()
    if object_type == "note":
        return "\n".join([f"Title: {row.title}", row.body_md or ""]).strip()
    latest = None
    if row.latest_published_version_id is not None:
        latest = session.query(KnowledgeAssetVersion).filter_by(id=int(row.latest_published_version_id)).first()
    if latest is None:
        latest = (
            session.query(KnowledgeAssetVersion)
            .filter(KnowledgeAssetVersion.asset_id == int(row.id), KnowledgeAssetVersion.status.in_(["draft", "published"]))
            .order_by(KnowledgeAssetVersion.updated_at.desc(), KnowledgeAssetVersion.id.desc())
            .first()
        )
    return "\n".join([f"Title: {row.title}", latest.content_md if latest is not None else row.summary or ""]).strip()


def _internal_evidence_bundle(session: Session, *, subject_row: ResearchSubject | None) -> list[dict[str, Any]]:
    if subject_row is None:
        return []
    try:
        resolved = resolve_report_subject(session, subject_row.subject_key)
        report_payload, _meta = assemble_report_payload(session, subject=resolved, report=None, window_days=365)
    except Exception:
        return []
    bundle: list[dict[str, Any]] = []
    for row in (report_payload.get("primary_source_timeline") or {}).get("timeline") or []:
        bundle.append(
            {
                "type": "primary_source",
                "title": row.get("title"),
                "url": row.get("url"),
                "published_at": row.get("published_at"),
            }
        )
        if len(bundle) >= 5:
            break
    for row in report_payload.get("thesis_ledger") or []:
        if len(bundle) >= 8:
            break
        bundle.append({"type": "thesis", "title": row.get("title"), "updated_at": row.get("updated_at"), "href": row.get("href")})
    for row in report_payload.get("note_ledger") or []:
        if len(bundle) >= 10:
            break
        bundle.append({"type": "note", "title": row.get("title"), "updated_at": row.get("updated_at"), "href": row.get("href")})
    return bundle


def _live_web_evidence(query: str) -> list[dict[str, Any]]:
    try:
        response = requests.get(
            "https://duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": getattr(settings, "discord_market_user_agent", "Mozilla/5.0")},
            timeout=12,
        )
    except requests.RequestException:
        return []
    if response.status_code >= 400:
        return []
    html = response.text or ""
    matches = re.findall(
        r'<a[^>]+class="result__a"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    out: list[dict[str, Any]] = []
    for url, title, snippet in matches[:5]:
        out.append(
            {
                "type": "live_web",
                "url": _clean_text(re.sub(r"<[^>]+>", " ", url)),
                "title": _clean_text(re.sub(r"<[^>]+>", " ", title)),
                "snippet": _clean_text(re.sub(r"<[^>]+>", " ", snippet)),
            }
        )
    return out


def _heuristic_disposition(*, row_updated_at: datetime, stale_after_days: int | None, evidence_bundle: list[dict[str, Any]]) -> tuple[str, str, str]:
    if stale_after_days is None:
        return "insufficient_evidence", "No stale policy is configured for this object.", "not_reviewable"
    now = utcnow()
    age_days = max(0, int((now - row_updated_at).total_seconds() // 86400))
    if age_days <= int(stale_after_days):
        return "current", f"Object is {age_days} days old, within the {stale_after_days}-day review window.", "checked_current"
    newer_signals = 0
    for item in evidence_bundle:
        published_at = str(item.get("published_at") or item.get("updated_at") or "").strip()
        if published_at and published_at > row_updated_at.isoformat():
            newer_signals += 1
    if newer_signals >= 2:
        return "stale", f"Object is past the stale window and there are {newer_signals} newer evidence items.", "confirmed_stale"
    return "mixed", "Object is past the stale window, but the available evidence is not conclusive.", "mixed"


def run_freshness_review(
    session: Session,
    *,
    object_type: str,
    object_id: int,
    trigger_kind: str = "manual",
    allow_live_web: bool = True,
) -> dict[str, Any]:
    kind, row = _resolve_target(session, object_type=object_type, object_id=object_id)
    subject_row = _target_subject(session, object_type=kind, row=row)
    subject_label = subject_row.display_name if subject_row is not None else getattr(row, "title", None) or f"{kind}:{object_id}"
    stale_after_days = _target_policy_days(row)
    updated_at = _target_updated_at(row)
    body_text = _target_body(session, object_type=kind, row=row)
    internal_evidence = _internal_evidence_bundle(session, subject_row=subject_row)
    live_web_evidence: list[dict[str, Any]] = []
    use_live_web = bool(allow_live_web and len(internal_evidence) < 4)
    if use_live_web:
        live_web_evidence = _live_web_evidence(f"{subject_label} latest developments")
    evidence_bundle = list(internal_evidence) + list(live_web_evidence)

    disposition, rationale, state = _heuristic_disposition(
        row_updated_at=updated_at,
        stale_after_days=stale_after_days,
        evidence_bundle=evidence_bundle,
    )
    summary = f"{subject_label} freshness review: {disposition}."
    model_name = "heuristic"
    payload: dict[str, Any] = {"heuristic": True, "stale_after_days": stale_after_days}

    if llm_available(provider=getattr(settings, "ai_provider", "openai")):
        prompt = (
            "Return JSON only with keys disposition, summary, rationale, stale_signals, current_signals, confidence. "
            "Allowed disposition values: current, stale, mixed, insufficient_evidence. "
            "Evaluate whether the object still appears current against the provided evidence. "
            "Do not rewrite the object body.\n\n"
            f"Object type: {kind}\n"
            f"Subject: {subject_label}\n"
            f"Updated at: {updated_at.isoformat()}\n"
            f"Stale after days: {stale_after_days}\n\n"
            f"Object body:\n{body_text[:12000]}\n\n"
            f"Evidence:\n{str(evidence_bundle)[:12000]}"
        )
        try:
            parsed = manifested_chat_json(
                session,
                run_kind="freshness_review",
                run_id=int(object_id),
                task_kind=f"freshness_review:{kind}",
                messages=[
                    ChatMessage(role="system", content="You verify whether research objects are stale without editing them."),
                    ChatMessage(role="user", content=prompt),
                ],
                provider=getattr(settings, "ai_provider", "openai"),
                timeout_s=90.0,
                max_input_chars=22000,
                prompt_cache_key="thoth-freshness-review-v1",
                permission_profile="read-only-evidence-review",
            )
            disposition_candidate = str(parsed.get("disposition") or "").strip().lower()
            if disposition_candidate in {"current", "stale", "mixed", "insufficient_evidence"}:
                disposition = disposition_candidate
            summary = _clean_text(parsed.get("summary")) or summary
            rationale = _clean_text(parsed.get("rationale")) or rationale
            payload = {
                "stale_signals": [str(item).strip() for item in (parsed.get("stale_signals") or []) if str(item).strip()],
                "current_signals": [str(item).strip() for item in (parsed.get("current_signals") or []) if str(item).strip()],
                "confidence": parsed.get("confidence"),
                "heuristic": False,
                "stale_after_days": stale_after_days,
            }
            state = {
                "current": "checked_current",
                "stale": "confirmed_stale",
                "mixed": "mixed",
                "insufficient_evidence": "insufficient_evidence",
            }.get(disposition, state)
            model_name = active_llm_model(getattr(settings, "ai_provider", "openai")) or "llm"
        except AgentLlmError:
            pass

    now = utcnow()
    review = FreshnessReview(
        object_type=kind,
        object_id=int(object_id),
        subject_id=int(subject_row.id) if subject_row is not None else None,
        disposition=disposition,
        trigger_kind=str(trigger_kind or "manual"),
        rationale=rationale,
        summary=summary,
        evidence_bundle=[dict(item) for item in evidence_bundle if isinstance(item, dict)],
        payload=payload,
        model=model_name,
        live_web_used=bool(live_web_evidence),
        created_at=now,
        checked_at=now,
    )
    session.add(review)

    row.last_stale_check_at = now
    row.stale_check_state = state
    if disposition == "current":
        row.last_reviewed_at = now

    if disposition in {"stale", "mixed"}:
        create_instrument_request(
            session,
            title=f"{subject_label} freshness follow-up",
            intent="freshness_follow_up",
            summary=summary,
            target_entities=[subject_label] if subject_row is not None and subject_row.kind == "company" else None,
            target_themes=[subject_label] if subject_row is None or subject_row.kind != "company" else None,
            desired_outputs=["refresh_review", kind],
            evidence_bundle=[dict(item) for item in evidence_bundle if isinstance(item, dict)],
            provenance_summary=rationale,
            evidence_tier="mixed" if live_web_evidence else "internal",
            status="draft",
            priority=7,
            automation_source="freshness_review",
            source_kind=kind,
            linked_objects={"object_type": kind, "object_id": int(object_id), "subject_id": int(subject_row.id) if subject_row is not None else None},
        )

    session.flush()
    return {
        "review": serialize_freshness_review(review),
        "object_type": kind,
        "object_id": int(object_id),
        "state": state,
    }


def due_freshness_review_targets(session: Session, *, limit: int = 100) -> list[dict[str, Any]]:
    now = utcnow()
    out: list[dict[str, Any]] = []
    thesis_rows = session.query(Thesis).order_by(Thesis.updated_at.asc(), Thesis.id.asc()).all()
    for row in thesis_rows:
        if row.stale_after_days is None:
            continue
        basis = row.last_stale_check_at or row.last_reviewed_at or row.updated_at or row.created_at
        if basis and basis <= now - timedelta(days=int(row.stale_after_days)):
            out.append({"object_type": "thesis", "object_id": int(row.id)})
        if len(out) >= limit:
            return out
    note_rows = session.query(Note).order_by(Note.updated_at.asc(), Note.id.asc()).all()
    for row in note_rows:
        if row.stale_after_days is None:
            continue
        basis = row.last_stale_check_at or row.last_reviewed_at or row.updated_at or row.created_at
        if basis and basis <= now - timedelta(days=int(row.stale_after_days)):
            out.append({"object_type": "note", "object_id": int(row.id)})
        if len(out) >= limit:
            return out
    asset_rows = session.query(KnowledgeAsset).order_by(KnowledgeAsset.updated_at.asc(), KnowledgeAsset.id.asc()).all()
    for row in asset_rows:
        if row.stale_after_days is None:
            continue
        basis = row.last_stale_check_at or row.last_reviewed_at or row.updated_at or row.created_at
        if basis and basis <= now - timedelta(days=int(row.stale_after_days)):
            out.append({"object_type": "knowledge_asset", "object_id": int(row.id)})
        if len(out) >= limit:
            return out
    return out
