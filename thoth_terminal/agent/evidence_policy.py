from __future__ import annotations

from typing import Any, Iterable

from thoth_terminal.models import Document, Note, Source
from thoth_terminal.sources.policy import is_source_deprecated_config


EVIDENCE_TIER_ISSUER_CONFIRMED = "issuer_confirmed"
EVIDENCE_TIER_SELL_SIDE = "sell_side"
EVIDENCE_TIER_TRUSTED_NEWS_TRADE_PRESS = "trusted_news_trade_press"
EVIDENCE_TIER_TRUSTED_ANALYST = "trusted_analyst"
EVIDENCE_TIER_DIRECTIONAL_UNVERIFIED = "directional_unverified"
PRECEDENCE_PRIMARY = EVIDENCE_TIER_ISSUER_CONFIRMED
PRECEDENCE_ISSUER_LOCAL = EVIDENCE_TIER_ISSUER_CONFIRMED
PRECEDENCE_TRUSTED_REPORTING = EVIDENCE_TIER_TRUSTED_NEWS_TRADE_PRESS
PRECEDENCE_ANALYST_SOCIAL = EVIDENCE_TIER_TRUSTED_ANALYST
PRECEDENCE_DEPRECATED_LOW_CONFIDENCE = EVIDENCE_TIER_DIRECTIONAL_UNVERIFIED
_BASE_PROXIMITY = {
    EVIDENCE_TIER_ISSUER_CONFIRMED: 0.70,
    EVIDENCE_TIER_SELL_SIDE: 0.70,
    EVIDENCE_TIER_TRUSTED_NEWS_TRADE_PRESS: 0.70,
    EVIDENCE_TIER_TRUSTED_ANALYST: 0.70,
    EVIDENCE_TIER_DIRECTIONAL_UNVERIFIED: 0.45,
}

_PAID_READER_INCOMPLETE_STATES = {
    "locked_or_unentitled",
    "login_required",
    "paywalled_stub",
    "authenticated_extraction_required",
    "incomplete_paid_reader",
}
_PAID_READER_PARTIAL_STATES = {
    "paid_teaser",
    "partial_paid_reader_teaser",
    "teaser",
}
_PAID_READER_EXACT_MARKERS = (
    "this post is for paid subscribers",
)


def _source_bits(
    *,
    doc: Document | None = None,
    source: Source | None = None,
    source_name: str | None = None,
    category: str | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    src = source or (doc.source if doc and getattr(doc, "source", None) else None)
    cfg = dict(config or (src.config if src and isinstance(src.config, dict) else {}))
    name = str(source_name or (src.name if src else "") or "").strip().lower()
    cat = str(category or (src.category if src else "") or "").strip().lower()
    return name, cat, cfg


def source_quality_score(
    doc: Document | None = None,
    *,
    source: Source | None = None,
    source_name: str | None = None,
    category: str | None = None,
    config: dict[str, Any] | None = None,
) -> float:
    name, cat, cfg = _source_bits(doc=doc, source=source, source_name=source_name, category=category, config=config)
    if is_source_deprecated_config(cfg):
        return 0.18
    # These are curation priors, not a ranking of publication media. Claim-level
    # access, track record, specificity, timing, and independence belong in the
    # later research judgment rather than an inferred source-name score.
    if name == "internal" or cat in {"ir", "analyst"}:
        return 0.78
    if name in {"x_list_analysts", "gmail_substack_trusted_analysts"}:
        return 0.78
    if any(token in name for token in (
        "goldman", "morgan_stanley", "morganstanley", "ubs", "jefferies",
        "bernstein", "jpmorgan", "bank_of_america", "bofa", "reuters",
        "trendforce", "sec", "filing", "calendar", "financial_reports",
        "annual_reports", "results", "press_release", "press_room",
    )):
        return 0.78
    if cat == "social":
        return 0.56
    if cat == "web":
        return 0.68
    return 0.60


def precedence_bucket(
    doc: Document | None = None,
    *,
    source: Source | None = None,
    source_name: str | None = None,
    category: str | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    name, cat, cfg = _source_bits(doc=doc, source=source, source_name=source_name, category=category, config=config)
    if is_source_deprecated_config(cfg):
        return EVIDENCE_TIER_DIRECTIONAL_UNVERIFIED
    if name == "internal":
        return EVIDENCE_TIER_ISSUER_CONFIRMED
    if any(token in name for token in ("sec", "filing", "transcript", "earnings_call", "annual_reports", "financial_reports")):
        return EVIDENCE_TIER_ISSUER_CONFIRMED
    if any(token in name for token in ("goldman", "morgan_stanley", "morganstanley", "ubs", "jefferies", "bernstein", "jpmorgan", "bank_of_america", "bofa")):
        return EVIDENCE_TIER_SELL_SIDE
    if cat == "ir" or any(token in name for token in ("press_release", "press_releases", "press_room", "results", "investor", "ir_")):
        return EVIDENCE_TIER_ISSUER_CONFIRMED
    if name in {"reuters_ai", "reuters_taiwan", "trendforce_presscenter", "trendforce_news"}:
        return EVIDENCE_TIER_TRUSTED_NEWS_TRADE_PRESS
    if name in {"x_list_analysts", "gmail_substack_trusted_analysts"}:
        return EVIDENCE_TIER_TRUSTED_ANALYST
    if cat == "analyst":
        return EVIDENCE_TIER_TRUSTED_ANALYST
    if cat == "social":
        return EVIDENCE_TIER_DIRECTIONAL_UNVERIFIED
    if cat == "web":
        return EVIDENCE_TIER_TRUSTED_NEWS_TRADE_PRESS
    return EVIDENCE_TIER_TRUSTED_NEWS_TRADE_PRESS


def translation_risk_score(doc: Document | None) -> float:
    if not doc:
        return 0.0
    title = str(doc.title or "").strip()
    lang = str(doc.language or "").strip().lower()
    conf = float(doc.language_confidence or 0.0)
    has_translation = bool(str(doc.translation_en or "").strip())
    non_ascii_title = bool(title) and not title.isascii()
    if lang in {"", "en", "eng"} and conf >= 0.90 and not non_ascii_title:
        return 0.0
    if non_ascii_title and not has_translation:
        return 0.78
    if non_ascii_title and has_translation:
        return 0.42
    if lang and lang not in {"en", "eng"} and not has_translation:
        return 0.70
    if lang and lang not in {"en", "eng"} and has_translation:
        return 0.38
    if conf and conf < 0.70:
        return 0.28
    return 0.0


def document_extraction_metadata(doc: Document | None) -> dict[str, Any]:
    if doc is None:
        return {
            "extraction_status": "unknown",
            "extraction_usable": False,
            "extraction_warning": "Document row is unavailable.",
        }
    extra = doc.extra if isinstance(getattr(doc, "extra", None), dict) else {}
    report_extraction = extra.get("report_extraction") if isinstance(extra.get("report_extraction"), dict) else {}
    entitlement = extra.get("entitlement") if isinstance(extra.get("entitlement"), dict) else {}
    connector = str(extra.get("connector") or "").strip().lower()
    paywall_state = str(extra.get("paywall_state") or "").strip().lower()
    explicit_status = str(
        extra.get("extraction_status")
        or report_extraction.get("status")
        or report_extraction.get("extraction_status")
        or ""
    ).strip().lower()
    source_kind = str(extra.get("report_source_kind") or "").strip().lower()
    rights_class = str(entitlement.get("rights_class") or "").strip().lower()
    is_paid_reader = (
        connector in {"substack_reader_browser", "note_reader_browser"}
        or source_kind == "paid_subscription_reader"
        or rights_class == "paid"
    )
    text = ""
    for attr in ("translation_en", "normalized_text", "raw_text"):
        try:
            value = getattr(doc, attr, None)
        except Exception:
            value = None
        if value:
            text = str(value)
            break
    lowered = text.lower()
    marker_hits = [marker for marker in _PAID_READER_EXACT_MARKERS if marker in lowered]
    partial = explicit_status in _PAID_READER_PARTIAL_STATES
    if partial:
        return {
            "extraction_status": explicit_status,
            "extraction_usable": True,
            "extraction_warning": "Only teaser/free-visible paid-reader text was captured; use it as partial evidence, not full-source extraction.",
            "extraction_marker_hits": marker_hits,
            "requires_authenticated_reextract": False,
            "requires_full_text_for_durable_write": True,
        }
    incomplete = (
        explicit_status in _PAID_READER_INCOMPLETE_STATES
        or paywall_state in _PAID_READER_INCOMPLETE_STATES
        or (is_paid_reader and bool(marker_hits))
    )
    if incomplete:
        status = explicit_status or paywall_state or "incomplete_paid_reader"
        return {
            "extraction_status": status,
            "extraction_usable": False,
            "extraction_warning": "Authenticated paid-reader extraction appears incomplete; treat text/refinery summaries as teaser or paywall context until re-extracted.",
            "extraction_marker_hits": marker_hits,
            "requires_authenticated_reextract": bool(is_paid_reader),
        }
    return {
        "extraction_status": explicit_status or paywall_state or "usable",
        "extraction_usable": True,
        "extraction_warning": None,
        "extraction_marker_hits": [],
        "requires_authenticated_reextract": False,
    }


def needs_corroboration(
    *,
    bucket: str,
    source_quality: float,
    translation_risk: float,
    corroboration_strength: float | None = None,
) -> bool:
    if translation_risk >= 0.35:
        return True
    if source_quality < 0.68:
        return True
    return False


def issuer_proximity_score(
    *,
    bucket: str,
    thesis_entities: Iterable[str] | None = None,
    doc_entities: Iterable[str] | None = None,
) -> float:
    base = _BASE_PROXIMITY.get(bucket, 0.45)
    thesis_set = {str(v or "").strip().upper() for v in (thesis_entities or []) if str(v or "").strip()}
    doc_set = {str(v or "").strip().upper() for v in (doc_entities or []) if str(v or "").strip()}
    if not thesis_set or not doc_set:
        return base
    overlap = len(thesis_set & doc_set)
    overlap_ratio = overlap / max(1, len(thesis_set))
    proximity = 0.55 * base + 0.45 * overlap_ratio
    if len(thesis_set) >= 2 and overlap < min(2, len(thesis_set)):
        proximity *= 0.82
    if overlap == 0:
        proximity *= 0.45
    return max(0.0, min(1.0, proximity))


def corroboration_strength(
    *,
    bucket: str,
    source_quality: float,
    translation_risk: float,
    best_existing_quality: float = 0.0,
    existing_evidence_count: int = 0,
    independent_owner_count: int = 0,
) -> float:
    # Only explicitly identified, independent underlying owners count. Source
    # quality, document count, and repeated relays cannot establish this.
    return min(1.0, max(0, independent_owner_count) * 0.65)


def document_evidence_metadata(doc: Document) -> dict[str, Any]:
    bucket = precedence_bucket(doc)
    quality = source_quality_score(doc)
    translation_risk = translation_risk_score(doc)
    extraction = document_extraction_metadata(doc)
    deprecated = bucket == PRECEDENCE_DEPRECATED_LOW_CONFIDENCE
    return {
        "precedence_bucket": bucket,
        "source_quality_score": round(quality, 3),
        "issuer_proximity": round(_BASE_PROXIMITY.get(bucket, 0.45), 3),
        "translation_risk": round(translation_risk, 3),
        "needs_corroboration": bool(
            needs_corroboration(bucket=bucket, source_quality=quality, translation_risk=translation_risk)
            or not bool(extraction.get("extraction_usable"))
        ),
        "deprecated_source": deprecated,
        **extraction,
    }


def note_evidence_metadata(note: Note) -> dict[str, Any]:
    extra = note.extra if isinstance(note.extra, dict) else {}
    discord_extra = extra.get("discord") if isinstance(extra.get("discord"), dict) else {}
    kind = str(discord_extra.get("kind") or "").strip().lower()
    bucket = EVIDENCE_TIER_TRUSTED_ANALYST if kind.startswith("discord") else EVIDENCE_TIER_TRUSTED_NEWS_TRADE_PRESS
    quality = 0.74 if bucket == EVIDENCE_TIER_TRUSTED_ANALYST else 0.66
    return {
        "precedence_bucket": bucket,
        "source_quality_score": round(quality, 3),
        "issuer_proximity": round(_BASE_PROXIMITY.get(bucket, 0.45), 3),
        "translation_risk": 0.0,
        "needs_corroboration": bool(
            needs_corroboration(bucket=bucket, source_quality=quality, translation_risk=0.0)
        ),
        "deprecated_source": False,
    }


def discord_source_evidence_metadata(row: Any) -> dict[str, Any]:
    return {
        "precedence_bucket": EVIDENCE_TIER_TRUSTED_ANALYST,
        "source_quality_score": 0.74,
        "issuer_proximity": round(_BASE_PROXIMITY.get(EVIDENCE_TIER_TRUSTED_ANALYST, 0.70), 3),
        "translation_risk": 0.0,
        "needs_corroboration": False,
        "deprecated_source": False,
    }
