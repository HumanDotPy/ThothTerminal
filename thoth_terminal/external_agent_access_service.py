"""Per-user credentials for the deliberately narrow public agent API."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.models import ExternalAgentApiKey, ExternalAgentPrincipal, ExternalAgentUsageDay


AGENT_API_SCOPES = frozenset({"models:read", "evidence:upload", "proposals:write"})
_KEY_PATTERN = re.compile(r"^thoth_agent_([A-Za-z0-9_-]{11})_([A-Za-z0-9_-]{32,64})$")


def issue_agent_api_key(
    session: Session, *, principal_slug: str, display_name: str,
    scopes: list[str] | tuple[str, ...], valid_days: int = 90,
) -> tuple[ExternalAgentApiKey, str]:
    slug = str(principal_slug or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", slug):
        raise ValueError("principal_slug must be 2-64 lowercase letters, digits, or hyphens")
    name = str(display_name or "").strip()
    if not 1 <= len(name) <= 255:
        raise ValueError("display_name must be 1-255 characters")
    normalized_scopes = sorted(set(scopes))
    if not normalized_scopes or set(normalized_scopes) - AGENT_API_SCOPES:
        raise ValueError("Unknown or empty agent API scope")
    if not 1 <= valid_days <= 365:
        raise ValueError("valid_days must be 1-365")
    now = utcnow()
    principal = session.query(ExternalAgentPrincipal).filter_by(slug=slug).first()
    if principal is None:
        principal = ExternalAgentPrincipal(
            slug=slug, display_name=name, status="active", created_at=now, updated_at=now,
        )
        session.add(principal)
        session.flush()
    elif principal.status != "active":
        raise ValueError("Principal is disabled")
    else:
        principal.display_name = name
        principal.updated_at = now
    prefix = secrets.token_urlsafe(8)
    secret = secrets.token_urlsafe(32)
    token = f"thoth_agent_{prefix}_{secret}"
    row = ExternalAgentApiKey(
        principal_id=int(principal.id), key_prefix=prefix,
        key_sha256=hashlib.sha256(token.encode()).hexdigest(),
        scopes_json=normalized_scopes, created_at=now,
        expires_at=now + timedelta(days=valid_days),
    )
    session.add(row)
    session.flush()
    return row, token


def authenticate_agent_api_key(session: Session, *, token: str, required_scope: str) -> ExternalAgentPrincipal | None:
    if required_scope not in AGENT_API_SCOPES:
        raise ValueError("Unknown agent API scope")
    match = _KEY_PATTERN.fullmatch(str(token or ""))
    if match is None:
        return None
    row = session.query(ExternalAgentApiKey).filter_by(key_prefix=match.group(1)).first()
    if row is None or row.revoked_at is not None or required_scope not in (row.scopes_json or []):
        return None
    expires = row.expires_at
    if expires is not None and expires <= utcnow():
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    if not secrets.compare_digest(digest, row.key_sha256):
        return None
    principal = session.query(ExternalAgentPrincipal).filter_by(id=row.principal_id).first()
    if principal is None or principal.status != "active":
        return None
    return principal


def revoke_agent_api_key(session: Session, *, key_prefix: str) -> bool:
    row = session.query(ExternalAgentApiKey).filter_by(key_prefix=str(key_prefix or "")).first()
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = utcnow()
    session.flush()
    return True


def reserve_agent_api_use(
    session: Session, *, principal_id: int, upload_bytes: int = 0,
    render: bool = False, proposal: bool = False,
) -> None:
    """Charge bounded public-agent work to the principal, across all its keys."""
    today = utcnow().date()
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        insert = pg_insert(ExternalAgentUsageDay)
    elif dialect == "sqlite":
        insert = sqlite_insert(ExternalAgentUsageDay)
    else:
        raise ValueError("Agent API quotas require PostgreSQL or SQLite")
    session.execute(insert.values(
        principal_id=int(principal_id), usage_date=today,
        request_count=0, upload_bytes=0, render_count=0, proposal_count=0,
    ).on_conflict_do_nothing(index_elements=["principal_id", "usage_date"]))
    row = (
        session.query(ExternalAgentUsageDay)
        .filter_by(principal_id=int(principal_id), usage_date=today)
        .with_for_update()
        .first()
    )
    if row is None:
        raise RuntimeError("Agent API quota row was not created")
    if row.request_count >= 1000:
        raise ValueError("Daily agent API request limit reached")
    if upload_bytes < 0 or row.upload_bytes + upload_bytes > 100 * 1024 * 1024:
        raise ValueError("Daily agent API upload limit reached")
    if render and row.render_count >= 200:
        raise ValueError("Daily page rendering limit reached")
    if proposal and row.proposal_count >= 25:
        raise ValueError("Daily proposal limit reached")
    row.request_count += 1
    row.upload_bytes += upload_bytes
    row.render_count += int(render)
    row.proposal_count += int(proposal)
    session.flush()
