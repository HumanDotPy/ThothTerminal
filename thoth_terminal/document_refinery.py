from __future__ import annotations

import re
from typing import Any

from thoth_terminal.agent.evidence_policy import precedence_bucket, translation_risk_score
from thoth_terminal.agent.local_llm import ChatMessage, LocalLlmError, chat_json
from thoth_terminal.config import settings
from thoth_terminal.db import utcnow
from thoth_terminal.local_model_runtime import local_model_endpoint, local_model_name
from thoth_terminal.models import Document
from thoth_terminal.text import clean_whitespace, sha256_text


REFINERY_EXTRA_KEY = "document_refinery"
REFINERY_VERSION = 2

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_TOPIC_TOKEN_RE = re.compile(r"\$?[A-Za-z][A-Za-z0-9&+/\-.]{2,}")
_STOPWORDS = {
    "about",
    "after",
    "again",
    "analyst",
    "analysts",
    "announced",
    "because",
    "company",
    "could",
    "from",
    "have",
    "into",
    "just",
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


def _clean(value: object) -> str:
    return clean_whitespace(str(value or ""))


def _compact(value: object, *, max_chars: int) -> str:
    text = _clean(value)
    if len(text) <= int(max_chars):
        return text
    trimmed = text[: max(0, int(max_chars) - 1)].rstrip(" ,;:-")
    return trimmed + "..."


def _dedupe_keep_order(rows: list[str], *, max_items: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        text = _clean(row)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= max(1, int(max_items)):
            break
    return out


def _translated_title(doc: Document) -> str:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    translated = str(extra.get("translation_title_en") or "").strip()
    if translated:
        return translated
    return _clean(doc.title)


def _ocr_summaries(doc: Document) -> list[str]:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    rows = extra.get("image_ocr")
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        summary = _clean(row.get("summary") or row.get("text") or "")
        if summary:
            out.append(summary)
        if len(out) >= 2:
            break
    return out


def _source_tier(source_name: str | None) -> str:
    raw = str(source_name or "").strip().lower()
    if not raw:
        return "secondary"
    if any(token in raw for token in ("sec", "filing", "transcript", "earnings", "investor", "ir_", "results", "press_release")):
        return "primary"
    if any(token in raw for token in ("x_list", "discord", "tweet", "twitter", "social")):
        return "social"
    return "secondary"


def _candidate_sentences(title: str, body: str, *, ocr_notes: list[str]) -> list[str]:
    parts: list[str] = []
    if title:
        parts.append(title)
    if body:
        parts.extend(_SENTENCE_SPLIT_RE.split(body))
    parts.extend(ocr_notes)
    cleaned: list[str] = []
    for part in parts:
        text = _clean(part)
        if len(text) < 24:
            continue
        if text.lower() in {"read more", "click here", "learn more"}:
            continue
        cleaned.append(text)
    return _dedupe_keep_order(cleaned, max_items=8)


def _extract_topics(*parts: str, limit: int = 6) -> list[str]:
    scores: dict[str, int] = {}
    display: dict[str, str] = {}
    for idx, part in enumerate(parts):
        seen_part: set[str] = set()
        for token in _TOPIC_TOKEN_RE.findall(str(part or "")):
            raw = token.strip()
            norm = raw.strip("$").lower()
            if (
                not norm
                or norm in seen_part
                or norm in _STOPWORDS
                or len(norm) < 3
                or norm.isdigit()
            ):
                continue
            seen_part.add(norm)
            scores[norm] = scores.get(norm, 0) + (3 if idx == 0 else 2 if idx == 1 else 1)
            display.setdefault(norm, raw.upper() if raw.startswith("$") or raw.isupper() else raw)
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [display[key] for key, _score in ordered[: max(1, int(limit))]]


def _heuristic_why_it_matters(doc: Document, *, source_name: str | None) -> str:
    translation_risk = float(translation_risk_score(doc) or 0.0)
    base = "Evaluate the attributed claim against active models, timing, assumptions, and contradictions. Preserve sparse leading evidence with uncertainty."
    if translation_risk >= 0.35:
        base += " Check the original wording or image before relying on its exact meaning."
    return _compact(base, max_chars=220)


def _heuristic_payload(doc: Document, *, source_name: str | None, source_hash: str) -> dict[str, Any]:
    title = _translated_title(doc)
    body = _clean(doc.translation_en or doc.normalized_text or doc.raw_text or "")
    ocr_notes = _ocr_summaries(doc)
    sentences = _candidate_sentences(title, body, ocr_notes=ocr_notes)
    signal = _compact(sentences[0] if sentences else (title or body or f"Document {doc.id}"), max_chars=140)
    summary_seed = ""
    for candidate in sentences:
        if candidate.lower() != signal.lower():
            summary_seed = candidate
            break
    if not summary_seed:
        summary_seed = signal
    summary = _compact(summary_seed, max_chars=280)
    key_points = _dedupe_keep_order(
        [row for row in sentences if row.lower() not in {signal.lower(), summary.lower()}],
        max_items=int(getattr(settings, "document_refinery_max_key_points", 3) or 3),
    )
    key_points = [_compact(row, max_chars=180) for row in key_points[:3]]
    topics = _extract_topics(signal, summary, *key_points, limit=6)
    return {
        "version": REFINERY_VERSION,
        "source_hash": source_hash,
        "signal": signal,
        "summary": summary,
        "why_it_matters": _heuristic_why_it_matters(doc, source_name=source_name),
        "key_points": key_points,
        "topics": topics,
        "confidence": 0.82 if title and len(body) >= 120 else 0.66 if body else 0.52,
        "engine": "heuristic",
        "model": None,
        "llm_used": False,
        "generated_at": utcnow().isoformat(),
    }


def _refinery_input(doc: Document, *, source_name: str | None) -> tuple[str, str]:
    title = _translated_title(doc)
    body = _clean(doc.translation_en or doc.normalized_text or doc.raw_text or "")
    ocr_notes = _ocr_summaries(doc)
    entity_names = [
        str(link.entity.canonical_name or "").strip()
        for link in (doc.entity_links or [])
        if link.entity and str(link.entity.canonical_name or "").strip()
    ][:10]
    max_chars = int(getattr(settings, "document_refinery_max_input_chars", 6000) or 6000)
    source_blob = "\n".join([title, body[:max_chars], *ocr_notes, " | ".join(entity_names)]).strip()
    source_hash = sha256_text(source_blob)
    prompt = (
        "Return JSON only with keys signal, summary, why_it_matters, key_points, topics, confidence.\n"
        "You are producing a durable refinery payload for downstream triage, context compression, and digest jobs.\n"
        "Requirements:\n"
        "- signal: one-line statement of the actual development, <= 140 chars\n"
        "- summary: dense factual summary, <= 280 chars\n"
        "- why_it_matters: analyst framing and corroboration posture, <= 220 chars\n"
        "- key_points: array with 1-3 short items, each <= 180 chars\n"
        "- topics: array with 1-6 short tokens or phrases\n"
        "- confidence: float 0.0-1.0\n"
        "Do not fabricate numbers, entities, dates, or causality. Prefer translated English phrasing when available.\n\n"
        f"Source: {str(source_name or '').strip() or 'unknown'}\n"
        f"PublishedAt: {doc.published_at.isoformat() if doc.published_at else ''}\n"
        f"Language: {str(doc.language or '').strip() or 'unknown'}\n"
        f"PrecedenceBucket: {precedence_bucket(doc)}\n"
        f"Title: {title}\n"
        + (f"Entities: {', '.join(entity_names)}\n" if entity_names else "")
        + (f"OCRNotes: {' || '.join(ocr_notes)}\n" if ocr_notes else "")
        + "\nText:\n"
        + source_blob[:max_chars]
    )
    return prompt, source_hash


def _normalize_payload(parsed: dict[str, Any], *, fallback: dict[str, Any], source_hash: str, model: str) -> dict[str, Any]:
    signal = _compact(parsed.get("signal") or fallback.get("signal") or "", max_chars=140) or fallback["signal"]
    summary = _compact(parsed.get("summary") or fallback.get("summary") or signal, max_chars=280) or fallback["summary"]
    why_it_matters = _compact(
        parsed.get("why_it_matters") or fallback.get("why_it_matters") or "",
        max_chars=220,
    ) or fallback["why_it_matters"]
    key_points = _dedupe_keep_order(
        [str(item or "").strip() for item in (parsed.get("key_points") or [])],
        max_items=int(getattr(settings, "document_refinery_max_key_points", 3) or 3),
    )
    if not key_points:
        key_points = list(fallback.get("key_points") or [])
    key_points = [_compact(item, max_chars=180) for item in key_points[:3]]
    topics = _extract_topics(
        signal,
        summary,
        *[str(item) for item in (parsed.get("topics") or [])],
        *key_points,
        limit=6,
    )
    confidence_raw = parsed.get("confidence")
    try:
        confidence = max(0.0, min(1.0, float(confidence_raw)))
    except Exception:
        confidence = float(fallback.get("confidence") or 0.5)
    return {
        "version": REFINERY_VERSION,
        "source_hash": source_hash,
        "signal": signal,
        "summary": summary,
        "why_it_matters": why_it_matters,
        "key_points": key_points,
        "topics": topics,
        "confidence": round(confidence, 3),
        "engine": "local_model",
        "model": model,
        "llm_used": True,
        "generated_at": utcnow().isoformat(),
    }


def get_document_refinery(doc: Document | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    payload = extra.get(REFINERY_EXTRA_KEY)
    if not isinstance(payload, dict):
        return None
    if int(payload.get("version") or 0) != REFINERY_VERSION:
        return None
    return payload


def ensure_document_refinery(doc: Document, *, source_name: str | None = None) -> dict[str, Any] | None:
    if not bool(getattr(settings, "document_refinery_enabled", True)):
        return get_document_refinery(doc)
    extra = dict(doc.extra or {}) if isinstance(doc.extra, dict) else {}
    if bool(extra.get("agent_generated")):
        return None
    prompt, source_hash = _refinery_input(doc, source_name=source_name)
    existing = get_document_refinery(doc)
    if existing and str(existing.get("source_hash") or "").strip() == source_hash:
        return existing
    fallback = _heuristic_payload(doc, source_name=source_name, source_hash=source_hash)
    endpoint = local_model_endpoint() or ""
    model = local_model_name() or ""
    payload = fallback
    if endpoint and model:
        try:
            parsed = chat_json(
                endpoint=endpoint,
                model=model,
                messages=[
                    ChatMessage(role="system", content="Return JSON only."),
                    ChatMessage(role="user", content=prompt),
                ],
                timeout_s=float(getattr(settings, "agent_llm_timeout_s", 60) or 60),
                max_input_chars=int(getattr(settings, "document_refinery_max_input_chars", 6000) or 6000),
            )
            if isinstance(parsed, dict):
                payload = _normalize_payload(parsed, fallback=fallback, source_hash=source_hash, model=model)
        except LocalLlmError:
            payload = fallback
        except Exception:
            payload = fallback
    extra[REFINERY_EXTRA_KEY] = payload
    doc.extra = extra
    return payload
