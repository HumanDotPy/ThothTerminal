from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from thoth_terminal.local_agent_service import search_objects_for_agent
from thoth_terminal.models import Document, KnowledgeAsset, KnowledgeAssetVersion, Note, ResearchReport, ResearchReportVersion, SupplyChain, Thesis


SOURCE_TIER_PRIMARY = "primary/company/regulatory/customer data"
SOURCE_TIER_TECHNICAL = "technical paper/conference material"
SOURCE_TIER_INSTITUTIONAL = "institutional analyst synthesis"
SOURCE_TIER_VETTED_RELAY = "vetted specialist analyst relay"
SOURCE_TIER_REPORT_MEMORY = "thoth report memory"
SOURCE_TIER_GAP = "gap/speculation"

QUERY_STOPWORDS = {
    "about",
    "across",
    "advanced",
    "analysis",
    "commercialization",
    "company",
    "focus",
    "landscape",
    "material",
    "materials",
    "pack",
    "packaging",
    "report",
    "roadmap",
    "source",
    "supply",
    "technical",
    "technology",
    "through",
}

STANDALONE_ACRONYM_MAX_CHARS = 5


def _text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _document_source_url(row: Document) -> str | None:
    """Return the best citation route without replacing stable local identity.

    Local research-library documents deliberately keep ``canonical_url`` as a
    file URI so path-based identity survives content and manifest changes. A
    hash-verified publisher URL is stored in ``extra.source_url`` (and normally
    mirrored in ``url``), so source packs must consult it before the identity
    URL.
    """

    extra = row.extra if isinstance(row.extra, dict) else {}
    for value in (extra.get("source_url"), row.url, row.canonical_url):
        candidate = _text(value)
        if candidate:
            return candidate
    return None


def _surface_has_any(surface: str, markers: tuple[str, ...]) -> bool:
    """Match provenance markers as tokens, not substrings in other names.

    This is especially important for conference acronyms: ``iedm`` is present
    inside ``appliedmaterials.com`` but that vendor domain is not an IEDM
    proceeding. Punctuation remains a valid boundary, so URL path/domain tokens
    such as ``/iedm-2025`` and ``ieee.org`` still match.
    """

    lowered = str(surface or "").lower()
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(marker.lower())}(?![a-z0-9])", lowered) is not None
        for marker in markers
    )


def _dt(value: object) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _snippet(text: object, query: str, *, size: int = 900) -> str:
    body = str(text or "").strip()
    if not body:
        return ""
    needle = str(query or "").strip().lower()
    idx = body.lower().find(needle) if needle else -1
    if idx < 0:
        return _text(body[:size])
    start = max(0, idx - size // 3)
    return _text(body[start : start + size])


def _matches_query(text: object, query: str) -> bool:
    body = str(text or "")
    needle = _text(query)
    if not needle:
        return True
    if re.fullmatch(r"[A-Z0-9]{2,4}", needle):
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(needle)}(?![A-Za-z0-9])", body, flags=re.IGNORECASE) is not None
    return needle.lower() in body.lower()


def _query_terms(query: str, *, max_terms: int = 16) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9+.-]{1,}", str(query or "")):
        term = raw.strip("-_.")
        if len(term) < 3:
            continue
        if re.fullmatch(r"20\d{2}", term):
            continue
        if term.lower() in QUERY_STOPWORDS:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= max(1, int(max_terms or 16)):
            break
    return terms


def _item_relevance(item: dict[str, Any], query: str) -> int:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    surface = " ".join(
        str(part or "")
        for part in (
            item.get("title"),
            item.get("snippet"),
            item.get("url"),
            metadata.get("document_kind"),
            metadata.get("source"),
        )
    ).lower()
    title_surface = str(item.get("title") or "").lower()
    score = 0
    if metadata.get("exact_query_match"):
        score += 60
        if _text(metadata.get("matched_query")).lower() == _text(query).lower():
            score += 200
    needle = _text(query).lower()
    if needle and needle in surface:
        score += 20
    for term in _query_terms(query, max_terms=32):
        lowered = term.lower()
        if lowered in surface:
            score += 4
        if lowered in title_surface:
            score += 3
    if metadata.get("document_kind") in {"research_source_card", "technical_source_card", "conference_paper_card"}:
        score += 30
    if metadata.get("source_pack_seed") or metadata.get("research_library"):
        score += 16
    if item.get("source_tier") in {SOURCE_TIER_PRIMARY, SOURCE_TIER_TECHNICAL}:
        score += 8
    if item.get("source_tier") == SOURCE_TIER_REPORT_MEMORY:
        score -= 16
    return score


def _query_variants(query: str, *, report: ResearchReport | None = None, subject: str | None = None, limit: int = 12) -> list[str]:
    raw_parts = [query, subject]
    if report is not None:
        raw_parts.extend([report.title, report.slug, report.subject_key])
    raw = " ".join(_text(part) for part in raw_parts if _text(part))
    variants: list[str] = []

    def add(value: object) -> None:
        text = _text(value)
        if len(text) < 3:
            return
        lowered = text.lower()
        if lowered in {row.lower() for row in variants}:
            return
        variants.append(text)

    def allow_standalone_token(token: str) -> bool:
        if token.isupper() and token.isalpha() and len(token) > STANDALONE_ACRONYM_MAX_CHARS:
            return False
        return True

    add(query)
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9+.-]{1,}", raw)
    salient: list[str] = []
    for token in tokens:
        normalized = token.strip("-_.")
        if len(normalized) < 3:
            continue
        if re.fullmatch(r"20\d{2}", normalized):
            continue
        if normalized.lower() in QUERY_STOPWORDS:
            continue
        if normalized not in salient:
            salient.append(normalized)
    for token in salient:
        if allow_standalone_token(token) and (token.isupper() or any(ch.isdigit() for ch in token)):
            add(token)
    for left, right in zip(salient, salient[1:]):
        if left.lower() in QUERY_STOPWORDS or right.lower() in QUERY_STOPWORDS:
            continue
        add(f"{left} {right}")
    for token in salient:
        if allow_standalone_token(token):
            add(token)
    return variants[: max(1, min(24, int(limit or 12)))]


def _source_tier_for_document(row: Document) -> str:
    extra = row.extra if isinstance(row.extra, dict) else {}
    explicit_tier = _text(extra.get("source_tier"))
    if explicit_tier:
        lowered_tier = explicit_tier.lower()
        if lowered_tier in {
            SOURCE_TIER_PRIMARY,
            SOURCE_TIER_TECHNICAL,
            SOURCE_TIER_INSTITUTIONAL,
            SOURCE_TIER_VETTED_RELAY,
            SOURCE_TIER_REPORT_MEMORY,
            SOURCE_TIER_GAP,
        }:
            return lowered_tier
        if any(token in lowered_tier for token in ("regulatory", "customer", "sec", "filing", "official")) or any(
            token in lowered_tier
            for token in (
                "company disclosure",
                "company technical disclosure",
                "company technical product",
                "company technical process",
                "company technical white",
                "company technical announcement",
                "company collaboration",
                "company investor technical",
                "company product launch",
                "company consortium",
                "issuer disclosure",
                "issuer product",
            )
        ):
            return SOURCE_TIER_PRIMARY
        if any(token in lowered_tier for token in ("paper", "conference", "proceeding", "technical", "academic", "ieee", "ofc", "isscc")):
            return SOURCE_TIER_TECHNICAL
        if "institutional" in lowered_tier:
            return SOURCE_TIER_INSTITUTIONAL
        if any(token in lowered_tier for token in ("relay", "specialist", "industry", "analyst")):
            return SOURCE_TIER_VETTED_RELAY
    content_kind = _text(extra.get("content_kind")).lower()
    if any(token in content_kind for token in ("paper", "conference", "proceeding", "source_card")):
        return SOURCE_TIER_TECHNICAL
    source_name = _text(getattr(row.source, "name", "") or getattr(row.source, "slug", "")).lower()
    title = _text(row.title).lower()
    url = _text(_document_source_url(row)).lower()
    surface = f"{source_name} {title} {url}"
    if _surface_has_any(surface, ("x.com", "twitter")):
        return SOURCE_TIER_VETTED_RELAY
    if _surface_has_any(
        surface,
        (
            "ieee",
            "isscc",
            "iedm",
            "ectc",
            "ofcconference",
            "ofc conference",
            "optica.org",
            "arxiv",
            "acm.org",
            "doi.org",
            "proceedings",
            "hotchips.org",
            "hot chips",
            "vlsisymposium",
            "vlsi symposium",
            "opencompute.org",
            "ocp global summit",
        ),
    ):
        return SOURCE_TIER_TECHNICAL
    if _surface_has_any(surface, ("goldman", "morgan", "ubs", "jpmorgan", "jefferies", "barclays", "bernstein", "citigroup", "citi", "bofa", "nomura", "mizuho", "clsa")):
        return SOURCE_TIER_INSTITUTIONAL
    if _surface_has_any(
        surface,
        (
            "sec.gov",
            "edgar",
            "press release",
            "investor relations",
            "news release",
            "samsungsemiconductor.com",
            "semiconductor.samsung.com",
            "samsung.com/semiconductor",
            "skhynix.com",
            "skhynix-newsroom.com",
            "micron.com",
            "broadcom.com",
            "intel.com",
            "lpkf.com",
            "ajinomoto.com",
        ),
    ):
        return SOURCE_TIER_PRIMARY
    if _surface_has_any(surface, ("semianalysis", "semicon", "thelec", "digitimes", "trendforce")):
        return SOURCE_TIER_VETTED_RELAY
    return SOURCE_TIER_VETTED_RELAY if row.kind in {"social", "analyst"} else SOURCE_TIER_GAP


def _pack_item(
    *,
    kind: str,
    object_id: int | None,
    title: str,
    source_tier: str,
    snippet: str,
    url: str | None = None,
    published_at: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "object_id": object_id,
        "title": title,
        "source_tier": source_tier,
        "snippet": snippet[:1200],
        "url": url,
        "published_at": published_at,
        "metadata": metadata or {},
    }


def _latest_report_version(session: Session, report_id: int) -> ResearchReportVersion | None:
    return (
        session.query(ResearchReportVersion)
        .filter_by(report_id=int(report_id), status="published")
        .order_by(ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc())
        .first()
    )


def _resolve_query(session: Session, *, query: str | None = None, report_id: int | None = None, subject: str | None = None) -> tuple[str, ResearchReport | None]:
    report: ResearchReport | None = session.get(ResearchReport, int(report_id)) if report_id is not None else None
    if report is None and subject:
        pattern = f"%{subject}%"
        report = (
            session.query(ResearchReport)
            .filter(or_(ResearchReport.slug.ilike(pattern), ResearchReport.title.ilike(pattern), ResearchReport.subject_key.ilike(pattern)))
            .order_by(ResearchReport.updated_at.desc())
            .first()
        )
    parts = [query, subject]
    if report is not None:
        parts.extend([report.title, report.slug, report.subject_key])
    resolved = " ".join(_text(part) for part in parts if _text(part)).strip()
    return resolved, report


def _add_report_items(session: Session, *, query: str, report: ResearchReport | None, out: list[dict[str, Any]], include_target_report: bool = False) -> None:
    if include_target_report and report is not None and report.id is not None:
        version = _latest_report_version(session, int(report.id))
        if version is not None:
            out.append(
                _pack_item(
                    kind="research_report",
                    object_id=int(report.id),
                    title=report.title,
                    source_tier=SOURCE_TIER_REPORT_MEMORY,
                    snippet=_snippet(version.content_md, query, size=1400),
                    url=f"/reports/{report.slug}",
                    published_at=_dt(version.published_at or version.updated_at),
                    metadata={"version_id": int(version.id), "content_chars": len(version.content_md or "")},
                )
            )
    pattern = f"%{query}%"
    for version, row in (
        session.query(ResearchReportVersion, ResearchReport)
        .join(ResearchReport, ResearchReport.id == ResearchReportVersion.report_id)
        .filter(ResearchReportVersion.status == "published")
        .filter(ResearchReportVersion.content_md.ilike(pattern))
        .order_by(ResearchReportVersion.updated_at.desc(), ResearchReportVersion.id.desc())
        .limit(5)
        .all()
    ):
        if not _matches_query(version.content_md, query):
            continue
        out.append(
            _pack_item(
                kind="research_report",
                object_id=int(row.id),
                title=row.title,
                source_tier=SOURCE_TIER_REPORT_MEMORY,
                snippet=_snippet(version.content_md, query, size=1000),
                url=f"/reports/{row.slug}",
                published_at=_dt(version.published_at or version.updated_at),
                metadata={"version_id": int(version.id)},
            )
        )


def _add_document_items(session: Session, *, query: str, out: list[dict[str, Any]], limit: int) -> None:
    pattern = f"%{query}%"
    rows = (
        session.query(Document)
        .filter(or_(Document.title.ilike(pattern), Document.normalized_text.ilike(pattern), Document.translation_en.ilike(pattern)))
        .order_by(Document.published_at.desc().nullslast(), Document.ingested_at.desc())
        .limit(max(1, min(20, limit)))
        .all()
    )
    for row in rows:
        body = row.translation_en or row.normalized_text or row.raw_text
        surface = f"{row.title or ''}\n{body or ''}"
        if not _matches_query(surface, query):
            continue
        exact_query_match = bool(_text(query)) and _text(query).lower() in surface.lower()
        out.append(
            _pack_item(
                kind="document",
                object_id=int(row.id),
                title=row.title or f"Document {row.id}",
                source_tier=_source_tier_for_document(row),
                snippet=_snippet(body, query),
                url=_document_source_url(row),
                published_at=_dt(row.published_at or row.ingested_at),
                metadata={
                    "source": getattr(row.source, "name", None) or getattr(row.source, "slug", None),
                    "document_kind": row.kind,
                    "matched_query": query,
                    "exact_query_match": exact_query_match,
                    "source_pack_seed": bool(row.extra.get("source_pack_seed")) if isinstance(row.extra, dict) else False,
                    "research_library": bool(row.extra.get("research_library")) if isinstance(row.extra, dict) else False,
                },
            )
        )


def _add_source_seed_items(session: Session, *, query: str, out: list[dict[str, Any]], limit: int) -> None:
    terms = _query_terms(query, max_terms=40)
    if not terms:
        return
    filters = []
    for term in terms:
        pattern = f"%{term}%"
        filters.append(or_(Document.title.ilike(pattern), Document.normalized_text.ilike(pattern), Document.translation_en.ilike(pattern)))
    rows = (
        session.query(Document)
        .filter(Document.kind.in_(("research_source_card", "technical_source_card", "conference_paper_card")))
        .filter(or_(*filters))
        .order_by(Document.published_at.desc().nullslast(), Document.ingested_at.desc(), Document.id.desc())
        .limit(max(20, min(200, int(limit or 20) * 6)))
        .all()
    )
    for row in rows:
        body = row.translation_en or row.normalized_text or row.raw_text
        out.append(
            _pack_item(
                kind="document",
                object_id=int(row.id),
                title=row.title or f"Document {row.id}",
                source_tier=_source_tier_for_document(row),
                snippet=_snippet(body, query),
                url=_document_source_url(row),
                published_at=_dt(row.published_at or row.ingested_at),
                metadata={
                    "source": getattr(row.source, "name", None) or getattr(row.source, "slug", None),
                    "document_kind": row.kind,
                    "source_pack_seed": bool(row.extra.get("source_pack_seed")) if isinstance(row.extra, dict) else False,
                    "research_library": bool(row.extra.get("research_library")) if isinstance(row.extra, dict) else False,
                },
            )
        )


def _add_note_thesis_items(session: Session, *, query: str, out: list[dict[str, Any]], limit: int) -> None:
    pattern = f"%{query}%"
    for row in (
        session.query(Thesis)
        .filter(or_(Thesis.title.ilike(pattern), Thesis.statement.ilike(pattern)))
        .order_by(Thesis.importance.asc(), Thesis.updated_at.desc())
        .limit(max(1, min(8, limit)))
        .all()
    ):
        if not _matches_query(f"{row.title or ''}\n{row.statement or ''}", query):
            continue
        out.append(_pack_item(kind="thesis", object_id=int(row.id), title=row.title, source_tier=SOURCE_TIER_REPORT_MEMORY, snippet=_snippet(row.statement, query), metadata={"status": row.status, "importance": row.importance}))
    for row in (
        session.query(Note)
        .filter(or_(Note.title.ilike(pattern), Note.body_md.ilike(pattern)))
        .order_by(Note.importance.asc(), Note.updated_at.desc())
        .limit(max(1, min(8, limit)))
        .all()
    ):
        if not _matches_query(f"{row.title or ''}\n{row.body_md or ''}", query):
            continue
        out.append(_pack_item(kind="note", object_id=int(row.id), title=row.title, source_tier=SOURCE_TIER_REPORT_MEMORY, snippet=_snippet(row.body_md, query), metadata={"note_type": row.note_type, "importance": row.importance}))


def _add_knowledge_supply_items(session: Session, *, query: str, out: list[dict[str, Any]], limit: int) -> None:
    pattern = f"%{query}%"
    for asset, version in (
        session.query(KnowledgeAsset, KnowledgeAssetVersion)
        .join(KnowledgeAssetVersion, KnowledgeAssetVersion.asset_id == KnowledgeAsset.id)
        .filter(KnowledgeAssetVersion.status == "published")
        .filter(or_(KnowledgeAsset.title.ilike(pattern), KnowledgeAsset.summary.ilike(pattern), KnowledgeAssetVersion.content_md.ilike(pattern)))
        .order_by(KnowledgeAsset.updated_at.desc(), KnowledgeAssetVersion.id.desc())
        .limit(max(1, min(6, limit)))
        .all()
    ):
        if not _matches_query(f"{asset.title or ''}\n{asset.summary or ''}\n{version.content_md or ''}", query):
            continue
        out.append(_pack_item(kind="knowledge_asset", object_id=int(asset.id), title=asset.title, source_tier=SOURCE_TIER_REPORT_MEMORY, snippet=_snippet(version.content_md or asset.summary, query), url=f"/knowledge/{asset.slug}", metadata={"asset_kind": asset.kind}))
    for row in (
        session.query(SupplyChain)
        .filter(or_(SupplyChain.title.ilike(pattern), SupplyChain.product.ilike(pattern), SupplyChain.description.ilike(pattern)))
        .order_by(SupplyChain.updated_at.desc())
        .limit(max(1, min(6, limit)))
        .all()
    ):
        if not _matches_query(f"{row.title or ''}\n{row.product or ''}\n{row.description or ''}", query):
            continue
        out.append(_pack_item(kind="supply_chain", object_id=int(row.id), title=row.title, source_tier=SOURCE_TIER_REPORT_MEMORY, snippet=_snippet(row.description or row.product or row.title, query), url=f"/supply-chains/{row.slug}", metadata={"product": row.product}))


def _dedupe_items(items: list[dict[str, Any]], *, limit: int, query: str | None = None) -> list[dict[str, Any]]:
    priority = {
        SOURCE_TIER_PRIMARY: 0,
        SOURCE_TIER_TECHNICAL: 1,
        SOURCE_TIER_INSTITUTIONAL: 2,
        SOURCE_TIER_VETTED_RELAY: 3,
        SOURCE_TIER_REPORT_MEMORY: 4,
        SOURCE_TIER_GAP: 5,
    }
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in sorted(
        items,
        key=lambda row: (
            -_item_relevance(row, str(query or "")),
            priority.get(str(row.get("source_tier")), 9),
            str(row.get("published_at") or ""),
            str(row.get("title") or ""),
        ),
    ):
        key = f"{item.get('kind')}:{item.get('object_id') or item.get('url') or item.get('title')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def render_source_pack_markdown(pack: dict[str, Any]) -> str:
    lines = [
        f"# Source Pack: {pack.get('query')}",
        "",
        str(pack.get("summary") or ""),
        "",
        "## Source Strategy",
        "Items are compacted and tiered as primary/company/regulatory/customer data, institutional analyst synthesis, vetted specialist analyst relay, Thoth report memory, or gap/speculation.",
        "",
    ]
    for tier, rows in (pack.get("tiers") or {}).items():
        lines.extend([f"## {tier}", ""])
        for item in rows:
            lines.append(f"### {item.get('title')}")
            meta = [str(item.get("kind") or ""), f"id={item.get('object_id')}" if item.get("object_id") else "", str(item.get("published_at") or "")]
            lines.append(f"- {' | '.join(part for part in meta if part)}")
            if item.get("url"):
                lines.append(f"- url: {item.get('url')}")
            lines.extend(["", str(item.get("snippet") or ""), ""])
    gaps = pack.get("gaps") if isinstance(pack.get("gaps"), list) else []
    if gaps:
        lines.extend(["## Gaps / Corroboration Needed", ""])
        for gap in gaps:
            lines.append(f"- {gap}")
    return "\n".join(lines).strip() + "\n"


def build_source_pack(
    session: Session,
    *,
    query: str | None = None,
    report_id: int | None = None,
    subject: str | None = None,
    limit: int = 18,
    include_object_search: bool = True,
) -> dict[str, Any]:
    resolved_query, report = _resolve_query(session, query=query, report_id=report_id, subject=subject)
    if not resolved_query:
        raise ValueError("query, report_id, or subject is required.")
    limit_val = max(4, min(40, int(limit or 18)))
    search_queries = _query_variants(resolved_query, report=report, subject=subject, limit=24)
    original_query = _text(query)
    if original_query and original_query.lower() not in {row.lower() for row in search_queries}:
        search_queries = [original_query, *search_queries]
    items: list[dict[str, Any]] = []
    _add_source_seed_items(session, query=resolved_query, out=items, limit=limit_val)
    for idx, search_query in enumerate(search_queries):
        per_query_limit = max(3, min(limit_val, 10 if idx else limit_val))
        document_query_limit = max(per_query_limit, min(40, max(20, limit_val * 3)))
        _add_report_items(session, query=search_query, report=report, out=items, include_target_report=idx == 0)
        if not (idx == 0 and len(_query_terms(search_query)) > 8):
            _add_document_items(session, query=search_query, out=items, limit=document_query_limit)
        _add_note_thesis_items(session, query=search_query, out=items, limit=per_query_limit)
        _add_knowledge_supply_items(session, query=search_query, out=items, limit=per_query_limit)
    object_search_payload: dict[str, Any] = {}
    if include_object_search:
        object_queries = search_queries[: min(5, len(search_queries))]
        object_search_payload = {
            "queries": [
                {
                    "query": search_query,
                    "result": search_objects_for_agent(session, query=search_query, limit=min(8, limit_val), include_neighborhoods=True),
                }
                for search_query in object_queries
            ]
        }
    relevance_query = _text(query) or resolved_query
    items = _dedupe_items(items, limit=limit_val, query=relevance_query)
    tiers: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        tiers.setdefault(str(item.get("source_tier") or SOURCE_TIER_GAP), []).append(item)
    gaps = []
    if not tiers.get(SOURCE_TIER_PRIMARY):
        gaps.append("No primary/company/regulatory/customer source was found in the compact pack.")
    if not tiers.get(SOURCE_TIER_INSTITUTIONAL):
        gaps.append("No institutional analyst synthesis was found in the compact pack.")
    pack = {
        "query": resolved_query,
        "report_id": int(report.id) if report is not None and report.id is not None else report_id,
        "subject": subject or (report.subject_key if report is not None else None),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "summary": f"Compact source pack with {len(items)} items across {len(tiers)} provenance tiers.",
        "items": items,
        "tiers": tiers,
        "gaps": gaps,
        "object_search": object_search_payload,
        "search_queries": search_queries,
        "compact_chars": sum(len(str(item.get("snippet") or "")) for item in items),
    }
    pack["markdown"] = render_source_pack_markdown(pack)
    return pack
