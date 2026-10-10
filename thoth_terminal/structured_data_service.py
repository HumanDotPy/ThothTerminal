from __future__ import annotations

import re
from collections import Counter
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.dataset_identity import ensure_managed_company_dataset, lock_managed_company_dataset
from thoth_terminal.dataset_service import replace_dataset_draft
from thoth_terminal.db import utcnow
from thoth_terminal.institutional_service import sync_structured_fact_to_institutional
from thoth_terminal.models import Company, CompanyDocument, Dataset, Document, Source, StructuredDataFact


DATASET_FAMILY_TRADE_CUSTOMS = "trade_customs"
DATASET_FAMILY_EARNINGS_ACTUALS = "earnings_actuals"
DATASET_FAMILY_EARNINGS_FILINGS = "earnings_filings"
DATASET_FAMILY_GUIDANCE_ACTUALS = "guidance_actuals"
DATASET_FAMILY_INSTRUMENT_PROJECTION = "instrument_projection"
DATASET_FAMILY_PRODUCT_STAGE_TRACKER = "product_stage_tracker"
DATASET_FAMILY_AI_APPLICATION_USAGE = "ai_application_usage"
DATASET_FAMILY_AI_DATA_PLATFORM = "ai_data_platform"
DATASET_FAMILY_AI_EVALOPS_OBSERVABILITY = "ai_evalops_observability"
DATASET_FAMILY_AI_GOVERNANCE_COMPLIANCE = "ai_governance_compliance"
DATASET_FAMILY_AI_CLOUD_INCIDENT = "ai_cloud_incident"
DATASET_FAMILY_PHYSICAL_LOGISTICS = "physical_logistics"
DATASET_FAMILY_SHIPMENT_CUSTOMS = "shipment_customs"
DATASET_FAMILY_COMPONENT_TRACEABILITY = "component_traceability"
DATASET_FAMILY_SUPPLIER_ROUTE_EXPOSURE = "supplier_route_exposure"
DATASET_FAMILY_DOCUMENT_HYDRATION_PROVENANCE = "document_hydration_provenance"

DATASET_FAMILIES = {
    DATASET_FAMILY_TRADE_CUSTOMS,
    DATASET_FAMILY_EARNINGS_ACTUALS,
    DATASET_FAMILY_EARNINGS_FILINGS,
    DATASET_FAMILY_GUIDANCE_ACTUALS,
    DATASET_FAMILY_INSTRUMENT_PROJECTION,
    DATASET_FAMILY_PRODUCT_STAGE_TRACKER,
    DATASET_FAMILY_AI_APPLICATION_USAGE,
    DATASET_FAMILY_AI_DATA_PLATFORM,
    DATASET_FAMILY_AI_EVALOPS_OBSERVABILITY,
    DATASET_FAMILY_AI_GOVERNANCE_COMPLIANCE,
    DATASET_FAMILY_AI_CLOUD_INCIDENT,
    DATASET_FAMILY_PHYSICAL_LOGISTICS,
    DATASET_FAMILY_SHIPMENT_CUSTOMS,
    DATASET_FAMILY_COMPONENT_TRACEABILITY,
    DATASET_FAMILY_SUPPLIER_ROUTE_EXPOSURE,
    DATASET_FAMILY_DOCUMENT_HYDRATION_PROVENANCE,
}

EARNINGS_DOC_TYPES = {
    "earnings_release",
    "earnings_transcript",
    "earnings_presentation",
    "financial_report",
}

GUIDANCE_HINTS = {"guidance", "outlook", "expects", "expected", "forecast", "guides", "guidance for"}
EARNINGS_FACT_FAMILIES = {DATASET_FAMILY_EARNINGS_ACTUALS, DATASET_FAMILY_GUIDANCE_ACTUALS}
_CURRENCY_LIKE_METRICS = {"revenue", "capex", "backlog", "operating_income", "gross_profit", "free_cash_flow"}
_MODEL_NUMBER_RE = re.compile(r"\b[A-Z]{1,6}\s?-?\d{2,5}[A-Z]?\b", re.IGNORECASE)

_SPACE_RE = re.compile(r"\s+")
_FISCAL_Q_RE = re.compile(r"\b(?:fy)?(?P<year>20\d{2}|\d{2})\s*[-/ ]?\s*q(?P<quarter>[1-4])\b", re.IGNORECASE)
_Q_FISCAL_RE = re.compile(r"\bq(?P<quarter>[1-4])\s*(?:fy)?(?P<year>20\d{2}|\d{2})\b", re.IGNORECASE)
_CURRENCY_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<currency>US\$|USD|NT\$|TWD|RMB|CNY|KRW|EUR|\$)?\s*(?P<value>\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?P<scale>billion|million|thousand|bn|mn|mm|m|b|k)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"(?P<value>\d{1,3}(?:\.\d+)?)\s*%")
_INTEGER_RE = re.compile(r"(?P<value>\d{1,3}(?:,\d{3})*(?:\.\d+)?)")

_METRIC_DEFS = [
    {"metric_key": "revenue", "label": "Revenue", "aliases": ("revenue", "sales"), "mode": "currency", "default_currency": "USD"},
    {"metric_key": "capex", "label": "Capex", "aliases": ("capex", "capital expenditure", "capital expenditures"), "mode": "currency", "default_currency": "USD"},
    {"metric_key": "gross_margin", "label": "Gross Margin", "aliases": ("gross margin",), "mode": "percent", "unit": "%"},
    {"metric_key": "operating_margin", "label": "Operating Margin", "aliases": ("operating margin",), "mode": "percent", "unit": "%"},
    {"metric_key": "eps", "label": "EPS", "aliases": ("eps", "earnings per share"), "mode": "currency", "default_currency": "USD", "unit": "per_share"},
    {"metric_key": "backlog", "label": "Backlog", "aliases": ("backlog",), "mode": "currency", "default_currency": "USD"},
    {"metric_key": "utilization", "label": "Utilization", "aliases": ("utilization",), "mode": "percent", "unit": "%"},
    {"metric_key": "wafers_per_month", "label": "Wafers Per Month", "aliases": ("wpm", "wafers per month"), "mode": "integer", "unit": "wpm"},
]


def normalize_dataset_family(value: str | None) -> str:
    raw = str(value or "").strip().lower() or "general"
    return raw if raw in DATASET_FAMILIES else raw


def company_structured_fact_scope_ids(session: Session, company: Company | None) -> list[int]:
    """Return company ids whose structured facts should read as one issuer scope.

    Official rails sometimes create weak company rows from regulator ids before a
    ticker-backed watchlist company is present. Same-issuer fact reads should not
    make those rows invisible to the canonical analyst company page/report.
    """

    if company is None or company.id is None:
        return []
    ids: list[int] = [int(company.id)]
    if company.issuer_id is None:
        return ids
    for row in (
        session.query(Company.id)
        .filter(Company.issuer_id == int(company.issuer_id))
        .order_by(Company.id.asc())
        .all()
    ):
        if row[0] is None:
            continue
        company_id = int(row[0])
        if company_id not in ids:
            ids.append(company_id)
    return ids


def _slugify(value: str, *, default: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned or default


def _clean_text(value: str | None) -> str:
    return _SPACE_RE.sub(" ", str(value or "").strip())


def _parse_scaled_number(value_text: str, scale_text: str | None) -> float | None:
    raw = str(value_text or "").replace(",", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except Exception:
        return None
    scale = (scale_text or "").strip().lower()
    multiplier = 1.0
    if scale in {"billion", "bn", "b"}:
        multiplier = 1_000_000_000.0
    elif scale in {"million", "mn", "mm", "m"}:
        multiplier = 1_000_000.0
    elif scale in {"thousand", "k"}:
        multiplier = 1_000.0
    return value * multiplier


def _normalize_currency(token: str | None, default: str | None = None) -> str | None:
    raw = str(token or "").strip().upper()
    if raw in {"$", "US$", "USD"}:
        return "USD"
    if raw in {"NT$", "TWD"}:
        return "TWD"
    if raw in {"RMB", "CNY"}:
        return "CNY"
    if raw in {"KRW"}:
        return "KRW"
    if raw in {"EUR"}:
        return "EUR"
    return default


def _currency_match_has_marker(match_obj: re.Match[str]) -> bool:
    return bool((match_obj.group("currency") or "").strip() or (match_obj.group("scale") or "").strip())


def _currency_match_is_percent(window: str, match_obj: re.Match[str]) -> bool:
    return window[match_obj.end() : match_obj.end() + 1] == "%"


def _prefer_metric_row(existing: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, Any]:
    if existing is None:
        return candidate
    metric_key = str(candidate.get("metric_key") or "").strip().lower()
    if metric_key in {"eps", "gross_margin", "operating_margin"}:
        existing_line = str(existing.get("source_line") or "").lower()
        candidate_line = str(candidate.get("source_line") or "").lower()
        if "non-gaap" in candidate_line and "non-gaap" not in existing_line:
            return candidate
    return existing


def _consolidate_metric_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[tuple[str, str, str | None, str | None], dict[str, Any]] = {}
    order: list[tuple[str, str, str | None, str | None]] = []
    for row in rows:
        key = (
            str(row.get("dataset_family") or DATASET_FAMILY_EARNINGS_ACTUALS),
            str(row.get("metric_key") or ""),
            row.get("period_label"),
            row.get("as_of_date"),
        )
        if key not in selected:
            order.append(key)
        selected[key] = _prefer_metric_row(selected.get(key), row)
    return [selected[key] for key in order if key in selected]


def _earnings_fact_key(
    *,
    company: Company,
    family: str,
    metric_key: str,
    period_label: str | None,
    as_of_date: date | None,
    document_id: int,
) -> str:
    return ":".join(
        [
            company.slug,
            family,
            metric_key,
            period_label or (as_of_date.isoformat() if as_of_date else "undated"),
            str(document_id),
        ]
    )


def structured_fact_is_repair_excluded(fact: StructuredDataFact) -> bool:
    payload = fact.payload if isinstance(fact.payload, dict) else {}
    return bool(payload.get("repair_excluded") or payload.get("excluded_by_repair"))


def _fact_payload_source_line(fact: StructuredDataFact) -> str:
    payload = fact.payload if isinstance(fact.payload, dict) else {}
    return str(payload.get("source_line") or payload.get("line") or "")


def _is_suspect_earnings_fact(fact: StructuredDataFact) -> bool:
    family = normalize_dataset_family(fact.dataset_family)
    if family not in EARNINGS_FACT_FAMILIES:
        return False
    if structured_fact_is_repair_excluded(fact):
        return False
    metric = str(fact.metric_key or "").strip().lower()
    value = fact.value_number
    source_line = _fact_payload_source_line(fact)
    source_lower = source_line.lower()
    if metric in _CURRENCY_LIKE_METRICS:
        if value is not None and abs(float(value)) < 100_000:
            return True
        if "%" in source_line and any(token in source_lower for token in ("growth", "margin", "rate", "range represents")):
            return True
        if value is not None and _MODEL_NUMBER_RE.search(source_line):
            digits = {float(part) for part in re.findall(r"\d{2,5}", source_line)}
            if float(value) in digits:
                return True
    return False


def _infer_period_label(title: str | None, text: str | None) -> str | None:
    hay = " ".join([str(title or ""), str(text or "")])
    for pattern in (_FISCAL_Q_RE, _Q_FISCAL_RE):
        match = pattern.search(hay)
        if match:
            year = str(match.group("year"))
            if len(year) == 2:
                year = f"20{year}"
            return f"{year} Q{match.group('quarter')}"
    return None


def _candidate_lines(text: str) -> list[str]:
    raw_lines = [part.strip(" -\t") for part in re.split(r"[\r\n]+|(?<=[.!?])\s+", text) if part and part.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for line in raw_lines:
        cleaned = _clean_text(line)
        if len(cleaned) < 12:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned[:320])
    return out


def _extract_metric_from_line(line: str, metric_def: dict[str, Any]) -> dict[str, Any] | None:
    lowered = line.lower()
    alias_hit = next((alias for alias in metric_def["aliases"] if alias in lowered), None)
    if not alias_hit:
        return None
    start = lowered.find(alias_hit)
    window = line[start : start + 120]
    if metric_def["mode"] == "percent":
        match = _PERCENT_RE.search(window)
        if not match:
            return None
        value_number = _parse_scaled_number(match.group("value"), None)
        if value_number is None:
            return None
        return {
            "metric_key": metric_def["metric_key"],
            "metric_label": metric_def["label"],
            "value_number": value_number,
            "value_text": None,
            "unit": metric_def.get("unit") or "%",
            "currency": None,
            "source_line": line,
        }
    if metric_def["mode"] == "integer":
        match_obj = None
        for candidate in _INTEGER_RE.finditer(window):
            value_number = _parse_scaled_number(candidate.group("value"), None)
            if value_number is None:
                continue
            if 1900 <= value_number <= 2100:
                continue
            match_obj = candidate
            break
        if match_obj is None:
            return None
        value_number = _parse_scaled_number(match_obj.group("value"), None)
        if value_number is None:
            return None
        return {
            "metric_key": metric_def["metric_key"],
            "metric_label": metric_def["label"],
            "value_number": value_number,
            "value_text": None,
            "unit": metric_def.get("unit"),
            "currency": None,
            "source_line": line,
        }
    match_obj = None
    for candidate in _CURRENCY_VALUE_RE.finditer(window):
        value_number = _parse_scaled_number(candidate.group("value"), candidate.group("scale"))
        if value_number is None:
            continue
        has_marker = _currency_match_has_marker(candidate)
        if _currency_match_is_percent(window, candidate):
            continue
        if metric_def.get("metric_key") != "eps" and not has_marker:
            continue
        if not has_marker and 1900 <= value_number <= 2100:
            continue
        match_obj = candidate
        break
    if match_obj is None:
        return None
    value_number = _parse_scaled_number(match_obj.group("value"), match_obj.group("scale"))
    if value_number is None:
        return None
    return {
        "metric_key": metric_def["metric_key"],
        "metric_label": metric_def["label"],
        "value_number": value_number,
        "value_text": None,
        "unit": metric_def.get("unit") or "currency",
        "currency": _normalize_currency(match_obj.group("currency"), metric_def.get("default_currency")),
        "source_line": line,
    }


def extract_earnings_metric_rows(
    *,
    title: str | None,
    text: str | None,
    doc_type: str | None,
    as_of_date: date | None,
) -> list[dict[str, Any]]:
    cleaned = _clean_text(text)
    if not cleaned:
        return []
    period_label = _infer_period_label(title, cleaned)
    doc_type_norm = str(doc_type or "").strip().lower()
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for line in _candidate_lines(cleaned):
        line_lower = line.lower()
        statement_type = "guidance" if any(token in line_lower for token in GUIDANCE_HINTS) else "actual"
        if doc_type_norm in {"earnings_transcript", "earnings_release", "financial_report", "earnings_presentation"}:
            pass
        elif doc_type_norm:
            statement_type = "actual"
        for metric_def in _METRIC_DEFS:
            row = _extract_metric_from_line(line, metric_def)
            if not row:
                continue
            family = DATASET_FAMILY_GUIDANCE_ACTUALS if statement_type == "guidance" else DATASET_FAMILY_EARNINGS_ACTUALS
            dedupe_key = (family, row["metric_key"], row.get("source_line") or "")
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            row.update(
                {
                    "dataset_family": family,
                    "statement_type": statement_type,
                    "period_label": period_label,
                    "as_of_date": as_of_date.isoformat() if as_of_date else None,
                }
            )
            out.append(row)
    return _consolidate_metric_rows(out)


def summarize_evidence_tiers(values: list[str]) -> str | None:
    cleaned = [str(v or "").strip() for v in values if str(v or "").strip()]
    if not cleaned:
        return None
    counts = Counter(cleaned)
    parts = [f"{key} x{counts[key]}" for key in sorted(counts.keys())]
    return ", ".join(parts)


def upsert_structured_fact(
    session: Session,
    *,
    dataset_family: str,
    fact_key: str,
    group_key: str,
    metric_key: str,
    metric_label: str | None = None,
    statement_type: str | None = None,
    company_id: int | None = None,
    document_id: int | None = None,
    source_id: int | None = None,
    ingestion_run_id: int | None = None,
    dataset_id: int | None = None,
    period_date: date | None = None,
    period_label: str | None = None,
    as_of_date: date | None = None,
    value_number: float | None = None,
    value_text: str | None = None,
    unit: str | None = None,
    currency: str | None = None,
    evidence_tier: str | None = None,
    provenance_label: str | None = None,
    provenance_url: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[StructuredDataFact, bool]:
    family = normalize_dataset_family(dataset_family)
    now = utcnow()
    row = session.query(StructuredDataFact).filter_by(dataset_family=family, fact_key=fact_key).first()
    created = row is None
    if row is None:
        row = StructuredDataFact(
            dataset_family=family,
            fact_key=fact_key,
            group_key=group_key,
            metric_key=metric_key,
            created_at=now,
            updated_at=now,
            payload={},
        )
        session.add(row)
    row.group_key = group_key
    row.company_id = company_id
    row.document_id = document_id
    row.source_id = source_id
    row.ingestion_run_id = ingestion_run_id
    row.dataset_id = dataset_id
    row.period_date = period_date
    row.period_label = period_label
    row.as_of_date = as_of_date
    row.metric_key = metric_key
    row.metric_label = metric_label
    row.statement_type = statement_type
    row.value_number = value_number
    row.value_text = value_text
    row.unit = unit
    row.currency = currency
    row.evidence_tier = evidence_tier
    row.provenance_label = provenance_label
    row.provenance_url = provenance_url
    row.payload = dict(payload or {})
    row.updated_at = now
    session.flush()
    return row, created


def _ensure_family_dataset(
    session: Session,
    *,
    company: Company,
    family: str,
    title: str,
    description: str,
    slug: str,
    category: str,
    source_kind: str,
    extra: dict[str, Any],
) -> Dataset:
    dataset, _ = ensure_managed_company_dataset(
        session,
        company_id=int(company.id),
        title=title,
        description=description,
        category=category,
        family=family,
        status="active",
        source_kind=source_kind,
        slug=slug,
        columns=[],
        rows=[],
        label="Initial materialization",
        extra=extra,
        source_event=source_kind,
    )
    dataset.title = title
    dataset.description = description
    dataset.category = category
    dataset.family = family
    dataset.source_kind = source_kind
    dataset.updated_at = utcnow()
    merged_extra = dict(dataset.extra or {})
    merged_extra.update(extra)
    dataset.extra = merged_extra
    session.add(dataset)
    session.flush()
    return dataset


def _materialize_fact_dataset(
    session: Session,
    *,
    company: Company,
    family: str,
    facts: list[StructuredDataFact],
) -> Dataset | None:
    if not facts:
        return None
    slug = f"{company.slug}-{family.replace('_', '-')}"
    title_suffix = family.replace("_", " ").title()
    dataset = _ensure_family_dataset(
        session,
        company=company,
        family=family,
        title=f"{company.name} {title_suffix}",
        description=f"Structured {title_suffix.lower()} facts materialized from issuer-linked documents.",
        slug=slug,
        category="earnings",
        source_kind=family,
        extra={
            "company_id": int(company.id),
            "company_slug": company.slug,
            "company_name": company.name,
            "provenance_summary": "Materialized from issuer-linked earnings documents and structured fact rows.",
            "evidence_tier_summary": summarize_evidence_tiers([str(f.evidence_tier or "") for f in facts]),
            "fact_count": len(facts),
        },
    )
    columns = [
        {"key": "as_of_date", "label": "As Of", "data_type": "date", "role": "timestamp"},
        {"key": "period_label", "label": "Period", "data_type": "text", "role": "dimension"},
        {"key": "metric_key", "label": "Metric Key", "data_type": "text", "role": "dimension"},
        {"key": "metric_label", "label": "Metric", "data_type": "text", "role": "dimension"},
        {"key": "value_number", "label": "Value", "data_type": "number", "role": "metric"},
        {"key": "value_text", "label": "Value Text", "data_type": "text", "role": "dimension"},
        {"key": "unit", "label": "Unit", "data_type": "text", "role": "dimension"},
        {"key": "currency", "label": "Currency", "data_type": "text", "role": "dimension"},
        {"key": "evidence_tier", "label": "Evidence Tier", "data_type": "text", "role": "dimension"},
        {"key": "statement_type", "label": "Statement Type", "data_type": "text", "role": "dimension"},
        {"key": "source_document_id", "label": "Source Document", "data_type": "number", "role": "dimension"},
        {"key": "source_url", "label": "Source URL", "data_type": "text", "role": "dimension"},
        {"key": "provenance_label", "label": "Provenance", "data_type": "text", "role": "dimension"},
    ]
    ordered_facts = sorted(
        facts,
        key=lambda row: (
            row.as_of_date.isoformat() if row.as_of_date else "",
            row.period_label or "",
            row.metric_key or "",
            row.id or 0,
        ),
        reverse=True,
    )
    rows = [
        {
            "row_key": row.fact_key,
            "values": {
                "as_of_date": row.as_of_date.isoformat() if row.as_of_date else None,
                "period_label": row.period_label,
                "metric_key": row.metric_key,
                "metric_label": row.metric_label,
                "value_number": row.value_number,
                "value_text": row.value_text,
                "unit": row.unit,
                "currency": row.currency,
                "evidence_tier": row.evidence_tier,
                "statement_type": row.statement_type,
                "source_document_id": row.document_id,
                "source_url": row.provenance_url,
                "provenance_label": row.provenance_label,
            },
        }
        for row in ordered_facts
    ]
    replace_dataset_draft(
        session,
        dataset,
        columns=columns,
        rows=rows,
        label="Structured facts",
        extra=dict(dataset.extra or {}),
        source_event=family,
    )
    session.flush()
    for fact in facts:
        fact.dataset_id = int(dataset.id)
    session.flush()
    return dataset


def materialize_company_fact_datasets(
    session: Session,
    *,
    company: Company,
    families: set[str] | list[str] | tuple[str, ...] | None = None,
) -> list[Dataset]:
    family_values = {normalize_dataset_family(f) for f in families} if families else {
        DATASET_FAMILY_EARNINGS_ACTUALS,
        DATASET_FAMILY_GUIDANCE_ACTUALS,
    }
    datasets: list[Dataset] = []
    for family in sorted(family_values):
        lock_managed_company_dataset(
            session, company_id=int(company.id), family=family, source_kind=family,
        )
        facts = (
            session.query(StructuredDataFact)
            .filter(
                StructuredDataFact.company_id == int(company.id),
                StructuredDataFact.dataset_family == family,
            )
            .order_by(StructuredDataFact.as_of_date.desc().nullslast(), StructuredDataFact.id.desc())
            .populate_existing()
            .all()
        )
        facts = [fact for fact in facts if not structured_fact_is_repair_excluded(fact)]
        dataset = _materialize_fact_dataset(session, company=company, family=family, facts=facts)
        if dataset is not None:
            datasets.append(dataset)
    return datasets


def sync_trade_customs_facts(
    session: Session,
    *,
    source: Source,
    item: dict[str, Any],
    dataset_id: int | None = None,
    ingestion_run_id: int | None = None,
    evidence_tier: str = "issuer_confirmed",
) -> dict[str, int]:
    slug = str(item.get("slug") or "").strip()
    if not slug:
        return {"facts_created": 0, "facts_updated": 0}
    created = updated = 0
    for point in item.get("points") or []:
        if not isinstance(point, dict):
            continue
        period_raw = str(point.get("period_date") or "").strip()
        try:
            period_date = date.fromisoformat(period_raw)
        except Exception:
            continue
        metric_key = str(item.get("metric") or "value").strip() or "value"
        fact_key = f"{slug}:{metric_key}:{period_raw}"
        row, was_created = upsert_structured_fact(
            session,
            dataset_family=DATASET_FAMILY_TRADE_CUSTOMS,
            fact_key=fact_key,
            group_key=slug,
            metric_key=metric_key,
            metric_label=str(item.get("name") or metric_key),
            statement_type="actual",
            source_id=int(source.id),
            ingestion_run_id=int(ingestion_run_id) if ingestion_run_id is not None else None,
            dataset_id=int(dataset_id) if dataset_id is not None else None,
            period_date=period_date,
            period_label=period_raw,
            as_of_date=period_date,
            value_number=float(point.get("value")) if point.get("value") is not None else None,
            value_text=None,
            unit=str(item.get("unit") or "") or None,
            currency="USD" if metric_key == "value_usd" else None,
            evidence_tier=evidence_tier,
            provenance_label=str(item.get("source_label") or source.name or "") or None,
            provenance_url=str(point.get("source_ref") or item.get("source_url") or "") or None,
            payload={
                "reporter_iso": item.get("reporter_iso"),
                "partner_scope": item.get("partner_scope"),
                "flow": item.get("flow"),
                "hs_code": item.get("hs_code"),
                "metric": metric_key,
                "theme": item.get("theme"),
                "note": point.get("note"),
            },
        )
        sync_structured_fact_to_institutional(session, row)
        created += 1 if was_created else 0
        updated += 0 if was_created else 1
    return {"facts_created": created, "facts_updated": updated}


def sync_earnings_document_facts(
    session: Session,
    *,
    doc: Document,
    company: Company,
    doc_type: str | None,
    source: Source | None = None,
    ingestion_run_id: int | None = None,
    evidence_tier: str = "issuer_confirmed",
) -> dict[str, Any]:
    doc_type_norm = str(doc_type or "").strip().lower()
    if doc_type_norm not in EARNINGS_DOC_TYPES:
        return {"facts_created": 0, "facts_updated": 0, "datasets": []}
    as_of_date = doc.published_at.date() if doc.published_at else doc.ingested_at.date()
    rows = extract_earnings_metric_rows(
        title=doc.title,
        text=doc.translation_en or doc.normalized_text or doc.raw_text,
        doc_type=doc_type_norm,
        as_of_date=as_of_date,
    )
    if not rows:
        return {"facts_created": 0, "facts_updated": 0, "datasets": []}
    created = updated = 0
    touched_families = {
        str(row.get("dataset_family") or DATASET_FAMILY_EARNINGS_ACTUALS) for row in rows
    }
    # Lock all families in a stable order before upserting facts or assembling
    # snapshots, so competing multi-family ingestions cannot invert the order.
    for family in sorted(touched_families):
        lock_managed_company_dataset(
            session, company_id=int(company.id), family=family, source_kind=family,
        )
    for row in rows:
        family = str(row.get("dataset_family") or DATASET_FAMILY_EARNINGS_ACTUALS)
        period_label = str(row.get("period_label") or "") or None
        metric_key = str(row.get("metric_key") or "")
        fact_key = _earnings_fact_key(
            company=company,
            family=family,
            metric_key=metric_key,
            period_label=period_label,
            as_of_date=as_of_date,
            document_id=int(doc.id),
        )
        fact_row, was_created = upsert_structured_fact(
            session,
            dataset_family=family,
            fact_key=fact_key,
            group_key=f"{company.slug}:{family}",
            company_id=int(company.id),
            document_id=int(doc.id),
            source_id=int(source.id) if source is not None else int(doc.source_id),
            ingestion_run_id=int(ingestion_run_id) if ingestion_run_id is not None else None,
            metric_key=metric_key,
            metric_label=str(row.get("metric_label") or "") or None,
            statement_type=str(row.get("statement_type") or "") or None,
            period_label=period_label,
            as_of_date=as_of_date,
            value_number=float(row.get("value_number")) if row.get("value_number") is not None else None,
            value_text=str(row.get("value_text") or "") or None,
            unit=str(row.get("unit") or "") or None,
            currency=str(row.get("currency") or "") or None,
            evidence_tier=evidence_tier,
            provenance_label=doc.title or f"document {doc.id}",
            provenance_url=doc.url or doc.canonical_url,
            payload={"source_line": row.get("source_line"), "doc_type": doc_type_norm},
        )
        sync_structured_fact_to_institutional(session, fact_row)
        created += 1 if was_created else 0
        updated += 0 if was_created else 1

    datasets: list[dict[str, Any]] = []
    for family in sorted(touched_families):
        facts = (
            session.query(StructuredDataFact)
            .filter(
                StructuredDataFact.company_id == int(company.id),
                StructuredDataFact.dataset_family == family,
            )
            .order_by(StructuredDataFact.as_of_date.desc(), StructuredDataFact.id.desc())
            .populate_existing()
            .all()
        )
        dataset = _materialize_fact_dataset(session, company=company, family=family, facts=facts)
        if dataset is not None:
            datasets.append({"id": int(dataset.id), "slug": dataset.slug, "family": dataset.family})
    return {"facts_created": created, "facts_updated": updated, "datasets": datasets}


def sync_company_earnings_document_facts(
    session: Session,
    *,
    company: Company,
    limit: int = 12,
    evidence_tier: str = "issuer_confirmed",
) -> dict[str, Any]:
    """Backfill structured facts from already-linked earnings documents.

    Ingest normally calls `sync_earnings_document_facts` as documents arrive.
    This helper gives decision-spine sync a bounded catch-up path for older
    company documents that predate the extractor or were linked after ingest.
    """

    rows = (
        session.query(CompanyDocument, Document)
        .join(Document, Document.id == CompanyDocument.document_id)
        .filter(CompanyDocument.company_id == int(company.id))
        .filter(CompanyDocument.doc_type.in_(sorted(EARNINGS_DOC_TYPES)))
        .order_by(Document.published_at.desc().nullslast(), Document.ingested_at.desc().nullslast(), Document.id.desc())
        .limit(max(1, int(limit or 12)))
        .all()
    )
    stats: dict[str, Any] = {
        "documents_scanned": 0,
        "documents_with_facts": 0,
        "facts_created": 0,
        "facts_updated": 0,
        "datasets": [],
    }
    dataset_ids: set[int] = set()
    for company_doc, doc in rows:
        stats["documents_scanned"] += 1
        before = (
            session.query(StructuredDataFact.id)
            .filter(
                StructuredDataFact.company_id == int(company.id),
                StructuredDataFact.document_id == int(doc.id),
                StructuredDataFact.dataset_family.in_(sorted(EARNINGS_FACT_FAMILIES)),
            )
            .first()
        )
        if before is not None:
            continue
        result = sync_earnings_document_facts(
            session,
            doc=doc,
            company=company,
            doc_type=company_doc.doc_type,
            source=doc.source,
            evidence_tier=evidence_tier,
        )
        created = int(result.get("facts_created") or 0)
        updated = int(result.get("facts_updated") or 0)
        if created or updated:
            stats["documents_with_facts"] += 1
            stats["facts_created"] += created
            stats["facts_updated"] += updated
            for dataset in result.get("datasets") or []:
                try:
                    dataset_ids.add(int(dataset.get("id")))
                except Exception:
                    continue
    if dataset_ids:
        stats["datasets"] = [
            {"id": int(dataset.id), "slug": dataset.slug, "family": dataset.family}
            for dataset in session.query(Dataset).filter(Dataset.id.in_(sorted(dataset_ids))).order_by(Dataset.id.asc()).all()
        ]
    session.flush()
    return stats


def _fact_needs_refresh(fact: StructuredDataFact | None, row: dict[str, Any], *, evidence_tier: str) -> bool:
    if fact is None:
        return True
    expected_value = float(row.get("value_number")) if row.get("value_number") is not None else None
    if expected_value is None:
        if fact.value_number is not None:
            return True
    elif fact.value_number is None or abs(float(fact.value_number) - expected_value) > 0.000001:
        return True
    comparisons = {
        "metric_label": str(row.get("metric_label") or "") or None,
        "statement_type": str(row.get("statement_type") or "") or None,
        "value_text": str(row.get("value_text") or "") or None,
        "unit": str(row.get("unit") or "") or None,
        "currency": str(row.get("currency") or "") or None,
        "evidence_tier": evidence_tier,
    }
    for attr, expected in comparisons.items():
        if (getattr(fact, attr) or None) != expected:
            return True
    payload = fact.payload if isinstance(fact.payload, dict) else {}
    if str(payload.get("source_line") or "") != str(row.get("source_line") or ""):
        return True
    return structured_fact_is_repair_excluded(fact)


def repair_earnings_structured_facts(
    session: Session,
    *,
    company_id: int | None = None,
    document_id: int | None = None,
    dry_run: bool = True,
    limit: int | None = None,
    evidence_tier: str = "issuer_confirmed",
) -> dict[str, Any]:
    """Re-run current earnings extraction and quarantine older suspect fact rows.

    This is intentionally class-based: it only touches earnings/guidance facts with
    source documents, and only excludes older rows when they match known bad
    extraction shapes such as percentages or product model numbers captured as
    currency values.
    """

    fact_query = session.query(StructuredDataFact).filter(
        StructuredDataFact.dataset_family.in_(sorted(EARNINGS_FACT_FAMILIES)),
        StructuredDataFact.company_id.isnot(None),
        StructuredDataFact.document_id.isnot(None),
    )
    if company_id is not None:
        fact_query = fact_query.filter(StructuredDataFact.company_id == int(company_id))
    if document_id is not None:
        fact_query = fact_query.filter(StructuredDataFact.document_id == int(document_id))
    fact_query = fact_query.order_by(StructuredDataFact.updated_at.desc(), StructuredDataFact.id.desc())
    if limit is not None:
        fact_query = fact_query.limit(max(1, int(limit)))

    pairs: list[tuple[int, int]] = []
    seen_pairs: set[tuple[int, int]] = set()
    for fact in fact_query.all():
        key = (int(fact.company_id or 0), int(fact.document_id or 0))
        if key[0] <= 0 or key[1] <= 0 or key in seen_pairs:
            continue
        seen_pairs.add(key)
        pairs.append(key)

    stats: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "documents_scanned": 0,
        "documents_with_rows": 0,
        "facts_created": 0,
        "facts_updated": 0,
        "facts_excluded": 0,
        "datasets_materialized": 0,
        "skipped": [],
        "touched_company_ids": [],
        "excluded_fact_ids": [],
        "created_fact_keys": [],
        "updated_fact_ids": [],
    }
    touched_companies: dict[int, set[str]] = {}

    for company_key, doc_key in pairs:
        company = session.get(Company, company_key)
        doc = session.get(Document, doc_key)
        if company is None or doc is None:
            stats["skipped"].append({"company_id": company_key, "document_id": doc_key, "reason": "missing_company_or_document"})
            continue
        doc_type_norm = str(doc.kind or "").strip().lower()
        if doc_type_norm not in EARNINGS_DOC_TYPES:
            existing = (
                session.query(StructuredDataFact)
                .filter(
                    StructuredDataFact.company_id == company_key,
                    StructuredDataFact.document_id == doc_key,
                    StructuredDataFact.dataset_family.in_(sorted(EARNINGS_FACT_FAMILIES)),
                )
                .first()
            )
            if existing is not None:
                payload = existing.payload if isinstance(existing.payload, dict) else {}
                doc_type_norm = str(payload.get("doc_type") or "").strip().lower()
        if doc_type_norm not in EARNINGS_DOC_TYPES:
            stats["skipped"].append({"company_id": company_key, "document_id": doc_key, "reason": "unsupported_doc_type"})
            continue

        as_of_date = doc.published_at.date() if doc.published_at else doc.ingested_at.date()
        rows = extract_earnings_metric_rows(
            title=doc.title,
            text=doc.translation_en or doc.normalized_text or doc.raw_text,
            doc_type=doc_type_norm,
            as_of_date=as_of_date,
        )
        stats["documents_scanned"] += 1
        if rows:
            stats["documents_with_rows"] += 1

        expected: dict[str, dict[str, Any]] = {}
        for row in rows:
            family = str(row.get("dataset_family") or DATASET_FAMILY_EARNINGS_ACTUALS)
            metric_key = str(row.get("metric_key") or "")
            period_label = str(row.get("period_label") or "") or None
            fact_key = _earnings_fact_key(
                company=company,
                family=family,
                metric_key=metric_key,
                period_label=period_label,
                as_of_date=as_of_date,
                document_id=int(doc.id),
            )
            expected[fact_key] = dict(row)

        existing_facts = (
            session.query(StructuredDataFact)
            .filter(
                StructuredDataFact.company_id == company_key,
                StructuredDataFact.document_id == doc_key,
                StructuredDataFact.dataset_family.in_(sorted(EARNINGS_FACT_FAMILIES)),
            )
            .all()
        )
        existing_by_key = {fact.fact_key: fact for fact in existing_facts}

        for fact_key, row in expected.items():
            family = str(row.get("dataset_family") or DATASET_FAMILY_EARNINGS_ACTUALS)
            period_label = str(row.get("period_label") or "") or None
            existing = existing_by_key.get(fact_key)
            if not _fact_needs_refresh(existing, row, evidence_tier=evidence_tier):
                continue
            if existing is None:
                stats["facts_created"] += 1
                stats["created_fact_keys"].append(fact_key)
            else:
                stats["facts_updated"] += 1
                stats["updated_fact_ids"].append(int(existing.id))
            touched_companies.setdefault(company_key, set()).add(family)
            if dry_run:
                continue
            fact_row, _ = upsert_structured_fact(
                session,
                dataset_family=family,
                fact_key=fact_key,
                group_key=f"{company.slug}:{family}",
                company_id=company_key,
                document_id=doc_key,
                source_id=int(doc.source_id) if doc.source_id is not None else None,
                metric_key=str(row.get("metric_key") or ""),
                metric_label=str(row.get("metric_label") or "") or None,
                statement_type=str(row.get("statement_type") or "") or None,
                period_label=period_label,
                as_of_date=as_of_date,
                value_number=float(row.get("value_number")) if row.get("value_number") is not None else None,
                value_text=str(row.get("value_text") or "") or None,
                unit=str(row.get("unit") or "") or None,
                currency=str(row.get("currency") or "") or None,
                evidence_tier=evidence_tier,
                provenance_label=doc.title or f"document {doc.id}",
                provenance_url=doc.url or doc.canonical_url,
                payload={"source_line": row.get("source_line"), "doc_type": doc_type_norm, "repaired_by": "earnings_structured_fact_repair"},
            )
            sync_structured_fact_to_institutional(session, fact_row)

        for fact in existing_facts:
            if fact.fact_key in expected or not _is_suspect_earnings_fact(fact):
                continue
            stats["facts_excluded"] += 1
            stats["excluded_fact_ids"].append(int(fact.id))
            touched_companies.setdefault(company_key, set()).add(normalize_dataset_family(fact.dataset_family))
            if dry_run:
                continue
            payload = dict(fact.payload or {})
            payload.update(
                {
                    "repair_excluded": True,
                    "repair_reason": "current_extractor_rejects_suspect_earnings_fact",
                    "repair_checked_at": utcnow().isoformat(),
                }
            )
            fact.payload = payload
            fact.dataset_id = None
            fact.updated_at = utcnow()
            session.add(fact)

    if not dry_run:
        for touched_company_id, families in touched_companies.items():
            company = session.get(Company, int(touched_company_id))
            if company is None:
                continue
            datasets = materialize_company_fact_datasets(session, company=company, families=families)
            stats["datasets_materialized"] += len(datasets)
        session.flush()

    stats["touched_company_ids"] = sorted(touched_companies.keys())
    return stats
