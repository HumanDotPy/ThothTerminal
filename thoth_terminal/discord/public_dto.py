from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import heapq
import re
from typing import Any

from sqlalchemy import and_, false, or_

from thoth_terminal.db import session_factory, utcnow
from thoth_terminal.discord import notes_bridge, services
from thoth_terminal.discord.models import DiscordEvidenceItem, DiscordEvidenceTarget, DiscordWatchlistEntry
from thoth_terminal.models import (
    Company,
    IRArtifact,
    Issuer,
    IssuerIdentifier,
    ResearchReport,
    ResearchReportVersion,
    SupplyChain,
    SupplyChainAlertRule,
)
from thoth_terminal.supply_chain_service import latest_chain_snapshot, serialize_supply_chain


PUBLIC_EVIDENCE_STATES = {"resolved", "approved", "public"}
PRIVATE_TAGS = {"private", "internal", "operator", "exec", "executive", "desk", "thoth-private"}


@dataclass(frozen=True)
class PublicSearchResult:
    kind: str
    title: str
    url: str | None
    summary: str | None = None
    symbol: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class PublicCompanyProfile:
    name: str
    ticker: str | None
    exchange: str | None
    sector: str | None
    industry: str | None
    website: str | None
    report_count: int
    transcript_count: int
    evidence_count: int
    watchlist_memberships: list[str]


@dataclass(frozen=True)
class PublicTickerBrief:
    ticker: str
    company_name: str | None
    watchlist_memberships: list[str]
    themes: list[str]
    evidence: list[PublicSearchResult]


@dataclass(frozen=True)
class PublicAlertSummary:
    name: str
    chain_title: str
    chain_slug: str
    scope: str
    trigger: str
    status: str
    last_triggered_at: datetime | None


@dataclass(frozen=True)
class PublicComparison:
    left: PublicCompanyProfile
    right: PublicCompanyProfile
    left_recent_evidence: list[PublicSearchResult]
    right_recent_evidence: list[PublicSearchResult]


@dataclass(frozen=True)
class PublicSupplyChainView:
    id: int
    slug: str
    title: str
    description: str | None
    category: str | None
    product: str | None
    counts: dict[str, int]
    version: str | None
    key_nodes: list[dict[str, Any]]
    alerts: list[PublicAlertSummary]


def _short(value: object, limit: int = 240) -> str:
    text = " ".join(str(value or "").replace("\n", " ").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "..."


def evidence_is_public(evidence: DiscordEvidenceItem) -> bool:
    state = str(evidence.resolution_state or "").strip().lower()
    if state not in PUBLIC_EVIDENCE_STATES:
        return False
    tags = {str(tag or "").strip().lower() for tag in (evidence.tags or [])}
    return not bool(tags & PRIVATE_TAGS)


def issuer_is_public(issuer: Issuer | None) -> bool:
    if issuer is None:
        return False
    extra = issuer.extra if isinstance(issuer.extra, dict) else {}
    return not bool(extra.get("private"))


def supply_chain_is_public(chain: SupplyChain | None) -> bool:
    if chain is None or str(chain.status or "").strip().lower() != "active":
        return False
    data = chain.data if isinstance(chain.data, dict) else {}
    about = data.get("about") if isinstance(data.get("about"), dict) else {}
    privateish = {"private", "internal", "operator", "draft"}
    visibility = str(data.get("visibility") or about.get("visibility") or "").strip().lower()
    return not bool(data.get("private") or about.get("private") or visibility in privateish)


def _report_url(report: ResearchReport) -> str | None:
    base = notes_bridge.public_base_url().rstrip("/")
    slug = str(report.slug or "").strip()
    if not slug:
        return None
    return f"{base}/public/reports/{slug}/reader"


def public_report_reader_url(slug: str) -> str | None:
    base = notes_bridge.public_base_url().rstrip("/")
    slug_v = str(slug or "").strip()
    if not slug_v:
        return None
    return f"{base}/public/reports/{slug_v}/reader"


def _public_evidence_results(
    session: Any,
    *,
    guild_id: int,
    symbol: str | None = None,
    limit: int = 5,
) -> list[PublicSearchResult]:
    limit_v = max(1, min(int(limit or 5), 25))
    q = (
        session.query(DiscordEvidenceItem, DiscordEvidenceTarget)
        .outerjoin(DiscordEvidenceTarget, DiscordEvidenceTarget.evidence_id == DiscordEvidenceItem.id)
        .filter(DiscordEvidenceItem.guild_id == int(guild_id))
    )
    sym = services.normalize_symbol(symbol or "")
    if sym:
        q = q.filter(DiscordEvidenceTarget.symbol == sym)
    rows = (
        q.order_by(DiscordEvidenceItem.pinned.desc(), DiscordEvidenceItem.updated_at.desc(), DiscordEvidenceItem.id.desc())
        .limit(limit_v * 4)
        .all()
    )
    out: list[PublicSearchResult] = []
    seen: set[int] = set()
    for evidence, target in rows:
        if int(evidence.id) in seen or not evidence_is_public(evidence):
            continue
        seen.add(int(evidence.id))
        out.append(
            PublicSearchResult(
                kind=str(evidence.source_type or "evidence"),
                title=str(evidence.title or evidence.url_canonical or "Evidence"),
                url=str(evidence.url_canonical or evidence.url_raw or "") or None,
                summary=_short(evidence.note, 180) if evidence.note else None,
                symbol=(services.normalize_symbol(getattr(target, "symbol", "") or "") or None),
                updated_at=evidence.updated_at,
            )
        )
        if len(out) >= limit_v:
            break
    return out


def _membership_label(row: DiscordWatchlistEntry) -> str:
    label = str(row.sector or row.list_kind or "").strip() or "Uncategorized"
    subgroup = str(row.subgroup or "").strip()
    return f"{label} / {subgroup}" if subgroup and subgroup != "General" else label


def _search_terms(query: str) -> list[str]:
    terms = list(dict.fromkeys(re.findall(r"[\w]+", query.casefold())))
    if not terms:
        raise ValueError("Search query needs a word or number.")
    if len(terms) > 12 or len(query) > 200:
        raise ValueError("Search query is too long; use at most 12 words and 200 characters.")
    return terms


def _like_term(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _matching_snippet(value: object, *, phrase: str, terms: list[str], limit: int = 220) -> str | None:
    text = str(value or "")
    if not text:
        return None
    lower = text.casefold()
    position = lower.find(phrase)
    if position < 0:
        positions = [lower.find(term) for term in terms]
        positions = [index for index in positions if index >= 0]
        if not positions:
            return None
        position = max(
            positions,
            key=lambda index: (sum(term in lower[max(0, index - 40):index + limit] for term in terms), -index),
        )
    start = max(0, position - 45)
    end = min(len(text), start + limit)
    snippet = _short(text[start:end], limit)
    return ("..." if start else "") + snippet + ("..." if end < len(text) else "")


def _search_score(
    *,
    phrase: str,
    terms: list[str],
    title: str,
    labels: str,
    note: str,
    url: str,
    updated_at: datetime | None,
) -> tuple[int, int, int, int, int, float]:
    fields = [title.casefold(), labels.casefold(), note.casefold(), url.casefold()]
    phrase_rank = next((4 - index for index, field in enumerate(fields) if phrase in field), 0)
    coverage = [sum(term in field for term in terms) for field in fields]
    instant = updated_at.replace(tzinfo=updated_at.tzinfo or timezone.utc).timestamp() if updated_at else 0.0
    return (phrase_rank, *coverage, instant)


def _remember_top(
    heap: list[tuple[tuple[int, int, int, int, int, float], int, PublicSearchResult]],
    *,
    score: tuple[int, int, int, int, int, float],
    row_id: int,
    result: PublicSearchResult,
    limit: int,
) -> None:
    candidate = (score, row_id, result)
    if len(heap) < limit:
        heapq.heappush(heap, candidate)
    elif candidate[:2] > heap[0][:2]:
        heapq.heapreplace(heap, candidate)


def search_public_memory(*, guild_id: int, query: str, limit: int = 10) -> list[PublicSearchResult]:
    q = str(query or "").strip()
    if not q:
        raise ValueError("Missing search query.")
    terms = _search_terms(q)
    phrase = q.casefold()
    limit_v = max(1, min(int(limit or 10), 30))
    evidence_top: list[tuple[tuple[int, int, int, int, int, float], int, PublicSearchResult]] = []
    report_top: list[tuple[tuple[int, int, int, int, int, float], int, PublicSearchResult]] = []
    SessionLocal = session_factory()
    with SessionLocal() as session:
        evidence_filters = []
        report_filters = []
        for term in terms:
            pattern = _like_term(term)
            target_match = session.query(DiscordEvidenceTarget.id).filter(
                DiscordEvidenceTarget.evidence_id == DiscordEvidenceItem.id,
                or_(
                    DiscordEvidenceTarget.symbol.ilike(pattern, escape="\\"),
                    DiscordEvidenceTarget.target_label.ilike(pattern, escape="\\"),
                ),
            ).exists().correlate_except(DiscordEvidenceTarget)
            evidence_filters.append(or_(
                DiscordEvidenceItem.title.ilike(pattern, escape="\\"),
                DiscordEvidenceItem.note.ilike(pattern, escape="\\"),
                DiscordEvidenceItem.url_canonical.ilike(pattern, escape="\\"),
                target_match,
            ))
            report_filters.append(or_(
                ResearchReport.title.ilike(pattern, escape="\\"),
                ResearchReport.slug.ilike(pattern, escape="\\"),
                ResearchReport.subject_key.ilike(pattern, escape="\\"),
                ResearchReportVersion.content_md.ilike(pattern, escape="\\"),
            ))
        evidence_rows = (
            session.query(DiscordEvidenceItem, DiscordEvidenceTarget)
            .outerjoin(DiscordEvidenceTarget, DiscordEvidenceTarget.evidence_id == DiscordEvidenceItem.id)
            .filter(DiscordEvidenceItem.guild_id == int(guild_id))
            .filter(DiscordEvidenceItem.resolution_state.in_(PUBLIC_EVIDENCE_STATES))
            .filter(and_(*evidence_filters))
            .order_by(DiscordEvidenceItem.updated_at.desc(), DiscordEvidenceItem.id.desc(), DiscordEvidenceTarget.id.asc())
            .yield_per(100)
        )
        current: DiscordEvidenceItem | None = None
        targets: list[DiscordEvidenceTarget] = []

        def finish_evidence() -> None:
            if current is None or not evidence_is_public(current):
                return
            labels = " ".join(f"{target.symbol or ''} {target.target_label or ''}" for target in targets)
            title = str(current.title or current.url_canonical or "Evidence")
            note = str(current.note or "")
            url = str(current.url_canonical or current.url_raw or "")
            matching_target = next(
                (target for target in targets if any(term in f"{target.symbol or ''} {target.target_label or ''}".casefold() for term in terms)),
                None,
            )
            primary_target = next((target for target in targets if target.is_primary), None)
            chosen_target = matching_target or primary_target or (targets[0] if targets else None)
            summary = _matching_snippet(note, phrase=phrase, terms=terms, limit=180)
            if summary is None and matching_target is not None:
                summary = f"Matched target: {_short(matching_target.target_label or matching_target.symbol, 160)}"
            if summary is None:
                summary = _matching_snippet(url, phrase=phrase, terms=terms, limit=180)
            result = PublicSearchResult(
                kind="evidence", title=title, url=url or None, summary=summary,
                symbol=(services.normalize_symbol(chosen_target.symbol or "") or None) if chosen_target else None,
                updated_at=current.updated_at,
            )
            _remember_top(
                evidence_top,
                score=_search_score(phrase=phrase, terms=terms, title=title, labels=labels, note=note, url=url, updated_at=current.updated_at),
                row_id=int(current.id), result=result, limit=limit_v,
            )

        for evidence, target in evidence_rows:
            if current is not None and int(evidence.id) != int(current.id):
                finish_evidence()
                targets = []
            current = evidence
            if target is not None:
                targets.append(target)
        finish_evidence()

        reports = (
            session.query(ResearchReport, ResearchReportVersion)
            .join(ResearchReportVersion, ResearchReportVersion.id == ResearchReport.latest_published_version_id)
            .filter(ResearchReport.status == "active")
            .filter(ResearchReportVersion.status == "published")
            .filter(and_(*report_filters))
            .order_by(ResearchReport.updated_at.desc(), ResearchReport.id.desc())
            .yield_per(100)
        )
        for report, version in reports:
            content = str(version.content_md or "")
            title = str(report.title or report.slug or "Research report")
            labels = f"{report.slug or ''} {report.subject_key or ''}"
            summary = _matching_snippet(content, phrase=phrase, terms=terms) or _short(report.subject_key, 220)
            result = PublicSearchResult(kind="report", title=title, url=_report_url(report), summary=summary, updated_at=report.updated_at)
            _remember_top(
                report_top,
                score=_search_score(phrase=phrase, terms=terms, title=title, labels=labels, note=content, url="", updated_at=report.updated_at),
                row_id=int(report.id), result=result, limit=limit_v,
            )
    combined = sorted([*evidence_top, *report_top], key=lambda item: item[:2], reverse=True)
    selected = combined[:limit_v]
    if limit_v >= 2 and evidence_top and report_top:
        for kind, pool in (("evidence", evidence_top), ("report", report_top)):
            if all(item[2].kind != kind for item in selected):
                selected[-1] = max(pool, key=lambda item: item[:2])
    return [item[2] for item in sorted(selected, key=lambda item: item[:2], reverse=True)]


def _issuer_for_symbol(session: Any, symbol: str) -> Issuer | None:
    sym = services.normalize_symbol(symbol)
    if not sym:
        return None
    ident = (
        session.query(IssuerIdentifier)
        .filter(IssuerIdentifier.id_type == "TICKER", IssuerIdentifier.id_value == sym)
        .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
        .first()
    )
    if not ident:
        return None
    return session.query(Issuer).filter_by(id=int(ident.issuer_id)).first()


def company_profile(*, guild_id: int, subject: str) -> PublicCompanyProfile:
    raw = str(subject or "").strip()
    if not raw:
        raise ValueError("Missing company or ticker.")
    sym = services.normalize_symbol(raw)
    pattern = f"%{raw}%"
    SessionLocal = session_factory()
    with SessionLocal() as session:
        company = (
            session.query(Company)
            .filter(or_(Company.ticker == sym, Company.slug == raw.lower(), Company.name.ilike(pattern)))
            .order_by(Company.ticker.desc(), Company.id.asc())
            .first()
        )
        issuer = _issuer_for_symbol(session, sym)
        if issuer is None and company and company.issuer_id:
            issuer = session.query(Issuer).filter_by(id=int(company.issuer_id)).first()
        if issuer is None:
            issuer = (
                session.query(Issuer)
                .filter(or_(Issuer.slug == raw.lower(), Issuer.display_name.ilike(pattern), Issuer.legal_name.ilike(pattern)))
                .order_by(Issuer.id.asc())
                .first()
            )
        if issuer is not None and not issuer_is_public(issuer):
            issuer = None
        if company is None and issuer is not None:
            ident = next((i for i in issuer.identifiers if i.id_type == "TICKER" and i.is_primary), None)
            ident = ident or next((i for i in issuer.identifiers if i.id_type == "TICKER"), None)
            sym = services.normalize_symbol(ident.id_value) if ident else sym
            company = session.query(Company).filter(Company.ticker == sym).order_by(Company.id.asc()).first() if sym else None

        if company is None and issuer is None:
            raise ValueError("No public company profile found.")

        ticker = services.normalize_symbol(getattr(company, "ticker", None) or sym) or None
        issuer_id = int(issuer.id) if issuer is not None else None
        company_id = int(company.id) if company is not None else None
        report_q = (
            session.query(ResearchReport)
            .filter(ResearchReport.status == "active")
            .filter(ResearchReport.latest_published_version_id.is_not(None))
        )
        if issuer_id is not None:
            report_q = report_q.filter(ResearchReport.issuer_id == issuer_id)
        elif company_id is not None:
            report_q = report_q.filter(ResearchReport.company_id == company_id)
        report_count = report_q.count()

        transcript_q = session.query(IRArtifact).filter(IRArtifact.artifact_type == "transcript")
        if issuer_id is not None:
            transcript_q = transcript_q.filter(IRArtifact.issuer_id == issuer_id)
        else:
            transcript_q = transcript_q.filter(false())
        transcript_count = transcript_q.count()

        evidence_q = (
            session.query(DiscordEvidenceItem)
            .join(DiscordEvidenceTarget, DiscordEvidenceTarget.evidence_id == DiscordEvidenceItem.id)
            .filter(DiscordEvidenceItem.guild_id == int(guild_id))
        )
        if ticker:
            evidence_q = evidence_q.filter(DiscordEvidenceTarget.symbol == ticker)
        elif issuer_id is not None:
            evidence_q = evidence_q.filter(DiscordEvidenceTarget.issuer_id == issuer_id)
        else:
            evidence_q = evidence_q.filter(false())
        evidence_count = sum(1 for row in evidence_q.limit(1000).all() if evidence_is_public(row))

        memberships: list[str] = []
        if ticker:
            rows = (
                session.query(DiscordWatchlistEntry)
                .filter_by(guild_id=int(guild_id), symbol=ticker, removed_at=None)
                .order_by(DiscordWatchlistEntry.list_kind.asc(), DiscordWatchlistEntry.sector_key.asc())
                .limit(8)
                .all()
            )
            for row in rows:
                if row.list_kind == "exec":
                    continue
                label = str(row.sector or row.list_kind or "").strip()
                subgroup = str(row.subgroup or "").strip()
                memberships.append(f"{label} / {subgroup}" if subgroup and subgroup != "General" else label)

        return PublicCompanyProfile(
            name=str(
                getattr(issuer, "display_name", None)
                or getattr(company, "name", None)
                or raw
            ),
            ticker=ticker,
            exchange=(str(getattr(company, "exchange", "") or "") or None),
            sector=(str(getattr(issuer, "sector", None) or getattr(company, "sector", None) or "") or None),
            industry=(str(getattr(issuer, "industry", None) or getattr(company, "industry", None) or "") or None),
            website=(str(getattr(issuer, "ir_website", None) or getattr(company, "ir_url", None) or "") or None),
            report_count=int(report_count),
            transcript_count=int(transcript_count),
            evidence_count=int(evidence_count),
            watchlist_memberships=memberships,
        )


def ticker_brief(*, guild_id: int, ticker: str, evidence_limit: int = 8) -> PublicTickerBrief:
    sym = services.normalize_symbol(ticker)
    if not sym:
        raise ValueError("Missing ticker.")
    SessionLocal = session_factory()
    with SessionLocal() as session:
        company = session.query(Company).filter(Company.ticker == sym).order_by(Company.id.asc()).first()
        rows = (
            session.query(DiscordWatchlistEntry)
            .filter(
                DiscordWatchlistEntry.guild_id == int(guild_id),
                DiscordWatchlistEntry.symbol == sym,
                DiscordWatchlistEntry.removed_at.is_(None),
                DiscordWatchlistEntry.list_kind.in_(["community", "masterlist"]),
            )
            .order_by(DiscordWatchlistEntry.list_kind.asc(), DiscordWatchlistEntry.sector_key.asc(), DiscordWatchlistEntry.subgroup_key.asc())
            .limit(50)
            .all()
        )
        memberships = [_membership_label(row) for row in rows if row.list_kind == "community"]
        themes = [_membership_label(row) for row in rows if row.list_kind == "masterlist"]
        company_name = str(getattr(company, "name", None) or "").strip() or None
        if company_name is None:
            company_name = next((str(row.company_name).strip() for row in rows if str(row.company_name or "").strip()), None)
        return PublicTickerBrief(
            ticker=sym,
            company_name=company_name,
            watchlist_memberships=memberships[:8],
            themes=themes[:8],
            evidence=_public_evidence_results(session, guild_id=int(guild_id), symbol=sym, limit=evidence_limit),
        )


def public_digest(*, guild_id: int, days: int = 7, limit: int = 10) -> list[PublicSearchResult]:
    days_v = max(1, min(int(days or 7), 30))
    limit_v = max(1, min(int(limit or 10), 25))
    cutoff = utcnow() - timedelta(days=days_v)
    out: list[PublicSearchResult] = []
    SessionLocal = session_factory()
    with SessionLocal() as session:
        evidence_rows = (
            session.query(DiscordEvidenceItem, DiscordEvidenceTarget)
            .outerjoin(DiscordEvidenceTarget, DiscordEvidenceTarget.evidence_id == DiscordEvidenceItem.id)
            .filter(DiscordEvidenceItem.guild_id == int(guild_id))
            .filter(DiscordEvidenceItem.updated_at >= cutoff)
            .order_by(DiscordEvidenceItem.updated_at.desc(), DiscordEvidenceItem.id.desc())
            .limit(limit_v * 3)
            .all()
        )
        seen: set[int] = set()
        for evidence, target in evidence_rows:
            if int(evidence.id) in seen or not evidence_is_public(evidence):
                continue
            seen.add(int(evidence.id))
            out.append(
                PublicSearchResult(
                    kind=str(evidence.source_type or "evidence"),
                    title=str(evidence.title or evidence.url_canonical or "Evidence"),
                    url=str(evidence.url_canonical or evidence.url_raw or "") or None,
                    summary=_short(evidence.note, 180) if evidence.note else None,
                    symbol=(services.normalize_symbol(getattr(target, "symbol", "") or "") or None),
                    updated_at=evidence.updated_at,
                )
            )

        reports = (
            session.query(ResearchReport)
            .filter(ResearchReport.status == "active")
            .filter(ResearchReport.latest_published_version_id.is_not(None))
            .filter(ResearchReport.updated_at >= cutoff)
            .order_by(ResearchReport.updated_at.desc(), ResearchReport.id.desc())
            .limit(limit_v)
            .all()
        )
        for report in reports:
            out.append(
                PublicSearchResult(
                    kind="report",
                    title=str(report.title or report.slug or "Research report"),
                    url=_report_url(report),
                    summary=str(report.subject_key or "") or None,
                    updated_at=report.updated_at,
                )
            )

    out.sort(key=lambda item: item.updated_at or datetime.min, reverse=True)
    return out[:limit_v]


def compare_companies(*, guild_id: int, left: str, right: str) -> PublicComparison:
    left_profile = company_profile(guild_id=guild_id, subject=left)
    right_profile = company_profile(guild_id=guild_id, subject=right)
    SessionLocal = session_factory()
    with SessionLocal() as session:
        left_evidence = _public_evidence_results(session, guild_id=int(guild_id), symbol=left_profile.ticker, limit=3)
        right_evidence = _public_evidence_results(session, guild_id=int(guild_id), symbol=right_profile.ticker, limit=3)
    return PublicComparison(
        left=left_profile,
        right=right_profile,
        left_recent_evidence=left_evidence,
        right_recent_evidence=right_evidence,
    )


def _alert_summary(chain: SupplyChain, rule: SupplyChainAlertRule) -> PublicAlertSummary:
    scope_type = str(rule.scope_type or "chain").strip()
    scope_key = str(rule.scope_key or "chain").strip()
    scope = scope_type if scope_key == "chain" else f"{scope_type}:{scope_key}"
    threshold = rule.threshold_value
    threshold_s = f"{threshold:g}" if isinstance(threshold, (int, float)) else str(threshold)
    window = f" over {int(rule.window_days)}d" if rule.window_days else ""
    return PublicAlertSummary(
        name=str(rule.name or "Alert"),
        chain_title=str(chain.title or chain.slug),
        chain_slug=str(chain.slug or ""),
        scope=scope,
        trigger=f"{rule.metric_kind} {rule.operator} {threshold_s}{window}",
        status=str(rule.status or "active"),
        last_triggered_at=rule.last_triggered_at,
    )


def public_alerts(*, query: str | None = None, limit: int = 10) -> list[PublicAlertSummary]:
    q = str(query or "").strip()
    limit_v = max(1, min(int(limit or 10), 25))
    SessionLocal = session_factory()
    with SessionLocal() as session:
        rows_q = (
            session.query(SupplyChainAlertRule, SupplyChain)
            .join(SupplyChain, SupplyChain.id == SupplyChainAlertRule.chain_id)
            .filter(SupplyChainAlertRule.status == "active")
            .filter(SupplyChain.status == "active")
        )
        if q:
            pattern = f"%{q}%"
            rows_q = rows_q.filter(
                or_(
                    SupplyChain.title.ilike(pattern),
                    SupplyChain.slug.ilike(pattern),
                    SupplyChain.product.ilike(pattern),
                    SupplyChainAlertRule.name.ilike(pattern),
                    SupplyChainAlertRule.metric_kind.ilike(pattern),
                )
            )
        rows = (
            rows_q.order_by(SupplyChainAlertRule.updated_at.desc(), SupplyChainAlertRule.id.desc())
            .limit(limit_v * 3)
            .all()
        )
        out = []
        for rule, chain in rows:
            if not supply_chain_is_public(chain):
                continue
            if latest_chain_snapshot(session, int(chain.id), status="published") is None:
                continue
            out.append(_alert_summary(chain, rule))
    return out[:limit_v]


def _public_supply_chain_view(session: Any, chain: SupplyChain) -> PublicSupplyChainView | None:
    if not supply_chain_is_public(chain):
        return None
    published = latest_chain_snapshot(session, int(chain.id), status="published")
    if published is None:
        return None
    payload = serialize_supply_chain(session, chain, snapshot=published, include_graph=True)
    graph = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    nodes = [dict(row) for row in (graph.get("nodes") or []) if isinstance(row, dict)]

    def _node_score(node: dict[str, Any]) -> float:
        vals = []
        for key in ("bottleneck", "criticality", "pricing_power", "confidence"):
            try:
                if node.get(key) is not None:
                    vals.append(float(node.get(key)))
            except Exception:
                continue
        return max(vals) if vals else 0.0

    key_nodes: list[dict[str, Any]] = []
    for node in sorted(nodes, key=_node_score, reverse=True)[:5]:
        key_nodes.append(
            {
                "name": str(node.get("name") or node.get("id") or "Node"),
                "layer": str(node.get("layer") or node.get("layer_key") or ""),
                "type": str(node.get("type") or node.get("node_type") or ""),
                "manufacturer": str(node.get("manufacturer") or ""),
                "location": str(node.get("location") or ""),
                "bottleneck": node.get("bottleneck"),
            }
        )

    alerts = [
        _alert_summary(chain, rule)
        for rule in (
            session.query(SupplyChainAlertRule)
            .filter_by(chain_id=int(chain.id), status="active")
            .order_by(SupplyChainAlertRule.updated_at.desc(), SupplyChainAlertRule.id.asc())
            .limit(5)
            .all()
        )
    ]
    version = payload.get("version") if isinstance(payload.get("version"), dict) else {}
    version_label = None
    if version:
        version_label = f"{version.get('status') or 'published'} v{version.get('version')}"
    return PublicSupplyChainView(
        id=int(chain.id),
        slug=str(chain.slug or ""),
        title=str(chain.title or chain.slug or f"Supply chain {chain.id}"),
        description=(str(chain.description).strip() if chain.description else None),
        category=(str(chain.category).strip() if chain.category else None),
        product=(str(chain.product).strip() if chain.product else None),
        counts={str(k): int(v or 0) for k, v in dict(payload.get("counts") or {}).items()},
        version=version_label,
        key_nodes=key_nodes,
        alerts=alerts,
    )


def supply_chain_views(*, query: str | None = None, limit: int = 5) -> list[PublicSupplyChainView]:
    q = str(query or "").strip()
    limit_v = max(1, min(int(limit or 5), 10))
    SessionLocal = session_factory()
    with SessionLocal() as session:
        chain_q = session.query(SupplyChain).filter(SupplyChain.status == "active")
        if q:
            pattern = f"%{q}%"
            chain_q = chain_q.filter(
                or_(
                    SupplyChain.title.ilike(pattern),
                    SupplyChain.slug.ilike(pattern),
                    SupplyChain.description.ilike(pattern),
                    SupplyChain.product.ilike(pattern),
                    SupplyChain.category.ilike(pattern),
                )
            )
        rows = chain_q.order_by(SupplyChain.updated_at.desc(), SupplyChain.id.desc()).limit(limit_v * 3).all()
        out: list[PublicSupplyChainView] = []
        for chain in rows:
            view = _public_supply_chain_view(session, chain)
            if view is None:
                continue
            out.append(view)
            if len(out) >= limit_v:
                break
    return out
