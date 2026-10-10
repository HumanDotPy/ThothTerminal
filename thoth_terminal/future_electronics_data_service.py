from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.dataset_service import (
    create_dataset,
    ensure_dataset_tables,
    latest_dataset_version,
    replace_dataset_draft,
)
from thoth_terminal.db import utcnow
from thoth_terminal.models import Dataset, DatasetRow, Source

FUTURE_ELECTRONICS_DATASET_SLUG = "future-electronics-product-availability"
FUTURE_ELECTRONICS_DATASET_FAMILY = "future_electronics_product_availability"


def _columns() -> list[dict[str, Any]]:
    return [
        {"key": "fetched_at", "label": "Fetched At", "data_type": "date", "role": "timestamp"},
        {"key": "manufacturer", "label": "Manufacturer", "data_type": "text", "role": "dimension"},
        {"key": "manufacturer_part_number", "label": "MPN", "data_type": "text", "role": "id"},
        {"key": "future_product_code", "label": "Future Product Code", "data_type": "text", "role": "id"},
        {"key": "description", "label": "Description", "data_type": "text", "role": "dimension"},
        {"key": "category", "label": "Category", "data_type": "text", "role": "dimension"},
        {"key": "variant", "label": "Variant", "data_type": "text", "role": "dimension"},
        {"key": "global_stock", "label": "Stock", "data_type": "number", "role": "metric"},
        {"key": "stock_status", "label": "Stock Status", "data_type": "text", "role": "dimension"},
        {"key": "lead_time", "label": "Lead Time", "data_type": "text", "role": "dimension"},
        {"key": "date_code", "label": "Date Code", "data_type": "text", "role": "dimension"},
        {"key": "package", "label": "Package", "data_type": "text", "role": "dimension"},
        {"key": "minimum_order_quantity", "label": "MOQ", "data_type": "number", "role": "metric"},
        {"key": "multiple_quantity", "label": "Multiple", "data_type": "number", "role": "metric"},
        {"key": "best_unit_price", "label": "Best Unit Price", "data_type": "number", "role": "metric"},
        {"key": "best_unit_price_qty", "label": "Best Price Qty", "data_type": "number", "role": "metric"},
        {"key": "price_breaks", "label": "Price Breaks", "data_type": "json", "role": "dimension"},
        {"key": "technical_attributes", "label": "Technical Attributes", "data_type": "json", "role": "dimension"},
        {"key": "product_specification", "label": "Product Specification", "data_type": "json", "role": "dimension"},
        {"key": "product_url", "label": "Product URL", "data_type": "text", "role": "dimension"},
        {"key": "datasheet_url", "label": "Datasheet URL", "data_type": "text", "role": "dimension"},
        {"key": "source_page_url", "label": "Source Page", "data_type": "text", "role": "dimension"},
    ]


def _best_price(price_breaks: list[dict[str, Any]]) -> tuple[float | None, int | None]:
    best_price = None
    best_qty = None
    for row in price_breaks:
        if not isinstance(row, dict):
            continue
        price = row.get("unit_price")
        try:
            price_val = float(price) if price is not None else None
        except Exception:
            price_val = None
        if price_val is None:
            continue
        if best_price is None or price_val < best_price:
            best_price = price_val
            qty = row.get("min_qty")
            try:
                best_qty = int(qty) if qty is not None else None
            except Exception:
                best_qty = None
    return best_price, best_qty


def _row_key(product: dict[str, Any]) -> str:
    for key in ("future_product_code", "manufacturer_part_number", "product_url"):
        value = str(product.get(key) or "").strip()
        if value:
            return value[:128]
    return str(product.get("external_id") or "unknown")[:128]


def _row_values(product: dict[str, Any], *, source: Source, fetched_at: str | None) -> dict[str, Any]:
    price_breaks = [row for row in (product.get("price_breaks") or []) if isinstance(row, dict)]
    best_price, best_qty = _best_price(price_breaks)
    return {
        "fetched_at": product.get("fetched_at") or fetched_at,
        "manufacturer": product.get("manufacturer"),
        "manufacturer_part_number": product.get("manufacturer_part_number"),
        "future_product_code": product.get("future_product_code"),
        "description": product.get("description"),
        "category": product.get("category"),
        "variant": product.get("variant"),
        "global_stock": product.get("global_stock"),
        "stock_status": product.get("stock_status") or product.get("availability_text"),
        "lead_time": product.get("lead_time"),
        "date_code": product.get("date_code"),
        "package": product.get("package"),
        "minimum_order_quantity": product.get("minimum_order_quantity"),
        "multiple_quantity": product.get("multiple_quantity"),
        "best_unit_price": best_price,
        "best_unit_price_qty": best_qty,
        "price_breaks": price_breaks,
        "technical_attributes": product.get("technical_attributes") or {},
        "product_specification": product.get("product_specification") or {},
        "product_url": product.get("product_url"),
        "datasheet_url": product.get("datasheet_url"),
        "source_page_url": product.get("source_page_url"),
        "source_id": int(source.id) if source.id is not None else None,
        "source_name": source.name,
    }


def upsert_future_electronics_products_from_stats(
    session: Session,
    *,
    source: Source,
    fetch_stats: dict[str, Any],
    ingestion_run_id: int | None = None,
) -> dict[str, int]:
    if str(fetch_stats.get("source_kind") or "") != "future_electronics":
        return {"datasets_synced": 0, "dataset_rows": 0, "products_seen": 0}
    products = [row for row in (fetch_stats.get("product_snapshots") or []) if isinstance(row, dict)]
    if not products:
        return {"datasets_synced": 0, "dataset_rows": 0, "products_seen": 0}

    ensure_dataset_tables(session)
    cfg = dict(source.config or {})
    slug = str(cfg.get("dataset_slug") or FUTURE_ELECTRONICS_DATASET_SLUG).strip()
    dataset = session.query(Dataset).filter_by(slug=slug).first()
    extra = {
        "source_id": int(source.id) if source.id is not None else None,
        "source_name": source.name,
        "connector": "future_electronics",
        "product_list_url": fetch_stats.get("product_list_url"),
        "report_url": fetch_stats.get("report_url"),
        "ingestion_run_id": ingestion_run_id,
        "last_synced_at": utcnow().isoformat(),
    }
    if dataset is None:
        dataset = create_dataset(
            session,
            title="Future Electronics Product Availability",
            slug=slug,
            description=(
                "Structured Future Electronics distributor catalog snapshots, including stock, "
                "lead time, packaging, and price breaks."
            ),
            category="supply_chain",
            family=FUTURE_ELECTRONICS_DATASET_FAMILY,
            source_kind="future_electronics_product_availability",
            columns=_columns(),
            rows=[],
            extra=extra,
            source_event="future_electronics_ingest",
        )
    else:
        dataset.title = dataset.title or "Future Electronics Product Availability"
        dataset.description = dataset.description or (
            "Structured Future Electronics distributor catalog snapshots, including stock, "
            "lead time, packaging, and price breaks."
        )
        dataset.category = "supply_chain"
        dataset.family = FUTURE_ELECTRONICS_DATASET_FAMILY
        dataset.source_kind = "future_electronics_product_availability"
        dataset.extra = {**dict(dataset.extra or {}), **extra}

    existing_by_key: dict[str, dict[str, Any]] = {}
    draft = latest_dataset_version(session, int(dataset.id), status="draft")
    if draft is not None:
        for row in (
            session.query(DatasetRow)
            .filter_by(dataset_version_id=int(draft.id))
            .order_by(DatasetRow.sort_order.asc(), DatasetRow.id.asc())
            .all()
        ):
            existing_by_key[str(row.row_key)] = dict(row.values or {})

    fetched_at = str(fetch_stats.get("fetched_at") or "")
    for product in products:
        key = _row_key(product)
        existing_by_key[key] = _row_values(product, source=source, fetched_at=fetched_at)

    rows = [{"row_key": key, "values": values} for key, values in sorted(existing_by_key.items())]
    replace_dataset_draft(
        session,
        dataset,
        columns=_columns(),
        rows=rows,
        label="Future Electronics product availability",
        extra=dict(dataset.extra or {}),
        source_event="future_electronics_ingest",
    )
    return {"datasets_synced": 1, "dataset_rows": len(rows), "products_seen": len(products)}
