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
from thoth_terminal.dataset_storage import canonical_dataset


def link_dataset(
    session: Session,
    *,
    dataset_id: int,
    object_type: str,
    object_id: int,
    relation: str = "context",
    extra: dict[str, Any] | None = None,
) -> DatasetLink:
    dataset = session.get(Dataset, int(dataset_id))
    if dataset is None:
        raise ValueError("Dataset not found")
    dataset_id = int(canonical_dataset(session, dataset).id)
    object_type_v = str(object_type or "").strip().lower()
    if object_type_v not in DATASET_OBJECT_TYPES:
        raise ValueError("Unsupported dataset link object_type.")
    existing = (
        session.query(DatasetLink)
        .filter_by(
            dataset_id=int(dataset_id),
            object_type=object_type_v,
            object_id=int(object_id),
            relation=str(relation or "context"),
        )
        .first()
    )
    if existing:
        return existing
    row = DatasetLink(
        dataset_id=int(dataset_id),
        object_type=object_type_v,
        object_id=int(object_id),
        relation=str(relation or "context"),
        created_at=utcnow(),
        extra=dict(extra or {}),
    )
    session.add(row)
    session.flush()
    return row


def unlink_dataset(
    session: Session,
    *,
    dataset_id: int,
    link_id: int,
) -> bool:
    dataset = session.get(Dataset, int(dataset_id))
    if dataset is None:
        return False
    canonical_id = int(canonical_dataset(session, dataset).id)
    row = session.query(DatasetLink).filter(DatasetLink.dataset_id.in_([int(dataset_id), canonical_id]), DatasetLink.id == int(link_id)).first()
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True
