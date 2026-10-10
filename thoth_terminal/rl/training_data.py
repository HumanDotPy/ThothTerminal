from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from thoth_terminal.agent.evidence_policy import (
    document_evidence_metadata,
    precedence_bucket,
    source_quality_score,
    translation_risk_score,
)
from thoth_terminal.config import settings
from thoth_terminal.db import session_scope, utcnow
from thoth_terminal.discord.models import DiscordLinkInboxItem
from thoth_terminal.models import (
    Document,
    DocumentEntity,
    Entity,
    LocalAgentDocumentCompression,
    LocalAgentEntityRoute,
    ResearchReport,
    ResearchReportVersion,
    Source,
)
from thoth_terminal.text import clean_whitespace, sha256_text


TASK_DOCUMENT_REFINERY = "document_refinery"
TASK_LINK_TRIAGE = "link_triage"
TASK_ENTITY_RESOLUTION = "entity_resolution"
TASK_RESEARCH_QA = "research_qa"
TASK_RESEARCH_MCQ = "research_mcq"
TASK_RESEARCH_MULTI_SELECT = "research_multi_select"
TASK_REPORT_MEMORY_PLAN = "report_memory_plan"
SUPPORTED_TASKS = (
    TASK_DOCUMENT_REFINERY,
    TASK_LINK_TRIAGE,
    TASK_ENTITY_RESOLUTION,
    TASK_RESEARCH_QA,
    TASK_RESEARCH_MCQ,
    TASK_RESEARCH_MULTI_SELECT,
    TASK_REPORT_MEMORY_PLAN,
)

SCHEMA_VERSION = 1
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+\-/.]{2,}", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_STOPWORDS = {
    "about",
    "after",
    "again",
    "among",
    "analyst",
    "analysts",
    "around",
    "because",
    "could",
    "from",
    "have",
    "into",
    "market",
    "news",
    "report",
    "reports",
    "said",
    "says",
    "that",
    "their",
    "there",
    "these",
    "they",
    "this",
    "update",
    "will",
    "with",
}


def _compact(value: object, *, max_chars: int) -> str:
    text = clean_whitespace(str(value or ""))
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip(" ,;:-") + "..."


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_loads_maybe(value: object) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _completion_text(completion: object) -> str:
    if isinstance(completion, str):
        return completion
    if isinstance(completion, dict):
        for key in ("content", "output_text", "text"):
            if completion.get(key) is not None:
                return str(completion.get(key) or "")
        return _json_dumps(completion)
    if isinstance(completion, list):
        for item in reversed(completion):
            if isinstance(item, dict) and item.get("content") is not None:
                return str(item.get("content") or "")
        if completion:
            return _completion_text(completion[-1])
    return str(completion or "")


def _parse_completion_json(completion: object) -> tuple[dict[str, Any] | None, str]:
    text = _completion_text(completion).strip()
    parsed = _json_loads_maybe(text)
    if isinstance(parsed, dict):
        return parsed, text
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        parsed = _json_loads_maybe(text[start : end + 1])
        if isinstance(parsed, dict):
            return parsed, text
    return None, text


def _tokens(value: object) -> set[str]:
    out: set[str] = set()
    for raw in _TOKEN_RE.findall(str(value or "").lower()):
        token = raw.strip(".$/-")
        if len(token) >= 3 and token not in _STOPWORDS:
            out.add(token)
    return out


def _f1(pred: Iterable[object], gold: Iterable[object]) -> float:
    pred_set = {str(v).strip().lower() for v in pred if str(v).strip()}
    gold_set = {str(v).strip().lower() for v in gold if str(v).strip()}
    if not pred_set and not gold_set:
        return 1.0
    if not pred_set or not gold_set:
        return 0.0
    overlap = len(pred_set & gold_set)
    precision = overlap / len(pred_set)
    recall = overlap / len(gold_set)
    if precision + recall <= 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _text_f1(pred: object, gold: object) -> float:
    return _f1(_tokens(pred), _tokens(gold))


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: object) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _source_name(doc: Document) -> str:
    return str(doc.source.name if doc.source else "").strip()


def _doc_text(doc: Document, *, max_chars: int = 6000) -> str:
    text = clean_whitespace(doc.translation_en or doc.normalized_text or doc.raw_text or "")
    return text[:max_chars]


def _doc_entity_names(doc: Document) -> list[str]:
    names: list[str] = []
    for link in doc.entity_links or []:
        entity = link.entity
        name = str(entity.canonical_name if entity else "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _candidate_sentences(title: str, text: str) -> list[str]:
    parts = [title, *_SENTENCE_SPLIT_RE.split(text)]
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        row = clean_whitespace(part)
        if len(row) < 20:
            continue
        key = row.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= 6:
            break
    return out


def _topic_tokens(*parts: object, entity_names: list[str] | None = None, limit: int = 6) -> list[str]:
    topics: list[str] = []
    for name in entity_names or []:
        if name and name not in topics:
            topics.append(name)
            if len(topics) >= limit:
                return topics
    scores: dict[str, int] = {}
    display: dict[str, str] = {}
    for idx, part in enumerate(parts):
        for raw in _TOKEN_RE.findall(str(part or "")):
            norm = raw.strip("$").lower()
            if len(norm) < 3 or norm in _STOPWORDS:
                continue
            scores[norm] = scores.get(norm, 0) + (3 if idx == 0 else 1)
            display.setdefault(norm, raw.upper() if raw.startswith("$") or raw.isupper() else raw)
    for key, _score in sorted(scores.items(), key=lambda item: (-item[1], item[0])):
        value = display.get(key) or key
        if value not in topics:
            topics.append(value)
        if len(topics) >= limit:
            break
    return topics


def build_document_refinery_answer(doc: Document) -> dict[str, Any]:
    title = clean_whitespace(doc.title or doc.url or f"Document {doc.id}")
    text = _doc_text(doc)
    metadata = document_evidence_metadata(doc)
    sentences = _candidate_sentences(title, text)
    signal = _compact(sentences[0] if sentences else title, max_chars=140)
    summary_seed = next((row for row in sentences[1:] if row.lower() != signal.lower()), signal)
    summary = _compact(summary_seed, max_chars=280)
    entity_names = _doc_entity_names(doc)
    topics = _topic_tokens(signal, summary, text[:1200], entity_names=entity_names, limit=6)
    key_points = [_compact(row, max_chars=180) for row in sentences[2:5]]
    if not key_points and summary and summary != signal:
        key_points = [summary]
    bucket = str(metadata.get("precedence_bucket") or precedence_bucket(doc))
    needs_corroboration = bool(metadata.get("needs_corroboration"))
    if needs_corroboration:
        why = "Uncertain evidence; preserve owner and relay provenance, then use bounded model updates where appropriate. Independent corroboration can strengthen confidence later."
    else:
        why = "Potentially thesis-relevant evidence; compare its claim and assumptions with existing objects."
    quality = _safe_float(metadata.get("source_quality_score"), source_quality_score(doc))
    risk = _safe_float(metadata.get("translation_risk"), translation_risk_score(doc))
    confidence = max(0.35, min(0.92, 0.50 + quality * 0.35 - risk * 0.18))
    return {
        "signal": signal,
        "summary": summary,
        "why_it_matters": _compact(why, max_chars=220),
        "key_points": key_points[:3],
        "topics": topics,
        "confidence": round(confidence, 3),
    }


def _document_refinery_prompt(doc: Document) -> list[dict[str, str]]:
    metadata = document_evidence_metadata(doc)
    user = {
        "task": TASK_DOCUMENT_REFINERY,
        "source": _source_name(doc),
        "published_at": doc.published_at.isoformat() if doc.published_at else None,
        "language": doc.language,
        "evidence_metadata": metadata,
        "entities": _doc_entity_names(doc),
        "title": clean_whitespace(doc.title or ""),
        "text": _doc_text(doc, max_chars=5000),
        "output_schema": {
            "signal": "string <= 140 chars",
            "summary": "string <= 280 chars",
            "why_it_matters": "string <= 220 chars",
            "key_points": "array of 0-3 strings",
            "topics": "array of 1-6 short strings",
            "confidence": "float 0.0-1.0",
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "You are ThothTerminal's document refinery. Return JSON only. "
                "Do not invent entities, dates, numbers, causality, or source certainty."
            ),
        },
        {"role": "user", "content": _json_dumps(user)},
    ]


def _target_link_action(status: str | None) -> str:
    raw = str(status or "").strip().lower()
    if "ignored" in raw:
        return "ignore"
    if "queue" in raw or raw == "queued":
        return "queue"
    if "resolution" in raw:
        return "needs_human"
    if "ingested" in raw or raw in {"saved", "actioned"}:
        return "ingest"
    return "queue"


def _link_ticker(row: DiscordLinkInboxItem) -> str | None:
    decision = row.agentic_decision_json if isinstance(row.agentic_decision_json, dict) else {}
    ticker = str(decision.get("ticker") or row.deterministic_ticker or "").strip().upper()
    return ticker or None


def build_link_triage_answer(row: DiscordLinkInboxItem) -> dict[str, Any]:
    action = _target_link_action(row.status)
    confidence = {
        "ingest": 0.84,
        "queue": 0.62,
        "needs_human": 0.54,
        "ignore": 0.80,
    }.get(action, 0.55)
    if row.deterministic_ticker and action == "ingest":
        confidence = 0.92
    return {
        "action": action,
        "ticker": _link_ticker(row),
        "confidence": confidence,
        "rationale": f"Historical inbox status mapped to {action}.",
    }


def _link_triage_prompt(row: DiscordLinkInboxItem) -> list[dict[str, str]]:
    payload = {
        "task": TASK_LINK_TRIAGE,
        "url": row.url_canonical or row.url_raw,
        "host": urlparse(row.url_canonical or row.url_raw or "").netloc.lower(),
        "message_excerpt": clean_whitespace(row.message_excerpt or ""),
        "embed_title": clean_whitespace(row.embed_title or ""),
        "deterministic_ticker": row.deterministic_ticker,
        "seen_count": int(row.seen_count or 0),
        "output_schema": {
            "action": "one of ingest, queue, needs_human, ignore",
            "ticker": "uppercase ticker string or null",
            "confidence": "float 0.0-1.0",
            "rationale": "short string",
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "You triage Discord-collected research links for ThothTerminal. "
                "Return JSON only. Prefer queue or needs_human over overconfident ingestion when evidence is ambiguous."
            ),
        },
        {"role": "user", "content": _json_dumps(payload)},
    ]


def _entity_candidates(session: Session, doc: Document, *, limit: int = 10) -> list[dict[str, Any]]:
    text_tokens = _tokens(" ".join([doc.title or "", _doc_text(doc, max_chars=1500)]))
    rows: list[Entity] = []
    seen: set[int] = set()
    for link in doc.entity_links or []:
        entity = link.entity
        if entity and int(entity.id) not in seen:
            rows.append(entity)
            seen.add(int(entity.id))
    if len(rows) < limit:
        for entity in session.query(Entity).order_by(Entity.canonical_name.asc()).limit(250).all():
            if int(entity.id) in seen:
                continue
            name_tokens = _tokens(entity.canonical_name)
            alias_tokens: set[str] = set()
            for alias in entity.aliases or []:
                alias_tokens |= _tokens(alias.alias)
            if (name_tokens | alias_tokens) & text_tokens or len(rows) < max(4, len(seen) + 2):
                rows.append(entity)
                seen.add(int(entity.id))
            if len(rows) >= limit:
                break
    candidates: list[dict[str, Any]] = []
    for entity in rows[:limit]:
        candidates.append(
            {
                "id": int(entity.id),
                "canonical_name": entity.canonical_name,
                "kind": entity.kind,
                "aliases": [alias.alias for alias in (entity.aliases or [])][:5],
            }
        )
    return candidates


def build_entity_resolution_answer(doc: Document) -> dict[str, Any]:
    ids = sorted({int(link.entity_id) for link in doc.entity_links or []})
    return {
        "entity_ids": ids,
        "confidence": 0.88 if ids else 0.62,
        "rationale": "Use only candidates directly supported by the document title/text.",
    }


def _entity_resolution_prompt(session: Session, doc: Document) -> list[dict[str, str]]:
    payload = {
        "task": TASK_ENTITY_RESOLUTION,
        "document_id": int(doc.id),
        "title": clean_whitespace(doc.title or ""),
        "text": _doc_text(doc, max_chars=3500),
        "candidates": _entity_candidates(session, doc),
        "output_schema": {
            "entity_ids": "array of candidate ids that are truly referenced",
            "confidence": "float 0.0-1.0",
            "rationale": "short string",
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "Resolve ThothTerminal document entities. Return JSON only. "
                "Choose only candidate ids that are explicitly supported; abstain with an empty list when ambiguous."
            ),
        },
        {"role": "user", "content": _json_dumps(payload)},
    ]


def _qa_fact_sentence(doc: Document) -> str:
    title = clean_whitespace(doc.title or doc.url or f"Document {doc.id}")
    text = _doc_text(doc, max_chars=5000)
    sentences = _candidate_sentences(title, text)
    for sentence in sentences:
        if sentence.lower() != title.lower():
            return _compact(sentence, max_chars=360)
    return _compact(sentences[0] if sentences else title, max_chars=360)


def build_research_qa_answer(doc: Document) -> dict[str, Any]:
    fact = _qa_fact_sentence(doc)
    entities = _doc_entity_names(doc)
    title = clean_whitespace(doc.title or doc.url or f"Document {doc.id}")
    if entities:
        question = f"What does this source say about {entities[0]}?"
    else:
        topics = _topic_tokens(title, fact, limit=2)
        subject = " and ".join(topics[:2]) if topics else title
        question = f"What does this source say about {subject}?"
    metadata = document_evidence_metadata(doc)
    quality = _safe_float(metadata.get("source_quality_score"), source_quality_score(doc))
    risk = _safe_float(metadata.get("translation_risk"), translation_risk_score(doc))
    confidence = max(0.35, min(0.92, 0.55 + quality * 0.30 - risk * 0.15))
    return {
        "question": _compact(question, max_chars=180),
        "answer": fact,
        "source_document_ids": [int(doc.id)],
        "topics": _topic_tokens(title, fact, _doc_text(doc, max_chars=1200), entity_names=entities, limit=6),
        "confidence": round(confidence, 3),
    }


def _research_qa_prompt(doc: Document) -> list[dict[str, str]]:
    payload = {
        "task": TASK_RESEARCH_QA,
        "document_id": int(doc.id),
        "source": _source_name(doc),
        "published_at": doc.published_at.isoformat() if doc.published_at else None,
        "evidence_metadata": document_evidence_metadata(doc),
        "entities": _doc_entity_names(doc),
        "title": clean_whitespace(doc.title or ""),
        "text": _doc_text(doc, max_chars=5000),
        "output_schema": {
            "question": "answerable research question grounded in the provided source",
            "answer": "concise answer using only the provided source",
            "source_document_ids": "array containing cited document ids",
            "topics": "array of 1-6 short strings",
            "confidence": "float 0.0-1.0",
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "Generate one grounded ThothTerminal research Q&A pair. Return JSON only. "
                "The answer must be supported by the supplied document and must cite source_document_ids."
            ),
        },
        {"role": "user", "content": _json_dumps(payload)},
    ]


def _qa_supported_statements(title: str, text: str, *, limit: int = 3) -> list[str]:
    statements: list[str] = []
    for sentence in _candidate_sentences(title, text):
        row = _compact(sentence, max_chars=260)
        if row and row.lower() not in {item.lower() for item in statements}:
            statements.append(row)
        if len(statements) >= limit:
            break
    return statements


def _qa_choice_distractors(title: str, text: str, *, limit: int = 3) -> list[str]:
    topics = _topic_tokens(title, text[:1200], limit=4)
    subject = topics[0] if topics else "the subject"
    candidates = [
        f"The source says {subject} is unrelated to the discussed investment topic.",
        "The source says there is no relevant update to evaluate.",
        "The source confirms the issue has already been fully resolved.",
    ]
    out: list[str] = []
    supported_tokens = _tokens(" ".join(_qa_supported_statements(title, text, limit=4)))
    for candidate in candidates:
        if _tokens(candidate) <= supported_tokens:
            continue
        out.append(_compact(candidate, max_chars=220))
        if len(out) >= limit:
            break
    return out


def _choice_rows(correct: list[str], incorrect: list[str]) -> list[dict[str, str]]:
    labels = ["A", "B", "C", "D", "E", "F"]
    texts = [*correct, *incorrect]
    return [{"id": labels[idx], "text": text} for idx, text in enumerate(texts[: len(labels)])]


def build_research_mcq_answer(doc: Document) -> dict[str, Any]:
    title = clean_whitespace(doc.title or doc.url or f"Document {doc.id}")
    text = _doc_text(doc, max_chars=5000)
    supported = _qa_supported_statements(title, text, limit=1) or [_qa_fact_sentence(doc)]
    choices = _choice_rows(supported[:1], _qa_choice_distractors(title, text, limit=3))
    return {
        "question": _compact("Which statement is directly supported by the source?", max_chars=180),
        "choices": choices,
        "correct_choice_id": choices[0]["id"],
        "source_document_ids": [int(doc.id)],
        "explanation": supported[0],
        "confidence": build_research_qa_answer(doc)["confidence"],
    }


def build_research_multi_select_answer(doc: Document) -> dict[str, Any]:
    title = clean_whitespace(doc.title or doc.url or f"Document {doc.id}")
    text = _doc_text(doc, max_chars=5000)
    supported = _qa_supported_statements(title, text, limit=2)
    if len(supported) < 2:
        fallback = _qa_fact_sentence(doc)
        if fallback not in supported:
            supported.append(fallback)
    while len(supported) < 2:
        supported.append(_compact(title, max_chars=220))
    choices = _choice_rows(supported[:2], _qa_choice_distractors(title, text, limit=2))
    correct_ids = [row["id"] for row in choices[:2]]
    return {
        "question": _compact("Select all statements directly supported by the source.", max_chars=180),
        "choices": choices,
        "correct_choice_ids": correct_ids,
        "source_document_ids": [int(doc.id)],
        "explanation": " ".join(supported[:2]),
        "confidence": build_research_qa_answer(doc)["confidence"],
    }


def _research_choice_prompt(doc: Document, *, task: str) -> list[dict[str, str]]:
    multi = task == TASK_RESEARCH_MULTI_SELECT
    payload = {
        "task": task,
        "document_id": int(doc.id),
        "source": _source_name(doc),
        "published_at": doc.published_at.isoformat() if doc.published_at else None,
        "evidence_metadata": document_evidence_metadata(doc),
        "entities": _doc_entity_names(doc),
        "title": clean_whitespace(doc.title or ""),
        "text": _doc_text(doc, max_chars=5000),
        "output_schema": {
            "question": "grounded multiple-choice question",
            "choices": "array of {id, text} choices",
            "correct_choice_ids" if multi else "correct_choice_id": "correct choice ids" if multi else "correct choice id",
            "source_document_ids": "array containing cited document ids",
            "explanation": "brief source-grounded explanation",
            "confidence": "float 0.0-1.0",
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "Generate a grounded ThothTerminal multiple-choice item. Return JSON only. "
                "Correct choices must be directly supported by the supplied document and must cite source_document_ids."
            ),
        },
        {"role": "user", "content": _json_dumps(payload)},
    ]


def _iso_dt(value: object) -> str | None:
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()  # type: ignore[no-any-return]
        except Exception:
            return None
    return None


def _date_text(value: object) -> str:
    if isinstance(value, str):
        text = value.strip()
        return text[:10] if text else ""
    iso = _iso_dt(value)
    return iso[:10] if iso else ""


def _claim_text(row: dict[str, Any]) -> str:
    for key in ("claim_text", "summary", "text", "value_text", "new_information"):
        text = clean_whitespace(row.get(key) or "")
        if text:
            return text
    return clean_whitespace(_json_dumps(row))[:220]


def _compression_claims(comp: LocalAgentDocumentCompression, *, limit: int = 4) -> list[str]:
    claims: list[str] = []
    for row in comp.atomic_claims_json or []:
        if not isinstance(row, dict):
            continue
        text = _compact(_claim_text(row), max_chars=220)
        if text and text not in claims:
            claims.append(text)
        if len(claims) >= limit:
            break
    if not claims:
        text = _compact(comp.summary_short or comp.summary_detail or "", max_chars=220)
        if text:
            claims.append(text)
    return claims


def _route_terms(routes: list[LocalAgentEntityRoute]) -> list[str]:
    terms: list[str] = []
    for route in routes:
        for value in (
            route.entity_name,
            route.update_type if hasattr(route, "update_type") else None,
            route.materiality,
            route.novelty,
            route.temporal_posture,
            route.evidence_tier,
            route.destination_type,
        ):
            text = clean_whitespace(value or "")
            if text and text not in terms:
                terms.append(text)
    return terms[:8]


def _report_latest_version(report: ResearchReport) -> ResearchReportVersion | None:
    versions = sorted(
        list(report.versions or []),
        key=lambda row: (int(row.version or 0), row.created_at or datetime.min),
        reverse=True,
    )
    return versions[0] if versions else None


def _report_context_text(report: ResearchReport, version: ResearchReportVersion | None) -> str:
    parts = [report.title, report.subject_key, report.slug]
    if version is not None:
        parts.extend([version.content_md, _json_dumps(version.content_json or {})])
    return clean_whitespace(" ".join(str(part or "") for part in parts))


def _compression_materiality(comp: LocalAgentDocumentCompression, routes: list[LocalAgentEntityRoute]) -> tuple[int, str]:
    gain = str(comp.information_gain or "").strip().lower()
    disposition = str(comp.document_disposition or "").strip().lower()
    score = 0
    if gain == "high":
        score += 5
    elif gain == "medium":
        score += 3
    elif gain == "low":
        score += 1
    if disposition in {"durable_candidate", "hold_for_corroboration"}:
        score += 2
    if any(bool(route.durable_relevance) for route in routes):
        score += 2
    if any(str(route.materiality or "").strip().lower() in {"high", "durable", "durable candidate"} for route in routes):
        score += 1
    label = "material" if score >= 4 else "supporting" if score >= 2 else "routine"
    return score, label


def _compression_packet(
    comp: LocalAgentDocumentCompression,
    *,
    routes: list[LocalAgentEntityRoute],
) -> dict[str, Any]:
    doc = comp.document
    materiality_score, materiality = _compression_materiality(comp, routes)
    return {
        "document_id": int(comp.document_id),
        "compression_id": int(comp.id),
        "title": clean_whitespace(doc.title or doc.url or f"Document {comp.document_id}") if doc else f"Document {comp.document_id}",
        "source": _source_name(doc) if doc else "",
        "source_category": str(doc.source.category if doc and doc.source else ""),
        "published_at": _iso_dt(doc.published_at) if doc else None,
        "ingested_at": _iso_dt(doc.ingested_at) if doc else None,
        "compression_created_at": _iso_dt(comp.created_at),
        "information_gain": comp.information_gain,
        "document_disposition": comp.document_disposition,
        "status": comp.status,
        "summary_short": _compact(comp.summary_short or "", max_chars=260),
        "summary_detail": _compact(comp.summary_detail or "", max_chars=520),
        "claims": _compression_claims(comp),
        "entities": [row.get("entity_name") or row.get("name") for row in (comp.entity_refs_json or []) if isinstance(row, dict)][:8],
        "route_terms": _route_terms(routes),
        "materiality_score": materiality_score,
        "materiality": materiality,
    }


def _memory_status(packet: dict[str, Any]) -> str:
    text = _json_dumps(packet).lower()
    if "conflict" in text or "contradict" in text:
        return "conflict"
    if "outdated" in text or "stale" in text or "superseded" in text:
        return "stale"
    return "current"


def build_report_memory_plan_answer(episode: dict[str, Any]) -> dict[str, Any]:
    documents = [row for row in episode.get("documents") or [] if isinstance(row, dict)]
    selected = [
        row
        for row in sorted(
            documents,
            key=lambda item: (-int(item.get("materiality_score") or 0), str(item.get("published_at") or item.get("ingested_at") or "")),
        )
        if int(row.get("materiality_score") or 0) >= 2
    ][: int(episode.get("max_memory_cards") or 5)]
    if not selected and documents:
        selected = documents[:1]

    memory_cards: list[dict[str, Any]] = []
    active_claims: list[str] = []
    for row in selected:
        claim = _compact((row.get("claims") or [row.get("summary_short") or ""])[0], max_chars=220)
        if not claim:
            continue
        active_claims.append(claim)
        memory_cards.append(
            {
                "claim": claim,
                "source_document_ids": [int(row["document_id"])],
                "as_of_date": _date_text(row.get("published_at") or row.get("ingested_at") or row.get("compression_created_at")),
                "status": _memory_status(row),
                "importance": row.get("materiality") or "supporting",
            }
        )

    omitted = [
        int(row["document_id"])
        for row in documents
        if row.get("document_id") is not None and int(row.get("document_id")) not in {sid for card in memory_cards for sid in card.get("source_document_ids", [])}
    ][:10]
    title = clean_whitespace(episode.get("report_title") or episode.get("subject_key") or "research report")
    update_claims = "; ".join(active_claims[:3])
    if not update_claims:
        update_claims = "No durable report change is supported by the current dated evidence packet."
    report_update = (
        f"As of {episode.get('evaluation_cutoff_date')}, keep the {title} state selective: "
        f"{_compact(update_claims, max_chars=420)}"
    )
    research_requests = []
    if any(str(row.get("information_gain") or "").lower() == "medium" for row in selected):
        research_requests.append("Investigate unresolved assumptions and missing model inputs for medium-confidence signals; retain attributed evidence in the meantime.")
    if any(_memory_status(row) == "conflict" for row in selected):
        research_requests.append("Resolve the conflict against the latest issuer or regulator source before rewriting the report.")

    return {
        "memory_cards": memory_cards,
        "active_claims": active_claims[: int(episode.get("max_active_claims") or 8)],
        "report_update": _compact(report_update, max_chars=int(episode.get("max_report_update_chars") or 900)),
        "research_requests": research_requests[: int(episode.get("max_research_requests") or 4)],
        "omitted_document_ids": omitted,
    }


def _report_memory_prompt(episode: dict[str, Any]) -> list[dict[str, str]]:
    payload = {
        "task": TASK_REPORT_MEMORY_PLAN,
        "episode_id": episode.get("episode_id"),
        "report_id": episode.get("report_id"),
        "report_title": episode.get("report_title"),
        "subject_key": episode.get("subject_key"),
        "report_as_of_at": episode.get("report_as_of_at"),
        "evaluation_cutoff_date": episode.get("evaluation_cutoff_date"),
        "existing_report_excerpt": episode.get("existing_report_excerpt"),
        "budgets": {
            "max_memory_cards": episode.get("max_memory_cards"),
            "max_active_claims": episode.get("max_active_claims"),
            "max_research_requests": episode.get("max_research_requests"),
            "max_report_update_chars": episode.get("max_report_update_chars"),
        },
        "documents": episode.get("documents") or [],
        "output_schema": {
            "memory_cards": [
                {
                    "claim": "one selective dated source-backed claim",
                    "source_document_ids": "array of cited document ids",
                    "as_of_date": "YYYY-MM-DD date for the claim",
                    "status": "current, stale, conflict, or unresolved",
                    "importance": "material, supporting, or routine",
                }
            ],
            "active_claims": "array of concise claims to keep live in memory",
            "report_update": "short synthesis for the report, not a source dump",
            "research_requests": "array of open evidence requests",
            "omitted_document_ids": "array of document ids intentionally omitted as duplicate/routine/noise",
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "You maintain dated ThothTerminal research memory for a sell-side style report. "
                "Return JSON only. Be selective: do not stuff the report or memory cards. "
                "Every kept claim needs a source document id and an as-of date. "
                "Treat stale, duplicate, social, and medium-confidence evidence conservatively."
            ),
        },
        {"role": "user", "content": _json_dumps(payload)},
    ]


def _build_report_memory_episode(
    session: Session,
    report: ResearchReport,
    *,
    max_documents: int,
) -> dict[str, Any] | None:
    version = _report_latest_version(report)
    report_text = _report_context_text(report, version)
    report_tokens = _tokens(report_text)
    rows = (
        session.query(LocalAgentDocumentCompression)
        .join(Document, LocalAgentDocumentCompression.document_id == Document.id)
        .join(Source, Document.source_id == Source.id)
        .filter(LocalAgentDocumentCompression.status != "error")
        .order_by(Document.ingested_at.desc(), LocalAgentDocumentCompression.id.desc())
        .limit(max(20, int(max_documents) * 8))
        .all()
    )
    route_rows = (
        session.query(LocalAgentEntityRoute)
        .filter(LocalAgentEntityRoute.compression_id.in_([int(row.id) for row in rows] or [-1]))
        .all()
    )
    routes_by_compression: dict[int, list[LocalAgentEntityRoute]] = {}
    for route in route_rows:
        if route.compression_id is None:
            continue
        routes_by_compression.setdefault(int(route.compression_id), []).append(route)

    scored: list[tuple[int, LocalAgentDocumentCompression, list[LocalAgentEntityRoute]]] = []
    for comp in rows:
        routes = routes_by_compression.get(int(comp.id), [])
        packet_text = " ".join(
            [
                comp.summary_short or "",
                comp.summary_detail or "",
                _json_dumps(comp.atomic_claims_json or []),
                _json_dumps(comp.entity_refs_json or []),
                " ".join(_route_terms(routes)),
            ]
        )
        overlap = len(report_tokens & _tokens(packet_text)) if report_tokens else 0
        materiality_score, _label = _compression_materiality(comp, routes)
        score = overlap + materiality_score
        if score <= 0 and len(scored) >= max_documents:
            continue
        scored.append((score, comp, routes))

    selected = [
        _compression_packet(comp, routes=routes)
        for _score, comp, routes in sorted(scored, key=lambda item: (-item[0], -(item[1].document.ingested_at.timestamp() if item[1].document else 0)))[:max_documents]
    ]
    if not selected:
        return None
    as_of = version.as_of_at if version is not None else report.updated_at
    episode = {
        "episode_id": f"report-memory:{int(report.id)}:{_date_text(as_of)}:{sha256_text(_json_dumps([row['document_id'] for row in selected]))[:8]}",
        "report_id": int(report.id),
        "report_version_id": int(version.id) if version is not None else None,
        "report_title": report.title,
        "subject_key": report.subject_key,
        "report_as_of_at": _iso_dt(as_of),
        "evaluation_cutoff_date": _date_text(max((row.get("published_at") or row.get("ingested_at") or "") for row in selected)),
        "existing_report_excerpt": _compact(version.content_md if version is not None else "", max_chars=1400),
        "documents": selected,
        "max_memory_cards": 5,
        "max_active_claims": 8,
        "max_research_requests": 4,
        "max_report_update_chars": 900,
    }
    return episode


def build_report_memory_plan_examples(
    session: Session,
    *,
    limit: int,
    include_baseline: bool = True,
    max_documents: int = 12,
) -> list[dict[str, Any]]:
    rows = (
        session.query(ResearchReport)
        .order_by(ResearchReport.updated_at.desc(), ResearchReport.id.desc())
        .limit(max(1, int(limit) * 4))
        .all()
    )
    out: list[dict[str, Any]] = []
    for report in rows:
        episode = _build_report_memory_episode(session, report, max_documents=max_documents)
        if episode is None:
            continue
        answer = build_report_memory_plan_answer(episode)
        required_ids = sorted({sid for card in answer.get("memory_cards") or [] for sid in card.get("source_document_ids", [])})
        out.append(
            _make_example(
                task=TASK_REPORT_MEMORY_PLAN,
                messages=_report_memory_prompt(episode),
                answer=answer,
                info={
                    "report_id": int(report.id),
                    "report_version_id": episode.get("report_version_id"),
                    "required_source_document_ids": required_ids,
                    "all_document_ids": [int(row["document_id"]) for row in episode.get("documents") or []],
                    "max_memory_cards": episode.get("max_memory_cards"),
                    "max_active_claims": episode.get("max_active_claims"),
                    "max_report_update_chars": episode.get("max_report_update_chars"),
                },
                include_baseline=include_baseline,
            )
        )
        if len(out) >= limit:
            break
    return out


def _make_example(
    *,
    task: str,
    messages: list[dict[str, str]],
    answer: dict[str, Any],
    info: dict[str, Any],
    include_baseline: bool,
) -> dict[str, Any]:
    stable = _json_dumps({"task": task, "answer": answer, "info": info})
    row = {
        "schema_version": SCHEMA_VERSION,
        "id": f"{task}:{sha256_text(stable)[:16]}",
        "task": task,
        "messages": messages,
        "prompt": messages,
        "answer": answer,
        "info": info,
        "created_at": utcnow().isoformat(),
    }
    if include_baseline:
        baseline = baseline_completion(row)
        row["baseline_completion"] = _json_dumps(baseline)
        row["baseline_score"] = score_completion(task, baseline, answer, info=info)
    return row


def build_document_refinery_examples(
    session: Session,
    *,
    limit: int,
    include_baseline: bool = True,
) -> list[dict[str, Any]]:
    rows = (
        session.query(Document)
        .join(Source, Document.source_id == Source.id)
        .order_by(Document.ingested_at.desc(), Document.id.desc())
        .limit(max(1, int(limit) * 3))
        .all()
    )
    out: list[dict[str, Any]] = []
    for doc in rows:
        extra = doc.extra if isinstance(doc.extra, dict) else {}
        if bool(extra.get("agent_generated")):
            continue
        if not clean_whitespace(doc.title or doc.normalized_text or doc.raw_text or ""):
            continue
        answer = build_document_refinery_answer(doc)
        out.append(
            _make_example(
                task=TASK_DOCUMENT_REFINERY,
                messages=_document_refinery_prompt(doc),
                answer=answer,
                info={
                    "document_id": int(doc.id),
                    "source": _source_name(doc),
                    "source_quality": round(source_quality_score(doc), 3),
                    "precedence_bucket": precedence_bucket(doc),
                },
                include_baseline=include_baseline,
            )
        )
        if len(out) >= limit:
            break
    return out


def build_link_triage_examples(
    session: Session,
    *,
    limit: int,
    include_baseline: bool = True,
) -> list[dict[str, Any]]:
    rows = (
        session.query(DiscordLinkInboxItem)
        .order_by(DiscordLinkInboxItem.updated_at.desc(), DiscordLinkInboxItem.id.desc())
        .limit(max(1, int(limit) * 3))
        .all()
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        if not clean_whitespace(row.url_canonical or row.url_raw or ""):
            continue
        answer = build_link_triage_answer(row)
        out.append(
            _make_example(
                task=TASK_LINK_TRIAGE,
                messages=_link_triage_prompt(row),
                answer=answer,
                info={
                    "inbox_id": int(row.id),
                    "status": row.status,
                    "guild_id": int(row.guild_id),
                    "host": urlparse(row.url_canonical or row.url_raw or "").netloc.lower(),
                },
                include_baseline=include_baseline,
            )
        )
        if len(out) >= limit:
            break
    return out


def build_entity_resolution_examples(
    session: Session,
    *,
    limit: int,
    include_baseline: bool = True,
) -> list[dict[str, Any]]:
    rows = (
        session.query(Document)
        .join(DocumentEntity, DocumentEntity.document_id == Document.id)
        .order_by(Document.ingested_at.desc(), Document.id.desc())
        .limit(max(1, int(limit) * 3))
        .all()
    )
    out: list[dict[str, Any]] = []
    seen_doc_ids: set[int] = set()
    for doc in rows:
        if int(doc.id) in seen_doc_ids:
            continue
        seen_doc_ids.add(int(doc.id))
        answer = build_entity_resolution_answer(doc)
        if not answer["entity_ids"]:
            continue
        out.append(
            _make_example(
                task=TASK_ENTITY_RESOLUTION,
                messages=_entity_resolution_prompt(session, doc),
                answer=answer,
                info={"document_id": int(doc.id)},
                include_baseline=include_baseline,
            )
        )
        if len(out) >= limit:
            break
    return out


def build_research_qa_examples(
    session: Session,
    *,
    limit: int,
    include_baseline: bool = True,
) -> list[dict[str, Any]]:
    rows = (
        session.query(Document)
        .join(Source, Document.source_id == Source.id)
        .order_by(Document.ingested_at.desc(), Document.id.desc())
        .limit(max(1, int(limit) * 3))
        .all()
    )
    out: list[dict[str, Any]] = []
    for doc in rows:
        extra = doc.extra if isinstance(doc.extra, dict) else {}
        if bool(extra.get("agent_generated")):
            continue
        if not clean_whitespace(doc.title or doc.normalized_text or doc.raw_text or doc.translation_en or ""):
            continue
        answer = build_research_qa_answer(doc)
        if not clean_whitespace(answer.get("question") or "") or not clean_whitespace(answer.get("answer") or ""):
            continue
        out.append(
            _make_example(
                task=TASK_RESEARCH_QA,
                messages=_research_qa_prompt(doc),
                answer=answer,
                info={
                    "document_id": int(doc.id),
                    "source": _source_name(doc),
                    "source_quality": round(source_quality_score(doc), 3),
                    "precedence_bucket": precedence_bucket(doc),
                },
                include_baseline=include_baseline,
            )
        )
        if len(out) >= limit:
            break
    return out


def build_research_choice_examples(
    session: Session,
    *,
    task: str,
    limit: int,
    include_baseline: bool = True,
) -> list[dict[str, Any]]:
    rows = (
        session.query(Document)
        .join(Source, Document.source_id == Source.id)
        .order_by(Document.ingested_at.desc(), Document.id.desc())
        .limit(max(1, int(limit) * 3))
        .all()
    )
    out: list[dict[str, Any]] = []
    for doc in rows:
        extra = doc.extra if isinstance(doc.extra, dict) else {}
        if bool(extra.get("agent_generated")):
            continue
        if not clean_whitespace(doc.title or doc.normalized_text or doc.raw_text or doc.translation_en or ""):
            continue
        answer = build_research_multi_select_answer(doc) if task == TASK_RESEARCH_MULTI_SELECT else build_research_mcq_answer(doc)
        if len(answer.get("choices") or []) < 3:
            continue
        out.append(
            _make_example(
                task=task,
                messages=_research_choice_prompt(doc, task=task),
                answer=answer,
                info={
                    "document_id": int(doc.id),
                    "source": _source_name(doc),
                    "source_quality": round(source_quality_score(doc), 3),
                    "precedence_bucket": precedence_bucket(doc),
                },
                include_baseline=include_baseline,
            )
        )
        if len(out) >= limit:
            break
    return out


def normalize_tasks(tasks: str | Iterable[str] | None) -> list[str]:
    if tasks is None:
        return list(SUPPORTED_TASKS)
    if isinstance(tasks, str):
        raw = [part.strip() for part in tasks.split(",")]
    else:
        raw = [str(part).strip() for part in tasks]
    out: list[str] = []
    for task in raw:
        if not task:
            continue
        if task == "all":
            return list(SUPPORTED_TASKS)
        if task not in SUPPORTED_TASKS:
            raise ValueError(f"Unsupported RL task {task!r}; expected one of {', '.join(SUPPORTED_TASKS)}.")
        if task not in out:
            out.append(task)
    return out or list(SUPPORTED_TASKS)


def build_training_examples(
    session: Session,
    *,
    tasks: str | Iterable[str] | None = None,
    limit_per_task: int = 100,
    include_baseline: bool = True,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    limit = max(1, int(limit_per_task or 100))
    for task in normalize_tasks(tasks):
        if task == TASK_DOCUMENT_REFINERY:
            out.extend(build_document_refinery_examples(session, limit=limit, include_baseline=include_baseline))
        elif task == TASK_LINK_TRIAGE:
            out.extend(build_link_triage_examples(session, limit=limit, include_baseline=include_baseline))
        elif task == TASK_ENTITY_RESOLUTION:
            out.extend(build_entity_resolution_examples(session, limit=limit, include_baseline=include_baseline))
        elif task == TASK_RESEARCH_QA:
            out.extend(build_research_qa_examples(session, limit=limit, include_baseline=include_baseline))
        elif task in {TASK_RESEARCH_MCQ, TASK_RESEARCH_MULTI_SELECT}:
            out.extend(build_research_choice_examples(session, task=task, limit=limit, include_baseline=include_baseline))
        elif task == TASK_REPORT_MEMORY_PLAN:
            out.extend(build_report_memory_plan_examples(session, limit=limit, include_baseline=include_baseline))
    return out


def default_training_data_path(*, prefix: str = "thoth_rl", suffix: str = ".jsonl") -> Path:
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    return Path(settings.data_dir) / "rl" / f"{prefix}_{stamp}{suffix}"


def write_jsonl(rows: Iterable[dict[str, Any]], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            count += 1
    return count


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                rows.append(parsed)
    return rows


def generate_training_dataset(
    *,
    tasks: str | Iterable[str] | None = None,
    limit_per_task: int = 100,
    output_path: Path | None = None,
    include_baseline: bool = True,
) -> dict[str, Any]:
    out_path = Path(output_path or default_training_data_path()).resolve(strict=False)
    with session_scope() as session:
        rows = build_training_examples(
            session,
            tasks=tasks,
            limit_per_task=limit_per_task,
            include_baseline=include_baseline,
        )
    count = write_jsonl(rows, out_path)
    task_counts: dict[str, int] = {}
    baseline_scores: list[float] = []
    for row in rows:
        task = str(row.get("task") or "")
        task_counts[task] = task_counts.get(task, 0) + 1
        if row.get("baseline_score") is not None:
            baseline_scores.append(float(row["baseline_score"]))
    return {
        "path": str(out_path),
        "count": count,
        "task_counts": task_counts,
        "baseline_mean_score": round(sum(baseline_scores) / len(baseline_scores), 4) if baseline_scores else None,
    }


def _user_payload_from_row(row: dict[str, Any]) -> dict[str, Any]:
    messages = row.get("messages") or row.get("prompt") or []
    if not isinstance(messages, list):
        return {}
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            parsed = _json_loads_maybe(msg.get("content"))
            return parsed if isinstance(parsed, dict) else {}
    return {}


def baseline_completion(row: dict[str, Any]) -> dict[str, Any]:
    task = str(row.get("task") or "")
    payload = _user_payload_from_row(row)
    if task == TASK_DOCUMENT_REFINERY:
        title = clean_whitespace(payload.get("title") or "")
        text = clean_whitespace(payload.get("text") or "")
        sentences = _candidate_sentences(title, text)
        signal = _compact(sentences[0] if sentences else title, max_chars=140)
        summary = _compact((sentences[1] if len(sentences) > 1 else signal), max_chars=280)
        return {
            "signal": signal,
            "summary": summary,
            "why_it_matters": "Requires placement against active theses and source provenance before action.",
            "key_points": [_compact(row, max_chars=180) for row in sentences[2:5]],
            "topics": _topic_tokens(signal, summary, text[:800], entity_names=list(payload.get("entities") or [])),
            "confidence": 0.65,
        }
    if task == TASK_LINK_TRIAGE:
        title = f"{payload.get('embed_title') or ''} {payload.get('message_excerpt') or ''}".lower()
        ticker = str(payload.get("deterministic_ticker") or "").strip().upper() or None
        host = str(payload.get("host") or "").lower()
        if ticker:
            action = "ingest"
            confidence = 0.86
        elif any(token in title for token in ("spam", "giveaway", "promo", "airdrop")):
            action = "ignore"
            confidence = 0.74
        elif any(token in host for token in ("x.com", "twitter.com", "reddit.com")):
            action = "queue"
            confidence = 0.58
        else:
            action = "queue"
            confidence = 0.62
        return {
            "action": action,
            "ticker": ticker,
            "confidence": confidence,
            "rationale": "Baseline uses deterministic ticker and source-fragility heuristics.",
        }
    if task == TASK_ENTITY_RESOLUTION:
        text = " ".join([str(payload.get("title") or ""), str(payload.get("text") or "")]).lower()
        entity_ids: list[int] = []
        for candidate in payload.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            names = [candidate.get("canonical_name"), *(candidate.get("aliases") or [])]
            if any(str(name or "").strip().lower() and str(name or "").strip().lower() in text for name in names):
                eid = _safe_int(candidate.get("id"))
                if eid is not None:
                    entity_ids.append(eid)
        return {
            "entity_ids": sorted(set(entity_ids)),
            "confidence": 0.72 if entity_ids else 0.55,
            "rationale": "Baseline links exact candidate names or aliases found in title/text.",
        }
    if task == TASK_RESEARCH_QA:
        title = clean_whitespace(payload.get("title") or "")
        text = clean_whitespace(payload.get("text") or "")
        entities = [str(row).strip() for row in (payload.get("entities") or []) if str(row).strip()]
        sentences = _candidate_sentences(title, text)
        fact = _compact((sentences[1] if len(sentences) > 1 else sentences[0] if sentences else title), max_chars=360)
        if entities:
            question = f"What does this source say about {entities[0]}?"
        else:
            topics = _topic_tokens(title, fact, limit=2)
            subject = " and ".join(topics[:2]) if topics else title
            question = f"What does this source say about {subject}?"
        document_id = _safe_int(payload.get("document_id"))
        return {
            "question": _compact(question, max_chars=180),
            "answer": fact,
            "source_document_ids": [document_id] if document_id is not None else [],
            "topics": _topic_tokens(title, fact, text[:800], entity_names=entities, limit=6),
            "confidence": 0.68,
        }
    if task in {TASK_RESEARCH_MCQ, TASK_RESEARCH_MULTI_SELECT}:
        title = clean_whitespace(payload.get("title") or "")
        text = clean_whitespace(payload.get("text") or "")
        document_id = _safe_int(payload.get("document_id"))
        supported = _qa_supported_statements(title, text, limit=2)
        if not supported:
            supported = [_compact(title, max_chars=220)]
        if task == TASK_RESEARCH_MULTI_SELECT:
            while len(supported) < 2:
                supported.append(supported[0])
            choices = _choice_rows(supported[:2], _qa_choice_distractors(title, text, limit=2))
            return {
                "question": "Select all statements directly supported by the source.",
                "choices": choices,
                "correct_choice_ids": [row["id"] for row in choices[:2]],
                "source_document_ids": [document_id] if document_id is not None else [],
                "explanation": " ".join(supported[:2]),
                "confidence": 0.68,
            }
        choices = _choice_rows(supported[:1], _qa_choice_distractors(title, text, limit=3))
        return {
            "question": "Which statement is directly supported by the source?",
            "choices": choices,
            "correct_choice_id": choices[0]["id"],
            "source_document_ids": [document_id] if document_id is not None else [],
            "explanation": supported[0],
            "confidence": 0.68,
        }
    if task == TASK_REPORT_MEMORY_PLAN:
        episode = {
            "report_title": payload.get("report_title"),
            "subject_key": payload.get("subject_key"),
            "evaluation_cutoff_date": payload.get("evaluation_cutoff_date"),
            "documents": payload.get("documents") or [],
            "max_memory_cards": _dict(payload.get("budgets")).get("max_memory_cards") or 5,
            "max_active_claims": _dict(payload.get("budgets")).get("max_active_claims") or 8,
            "max_research_requests": _dict(payload.get("budgets")).get("max_research_requests") or 4,
            "max_report_update_chars": _dict(payload.get("budgets")).get("max_report_update_chars") or 900,
        }
        return build_report_memory_plan_answer(episode)
    return {}


def _memory_source_ids(value: object) -> list[int]:
    ids: list[int] = []
    payload = value if isinstance(value, dict) else {}
    for card in payload.get("memory_cards") or []:
        if not isinstance(card, dict):
            continue
        for raw in card.get("source_document_ids") or []:
            sid = _safe_int(raw)
            if sid is not None:
                ids.append(sid)
    return sorted(set(ids))


def _valid_memory_cards(value: object) -> list[dict[str, Any]]:
    payload = value if isinstance(value, dict) else {}
    return [row for row in (payload.get("memory_cards") or []) if isinstance(row, dict)]


def _dated_card_score(cards: list[dict[str, Any]]) -> float:
    if not cards:
        return 0.0
    ok = 0
    for card in cards:
        date_text = str(card.get("as_of_date") or "").strip()
        status = str(card.get("status") or "").strip().lower()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", date_text) and status in {"current", "stale", "conflict", "unresolved"}:
            ok += 1
    return ok / len(cards)


def _report_memory_text(value: object) -> str:
    payload = value if isinstance(value, dict) else {}
    parts: list[str] = []
    for card in _valid_memory_cards(payload):
        parts.append(str(card.get("claim") or ""))
    for claim in payload.get("active_claims") or []:
        parts.append(str(claim or ""))
    parts.append(str(payload.get("report_update") or ""))
    for request in payload.get("research_requests") or []:
        parts.append(str(request or ""))
    return clean_whitespace(" ".join(parts))


def score_completion(
    task: str,
    completion: object,
    answer: object,
    *,
    info: dict[str, Any] | None = None,
) -> float:
    info = info or {}
    gold = _json_loads_maybe(answer)
    if not isinstance(gold, dict):
        return 0.0
    pred, raw_text = _parse_completion_json(completion)
    if not isinstance(pred, dict):
        return 0.0
    task_v = str(task or "").strip()
    if task_v == TASK_DOCUMENT_REFINERY:
        required = ("signal", "summary", "why_it_matters", "key_points", "topics", "confidence")
        schema = sum(1 for key in required if key in pred) / len(required)
        topics = _f1(pred.get("topics") or [], gold.get("topics") or [])
        key_points = _f1(pred.get("key_points") or [], gold.get("key_points") or [])
        brevity = 1.0
        if len(str(pred.get("signal") or "")) > 170:
            brevity -= 0.25
        if len(str(pred.get("summary") or "")) > 340:
            brevity -= 0.25
        if len(raw_text) > 2200:
            brevity -= 0.20
        confidence = max(0.0, 1.0 - abs(_safe_float(pred.get("confidence"), 0.0) - _safe_float(gold.get("confidence"), 0.0)))
        score = (
            0.15 * schema
            + 0.20 * _text_f1(pred.get("signal"), gold.get("signal"))
            + 0.22 * _text_f1(pred.get("summary"), gold.get("summary"))
            + 0.13 * _text_f1(pred.get("why_it_matters"), gold.get("why_it_matters"))
            + 0.10 * key_points
            + 0.10 * topics
            + 0.05 * max(0.0, brevity)
            + 0.05 * confidence
        )
        return round(max(0.0, min(1.0, score)), 4)
    if task_v == TASK_LINK_TRIAGE:
        valid_action = str(pred.get("action") or "").strip() in {"ingest", "queue", "needs_human", "ignore"}
        action_score = 1.0 if str(pred.get("action") or "").strip() == str(gold.get("action") or "").strip() else 0.0
        pred_ticker = str(pred.get("ticker") or "").strip().upper()
        gold_ticker = str(gold.get("ticker") or "").strip().upper()
        ticker_score = 1.0 if pred_ticker == gold_ticker else 0.0
        confidence = max(0.0, 1.0 - abs(_safe_float(pred.get("confidence"), 0.0) - _safe_float(gold.get("confidence"), 0.0)))
        rationale = 1.0 if clean_whitespace(pred.get("rationale") or "") else 0.0
        score = 0.10 * float(valid_action) + 0.58 * action_score + 0.14 * ticker_score + 0.13 * confidence + 0.05 * rationale
        return round(max(0.0, min(1.0, score)), 4)
    if task_v == TASK_ENTITY_RESOLUTION:
        pred_ids = [_safe_int(v) for v in (pred.get("entity_ids") or [])]
        gold_ids = [_safe_int(v) for v in (gold.get("entity_ids") or [])]
        pred_ids = [v for v in pred_ids if v is not None]
        gold_ids = [v for v in gold_ids if v is not None]
        f1 = _f1(pred_ids, gold_ids)
        confidence = max(0.0, 1.0 - abs(_safe_float(pred.get("confidence"), 0.0) - _safe_float(gold.get("confidence"), 0.0)))
        rationale = 1.0 if clean_whitespace(pred.get("rationale") or "") else 0.0
        score = 0.80 * f1 + 0.10 * confidence + 0.10 * rationale
        return round(max(0.0, min(1.0, score)), 4)
    if task_v == TASK_RESEARCH_QA:
        required = ("question", "answer", "source_document_ids", "topics", "confidence")
        schema = sum(1 for key in required if key in pred) / len(required)
        pred_ids = [_safe_int(v) for v in (pred.get("source_document_ids") or [])]
        gold_ids = [_safe_int(v) for v in (gold.get("source_document_ids") or [])]
        pred_ids = [v for v in pred_ids if v is not None]
        gold_ids = [v for v in gold_ids if v is not None]
        citation_recall = len(set(pred_ids) & set(gold_ids)) / len(set(gold_ids)) if gold_ids else (1.0 if not pred_ids else 0.0)
        citation_precision = len(set(pred_ids) & set(gold_ids)) / len(set(pred_ids)) if pred_ids else (1.0 if not gold_ids else 0.0)
        topics = _f1(pred.get("topics") or [], gold.get("topics") or [])
        confidence = max(0.0, 1.0 - abs(_safe_float(pred.get("confidence"), 0.0) - _safe_float(gold.get("confidence"), 0.0)))
        brevity = 1.0
        if len(str(pred.get("question") or "")) > 240:
            brevity -= 0.20
        if len(str(pred.get("answer") or "")) > 700:
            brevity -= 0.25
        if len(raw_text) > 1800:
            brevity -= 0.20
        score = (
            0.12 * schema
            + 0.16 * _text_f1(pred.get("question"), gold.get("question"))
            + 0.34 * _text_f1(pred.get("answer"), gold.get("answer"))
            + 0.16 * citation_recall
            + 0.08 * citation_precision
            + 0.06 * topics
            + 0.04 * max(0.0, brevity)
            + 0.04 * confidence
        )
        return round(max(0.0, min(1.0, score)), 4)
    if task_v in {TASK_RESEARCH_MCQ, TASK_RESEARCH_MULTI_SELECT}:
        multi = task_v == TASK_RESEARCH_MULTI_SELECT
        required = ("question", "choices", "source_document_ids", "explanation", "confidence")
        required = (*required, "correct_choice_ids" if multi else "correct_choice_id")
        schema = sum(1 for key in required if key in pred) / len(required)
        pred_ids = [_safe_int(v) for v in (pred.get("source_document_ids") or [])]
        gold_ids = [_safe_int(v) for v in (gold.get("source_document_ids") or [])]
        pred_ids = [v for v in pred_ids if v is not None]
        gold_ids = [v for v in gold_ids if v is not None]
        citation = _f1(pred_ids, gold_ids)
        if multi:
            pred_choice_ids = [str(v).strip() for v in (pred.get("correct_choice_ids") or pred.get("selected_choice_ids") or [])]
            gold_choice_ids = [str(v).strip() for v in (gold.get("correct_choice_ids") or [])]
            choice_score = _f1(pred_choice_ids, gold_choice_ids)
        else:
            pred_choice = str(pred.get("correct_choice_id") or pred.get("selected_choice_id") or "").strip()
            gold_choice = str(gold.get("correct_choice_id") or "").strip()
            choice_score = 1.0 if pred_choice and pred_choice == gold_choice else 0.0
        pred_choices = pred.get("choices") if isinstance(pred.get("choices"), list) else []
        gold_choices = gold.get("choices") if isinstance(gold.get("choices"), list) else []
        choice_text_score = _text_f1(_json_dumps(pred_choices), _json_dumps(gold_choices))
        confidence = max(0.0, 1.0 - abs(_safe_float(pred.get("confidence"), 0.0) - _safe_float(gold.get("confidence"), 0.0)))
        brevity = 1.0
        if len(str(pred.get("question") or "")) > 240:
            brevity -= 0.20
        if len(str(pred.get("explanation") or "")) > 520:
            brevity -= 0.20
        if len(raw_text) > 1800:
            brevity -= 0.20
        score = (
            0.12 * schema
            + 0.45 * choice_score
            + 0.14 * citation
            + 0.12 * choice_text_score
            + 0.09 * _text_f1(pred.get("explanation"), gold.get("explanation"))
            + 0.04 * max(0.0, brevity)
            + 0.04 * confidence
        )
        return round(max(0.0, min(1.0, score)), 4)
    if task_v == TASK_REPORT_MEMORY_PLAN:
        required = ("memory_cards", "active_claims", "report_update", "research_requests", "omitted_document_ids")
        schema = sum(1 for key in required if key in pred) / len(required)
        pred_cards = _valid_memory_cards(pred)
        gold_cards = _valid_memory_cards(gold)
        max_cards = int(info.get("max_memory_cards") or 5)
        max_claims = int(info.get("max_active_claims") or 8)
        max_update_chars = int(info.get("max_report_update_chars") or 900)
        all_ids = {_safe_int(value) for value in info.get("all_document_ids") or []}
        all_ids = {value for value in all_ids if value is not None}
        gold_ids = {_safe_int(value) for value in info.get("required_source_document_ids") or []}
        gold_ids = {value for value in gold_ids if value is not None} or set(_memory_source_ids(gold))
        pred_ids = set(_memory_source_ids(pred))

        citation_recall = len(pred_ids & gold_ids) / len(gold_ids) if gold_ids else (1.0 if not pred_ids else 0.0)
        citation_precision = len(pred_ids & gold_ids) / len(pred_ids) if pred_ids else (1.0 if not gold_ids else 0.0)
        unsupported_penalty = 0.0
        if all_ids:
            unsupported_penalty = len(pred_ids - all_ids) / max(1, len(pred_ids))

        selectivity = 1.0
        if len(pred_cards) > max_cards:
            selectivity -= min(0.7, (len(pred_cards) - max_cards) / max(1, max_cards))
        if len(pred.get("active_claims") or []) > max_claims:
            selectivity -= min(0.4, (len(pred.get("active_claims") or []) - max_claims) / max(1, max_claims))
        if len(pred_ids) > max(max_cards, len(gold_ids) + 2):
            selectivity -= min(0.5, (len(pred_ids) - max(max_cards, len(gold_ids) + 2)) / max(1, len(pred_ids)))
        update_len = len(str(pred.get("report_update") or ""))
        if update_len > max_update_chars:
            selectivity -= min(0.6, (update_len - max_update_chars) / max(1, max_update_chars))
        if len(raw_text) > 4500:
            selectivity -= min(0.4, (len(raw_text) - 4500) / 4500)
        selectivity = max(0.0, selectivity)

        claim_score = _text_f1(_report_memory_text(pred), _report_memory_text(gold))
        dated_score = _dated_card_score(pred_cards)
        omitted_score = _f1(pred.get("omitted_document_ids") or [], gold.get("omitted_document_ids") or [])
        request_shape = 1.0 if isinstance(pred.get("research_requests"), list) else 0.0
        update_present = 1.0 if clean_whitespace(pred.get("report_update") or "") else 0.0
        score = (
            0.10 * schema
            + 0.20 * citation_recall
            + 0.13 * citation_precision
            + 0.19 * claim_score
            + 0.14 * dated_score
            + 0.14 * selectivity
            + 0.05 * omitted_score
            + 0.03 * request_shape
            + 0.03 * update_present
            - 0.15 * unsupported_penalty
        )
        return round(max(0.0, min(1.0, score)), 4)
    return 0.0


def score_baseline_dataset(*, input_path: Path, output_path: Path | None = None) -> dict[str, Any]:
    rows = read_jsonl(Path(input_path))
    scored: list[dict[str, Any]] = []
    task_scores: dict[str, list[float]] = {}
    for row in rows:
        task = str(row.get("task") or "")
        completion = row.get("baseline_completion")
        if completion is None:
            completion = _json_dumps(baseline_completion(row))
        score = score_completion(
            task,
            completion,
            row.get("answer"),
            info=row.get("info") if isinstance(row.get("info"), dict) else None,
        )
        scored_row = {"id": row.get("id"), "task": task, "score": score}
        scored.append(scored_row)
        task_scores.setdefault(task, []).append(score)
    summary = {
        "input_path": str(Path(input_path).resolve(strict=False)),
        "count": len(rows),
        "mean_score": round(sum(item["score"] for item in scored) / len(scored), 4) if scored else None,
        "task_scores": {
            task: {
                "count": len(values),
                "mean_score": round(sum(values) / len(values), 4) if values else None,
            }
            for task, values in sorted(task_scores.items())
        },
        "rows": scored,
    }
    if output_path is not None:
        out_path = Path(output_path).resolve(strict=False)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        summary["output_path"] = str(out_path)
    return summary
