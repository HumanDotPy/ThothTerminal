from __future__ import annotations

import re
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.dataset_service import (
    create_dataset,
    ensure_dataset_tables,
    replace_dataset_draft,
    serialize_dataset,
)
from thoth_terminal.db import utcnow
from thoth_terminal.issuer_identity import enrich_metadata_with_identity
from thoth_terminal.localized_names import resolve_taiwan_company_name, resolve_taiwan_industry_name
from thoth_terminal.models import DataPoint, DataSeries, Dataset, Source


MONTHLY_REVENUE_CATEGORY = "issuer_monthly_revenue"
MONTHLY_REVENUE_FAMILY = "monthly_revenue"


def _slugify(value: object, *, default: str = "series") -> str:
    raw = str(value or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug or default


def _safe_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value or "").strip().replace(",", "")
    if raw in {"", "-", "nan", "NaN"}:
        return None
    try:
        return float(raw)
    except Exception:
        return None


def _parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except Exception:
        return None


def _series_slug(row: dict[str, Any]) -> str:
    return _slugify(
        f"tw-mops-monthly-revenue-{row.get('market') or 'tw'}-{row.get('company_code')}",
        default="tw-mops-monthly-revenue",
    )


def _company_identity(row: dict[str, Any]) -> dict[str, object]:
    original = row.get("company_name_original") or row.get("company_name")
    resolved = resolve_taiwan_company_name(company_code=row.get("company_code"), local_name=original)
    english = str(row.get("company_name_en") or resolved.get("company_name_en") or "").strip()
    display = str(
        row.get("company_display_name")
        or english
        or resolved.get("company_name")
        or row.get("company_name")
        or row.get("company_code")
        or "Taiwan issuer"
    ).strip()
    aliases = list(resolved.get("company_aliases") or [])
    row_aliases = row.get("company_aliases") or []
    if isinstance(row_aliases, str):
        row_aliases = [item.strip() for item in row_aliases.split(",")]
    for value in row_aliases:
        text = str(value or "").strip()
        if text and text.casefold() not in {str(alias).casefold() for alias in aliases}:
            aliases.append(text)
    return {
        "display": display,
        "english": english or None,
        "original": str(original or "").strip() or None,
        "aliases": aliases,
        "source": row.get("company_name_source") or resolved.get("company_name_source"),
    }


def _industry_identity(row: dict[str, Any]) -> dict[str, object]:
    original = row.get("industry_original") or row.get("industry")
    resolved = resolve_taiwan_industry_name(original)
    english = str(row.get("industry_en") or resolved.get("industry_en") or "").strip()
    display = str(
        row.get("industry_display_name") or english or resolved.get("industry") or row.get("industry") or ""
    ).strip()
    return {
        "display": display or None,
        "english": english or None,
        "original": str(original or "").strip() or None,
        "source": row.get("industry_name_source") or resolved.get("industry_name_source"),
    }


def _series_name(row: dict[str, Any]) -> str:
    company = str(_company_identity(row).get("display") or row.get("company_code") or "Taiwan issuer").strip()
    code = str(row.get("company_code") or "").strip()
    suffix = f" ({code})" if code else ""
    return f"Taiwan MOPS Monthly Revenue · {company}{suffix}"


def _dataset_columns() -> list[dict[str, Any]]:
    return [
        {"key": "period_date", "label": "Period", "data_type": "date", "role": "timestamp"},
        {"key": "market", "label": "Market", "data_type": "text", "role": "dimension"},
        {"key": "company_code", "label": "Code", "data_type": "text", "role": "dimension"},
        {"key": "issuer_id", "label": "Issuer ID", "data_type": "number", "role": "id"},
        {"key": "company_id", "label": "Company ID", "data_type": "number", "role": "id"},
        {"key": "security_id", "label": "Security ID", "data_type": "number", "role": "id"},
        {"key": "company_name", "label": "Company", "data_type": "text", "role": "dimension"},
        {"key": "company_name_original", "label": "Company Original", "data_type": "text", "role": "dimension"},
        {"key": "company_aliases", "label": "Company Aliases", "data_type": "text", "role": "dimension"},
        {"key": "source_identifiers", "label": "Source Identifiers", "data_type": "text", "role": "id"},
        {"key": "identity_match_basis", "label": "Identity Match", "data_type": "text", "role": "dimension"},
        {"key": "identity_confidence", "label": "Identity Confidence", "data_type": "number", "role": "metric"},
        {"key": "industry", "label": "Industry", "data_type": "text", "role": "dimension"},
        {"key": "industry_original", "label": "Industry Original", "data_type": "text", "role": "dimension"},
        {"key": "revenue_ntd_thousand", "label": "Revenue (NT$k)", "data_type": "number", "role": "metric"},
        {"key": "mom_pct", "label": "MoM %", "data_type": "number", "role": "metric"},
        {"key": "yoy_pct", "label": "YoY %", "data_type": "number", "role": "metric"},
        {"key": "cumulative_revenue_ntd_thousand", "label": "YTD Revenue (NT$k)", "data_type": "number", "role": "metric"},
        {"key": "cumulative_yoy_pct", "label": "YTD YoY %", "data_type": "number", "role": "metric"},
        {"key": "report_date", "label": "Report Date", "data_type": "date", "role": "dimension"},
        {"key": "source_url", "label": "Source", "data_type": "text", "role": "dimension"},
        {"key": "note", "label": "Note", "data_type": "text", "role": "dimension"},
    ]


def _dataset_row_values(row: dict[str, Any]) -> dict[str, Any]:
    company = _company_identity(row)
    industry = _industry_identity(row)
    identity = row.get("identity_resolution") if isinstance(row.get("identity_resolution"), dict) else {}
    source_identifiers = row.get("source_identifiers") or identity.get("source_identifiers") or []
    if isinstance(source_identifiers, list):
        source_identifier_text = ", ".join(
            f"{entry.get('id_type')}:{entry.get('id_value')}"
            for entry in source_identifiers
            if isinstance(entry, dict) and str(entry.get("id_type") or "").strip() and str(entry.get("id_value") or "").strip()
        )
    else:
        source_identifier_text = str(source_identifiers or "")
    return {
        "period_date": row.get("period_date"),
        "market": row.get("market"),
        "company_code": row.get("company_code"),
        "issuer_id": row.get("issuer_id") or identity.get("issuer_id"),
        "company_id": row.get("company_id") or identity.get("company_id"),
        "security_id": row.get("security_id") or identity.get("security_id"),
        "company_name": company.get("display"),
        "company_name_original": (
            company.get("original")
            if company.get("original") and company.get("original") != company.get("display")
            else ""
        ),
        "company_aliases": ", ".join(
            str(alias) for alias in (company.get("aliases") or []) if str(alias or "").strip()
        ),
        "source_identifiers": source_identifier_text,
        "identity_match_basis": identity.get("match_basis"),
        "identity_confidence": identity.get("confidence"),
        "industry": industry.get("display"),
        "industry_original": (
            industry.get("original")
            if industry.get("original") and industry.get("original") != industry.get("display")
            else ""
        ),
        "revenue_ntd_thousand": _safe_float(row.get("revenue_ntd_thousand")),
        "mom_pct": _safe_float(row.get("mom_pct")),
        "yoy_pct": _safe_float(row.get("yoy_pct")),
        "cumulative_revenue_ntd_thousand": _safe_float(row.get("cumulative_revenue_ntd_thousand")),
        "cumulative_yoy_pct": _safe_float(row.get("cumulative_yoy_pct")),
        "report_date": row.get("report_date"),
        "source_url": row.get("source_url"),
        "note": row.get("note") or "",
    }


def _dataset_slug(market: str) -> str:
    return _slugify(f"taiwan-mops-monthly-revenue-{market}", default="taiwan-mops-monthly-revenue")


def _dataset_title(market: str) -> str:
    if market == "listed":
        return "Taiwan MOPS Monthly Revenue (Listed Companies)"
    if market == "public":
        return "Taiwan MOPS Monthly Revenue (Public Companies)"
    return f"Taiwan MOPS Monthly Revenue ({market.title()})"


def _sync_market_dataset(
    session: Session,
    *,
    source: Source,
    market: str,
    rows: list[dict[str, Any]],
    ingestion_run_id: int | None,
) -> tuple[Dataset, int]:
    ensure_dataset_tables(session)
    slug = _dataset_slug(market)
    dataset = session.query(Dataset).filter_by(slug=slug).first()
    extra = {
        "provenance_summary": "Official Taiwan MOPS/TWSE OpenAPI monthly operating revenue feed.",
        "evidence_tier_summary": "official_exchange",
        "source_name": source.name,
        "source_kind": "taiwan_mops_monthly_revenue",
        "identity_resolution": {
            "mode": "identifier_first",
            "jurisdiction": "TW",
            "source": "taiwan_mops_monthly_revenue",
            "primary_identifier": "TW_STOCK",
        },
        "unit": "NT$ thousand",
        "currency": "TWD",
    }
    if dataset is None:
        dataset = create_dataset(
            session,
            title=_dataset_title(market),
            description="Company-level Taiwan monthly operating revenue from the official MOPS/TWSE OpenAPI feed. Values are reported in NT$ thousand.",
            category="issuer",
            family=MONTHLY_REVENUE_FAMILY,
            source_kind="taiwan_mops_monthly_revenue",
            slug=slug,
            columns=_dataset_columns(),
            rows=[],
            extra=extra,
        )
    else:
        dataset.title = _dataset_title(market)
        dataset.description = dataset.description or "Company-level Taiwan monthly operating revenue from the official MOPS/TWSE OpenAPI feed. Values are reported in NT$ thousand."
        dataset.category = "issuer"
        dataset.family = MONTHLY_REVENUE_FAMILY
        dataset.source_kind = "taiwan_mops_monthly_revenue"
        dataset.extra = {**dict(dataset.extra or {}), **extra}

    existing_by_key: dict[str, dict[str, Any]] = {}
    try:
        payload = serialize_dataset(session, dataset, include_rows=True, limit=200_000)
        for existing in payload.get("rows") or []:
            if not isinstance(existing, dict):
                continue
            row_key = str(existing.get("row_key") or "").strip()
            values = existing.get("values") if isinstance(existing.get("values"), dict) else None
            if row_key and values is not None:
                existing_by_key[row_key] = dict(values)
    except Exception:
        existing_by_key = {}

    for row in rows:
        period = str(row.get("period_date") or "").strip()
        code = str(row.get("company_code") or "").strip()
        if not period or not code:
            continue
        existing_by_key[f"{period}:{market}:{code}"] = _dataset_row_values(row)

    merged_rows = [
        {"row_key": key, "values": values}
        for key, values in sorted(
            existing_by_key.items(),
            key=lambda item: (
                str(item[1].get("period_date") or ""),
                str(item[1].get("company_code") or ""),
            ),
        )
    ]
    replace_dataset_draft(
        session,
        dataset,
        columns=_dataset_columns(),
        rows=merged_rows,
        label=f"official refresh {utcnow().date().isoformat()}",
        extra=dict(dataset.extra or {}),
        source_event=f"ingestion_run:{ingestion_run_id}" if ingestion_run_id is not None else "manual_refresh",
    )
    return dataset, len(merged_rows)


def upsert_monthly_revenue_from_stats(
    session: Session,
    *,
    source: Source,
    fetch_stats: dict[str, Any] | None,
    ingestion_run_id: int | None = None,
) -> dict[str, int]:
    stats = dict(fetch_stats or {})
    if stats.get("source_kind") != "taiwan_mops_monthly_revenue":
        return {}
    raw_rows = stats.get("monthly_revenue_rows")
    if not isinstance(raw_rows, list):
        return {}
    rows = [dict(row) for row in raw_rows if isinstance(row, dict)]
    now = utcnow()
    series_created = 0
    series_updated = 0
    points_created = 0
    points_updated = 0

    rows_by_market: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        market = str(row.get("market") or "listed").strip().lower() or "listed"
        try:
            enrich_metadata_with_identity(
                session,
                row,
                source="taiwan_mops_monthly_revenue",
                author=row.get("company_display_name") or row.get("company_name_en") or row.get("company_name"),
            )
        except Exception:
            pass
        rows_by_market.setdefault(market, []).append(row)
        period = _parse_date(row.get("period_date"))
        value = _safe_float(row.get("revenue_ntd_thousand"))
        code = str(row.get("company_code") or "").strip()
        if period is None or value is None or not code:
            continue
        slug = _series_slug(row)
        series = session.query(DataSeries).filter_by(slug=slug).first()
        company = _company_identity(row)
        industry = _industry_identity(row)
        extra = {
            "connector": "taiwan_mops_monthly_revenue",
            "market": market,
            "company_code": code,
            "issuer_id": row.get("issuer_id"),
            "company_id": row.get("company_id"),
            "security_id": row.get("security_id"),
            "identity_resolution": row.get("identity_resolution"),
            "source_identifiers": row.get("source_identifiers") or [],
            "company_name": company.get("display"),
            "company_name_en": company.get("english"),
            "company_name_original": company.get("original"),
            "company_aliases": company.get("aliases") or [],
            "company_name_source": company.get("source"),
            "industry": industry.get("display"),
            "industry_en": industry.get("english"),
            "industry_original": industry.get("original"),
            "industry_name_source": industry.get("source"),
            "currency": "TWD",
            "unit_scale": 1000,
            "unit_display": "NT$",
            "official_unit": "NT$ thousand",
            "source_url": row.get("source_url"),
            "endpoint_path": row.get("endpoint_path"),
        }
        if series is None:
            series = DataSeries(
                slug=slug,
                name=_series_name(row),
                description="Official Taiwan MOPS/TWSE monthly operating revenue. Raw values are NT$ thousand.",
                category=MONTHLY_REVENUE_CATEGORY,
                unit="NT$ thousand",
                frequency="monthly",
                source_label="TWSE/MOPS OpenAPI",
                axis_type="value",
                chart_type="bar",
                color="#ffbf49",
                created_at=now,
                updated_at=now,
                extra=extra,
            )
            session.add(series)
            session.flush()
            series_created += 1
        else:
            series.name = _series_name(row)
            series.description = series.description or "Official Taiwan MOPS/TWSE monthly operating revenue. Raw values are NT$ thousand."
            series.category = MONTHLY_REVENUE_CATEGORY
            series.unit = "NT$ thousand"
            series.frequency = "monthly"
            series.source_label = "TWSE/MOPS OpenAPI"
            series.chart_type = "bar"
            series.extra = {**dict(series.extra or {}), **extra}
            series.updated_at = now
            series_updated += 1

        point = session.query(DataPoint).filter_by(series_id=int(series.id), period_date=period, is_estimate=False).first()
        note_parts = []
        if row.get("mom_pct") is not None:
            note_parts.append(f"MoM {float(row['mom_pct']):+.1f}%")
        if row.get("yoy_pct") is not None:
            note_parts.append(f"YoY {float(row['yoy_pct']):+.1f}%")
        note = " · ".join(note_parts)[:512] or None
        if point is None:
            session.add(
                DataPoint(
                    series_id=int(series.id),
                    period_date=period,
                    value=value,
                    is_estimate=False,
                    revision=0,
                    source_ref=str(row.get("source_url") or "")[:255] or None,
                    note=note,
                    created_at=now,
                    updated_at=now,
                )
            )
            points_created += 1
        else:
            point.value = value
            point.source_ref = str(row.get("source_url") or "")[:255] or None
            point.note = note
            point.revision = int(point.revision or 0) + 1
            point.updated_at = now
            points_updated += 1

    datasets_synced = 0
    dataset_rows = 0
    for market, market_rows in rows_by_market.items():
        try:
            _dataset, count = _sync_market_dataset(
                session,
                source=source,
                market=market,
                rows=market_rows,
                ingestion_run_id=ingestion_run_id,
            )
            datasets_synced += 1
            dataset_rows += count
        except Exception:
            continue

    return {
        "series_created": series_created,
        "series_updated": series_updated,
        "points_created": points_created,
        "points_updated": points_updated,
        "datasets_synced": datasets_synced,
        "dataset_rows": dataset_rows,
    }
