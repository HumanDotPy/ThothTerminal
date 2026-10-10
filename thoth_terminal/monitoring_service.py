from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.dataset_service import (
    create_dataset,
    create_dataset_view,
    latest_dataset_version,
    link_dataset,
    query_dataset,
    replace_dataset_draft,
)
from thoth_terminal.db import utcnow
from thoth_terminal.instrument_service import create_instrument_request
from thoth_terminal.models import Dataset, DatasetColumn, DatasetView, Note, SupplyChain
from thoth_terminal.supply_chain_service import apply_supply_chain_ops


CAPEX_DELTA_REVIEW_THRESHOLD_PCT = 20.0


def _dataset_by_slug_or_title(session: Session, *candidates: str) -> Dataset | None:
    slugs = [str(value).strip() for value in candidates if str(value or "").strip()]
    for slug in slugs:
        row = session.query(Dataset).filter_by(slug=slug).first()
        if row is not None:
            return row
    for value in slugs:
        pattern = f"%{value.replace('-', ' ')}%"
        row = session.query(Dataset).filter(Dataset.title.ilike(pattern)).order_by(Dataset.updated_at.desc()).first()
        if row is not None:
            return row
    return None


def _chain_by_slug_or_title(session: Session, *candidates: str) -> SupplyChain | None:
    slugs = [str(value).strip() for value in candidates if str(value or "").strip()]
    for slug in slugs:
        row = session.query(SupplyChain).filter_by(slug=slug).first()
        if row is not None:
            return row
    for value in slugs:
        pattern = f"%{value.replace('-', ' ')}%"
        row = session.query(SupplyChain).filter(SupplyChain.title.ilike(pattern)).order_by(SupplyChain.updated_at.desc()).first()
        if row is not None:
            return row
    return None


def _note_by_title(session: Session, *titles: str) -> Note | None:
    for title in [str(value).strip() for value in titles if str(value or "").strip()]:
        row = session.query(Note).filter(Note.title.ilike(f"%{title}%")).order_by(Note.updated_at.desc()).first()
        if row is not None:
            return row
    return None


def _dataset_columns(session: Session, dataset: Dataset) -> list[DatasetColumn]:
    version = latest_dataset_version(session, int(dataset.id), status="draft") or latest_dataset_version(session, int(dataset.id), status="published")
    if version is None:
        return []
    return (
        session.query(DatasetColumn)
        .filter_by(dataset_version_id=int(version.id))
        .order_by(DatasetColumn.sort_order.asc(), DatasetColumn.id.asc())
        .all()
    )


def _preferred_columns(columns: list[DatasetColumn], *, metric_candidates: tuple[str, ...]) -> tuple[list[str], str | None]:
    available = [col.key for col in columns]
    timestamp = next((col.key for col in columns if col.role == "timestamp"), None)
    metric = next((key for key in metric_candidates if key in available), None)
    if metric is None:
        metric = next((col.key for col in columns if col.role == "metric"), None)
    selected = [key for key in [timestamp, "reporter_iso", "partner_iso", "flow", "hs_code", "company", metric] if key and key in available]
    return selected or available, metric


def ensure_wfe_import_monitor(
    session: Session,
    *,
    chain_slug: str = "wafer-fab-equipment-chain",
    dataset_slug: str = "wafer-fab-imports",
) -> dict[str, Any]:
    chain = _chain_by_slug_or_title(session, chain_slug, "Wafer Fab Equipment Chain", "WFE")
    dataset = _dataset_by_slug_or_title(session, dataset_slug, "Wafer Fab Imports", "WFE imports")
    if chain is None or dataset is None:
        request = create_instrument_request(
            session,
            title="Wire WFE import monitor",
            intent="dataset_monitoring",
            summary="Create or resolve the missing Wafer Fab Equipment supply-chain and customs dataset objects needed for WFE monitoring.",
            target_themes=["WFE", "trade_customs", "supply_chain_monitoring"],
            desired_outputs=["supply_chain_metric_binding", "alert_rule", "validated_hs_scope"],
            evidence_bundle=[],
            provenance_summary="WFE monitor materializer could not resolve both chain and dataset.",
            evidence_tier="internal_gap",
            status="review",
            priority=7,
            automation_source="monitoring_service",
            source_kind="internal",
            request_key="monitoring:wfe-imports:missing-objects",
        )
        return {"ok": False, "request_id": int(request.id), "missing_chain": chain is None, "missing_dataset": dataset is None}

    columns, metric_key = _preferred_columns(_dataset_columns(session, dataset), metric_candidates=("value_usd", "value", "imports_usd"))
    view = create_dataset_view(
        session,
        dataset,
        name="WFE Import Monitor",
        slug="wfe-import-monitor",
        description="Monitoring view for wafer-fab-equipment import value and momentum checks.",
        columns=columns,
        sort_spec=[{"column": "period_date", "direction": "desc"}] if "period_date" in columns else [],
        extra={
            "monitoring_kind": "wfe_imports",
            "created_by": "monitoring_service",
            "metric_key": metric_key,
        },
    )
    link_dataset(
        session,
        dataset_id=int(dataset.id),
        object_type="supply_chain",
        object_id=int(chain.id),
        relation="monitoring",
        extra={"monitoring_kind": "wfe_imports", "dataset_view_id": int(view.id)},
    )
    note = _note_by_title(session, "WFE monitoring")
    if note is not None:
        link_dataset(
            session,
            dataset_id=int(dataset.id),
            object_type="note",
            object_id=int(note.id),
            relation="monitoring",
            extra={"monitoring_kind": "wfe_imports", "dataset_view_id": int(view.id)},
        )

    _draft, ops_result = apply_supply_chain_ops(
        session,
        chain,
        [
            {
                "op": "set_meta",
                "about": {
                    "monitoring": "WFE import value is bound to a customs dataset view; alert rules flag large monthly moves for analyst review.",
                    "monitoring_updated_at": utcnow().isoformat(),
                },
            },
            {
                "op": "upsert_layer",
                "layer_id": "official-data",
                "name": "Official Data",
                "family": "data",
                "description": "Customs and issuer data used as monitored evidence.",
            },
            {
                "op": "upsert_node",
                "node_id": "wfe-imports-monitor",
                "name": "WFE import monitor",
                "layer": "official-data",
                "type": "dataset_monitor",
                "definition": "Wafer fab equipment import value monitor using the linked customs dataset.",
                "watchpoints": ["MoM import acceleration", "YoY recovery", "HS-code coverage drift"],
                "evidence": [{"dataset_id": int(dataset.id), "dataset_view_id": int(view.id)}],
                "confidence": 0.78,
            },
            {
                "op": "bind_metric",
                "scope_type": "node",
                "scope_key": "wfe-imports-monitor",
                "metric_kind": "wfe_import_value_usd",
                "dataset_id": int(dataset.id),
                "dataset_view_id": int(view.id),
                "column_key": metric_key or "value_usd",
                "aggregation": "latest",
                "extra": {
                    "monitoring_kind": "wfe_imports",
                    "interpretation": "Use with MoM/YoY context; a single month is not thesis-changing.",
                },
            },
            {
                "op": "upsert_alert",
                "scope_type": "node",
                "scope_key": "wfe-imports-monitor",
                "name": "WFE import momentum review",
                "metric_kind": "wfe_import_value_usd",
                "operator": "gte",
                "threshold_value": 20,
                "window_days": 45,
                "status": "active",
                "extra": {
                    "trigger_basis": "absolute MoM percentage move >= 20% when computed by the analyst review path",
                    "requires_review": True,
                },
            },
        ],
    )
    return {
        "ok": True,
        "chain_id": int(chain.id),
        "dataset_id": int(dataset.id),
        "dataset_view_id": int(view.id),
        "metric_key": metric_key,
        "ops": ops_result,
    }


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _derive_capex_delta_rows(source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in source_rows:
        values = dict(row.get("values") or {})
        company = str(values.get("company") or values.get("issuer") or values.get("entity") or "aggregate").strip() or "aggregate"
        period = str(values.get("period_date") or values.get("period") or values.get("date") or "").strip()
        value = _num(values.get("value", values.get("capex")))
        if not period or value is None:
            continue
        groups.setdefault(company, []).append({"company": company, "period_date": period, "capex": value})

    out: list[dict[str, Any]] = []
    for company, rows in sorted(groups.items()):
        ordered = sorted(rows, key=lambda item: item["period_date"])
        previous: dict[str, Any] | None = None
        for item in ordered:
            delta = None
            delta_pct = None
            if previous is not None:
                delta = float(item["capex"]) - float(previous["capex"])
                if float(previous["capex"]) != 0:
                    delta_pct = (delta / abs(float(previous["capex"]))) * 100.0
            alert_state = "baseline"
            if delta_pct is not None:
                alert_state = "threshold_candidate" if abs(delta_pct) >= CAPEX_DELTA_REVIEW_THRESHOLD_PCT else "inside_band"
            out.append(
                {
                    "row_key": f"{company.lower().replace(' ', '-')}-{item['period_date']}",
                    "period_date": item["period_date"],
                    "company": company,
                    "capex": item["capex"],
                    "prior_period_date": previous["period_date"] if previous is not None else None,
                    "capex_delta": delta,
                    "capex_delta_pct": delta_pct,
                    "monitor_state": "needs_baseline" if previous is None else ("expansion" if (delta or 0) > 0 else ("contraction" if (delta or 0) < 0 else "flat")),
                    "alert_state": alert_state,
                }
            )
            previous = item
    return out


def ensure_capex_delta_monitor(
    session: Session,
    *,
    source_dataset_slug: str = "tsmc-capex-tracker",
    derived_slug: str = "tsmc-capex-delta-monitor",
) -> dict[str, Any]:
    source = _dataset_by_slug_or_title(session, source_dataset_slug, "TSMC Capex Tracker", "TSMC Capex Actuals", "Capex Tracker")
    if source is None:
        request = create_instrument_request(
            session,
            title="Create Capex Delta monitor",
            intent="dataset_monitoring",
            summary="No capex source dataset was found. Resolve issuer capex actual/guidance source data before materializing a delta monitor.",
            target_themes=["capex_delta", "earnings_actuals"],
            desired_outputs=["capex_actuals_dataset", "capex_delta_dataset", "monitoring_view"],
            provenance_summary="Capex Delta monitor materializer could not resolve a source dataset.",
            evidence_tier="internal_gap",
            status="review",
            priority=7,
            automation_source="monitoring_service",
            source_kind="internal",
            request_key="monitoring:capex-delta:missing-source",
        )
        return {"ok": False, "request_id": int(request.id), "missing_source": True}

    source_rows = list(query_dataset(session, dataset_id=int(source.id), limit=None).get("rows") or [])
    derived_rows = _derive_capex_delta_rows(source_rows)
    companies = sorted({str(row.get("company") or "").strip() for row in derived_rows if str(row.get("company") or "").strip()})
    alert_candidate_count = sum(1 for row in derived_rows if row.get("alert_state") == "threshold_candidate")
    source_extra = dict(source.extra or {})
    source_validated = bool(
        source_extra.get("capex_delta_validated")
        or source_extra.get("issuer_source_validated")
        or source_extra.get("monitoring_validated")
    )
    validation_state = (
        "validated_multi_issuer"
        if len(companies) >= 3 and source_validated
        else "multi_issuer_unvalidated"
        if len(companies) >= 3
        else "single_issuer_seed"
        if len(companies) == 1
        else "needs_source_coverage"
    )
    automation_write_enabled = validation_state == "validated_multi_issuer"
    columns = [
        {"key": "period_date", "label": "Period", "data_type": "date", "role": "timestamp"},
        {"key": "company", "label": "Company", "data_type": "text", "role": "dimension"},
        {"key": "capex", "label": "Capex", "data_type": "number", "role": "metric"},
        {"key": "prior_period_date", "label": "Prior Period", "data_type": "date", "role": "dimension"},
        {"key": "capex_delta", "label": "Capex Delta", "data_type": "number", "role": "metric"},
        {"key": "capex_delta_pct", "label": "Capex Delta %", "data_type": "number", "role": "metric"},
        {"key": "monitor_state", "label": "Monitor State", "data_type": "text", "role": "dimension"},
        {"key": "alert_state", "label": "Alert State", "data_type": "text", "role": "dimension"},
    ]
    monitor_extra = {
        "source_dataset_id": int(source.id),
        "monitoring_kind": "capex_delta",
        "created_by": "monitoring_service",
        "coverage_company_count": len(companies),
        "coverage_companies": companies,
        "alert_threshold_pct": CAPEX_DELTA_REVIEW_THRESHOLD_PCT,
        "alert_candidate_count": alert_candidate_count,
        "validation_state": validation_state,
        "automation_write_enabled": automation_write_enabled,
        "source_validation_flag": source_validated,
        "requires_issuer_validation": not automation_write_enabled,
    }
    existing = session.query(Dataset).filter_by(slug=derived_slug).first()
    if existing is None:
        derived = create_dataset(
            session,
            title="TSMC Capex Delta Monitor",
            slug=derived_slug,
            description="Derived capex delta monitor from issuer capex actuals/guidance rows.",
            category="earnings",
            family="capex_delta",
            source_kind="derived",
            columns=columns,
            rows=derived_rows,
            label="capex delta materialization",
            extra=monitor_extra,
        )
    else:
        derived = existing
        replace_dataset_draft(
            session,
            derived,
            columns=columns,
            rows=derived_rows,
            label="capex delta materialization",
            extra=monitor_extra,
            source_event="monitoring_service",
        )
        derived.updated_at = utcnow()
        derived.extra = dict(derived.extra or {}) | monitor_extra
        session.add(derived)

    view = create_dataset_view(
        session,
        derived,
        name="Capex Delta Review",
        slug="capex-delta-review",
        description="Rows where capex delta can be reviewed against the prior period.",
        filters={"rules": [{"column": "monitor_state", "op": "neq", "value": "needs_baseline"}]},
        columns=["period_date", "company", "capex", "prior_period_date", "capex_delta", "capex_delta_pct", "monitor_state", "alert_state"],
        sort_spec=[{"column": "period_date", "direction": "desc"}],
        extra=monitor_extra,
    )
    note = _note_by_title(session, "Dataset Coverage Note", "capex deltas")
    if note is not None:
        link_dataset(
            session,
            dataset_id=int(derived.id),
            object_type="note",
            object_id=int(note.id),
            relation="monitoring",
            extra={"monitoring_kind": "capex_delta", "source_dataset_id": int(source.id)},
        )

    request = create_instrument_request(
        session,
        title="Validate Capex Delta monitor",
        intent="dataset_monitoring",
        summary="Validate the derived capex-delta dataset against issuer filings/guidance and define alert thresholds before relying on it for unattended thesis/report updates.",
        target_entities=sorted({str((row.get("company") or "")).strip() for row in derived_rows if str(row.get("company") or "").strip()}),
        target_themes=["capex_delta", "earnings_actuals", "revision_monitoring"],
        desired_outputs=["validated_capex_delta_dataset", "alert_thresholds", "report_memory_link"],
        evidence_bundle=[
            {"title": source.title, "object_type": "dataset", "object_id": int(source.id)},
            {"title": derived.title, "object_type": "dataset", "object_id": int(derived.id), "view_id": int(view.id)},
        ],
        provenance_summary="Derived from existing Thoth capex tracker rows; still requires issuer-source validation before automation changes reports or theses.",
        evidence_tier="derived_internal",
        status="review",
        priority=7,
        automation_source="monitoring_service",
        source_kind="internal",
        draft_payload={
            "source_dataset_id": int(source.id),
            "derived_dataset_id": int(derived.id),
            "dataset_view_id": int(view.id),
            "row_count": len(derived_rows),
            "monitoring_kind": "capex_delta",
            "coverage_company_count": len(companies),
            "coverage_companies": companies,
            "alert_threshold_pct": CAPEX_DELTA_REVIEW_THRESHOLD_PCT,
            "alert_candidate_count": alert_candidate_count,
            "validation_state": validation_state,
            "automation_write_enabled": automation_write_enabled,
        },
        linked_objects={
            "source_dataset_id": int(source.id),
            "derived_dataset_id": int(derived.id),
            "dataset_view_id": int(view.id),
        },
        request_key=f"monitoring:capex-delta:dataset:{int(source.id)}",
    )
    return {
        "ok": True,
        "source_dataset_id": int(source.id),
        "derived_dataset_id": int(derived.id),
        "dataset_view_id": int(view.id),
        "request_id": int(request.id),
        "row_count": len(derived_rows),
        "coverage_company_count": len(companies),
        "coverage_companies": companies,
        "alert_candidate_count": alert_candidate_count,
        "validation_state": validation_state,
        "automation_write_enabled": automation_write_enabled,
    }
