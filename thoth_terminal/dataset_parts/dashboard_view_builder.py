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





def dataset_capabilities(dataset: Dataset) -> dict[str, Any]:
    return {
        "family_dashboard": True,
        "view_builder": True,
        "advanced_view_json": True,
        "draft_editing": True,
        "bulk_paste": True,
        "publish_snapshot": True,
        "inline_import": True,
        "family": str(dataset.family or "general").strip() or "general",
    }


def _default_view_suggestions(columns: list[dict[str, Any]], family: str | None) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    timestamp = _first_column_by_role(columns, "timestamp")
    metric_columns = _columns_by_role(columns, "metric")
    dimension_columns = _columns_by_role(columns, "dimension")
    family_key = str(family or "general").strip().lower() or "general"
    if family_key == "trade_customs":
        lookup = _column_lookup(columns)
        preferred = [
            key
            for key in ("period_date", "date", "reporter_iso", "partner_scope", "partner_iso", "flow", "hs_code", "theme")
            if key in lookup
        ]
        if timestamp:
            timestamp_key = str(timestamp.get("key") or "")
            if timestamp_key and timestamp_key not in preferred:
                preferred.insert(0, timestamp_key)
        for col in metric_columns[:1]:
            metric_key = str(col.get("key") or "")
            if metric_key and metric_key not in preferred:
                preferred.append(metric_key)
        if preferred:
            suggestions.append(
                {
                    "name": "Trade Lens",
                    "description": "Reporter, partner, flow, HS code, and primary metric for customs review.",
                    "columns": preferred,
                    "filters": {},
                    "sort_spec": [{"column": str(timestamp.get("key") or ""), "direction": "asc"}] if timestamp else [],
                }
            )
    if timestamp and metric_columns:
        base_columns = [str(timestamp.get("key") or "")] + [str(col.get("key") or "") for col in metric_columns[:2]]
        suggestions.append(
            {
                "name": "Timeline Core",
                "description": "Focused timeline of timestamp plus primary metrics.",
                "columns": base_columns,
                "filters": {},
                "sort_spec": [{"column": str(timestamp.get("key") or ""), "direction": "asc"}],
            }
        )
    if dimension_columns and metric_columns:
        suggestions.append(
            {
                "name": "Metric + Dimensions",
                "description": f"{dataset_family_meta(family).get('label')} analytical slice with primary dimensions retained.",
                "columns": [str(col.get("key") or "") for col in dimension_columns[:2] + metric_columns[:2]],
                "filters": {},
                "sort_spec": [],
            }
        )
    return suggestions[:4]


def _family_fit_warnings(
    dataset: Dataset,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    facts: list[StructuredDataFact],
    links: list[DatasetLink],
    linked_stage_entries: list[ProductStageEntry],
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    family = str(dataset.family or "general").strip().lower() or "general"
    column_keys = {str(col.get("key") or "").strip() for col in columns if str(col.get("key") or "").strip()}
    roles = {str(col.get("role") or "").strip().lower() for col in columns if str(col.get("role") or "").strip()}
    extra = dict(dataset.extra or {})
    if family == "trade_customs":
        trade_identity_present = any(str(extra.get(key) or "").strip() for key in ("reporter_iso", "partner_scope", "flow", "hs_code")) or bool(
            {"reporter_iso", "partner_scope", "flow", "hs_code"} & column_keys
        )
        if not trade_identity_present:
            warnings.append(
                {
                    "title": "Trade Identity Missing",
                    "detail": "This dataset is labeled trade/customs, but reporter, partner, flow, or HS identity fields are missing.",
                }
            )
        linked_types = {str(link.object_type or "").strip().lower() for link in links}
        if rows and not (linked_types & {"company", "issuer", "instrument_request", "product_stage", "supply_chain", "thesis"}):
            warnings.append(
                {
                    "title": "Decision Linkage Missing",
                    "detail": "Trade/customs datasets should link to the company, supply chain, thesis, stage entry, or request they inform.",
                }
            )
        timestamp_column = _first_column_by_role(columns, "timestamp")
        timestamp_key = str(timestamp_column.get("key") or "") if timestamp_column else ""
        future_count = 0
        if timestamp_key:
            today = utcnow().date()
            for row in rows:
                row_date = _parse_dataset_date((row.get("values") or {}).get(timestamp_key))
                if row_date and row_date > today:
                    future_count += 1
        if future_count:
            warnings.append(
                {
                    "title": "Future Period Quarantined",
                    "detail": f"{future_count} future-dated row(s) are excluded from trade/customs Latest, MoM, YoY, and chart calculations.",
                }
            )
    if family in {"earnings_actuals", "guidance_actuals"}:
        issuer_present = bool({"company", "company_name", "issuer", "ticker"} & column_keys)
        if not issuer_present:
            warnings.append(
                {
                    "title": "Issuer Context Missing",
                    "detail": "Earnings-family datasets usually need an issuer or company dimension for period-level interpretation.",
                }
            )
        if not facts and not str(extra.get("provenance_summary") or "").strip():
            warnings.append(
                {
                    "title": "Lineage Thin",
                    "detail": "Issuer-family datasets read best when facts or provenance point back to the source document.",
                }
            )
    if family == "earnings_filings":
        issuer_present = bool({"company", "company_name", "issuer", "ticker"} & column_keys) or bool(str(extra.get("company_name") or "").strip())
        if not issuer_present:
            warnings.append(
                {
                    "title": "Issuer Context Missing",
                    "detail": "Filing-ledger datasets need issuer identity so filing rows can be audited against company-level earnings history.",
                }
            )
        if "source_url" not in column_keys and "source_document_id" not in column_keys:
            warnings.append(
                {
                    "title": "Filing Linkage Missing",
                    "detail": "Official filing ledgers should expose either source URL or source document id for every row.",
                }
            )
    if family == "product_stage_tracker" and not linked_stage_entries:
        warnings.append(
            {
                "title": "Stage Tracker Unlinked",
                "detail": "Stage-tracker datasets should usually link to one or more product stage entries so reviewed and candidate state can be surfaced.",
            }
        )
    if family == "instrument_projection" and not str(extra.get("evidence_tier_summary") or "").strip():
        warnings.append(
            {
                "title": "Confidence Framing Missing",
                "detail": "Projection datasets should carry an evidence tier summary so they are clearly separated from confirmed data.",
            }
        )
    if rows and "timestamp" not in roles and family in {"trade_customs", "earnings_actuals", "earnings_filings", "guidance_actuals", "instrument_projection"}:
        warnings.append(
            {
                "title": "Timeline Column Missing",
                "detail": "This family usually benefits from a timestamp/date column so charting and view suggestions remain comparable.",
            }
        )
    if rows and "metric" not in roles and family in {"trade_customs", "earnings_actuals", "guidance_actuals", "instrument_projection"}:
        warnings.append(
            {
                "title": "Metric Column Missing",
                "detail": "This family usually expects at least one numeric metric column for dashboards and charts.",
            }
        )
    return warnings


def build_dataset_view_builder(columns: list[dict[str, Any]], views: list[dict[str, Any]], *, active_view: DatasetView | None, family: str | None) -> dict[str, Any]:
    active_payload = None
    if active_view is not None:
        active_payload = {
            "id": int(active_view.id),
            "name": active_view.name,
            "description": active_view.description,
            "filters": dict(active_view.filters or {}),
            "columns": list(active_view.columns or []),
            "sort_spec": list(active_view.sort_spec or []),
            "extra": dict(active_view.extra or {}),
        }
    return {
        "operators": list(DATASET_VIEW_OPERATORS),
        "columns": [{"key": str(col.get("key") or ""), "label": str(col.get("label") or col.get("key") or "")} for col in columns],
        "active": active_payload,
        "saved_views": list(views or []),
        "recommended_views": _default_view_suggestions(columns, family),
    }
