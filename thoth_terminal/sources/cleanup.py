from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.discord.models import DiscordLinkInboxItem
from thoth_terminal.models import Document, IngestionRun, NotificationRule, Source, SourceProfile, SourceState
from thoth_terminal.sources.policy import is_source_policy_deprecated


RETIRED_COMPANY_SOURCE_NAME = "retired_company_pages"


def is_cleanup_candidate(
    name: str | None,
    config: dict[str, Any] | None = None,
    *,
    category: str | None = None,
) -> bool:
    return is_source_policy_deprecated(name, config, category=category)


def ensure_retired_company_source(session: Session) -> Source:
    row = session.query(Source).filter_by(name=RETIRED_COMPANY_SOURCE_NAME).first()
    if row:
        return row
    row = Source(
        name=RETIRED_COMPANY_SOURCE_NAME,
        category="internal",
        priority=1,
        polling_interval_seconds=None,
        config={
            "enabled": False,
            "deleted": True,
            "system": True,
            "retired": True,
            "disabled_reason": "retired source provenance bucket",
        },
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def _merge_retired_meta(extra: dict[str, Any] | None, *, source: Source, replacement: Source) -> dict[str, Any]:
    payload = dict(extra or {})
    retired = payload.get("retired_source")
    retired_payload = dict(retired) if isinstance(retired, dict) else {}
    retired_payload.update(
        {
            "source_id": int(source.id),
            "source_name": str(source.name),
            "source_category": str(source.category),
            "replacement_source_id": int(replacement.id),
            "replacement_source_name": str(replacement.name),
            "retired_at": utcnow().isoformat(),
        }
    )
    payload["retired_source"] = retired_payload
    return payload


def retire_source_definition(session: Session, source: Source) -> dict[str, Any]:
    replacement = ensure_retired_company_source(session)
    doc_count = 0
    run_count = 0

    docs = session.query(Document).filter(Document.source_id == int(source.id)).all()
    for doc in docs:
        doc.source_id = int(replacement.id)
        doc.extra = _merge_retired_meta(doc.extra if isinstance(doc.extra, dict) else {}, source=source, replacement=replacement)
        doc_count += 1

    runs = session.query(IngestionRun).filter(IngestionRun.source_id == int(source.id)).all()
    for row in runs:
        row.source_id = int(replacement.id)
        extra = row.extra if isinstance(row.extra, dict) else {}
        row.extra = _merge_retired_meta(extra, source=source, replacement=replacement)
        run_count += 1

    for rule in session.query(NotificationRule).all():
        source_ids = [int(v) for v in (rule.source_ids or []) if str(v).isdigit()]
        if int(source.id) not in source_ids:
            continue
        rule.source_ids = [sid for sid in source_ids if sid != int(source.id)]

    session.query(DiscordLinkInboxItem).filter(DiscordLinkInboxItem.source_id == int(source.id)).update(
        {"source_id": None},
        synchronize_session=False,
    )
    if source.state is not None:
        session.delete(source.state)
    else:
        session.query(SourceState).filter(SourceState.source_id == int(source.id)).delete(synchronize_session=False)
    if source.profile is not None:
        session.delete(source.profile)
    else:
        session.query(SourceProfile).filter(SourceProfile.source_id == int(source.id)).delete(synchronize_session=False)
    session.delete(source)
    session.flush()
    return {
        "source_id": int(source.id),
        "source_name": str(source.name),
        "documents_reassigned": int(doc_count),
        "runs_reassigned": int(run_count),
    }


def cleanup_legacy_company_sources(session: Session) -> list[dict[str, Any]]:
    targets = [
        row
        for row in session.query(Source).order_by(Source.id.asc()).all()
        if row.name != RETIRED_COMPANY_SOURCE_NAME
        and is_cleanup_candidate(row.name, row.config if isinstance(row.config, dict) else {}, category=row.category)
    ]
    removed: list[dict[str, Any]] = []
    for row in targets:
        removed.append(retire_source_definition(session, row))
    return removed
