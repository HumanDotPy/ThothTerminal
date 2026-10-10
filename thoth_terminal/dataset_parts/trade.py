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


def sync_trade_dataset(session: Session, *, source_name: str, item: dict[str, Any]) -> Dataset:
    ensure_dataset_tables(session)
    slug = str(item.get("slug") or "").strip()
    if not slug:
        raise ValueError("Trade dataset item requires slug.")
    now = utcnow()
    dataset = session.query(Dataset).filter_by(slug=slug).first()
    title = str(item.get("name") or slug)
    extra = {
        "trade": True,
        "reporter_iso": str(item.get("reporter_iso") or ""),
        "partner_scope": str(item.get("partner_scope") or ""),
        "flow": str(item.get("flow") or ""),
        "hs_code": str(item.get("hs_code") or ""),
        "hs_revision": str(item.get("hs_revision") or ""),
        "metric": str(item.get("metric") or ""),
        "theme": item.get("theme"),
        "source_label": item.get("source_label"),
        "source_name": source_name,
        "source_url": item.get("source_url"),
        "trade_extra": dict(item.get("extra") or {}),
        "provenance_summary": f"{source_name} trade/customs source materialized into a curated dataset workbench.",
        "evidence_tier_summary": "issuer_confirmed",
    }
    for identity_key in (
        "company_id",
        "company_ids",
        "issuer_id",
        "issuer_ids",
        "ticker",
        "tickers",
        "company_name",
        "company_names",
    ):
        if item.get(identity_key) not in (None, "", []):
            extra[identity_key] = item.get(identity_key)
    rows = []
    for point in item.get("points") or []:
        if not isinstance(point, dict):
            continue
        rows.append(
            {
                "period_date": str(point.get("period_date") or ""),
                "value": point.get("value"),
                "source_ref": point.get("source_ref"),
                "note": point.get("note"),
                "reporter_iso": extra["reporter_iso"],
                "partner_scope": extra["partner_scope"],
                "flow": extra["flow"],
                "hs_code": extra["hs_code"],
                "metric": extra["metric"],
            }
        )
    columns = [
        {"key": "period_date", "label": "Period", "data_type": "date", "role": "timestamp"},
        {"key": "value", "label": "Value", "data_type": "number", "role": "metric"},
        {"key": "source_ref", "label": "Source Ref", "data_type": "text", "role": "dimension"},
        {"key": "note", "label": "Note", "data_type": "text", "role": "dimension"},
        {"key": "reporter_iso", "label": "Reporter", "data_type": "text", "role": "dimension"},
        {"key": "partner_scope", "label": "Partner Scope", "data_type": "text", "role": "dimension"},
        {"key": "flow", "label": "Flow", "data_type": "text", "role": "dimension"},
        {"key": "hs_code", "label": "HS Code", "data_type": "text", "role": "dimension"},
        {"key": "metric", "label": "Metric", "data_type": "text", "role": "dimension"},
    ]
    if dataset is None:
        dataset = create_dataset(
            session,
            title=title,
            description=str(item.get("description") or "").strip() or None,
            category=str(item.get("category") or "trade"),
            family="trade_customs",
            status="active",
            source_kind="trade_customs",
            slug=slug,
            columns=columns,
            rows=rows,
            label="Trade Sync",
            extra=extra,
            source_event="trade_sync",
        )
    else:
        dataset.title = title
        dataset.description = str(item.get("description") or "").strip() or dataset.description
        dataset.category = str(item.get("category") or dataset.category or "trade")
        dataset.family = "trade_customs"
        dataset.source_kind = "trade_customs"
        dataset.updated_at = now
        dataset.extra = extra
        replace_dataset_draft(
            session,
            dataset,
            columns=columns,
            rows=rows,
            label="Trade Sync",
            extra=extra,
            source_event="trade_sync",
        )
    session.add(dataset)
    session.flush()
    return dataset
