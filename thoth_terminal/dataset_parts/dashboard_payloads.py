from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date, datetime
from io import BytesIO, StringIO
from threading import RLock
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.dataset_catalog import (
    DATASET_FAMILY_COLUMN_TEMPLATES,
    DATASET_FAMILY_MATERIALIZER_FAMILIES,
    DATASET_FAMILY_META,
    DATASET_FAMILY_REQUIRED_ROLES,
    DATASET_OBJECT_TYPES,
    DATASET_VIEW_OPERATOR_VALUES,
    DATASET_VIEW_OPERATORS,
)
from thoth_terminal.models import (
    ChartDatasetView,
    Dataset,
    DatasetColumn,
    Document,
    DatasetLink,
    DatasetRow,
    DatasetVersion,
    DatasetView,
    InstrumentRequest,
    ProductStageEntry,
    StructuredDataFact,
)
from thoth_terminal.product_stage_service import serialize_product_stage_entry





def _format_compact_number(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    abs_value = abs(float(value))
    sign = "-" if float(value) < 0 else ""
    if abs_value >= 1_000_000_000_000:
        return f"{sign}{abs_value / 1_000_000_000_000:.2f}T"
    if abs_value >= 1_000_000_000:
        return f"{sign}{abs_value / 1_000_000_000:.2f}B"
    if abs_value >= 1_000_000:
        return f"{sign}{abs_value / 1_000_000:.2f}M"
    if abs_value >= 10_000:
        return f"{sign}{abs_value / 1_000:.1f}K"
    if isinstance(value, int) or float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _kpi_card(label: str, value: Any, *, detail: str | None = None, tone: str = "neutral") -> dict[str, Any]:
    return {
        "label": label,
        "value": _format_compact_number(value) if value not in (None, "") else "—",
        "raw_value": value,
        "detail": detail,
        "tone": tone,
    }


def _identity_items(dataset: Dataset) -> list[dict[str, Any]]:
    extra = dict(dataset.extra or {})
    items = []
    identity_map = [
        ("reporter_iso", "Reporter"),
        ("partner_scope", "Partner Scope"),
        ("flow", "Flow"),
        ("hs_code", "HS Code"),
        ("hs_revision", "HS Revision"),
        ("metric", "Metric"),
        ("source_name", "Source"),
        ("source_label", "Source Label"),
    ]
    for key, label in identity_map:
        value = str(extra.get(key) or "").strip()
        if value:
            items.append({"label": label, "value": value})
    if dataset.source_kind:
        items.append({"label": "Source Kind", "value": dataset.source_kind})
    return items


def _recent_table(columns: list[dict[str, Any]], rows: list[dict[str, Any]], *, limit: int = 6, column_limit: int = 5) -> dict[str, Any] | None:
    if not rows or not columns:
        return None
    interesting = columns[:column_limit]
    return {
        "title": "Recent Rows",
        "kind": "table",
        "columns": [{"key": str(col.get("key") or ""), "label": str(col.get("label") or col.get("key") or "")} for col in interesting],
        "rows": [
            {
                "row_key": str(row.get("row_key") or ""),
                "values": {str(col.get("key") or ""): (row.get("values") or {}).get(str(col.get("key") or "")) for col in interesting},
            }
            for row in rows[-limit:]
        ],
    }


def _filing_recent_table(columns: list[dict[str, Any]], rows: list[dict[str, Any]], *, limit: int = 10) -> dict[str, Any] | None:
    if not rows:
        return None
    label_by_key = {
        str(col.get("key") or ""): str(col.get("label") or col.get("key") or "")
        for col in columns
        if str(col.get("key") or "")
    }
    row_value_keys = {
        str(key)
        for row in rows
        for key in ((row.get("values") or {}).keys() if isinstance(row.get("values"), dict) else [])
    }
    preferred_keys = [
        "document_date",
        "title",
        "form_type",
        "document_type",
        "source_name",
        "source_document_id",
        "accession_number",
    ]
    if "source_document_id" not in row_value_keys and "accession_number" not in row_value_keys:
        preferred_keys.append("source_url")
    display_keys = [key for key in preferred_keys if key in row_value_keys]
    if not display_keys:
        return _recent_table(columns, rows, limit=limit, column_limit=8)

    def clean(value: Any) -> str:
        text = str(value or "").strip()
        return "" if text.lower() in {"none", "null", "nan"} else text

    def filing_key(row: dict[str, Any]) -> tuple[str, str]:
        values = row.get("values") or {}
        for key in ("accession_number", "source_url", "source_document_id"):
            text = clean(values.get(key)).lower()
            if text:
                return (key, text)
        visible = "|".join(clean(values.get(key)).lower() for key in ("document_date", "title", "form_type", "document_type", "source_name"))
        return ("visible", visible or str(row.get("row_key") or ""))

    def sort_key(row: dict[str, Any]) -> tuple[date, str]:
        values = row.get("values") or {}
        row_date = _parse_iso_date(values.get("document_date") or values.get("published_at") or values.get("ingested_at")) or date.min
        row_identity = clean(values.get("accession_number") or values.get("source_document_id") or row.get("row_key"))
        return (row_date, row_identity)

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in sorted(rows, key=sort_key, reverse=True):
        key = filing_key(row)
        if key in seen:
            continue
        seen.add(key)
        values = row.get("values") or {}
        deduped.append(
            {
                "row_key": str(row.get("row_key") or ""),
                "values": {key: clean(values.get(key)) or None for key in display_keys},
            }
        )
        if len(deduped) >= max(1, int(limit)):
            break

    return {
        "title": "Recent Filing Events",
        "kind": "table",
        "wide": True,
        "description": "Newest unique filing events with source identity visible.",
        "columns": [
            {
                "key": key,
                "label": label_by_key.get(key) or key.replace("_", " ").title(),
                "class_name": "mono" if key in {"source_document_id", "accession_number", "source_url"} else "",
            }
            for key in display_keys
        ],
        "rows": deduped,
    }


def _related_object_counts(links: list[DatasetLink]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for link in links:
        key = str(link.object_type or "").strip().lower()
        counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [{"label": key.replace("_", " "), "value": value} for key, value in ordered]


def _dashboard_lineage(dataset: Dataset, facts: list[StructuredDataFact]) -> dict[str, Any]:
    latest = facts[0] if facts else None
    return {
        "summary": str((dataset.extra or {}).get("provenance_summary") or "").strip() or None,
        "source_kind": dataset.source_kind,
        "fact_count": len(facts),
        "latest_fact_at": latest.updated_at.isoformat() if latest and latest.updated_at else None,
        "latest_import": {
            "label": latest.provenance_label or latest.metric_label or latest.metric_key,
            "url": latest.provenance_url,
            "evidence_tier": latest.evidence_tier,
        }
        if latest
        else None,
        "items": [
            {
                "metric": row.metric_label or row.metric_key,
                "period": row.period_label or (row.as_of_date.isoformat() if row.as_of_date else None),
                "value": row.value_number if row.value_number is not None else row.value_text,
                "evidence_tier": row.evidence_tier,
                "label": row.provenance_label,
                "url": row.provenance_url,
            }
            for row in facts[:6]
        ],
    }


def _trade_customs_dashboard(
    dataset: Dataset,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    facts: list[StructuredDataFact],
    links: list[DatasetLink],
) -> dict[str, Any]:
    family = dataset_family_meta(dataset.family)
    metric_column = _first_column_by_role(columns, "metric")
    timestamp_column = _first_column_by_role(columns, "timestamp")
    timestamp_key = str(timestamp_column.get("key") or "") if timestamp_column else None
    metric_key = str(metric_column.get("key") or "") if metric_column else None
    sorted_rows = _rows_sorted_by_timestamp(rows, timestamp_key)
    today = utcnow().date()
    future_rows = [
        row
        for row in sorted_rows
        if timestamp_key and (_parse_dataset_date((row.get("values") or {}).get(timestamp_key)) or date.min) > today
    ]
    usable_rows = [
        row
        for row in sorted_rows
        if not timestamp_key or (_parse_dataset_date((row.get("values") or {}).get(timestamp_key)) or date.min) <= today
    ]
    points = _metric_points(usable_rows, timestamp_key=timestamp_key, metric_key=metric_key)
    latest_point = points[-1] if points else None
    prior_point = points[-2] if len(points) > 1 else None
    yoy_point = _same_month_prior_year(points, latest_point)
    latest_metric = latest_point.get("value") if latest_point else None
    prior_metric = prior_point.get("value") if prior_point else None
    change, change_pct = _delta_parts(latest_point, prior_point)
    yoy_change, yoy_change_pct = _delta_parts(latest_point, yoy_point)
    range_detail = None
    if len(points) > 1:
        range_detail = f"{points[0].get('date_label')} to {points[-1].get('date_label')}"
    dimension_items = []
    for key in ("reporter_iso", "partner_scope", "partner_iso", "flow", "hs_code", "theme"):
        summary = _dimension_summary(rows, key)
        if summary:
            dimension_items.append({"label": key.replace("_", " ").title(), "items": summary})
    extra = dict(dataset.extra or {})

    def _trade_identity_value(key: str) -> str:
        value = str(extra.get(key) or "").strip()
        if value:
            return value
        summary = _dimension_summary(rows, key, limit=3)
        if not summary:
            return ""
        return ", ".join(str(item.get("label") or "") for item in summary if str(item.get("label") or "").strip())

    reporter = _trade_identity_value("reporter_iso")
    partner = _trade_identity_value("partner_iso") or _trade_identity_value("partner_scope")
    flow = _trade_identity_value("flow")
    hs_code = _trade_identity_value("hs_code")
    mirror_columns = {str(col.get("key") or "").strip().lower() for col in columns}
    mirror_present = bool(mirror_columns & {"mirror_value", "mirror_reporter_iso", "mirror_partner_iso", "mirror_flow"})
    country_hs_lens, trade_group_count = _trade_country_hs_lens(
        dataset,
        columns,
        usable_rows,
        timestamp_key=timestamp_key,
        metric_key=metric_key,
        metric_column=metric_column,
        mirror_present=mirror_present,
    )
    mirror_coverage, mirror_counts = _trade_mirror_coverage_card(
        dataset,
        usable_rows,
        timestamp_key=timestamp_key,
        metric_key=metric_key,
        mirror_present=mirror_present,
    )
    multi_group = trade_group_count > 1
    linked_types = {str(link.object_type or "").strip().lower() for link in links}
    linked_decision_surface = bool(linked_types & {"company", "issuer", "instrument_request", "product_stage", "supply_chain", "thesis"})
    groups_needing_mirror = int(mirror_counts.get("Needed", 0)) + int(mirror_counts.get("Schema empty", 0))
    mirrored_groups = int(mirror_counts.get("Present", 0))
    world_groups = int(mirror_counts.get("World aggregate", 0))
    total_mirror_groups = sum(int(value) for value in mirror_counts.values())
    if groups_needing_mirror:
        suffix = "group" if groups_needing_mirror == 1 else "groups"
        verb = "needs" if groups_needing_mirror == 1 else "need"
        mirror_value = f"Partial: {groups_needing_mirror} {suffix} {verb} mirror values" if mirrored_groups else (
            "Schema empty" if mirror_counts.get("Schema empty") else "Not present"
        )
    elif mirrored_groups:
        mirror_value = "Mirror fields present"
    elif total_mirror_groups and world_groups == total_mirror_groups:
        mirror_value = "Not required for world aggregate"
    elif mirror_present:
        mirror_value = "Mirror fields present"
    else:
        mirror_value = "Not present"
    if not linked_decision_surface:
        next_action = "Link this dataset to a company, supply chain, thesis, stage entry, or instrument request."
    elif future_rows:
        next_action = "Review quarantined future rows before using the latest-period claim."
    elif yoy_point is None:
        next_action = "Backfill the same-month prior-year point before relying on YoY interpretation."
    elif groups_needing_mirror or mirror_value == "Not present":
        next_action = "Add partner mirror flow before making strong bilateral-flow conclusions."
    else:
        next_action = "Ready for linked analyst review; treat monthly customs data as lagged evidence."
    recent_table = _recent_table(columns, usable_rows, limit=6, column_limit=6)
    sections: list[dict[str, Any]] = []
    if country_hs_lens is not None:
        sections.append(country_hs_lens)
    if mirror_coverage is not None:
        sections.append(mirror_coverage)
    if points:
        sections.append(
            {
                "title": "Trade Interpretation",
                "kind": "pairs",
                "items": [
                    {
                        "label": "Latest period",
                        "value": latest_point.get("date_label") if latest_point else "—",
                        "meta": _column_label(metric_column, metric_key or "metric"),
                    },
                    {
                        "label": "Sequential move",
                        "value": "By group" if multi_group else _format_delta(change, change_pct),
                        "meta": "Computed in Country / HS Lens" if multi_group else "Latest minus prior period",
                    },
                    {
                        "label": "YoY move",
                        "value": "By group" if multi_group else _format_delta(yoy_change, yoy_change_pct),
                        "meta": "Computed in Country / HS Lens" if multi_group else (f"Compared with {yoy_point.get('date_label')}" if yoy_point else "No same-month prior-year point"),
                    },
                    {
                        "label": "Coverage",
                        "value": f"{len(points)} period rows",
                        "meta": f"{trade_group_count} reporter/partner/flow/HS group(s)" if multi_group else range_detail,
                    },
                ],
            }
        )
    sections.append(
        {
            "title": "Analyst Checks",
            "kind": "pairs",
            "items": [
                {
                    "label": "Reporter / Partner / Flow",
                    "value": " / ".join(part for part in (reporter or "—", partner or "—", flow or "—")),
                    "meta": "Required identity before interpreting a trade move",
                },
                {"label": "HS code", "value": hs_code or "—", "meta": "Validate code fit before linking to a thesis"},
                {
                    "label": "Mirror flow",
                    "value": mirror_value,
                    "meta": "Use partner mirror data when a bilateral conclusion depends on it",
                },
                {
                    "label": "Decision linkage",
                    "value": "Linked" if linked_decision_surface else "Missing",
                    "meta": "Dataset should attach to the object it informs",
                },
                {"label": "Next action", "value": next_action, "meta": "Analyst handoff"},
            ],
        }
    )
    if dimension_items:
        sections.append({"title": "Trade Identity", "kind": "grouped-list", "groups": dimension_items})
    if recent_table is not None:
        sections.append(recent_table)
    chart_cards = _trade_grouped_chart_cards(
        dataset,
        columns,
        usable_rows,
        timestamp_key=timestamp_key,
        metric_key=metric_key,
        metric_column=metric_column,
    ) or _chart_cards(columns, usable_rows)
    return {
        "family": dataset.family,
        "family_label": family["label"],
        "summary": family["summary"],
        "framing": "Institutional customs dashboard with reporter/partner identity, latest move, and lineage context.",
        "kpis": [
            _kpi_card("Latest", latest_metric, detail=f"Most recent point: {latest_point.get('date_label')}" if latest_point else "Most recent metric point", tone="confirmed"),
            _kpi_card(
                "MoM",
                "By group" if multi_group else (f"{change_pct:+.1f}%" if change_pct is not None else None),
                detail="See Country / HS Lens" if multi_group else "Latest versus prior period",
            ),
            _kpi_card(
                "YoY",
                "By group" if multi_group else (f"{yoy_change_pct:+.1f}%" if yoy_change_pct is not None else None),
                detail="See Country / HS Lens" if multi_group else (f"Latest versus {yoy_point.get('date_label')}" if yoy_point else "Needs same-month prior-year point"),
            ),
            _kpi_card("Coverage", len(points), detail=f"{trade_group_count} reporter/partner/flow/HS group(s)" if multi_group else (range_detail or "Usable metric periods")),
        ],
        "identity": _identity_items(dataset),
        "chart_cards": chart_cards,
        "cards": sections,
        "lineage": _dashboard_lineage(dataset, facts),
        "related_objects": _related_object_counts(links),
    }


def _issuer_dashboard(
    dataset: Dataset,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    facts: list[StructuredDataFact],
    links: list[DatasetLink],
    forward: bool,
) -> dict[str, Any]:
    family = dataset_family_meta(dataset.family)
    profile = build_dataset_profile(columns, rows)
    issuer = ""
    for key in ("company", "company_name", "issuer", "ticker"):
        summary = _dimension_summary(rows, key, limit=1)
        if summary:
            issuer = summary[0]["label"]
            break
    recent_table = _recent_table(columns, rows, limit=8, column_limit=6)
    sections: list[dict[str, Any]] = []
    if recent_table is not None:
        recent_table["title"] = "Period Table"
        sections.append(recent_table)
    if facts:
        sections.append(
            {
                "title": "Source Document Lineage",
                "kind": "list",
                "items": [
                    {
                        "label": row.provenance_label or row.metric_label or row.metric_key,
                        "value": row.period_label or (row.as_of_date.isoformat() if row.as_of_date else "undated"),
                        "meta": row.evidence_tier,
                        "url": row.provenance_url,
                    }
                    for row in facts[:6]
                ],
            }
        )
    badge = "Forward-looking issuer guidance" if forward else "Issuer-reported actuals"
    return {
        "family": dataset.family,
        "family_label": family["label"],
        "summary": family["summary"],
        "framing": badge,
        "kpis": [
            _kpi_card("Issuer", issuer or dataset.title, detail="Primary issuer context", tone="confirmed"),
            _kpi_card("Metrics", profile.get("metric_count"), detail="Numeric measures in current view"),
            _kpi_card("Periods", profile.get("row_count"), detail="Rows available"),
            _kpi_card("Evidence", str((dataset.extra or {}).get("evidence_tier_summary") or "").strip() or "issuer_confirmed", detail="Dataset evidence tier"),
        ],
        "identity": [
            {"label": "Issuer", "value": issuer or dataset.title},
            {"label": "Source Kind", "value": dataset.source_kind or ("guidance" if forward else "issuer")},
            {"label": "Framing", "value": badge},
        ],
        "chart_cards": _chart_cards(columns, rows),
        "cards": sections,
        "lineage": _dashboard_lineage(dataset, facts),
        "related_objects": _related_object_counts(links),
    }


def _filing_dashboard(
    dataset: Dataset,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    facts: list[StructuredDataFact],
    links: list[DatasetLink],
) -> dict[str, Any]:
    family = dataset_family_meta(dataset.family)
    profile = build_dataset_profile(columns, rows)
    company = str((dataset.extra or {}).get("company_name") or "").strip()
    if not company:
        summary = _dimension_summary(rows, "company", limit=1)
        company = str(summary[0]["label"]) if summary else dataset.title
    values = [row.get("values") or {} for row in rows]
    earnings_count = sum(
        1
        for row in values
        if bool(row.get("is_earnings_release")) or str(row.get("document_type") or "").strip().lower() == "earnings_release"
    )
    latest_date = None
    for row in values:
        parsed = _parse_iso_date(row.get("document_date") or row.get("published_at") or row.get("ingested_at"))
        if parsed is not None and (latest_date is None or parsed > latest_date):
            latest_date = parsed
    form_groups = []
    for key, label in (("form_type", "Forms"), ("connector", "Connectors"), ("document_type", "Document Types")):
        summary = _dimension_summary(rows, key, limit=6)
        if summary:
            form_groups.append({"label": label, "items": summary})
    recent_table = _filing_recent_table(columns, rows, limit=10)
    cards: list[dict[str, Any]] = []
    if form_groups:
        cards.append({"title": "Official Source Mix", "kind": "grouped-list", "groups": form_groups})
    if recent_table is not None:
        cards.append(recent_table)
    return {
        "family": dataset.family,
        "family_label": family["label"],
        "summary": family["summary"],
        "framing": "Official/regulatory filing ledger. Use this to audit what arrived, when it arrived, and which source document backs each row before metric extraction.",
        "kpis": [
            _kpi_card("Issuer", company, detail="Dataset issuer context", tone="confirmed"),
            _kpi_card("Filings", profile.get("row_count"), detail="Tracked official source events"),
            _kpi_card("Earnings Updates", earnings_count, detail="Rows flagged as earnings releases or Item 2.02-style updates"),
            _kpi_card("Latest Filing", latest_date.isoformat() if latest_date else None, detail="Newest document date"),
        ],
        "identity": [
            {"label": "Issuer", "value": company},
            {"label": "Source Kind", "value": dataset.source_kind or "official_filings"},
            {"label": "Framing", "value": "Regulatory and issuer-source ledger"},
        ],
        "chart_cards": [],
        "cards": cards,
        "lineage": _dashboard_lineage(dataset, facts),
        "related_objects": _related_object_counts(links),
    }


def _product_stage_dashboard(
    dataset: Dataset,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    facts: list[StructuredDataFact],
    links: list[DatasetLink],
    linked_stage_entries: list[ProductStageEntry],
) -> dict[str, Any]:
    family = dataset_family_meta(dataset.family)
    stage_payloads = [serialize_product_stage_entry(row) for row in linked_stage_entries[:3]]
    primary = stage_payloads[0] if stage_payloads else None
    cards: list[dict[str, Any]] = []
    if primary:
        cards.append(
            {
                "title": "Reviewed vs Candidate",
                "kind": "pairs",
                "items": [
                    {
                        "label": "Reviewed",
                        "value": (primary.get("current_reviewed") or {}).get("stage") or "—",
                        "meta": (primary.get("current_reviewed") or {}).get("date_text") or (primary.get("current_reviewed") or {}).get("target_date"),
                    },
                    {
                        "label": "Candidate",
                        "value": (primary.get("current_candidate") or {}).get("stage") or "—",
                        "meta": (primary.get("current_candidate") or {}).get("date_text") or (primary.get("current_candidate") or {}).get("target_date"),
                    },
                    {
                        "label": "Conflict",
                        "value": primary.get("conflict_state") or "clear",
                        "meta": f"{primary.get('claim_count') or 0} claims",
                    },
                ],
            }
        )
        claims_preview = primary.get("claims_preview") or []
        if claims_preview:
            cards.append(
                {
                    "title": "Recent Claim Timeline",
                    "kind": "list",
                    "items": [
                        {
                            "label": str(item.get("stage") or "stage"),
                            "value": str(item.get("date_text") or item.get("target_date") or "undated"),
                            "meta": " · ".join(
                                part
                                for part in [str(item.get("review_status") or ""), str(item.get("evidence_tier") or ""), str(item.get("certainty") or "")]
                                if part
                            ),
                        }
                        for item in claims_preview[:5]
                    ],
                }
            )
    recent_table = _recent_table(columns, rows, limit=6, column_limit=6)
    if recent_table is not None:
        recent_table["title"] = "Tracker Rows"
        cards.append(recent_table)
    return {
        "family": dataset.family,
        "family_label": family["label"],
        "summary": family["summary"],
        "framing": "Stage-linked tracker view with reviewed state, candidate state, and corroboration context.",
        "kpis": [
            _kpi_card("Linked Entries", len(stage_payloads), detail="Stage tracker objects linked"),
            _kpi_card("Reviewed Stage", (primary.get("current_reviewed") or {}).get("stage") if primary else None, detail="Primary reviewed state"),
            _kpi_card("Candidate Stage", (primary.get("current_candidate") or {}).get("stage") if primary else None, detail="Best current candidate"),
            _kpi_card("Conflict", primary.get("conflict_state") if primary else None, detail="Corroboration state"),
        ],
        "identity": [
            {"label": "Primary Tracker", "value": f"{primary.get('company_name')} · {primary.get('product_name')}"}
            for primary in ([primary] if primary else [])
        ],
        "chart_cards": _chart_cards(columns, rows, max_cards=2),
        "cards": cards,
        "lineage": _dashboard_lineage(dataset, facts),
        "related_objects": _related_object_counts(links),
    }


def _projection_dashboard(
    dataset: Dataset,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    facts: list[StructuredDataFact],
    links: list[DatasetLink],
) -> dict[str, Any]:
    family = dataset_family_meta(dataset.family)
    profile = build_dataset_profile(columns, rows)
    recent_table = _recent_table(columns, rows, limit=8, column_limit=6)
    cards: list[dict[str, Any]] = []
    if recent_table is not None:
        recent_table["title"] = "Scenario Table"
        cards.append(recent_table)
    evidence = str((dataset.extra or {}).get("evidence_tier_summary") or "").strip() or "directional_unverified"
    return {
        "family": dataset.family,
        "family_label": family["label"],
        "summary": family["summary"],
        "framing": "Projected / attributed dataset. Treat as scenario scaffolding until corroborated.",
        "kpis": [
            _kpi_card("Scenario Rows", profile.get("row_count"), detail="Rows in current scenario sheet", tone="draft"),
            _kpi_card("Metrics", profile.get("metric_count"), detail="Numeric scenario fields"),
            _kpi_card("Evidence Tier", evidence, detail="Current provenance framing", tone="draft"),
            _kpi_card("Links", len(links), detail="Linked research objects"),
        ],
        "identity": [
            {"label": "Evidence Tier", "value": evidence},
            {"label": "Source Kind", "value": dataset.source_kind or "projection"},
            {"label": "Status", "value": dataset.status},
        ],
        "chart_cards": _chart_cards(columns, rows),
        "cards": cards,
        "lineage": _dashboard_lineage(dataset, facts),
        "related_objects": _related_object_counts(links),
    }


def build_dataset_dashboard(
    session: Session,
    dataset: Dataset,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    facts = (
        session.query(StructuredDataFact)
        .filter(StructuredDataFact.dataset_id == int(dataset.id))
        .order_by(StructuredDataFact.updated_at.desc(), StructuredDataFact.id.desc())
        .limit(12)
        .all()
    )
    links = session.query(DatasetLink).filter_by(dataset_id=int(dataset.id)).all()
    stage_ids = [int(link.object_id) for link in links if str(link.object_type or "").strip().lower() == "product_stage"]
    linked_stage_entries = (
        session.query(ProductStageEntry).filter(ProductStageEntry.id.in_(stage_ids)).order_by(ProductStageEntry.updated_at.desc()).all()
        if stage_ids
        else []
    )
    family = str(dataset.family or "general").strip().lower() or "general"
    if family == "trade_customs":
        dashboard = _trade_customs_dashboard(dataset, columns, rows, facts=facts, links=links)
    elif family == "earnings_actuals":
        dashboard = _issuer_dashboard(dataset, columns, rows, facts=facts, links=links, forward=False)
    elif family == "earnings_filings":
        dashboard = _filing_dashboard(dataset, columns, rows, facts=facts, links=links)
    elif family == "guidance_actuals":
        dashboard = _issuer_dashboard(dataset, columns, rows, facts=facts, links=links, forward=True)
    elif family == "product_stage_tracker":
        dashboard = _product_stage_dashboard(dataset, columns, rows, facts=facts, links=links, linked_stage_entries=linked_stage_entries)
    elif family == "instrument_projection":
        dashboard = _projection_dashboard(dataset, columns, rows, facts=facts, links=links)
    else:
        meta = dataset_family_meta(family)
        dashboard = {
            "family": family,
            "family_label": meta["label"],
            "summary": meta["summary"],
            "framing": "General institutional dataset workbench.",
            "kpis": [
                _kpi_card("Rows", len(rows)),
                _kpi_card("Columns", len(columns)),
                _kpi_card("Links", len(links)),
            ],
            "identity": [{"label": "Source Kind", "value": dataset.source_kind}] if dataset.source_kind else [],
            "chart_cards": _chart_cards(columns, rows),
            "cards": [_recent_table(columns, rows, limit=8, column_limit=6)] if rows and columns else [],
            "lineage": _dashboard_lineage(dataset, facts),
            "related_objects": _related_object_counts(links),
        }
    dashboard["cards"] = [card for card in (dashboard.get("cards") or []) if card]
    dashboard["lineage"]["summary"] = dashboard["lineage"].get("summary") or str((dataset.extra or {}).get("provenance_summary") or "").strip() or None
    dashboard["related_objects"] = dashboard.get("related_objects") or []
    dashboard["warnings"] = _family_fit_warnings(dataset, columns, rows, facts=facts, links=links, linked_stage_entries=linked_stage_entries)
    dashboard["family_fit"] = "attention" if dashboard["warnings"] else "aligned"
    return dashboard
