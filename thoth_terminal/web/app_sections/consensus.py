from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context
from thoth_terminal.decision_spine_service import relink_analyst_evidence_subject
from starlette.concurrency import run_in_threadpool


bind_app_context(globals())


def _consensus_first_text(*values: object) -> str:
    for value in values:
        text = " ".join(str(value or "").strip().split())
        if text:
            return text
    return ""


def _consensus_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _consensus_median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _consensus_format_value(value: object, *, unit: str = "", currency: str = "", value_type: str = "") -> str:
    number = _consensus_float(value)
    if number is None:
        return "—"
    value_type_l = str(value_type or "").lower()
    unit_l = str(unit or "").lower()
    currency_v = str(currency or "").strip().upper()
    if "percent" in value_type_l or unit_l in {"%", "percent", "percentage"}:
        pct = number * 100 if abs(number) <= 1 else number
        return f"{pct:.1f}%"
    prefix = "$" if currency_v == "USD" else "€" if currency_v == "EUR" else "NT$" if currency_v == "TWD" else ""
    scale_suffix = "B" if unit_l in {"billion", "bn", "billions"} else "M" if unit_l in {"million", "mn", "millions"} else "K" if unit_l in {"thousand", "thousands"} else ""
    if prefix and scale_suffix:
        return f"{prefix}{number:,.2f}".rstrip("0").rstrip(".") + scale_suffix
    suffix = "" if prefix else (currency_v or unit or "")
    if abs(number) >= 1_000_000_000:
        text = f"{prefix}{number / 1_000_000_000:.2f}B"
    elif abs(number) >= 1_000_000:
        text = f"{prefix}{number / 1_000_000:.2f}M"
    elif abs(number) >= 1_000:
        text = f"{prefix}{number:,.0f}"
    else:
        text = f"{prefix}{number:.2f}".rstrip("0").rstrip(".")
    return f"{text} {suffix}".strip()


def _consensus_forecast_payload(row: AnalystEvidence) -> dict[str, Any]:
    payload = dict(row.payload or {})
    forecast = payload.get("forecast")
    return dict(forecast) if isinstance(forecast, dict) else {}


def _consensus_metric(row: AnalystEvidence) -> str:
    forecast = _consensus_forecast_payload(row)
    if row.price_target is not None and str(row.evidence_kind or "").lower() == "price_target":
        return "price_target"
    return _consensus_first_text(forecast.get("metric_key"), row.estimate_metric)


def _consensus_period(row: AnalystEvidence) -> str:
    forecast = _consensus_forecast_payload(row)
    return _consensus_first_text(forecast.get("period"), row.estimate_period, row.target_horizon, "latest")


def _consensus_value(row: AnalystEvidence) -> tuple[float | None, str, str, str]:
    forecast = _consensus_forecast_payload(row)
    value = _consensus_float(forecast.get("value"))
    if value is None:
        value = _consensus_float(row.estimate_value)
    if value is None and row.price_target is not None:
        value = _consensus_float(row.price_target)
    unit = _consensus_first_text(forecast.get("unit"), row.estimate_unit)
    currency = _consensus_first_text(forecast.get("currency"), row.currency)
    value_type = _consensus_first_text(forecast.get("metric_value_type"))
    return value, unit, currency, value_type


def _consensus_reported_range(row: AnalystEvidence, value: float | None) -> tuple[float | None, float | None, str]:
    forecast = _consensus_forecast_payload(row)
    low = _consensus_float(forecast.get("range_low"))
    high = _consensus_float(forecast.get("range_high"))
    if low is not None and high is not None and low <= high:
        return low, high, str(forecast.get("value_basis") or "range_midpoint")
    return value, value, "reported_point"


def _consensus_subject(row: AnalystEvidence) -> dict[str, Any]:
    if row.company is not None:
        slug = str(row.company.slug or "").strip()
        return {
            "key": f"company:{int(row.company.id)}",
            "label": row.company.name,
            "ticker": row.company.ticker or "",
            "href": f"/companies/{slug}" if slug else f"/companies/{int(row.company.id)}",
            "scope": "company",
        }
    if row.issuer is not None:
        ticker = ""
        identifiers = getattr(row.issuer, "identifiers", None) or []
        for ident in identifiers:
            if str(getattr(ident, "id_type", "") or "").upper() == "TICKER":
                ticker = str(getattr(ident, "id_value", "") or "")
                break
        return {
            "key": f"issuer:{int(row.issuer.id)}",
            "label": row.issuer.display_name,
            "ticker": ticker,
            "href": f"/companies/{row.issuer.slug}" if row.issuer.slug else "",
            "scope": "issuer",
        }
    if row.subject is not None:
        href = ""
        if row.subject.company is not None:
            slug = str(row.subject.company.slug or "").strip()
            href = f"/companies/{slug}" if slug else f"/companies/{int(row.subject.company.id)}"
        elif row.subject.issuer is not None and row.subject.issuer.slug:
            href = f"/companies/{row.subject.issuer.slug}"
        return {
            "key": f"subject:{int(row.subject.id)}",
            "label": row.subject.display_name,
            "ticker": "",
            "href": href,
            "scope": "subject",
        }
    return {"key": "unscoped", "label": "Unscoped", "ticker": "", "href": "", "scope": "unscoped"}


def _consensus_source_label(row: AnalystEvidence) -> str:
    if row.underlying_source_actor is not None:
        actor_label = _consensus_first_text(
            row.underlying_source_actor.display_name,
            row.underlying_source_actor.actor_key,
        )
        if actor_label:
            return actor_label
    payload = dict(row.payload or {})
    attribution = payload.get("source_attribution") if isinstance(payload.get("source_attribution"), dict) else {}
    underlying = attribution.get("underlying_source") if isinstance(attribution.get("underlying_source"), dict) else {}
    return _consensus_first_text(underlying.get("firm"), underlying.get("name"), row.source_name, row.outlet, row.author, "Unknown source")


def _consensus_owner_key(row: AnalystEvidence, source_label: str) -> str:
    if row.underlying_source_actor_id is not None:
        return f"actor:{int(row.underlying_source_actor_id)}"
    payload = dict(row.payload or {})
    owner = _consensus_first_text(
        payload.get("estimate_owner"),
        payload.get("underlying_firm"),
        source_label,
    ).casefold()
    owner = re.sub(r"\b(incorporated|corporation|corp\.?|company|co\.?|limited|ltd\.?)\b", "", owner)
    owner = re.sub(r"[^a-z0-9]+", " ", owner).strip()
    return f"label:{owner or 'unknown'}"


def _consensus_lane(row: AnalystEvidence) -> str:
    payload = dict(row.payload or {})
    explicit = _consensus_first_text(payload.get("estimate_owner_type"), payload.get("consensus_lane")).lower()
    aliases = {
        "sell_side": "sell_side",
        "sell-side": "sell_side",
        "buy_side": "buy_side",
        "buy-side": "buy_side",
        "issuer": "issuer_guidance",
        "company": "issuer_guidance",
        "specialist_research": "specialist_research",
        "media": "media",
    }
    if explicit in aliases:
        return aliases[explicit]
    side = _consensus_first_text(row.source_side, row.source_role_class).lower()
    if side == "issuer" or row.source_role_class == "issuer":
        return "issuer_guidance"
    if "sell" in side or "research_analyst" in side:
        return "sell_side"
    if "buy" in side or "portfolio_manager" in side:
        return "buy_side"
    if "specialist" in side:
        return "specialist_research"
    if "media" in side:
        return "media"
    return "unknown"


def _consensus_aggregate_exclusions(
    row: AnalystEvidence,
    *,
    subject: dict[str, Any],
    source_label: str,
    metric: str,
    period: str,
    value: float | None,
    value_type: str,
) -> list[str]:
    reasons: list[str] = []
    status = str(row.status or "").strip().lower()
    if status in {"archived", "rejected", "invalid", "withdrawn", "superseded"}:
        reasons.append("inactive_status")
    if subject.get("scope") == "unscoped":
        reasons.append("unscoped_subject")
    if source_label.casefold() in {"unknown", "unknown source"}:
        reasons.append("unknown_estimate_owner")
    if not metric:
        reasons.append("missing_metric")
    if value is None:
        reasons.append("nonnumeric_observation")
    value_type_l = str(value_type or "").strip().lower()
    if value_type_l in {"status", "event", "date", "text", "qualitative"}:
        reasons.append("non_numeric_value_type")
    forecast = _consensus_forecast_payload(row)
    if value == 0 and forecast.get("forecast_status") and value_type_l in {"", "status", "event", "date"}:
        reasons.append("status_sentinel_value")
    if not period:
        reasons.append("missing_period")
    return list(dict.fromkeys(reasons))


def _consensus_person_label(row: AnalystEvidence) -> str:
    payload = dict(row.payload or {})
    attribution = payload.get("source_attribution") if isinstance(payload.get("source_attribution"), dict) else {}
    observed = attribution.get("observed_source") if isinstance(attribution.get("observed_source"), dict) else {}
    return _consensus_first_text(
        row.author,
        observed.get("name"),
        observed.get("handle"),
        payload.get("observed_source_name"),
        payload.get("observed_source_handle"),
        "",
    )


def _consensus_product_category(row: AnalystEvidence) -> str:
    payload = dict(row.payload or {})
    forecast = _consensus_forecast_payload(row)
    values = [
        forecast.get("product_category"),
        forecast.get("product"),
        forecast.get("category"),
        forecast.get("segment"),
        forecast.get("market"),
        payload.get("product_category"),
        payload.get("product"),
        payload.get("category"),
        payload.get("segment"),
        payload.get("market"),
    ]
    return _consensus_first_text(*values)


def _consensus_product_haystack(row: AnalystEvidence) -> str:
    payload = dict(row.payload or {})
    forecast = _consensus_forecast_payload(row)
    parts = [
        _consensus_product_category(row),
        _consensus_metric(row),
        _consensus_first_text(forecast.get("metric_label")),
        row.estimate_metric or "",
        row.summary or "",
        json.dumps(payload, ensure_ascii=False)[:4000],
    ]
    return " ".join(str(part or "") for part in parts).lower()


def _consensus_dataset_href(label: str) -> str:
    return "/visualizations/datasets?" + urlencode({"q": f"{label} Analyst Evidence", "family": "analyst_evidence"})


def _consensus_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                return None
    return None


def _consensus_event_direction(row: InvestmentCatalyst) -> str:
    payload = dict(row.payload or {})
    raw = _consensus_first_text(
        payload.get("impact_direction"),
        payload.get("directional_impact"),
        payload.get("consensus_direction"),
        payload.get("direction"),
        payload.get("polarity"),
        payload.get("bias"),
    ).lower()
    text = f"{row.catalyst_kind} {row.title} {row.summary or ''}".lower()
    if any(token in raw for token in ("down", "negative", "bear", "headwind", "lower")):
        return "downside"
    if any(token in raw for token in ("up", "positive", "bull", "tailwind", "higher")):
        return "upside"
    if any(token in text for token in ("undersupply", "shortage", "supply tight", "tightness", "capacity constraint")):
        return "upside"
    if any(token in text for token in ("oversupply", "glut", "inventory correction", "supply glut")):
        return "downside"
    return "watch"


def _consensus_event_expiry(row: InvestmentCatalyst) -> tuple[str, str]:
    payload = dict(row.payload or {})
    explicit = _consensus_date(
        payload.get("expires_at")
        or payload.get("expiration_date")
        or payload.get("stale_after")
        or payload.get("stale_after_date")
    )
    basis = _consensus_date(row.event_date or row.event_at or row.updated_at)
    if explicit is not None:
        expiry = explicit
        source = "explicit"
    elif basis is not None:
        try:
            ttl_days = int(payload.get("ttl_days") or payload.get("expires_after_days") or 90)
        except (TypeError, ValueError):
            ttl_days = 90
        expiry = basis + timedelta(days=max(7, min(ttl_days, 365)))
        source = "derived"
    else:
        return "", ""
    status = "expired" if expiry < calendar_today() else source
    return expiry.isoformat(), status


def _consensus_event_reference_label(row: InvestmentCatalyst) -> str:
    payload = dict(row.payload or {})
    value = _consensus_first_text(
        payload.get("reference_value"),
        payload.get("previous_value"),
        payload.get("latest_value"),
        payload.get("baseline_value"),
    )
    if not value:
        return ""
    unit = _consensus_first_text(payload.get("reference_unit"), payload.get("unit"), payload.get("currency"))
    period = _consensus_first_text(payload.get("reference_period"), payload.get("period"), payload.get("baseline_period"))
    parts = [value]
    if unit:
        parts.append(unit)
    if period:
        parts.append(period)
    return " ".join(parts)


def _consensus_directional_events(db: Session, rows: list[AnalystEvidence]) -> list[dict[str, Any]]:
    company_ids = {int(row.company_id) for row in rows if row.company_id is not None}
    issuer_ids = {int(row.issuer_id) for row in rows if row.issuer_id is not None}
    subject_ids = {int(row.subject_id) for row in rows if row.subject_id is not None}
    if not company_ids and not issuer_ids and not subject_ids:
        return []

    source_sets: dict[str, set[str]] = {}
    source_updates: dict[tuple[str, str], datetime] = {}
    for row in rows:
        source = _consensus_source_label(row)
        published_at = row.published_at or row.updated_at
        keys = []
        if row.company_id is not None:
            keys.append(f"company:{int(row.company_id)}")
        if row.issuer_id is not None:
            keys.append(f"issuer:{int(row.issuer_id)}")
        if row.subject_id is not None:
            keys.append(f"subject:{int(row.subject_id)}")
        for key in keys:
            source_sets.setdefault(key, set()).add(source)
            if published_at is not None:
                update_key = (key, source)
                if update_key not in source_updates or published_at > source_updates[update_key]:
                    source_updates[update_key] = published_at

    filters = []
    if company_ids:
        filters.append(InvestmentCatalyst.company_id.in_(company_ids))
    if issuer_ids:
        filters.append(InvestmentCatalyst.issuer_id.in_(issuer_ids))
    if subject_ids:
        filters.append(InvestmentCatalyst.subject_id.in_(subject_ids))
    event_rows = (
        db.query(InvestmentCatalyst)
        .filter(or_(*filters), InvestmentCatalyst.status != "archived")
        .order_by(
            InvestmentCatalyst.event_date.desc().nullslast(),
            InvestmentCatalyst.updated_at.desc(),
            InvestmentCatalyst.priority.desc(),
            InvestmentCatalyst.id.desc(),
        )
        .limit(80)
        .all()
    )
    directional: list[dict[str, Any]] = []
    for event in event_rows:
        direction = _consensus_event_direction(event)
        if direction == "watch" and event.catalyst_kind not in {"undersupply", "company_disclosure", "capacity", "capex", "pricing"}:
            continue
        event_date = _consensus_date(event.event_date or event.event_at or event.updated_at)
        event_keys = []
        if event.company_id is not None:
            event_keys.append(f"company:{int(event.company_id)}")
        if event.issuer_id is not None:
            event_keys.append(f"issuer:{int(event.issuer_id)}")
        if event.subject_id is not None:
            event_keys.append(f"subject:{int(event.subject_id)}")
        all_sources = set().union(*(source_sets.get(key, set()) for key in event_keys)) if event_keys else set()
        updated_sources: set[str] = set()
        if event_date is not None:
            event_dt = datetime.combine(event_date, datetime.min.time())
            for key in event_keys:
                for source in source_sets.get(key, set()):
                    latest_at = source_updates.get((key, source))
                    if latest_at is not None and latest_at >= event_dt:
                        updated_sources.add(source)
        pending_sources = sorted(all_sources - updated_sources)
        expires_at, expiry_status = _consensus_event_expiry(event)
        payload = dict(event.payload or {})
        directional.append(
            {
                "id": int(event.id),
                "title": event.title,
                "kind": event.catalyst_kind,
                "direction": direction,
                "event_date": event_date.isoformat() if event_date else "",
                "expires_at": expires_at,
                "expiry_status": expiry_status,
                "status": event.status,
                "summary": event.summary or "",
                "source_url": event.source_url or "",
                "source_document_href": f"/docs/{int(event.source_document_id)}" if event.source_document_id is not None else "",
                "impact_target": _consensus_first_text(payload.get("impact_target"), payload.get("affected_metric"), payload.get("affected_subject")),
                "impact_marker": _consensus_first_text(payload.get("impact_marker"), "marker" if direction in {"upside", "downside"} else ""),
                "reference_label": _consensus_event_reference_label(event),
                "updated_sources": len(updated_sources),
                "total_sources": len(all_sources),
                "pending_sources": len(pending_sources),
                "pending_sources_label": ", ".join(pending_sources[:4]),
            }
        )
        if len(directional) >= 12:
            break
    return directional


def _consensus_period_sort_key(value: str) -> tuple[int, str]:
    text = str(value or "").strip()
    match = re.search(r"(20\d{2})", text)
    year = int(match.group(1)) if match else 9999
    quarter_match = re.search(r"\bQ([1-4])\b|\b([1-4])Q\b", text.upper())
    half_match = re.search(r"\bH([12])\b|\b([12])H\b", text.upper())
    part = 0
    if quarter_match:
        part = int(quarter_match.group(1) or quarter_match.group(2))
    elif half_match:
        part = 10 + int(half_match.group(1) or half_match.group(2))
    elif text.lower() == "latest":
        part = -1
    return (year * 20 + part, text.lower())


def _consensus_matrix_rows(consensus_rows: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    columns = sorted({str(row.get("period") or "latest") for row in consensus_rows}, key=_consensus_period_sort_key)[:8]
    row_map: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in consensus_rows:
        subject = str(row.get("subject") or "Unscoped")
        metric = str(row.get("metric") or "evidence")
        lane = str(row.get("lane") or "unknown")
        scenario = str(row.get("scenario") or "")
        product_category = str(row.get("product_category") or "")
        unit = str(row.get("unit") or "")
        currency = str(row.get("currency") or "")
        value_type = str(row.get("value_type") or "")
        period_basis = str(row.get("period_basis") or "")
        period = str(row.get("period") or "latest")
        entry = row_map.setdefault(
            (subject, metric, lane, scenario, product_category, unit, currency, value_type, period_basis),
            {
                "subject": subject,
                "metric": " · ".join(part for part in (metric, lane.replace("_", " "), product_category, scenario, unit or currency, period_basis) if part),
                "metric_key": metric,
                "lane": lane,
                "scenario": scenario,
                "product_category": product_category,
                "unit": unit,
                "currency": currency,
                "value_type": value_type,
                "period_basis": period_basis,
                "source_count": 0,
                "latest_at": "",
                "cells_by_period": {},
            },
        )
        entry["source_count"] = max(int(entry.get("source_count") or 0), int(row.get("source_count") or 0))
        if str(row.get("latest_at") or "") > str(entry.get("latest_at") or ""):
            entry["latest_at"] = str(row.get("latest_at") or "")
        entry["cells_by_period"][period] = {
            "consensus": row.get("consensus") or "n/a",
            "range": row.get("range") or "n/a",
            "sources": int(row.get("source_count") or 0),
            "rows": int(row.get("evidence_count") or 0),
        }
    matrix_rows: list[dict[str, Any]] = []
    for entry in row_map.values():
        matrix_rows.append(
            {
                "subject": entry["subject"],
                "metric": entry["metric"],
                "source_count": entry["source_count"],
                "latest_at": entry["latest_at"],
                "cells": [
                    {
                        "period": period,
                        **(entry["cells_by_period"].get(period) or {"consensus": "", "range": "", "sources": 0, "rows": 0}),
                    }
                    for period in columns
                ],
            }
        )
    matrix_rows.sort(key=lambda row: (int(row.get("source_count") or 0), str(row.get("latest_at") or "")), reverse=True)
    return columns, matrix_rows[:80]


def _consensus_suggestion_groups(db: Session, query_text: str, *, limit: int = 6) -> list[dict[str, Any]]:
    q = str(query_text or "").strip()
    q_l = q.lower()
    buckets: dict[str, dict[str, dict[str, Any]]] = {
        "subjects": {},
        "products": {},
        "sources": {},
        "people": {},
        "metrics": {},
        "periods": {},
    }

    def add(group: str, label: str, *, value: str | None = None, param: str = "q", meta: str = "", href: str = "") -> None:
        label = str(label or "").strip()
        if not label:
            return
        value = str(value or label).strip()
        key = f"{param}:{value}".lower()
        row = buckets[group].setdefault(
            key,
            {
                "label": label,
                "value": value,
                "param": param,
                "meta": meta,
                "href": href,
                "count": 0,
            },
        )
        row["count"] = int(row.get("count") or 0) + 1
        if meta and not row.get("meta"):
            row["meta"] = meta
        if href and not row.get("href"):
            row["href"] = href

    rows = (
        db.query(AnalystEvidence)
        .options(
            joinedload(AnalystEvidence.company),
            joinedload(AnalystEvidence.issuer).selectinload(Issuer.identifiers),
            joinedload(AnalystEvidence.subject).joinedload(ResearchSubject.company),
            joinedload(AnalystEvidence.subject).joinedload(ResearchSubject.issuer),
            joinedload(AnalystEvidence.underlying_source_actor),
            joinedload(AnalystEvidence.observed_source_actor),
        )
        .filter(AnalystEvidence.status != "archived")
        .order_by(AnalystEvidence.published_at.desc().nullslast(), AnalystEvidence.updated_at.desc(), AnalystEvidence.id.desc())
        .limit(700)
        .all()
    )
    for row in rows:
        subject = _consensus_subject(row)
        product = _consensus_product_category(row)
        source = _consensus_source_label(row)
        person = _consensus_person_label(row)
        metric = _consensus_metric(row)
        period = _consensus_period(row)
        hay = " ".join(
            [
                str(subject.get("label") or ""),
                str(subject.get("ticker") or ""),
                product,
                source,
                person,
                metric,
                period,
                row.summary or "",
            ]
        ).lower()
        if q_l and q_l not in hay:
            continue
        add("subjects", str(subject.get("label") or ""), value=str(subject.get("label") or ""), param="subject", meta=str(subject.get("scope") or ""), href=str(subject.get("href") or ""))
        add("products", product, param="product_category", meta="product/category")
        add("sources", source, param="source_firm", meta="source firm")
        add("people", person, param="analyst", meta="analyst/person")
        add("metrics", metric, param="metric", meta="metric")
        add("periods", period, param="period", meta="period")

    if q:
        pattern = f"%{q}%"
        actor_rows = (
            db.query(SourceActor)
            .outerjoin(SourceActorAlias, SourceActorAlias.actor_id == SourceActor.id)
            .filter(SourceActor.status != "archived")
            .filter(or_(SourceActor.display_name.ilike(pattern), SourceActorAlias.alias.ilike(pattern)))
            .order_by(SourceActor.display_name.asc(), SourceActor.id.asc())
            .limit(40)
            .all()
        )
        seen_actor_ids: set[int] = set()
        for actor in actor_rows:
            if int(actor.id) in seen_actor_ids:
                continue
            seen_actor_ids.add(int(actor.id))
            group = "people" if str(actor.actor_type or "").lower() == "person" else "sources"
            param = "analyst" if group == "people" else "source_firm"
            add(group, actor.display_name, param=param, meta=_consensus_first_text(actor.side, actor.actor_type, "source actor"))

        for metric in MODEL_METRICS:
            metric_hay = " ".join([metric.key, metric.label, metric.description, *metric.aliases]).lower()
            if q_l in metric_hay:
                add("metrics", metric.label, value=metric.key, param="metric", meta=metric.value_type)

        if q_l not in {str(item.get("value") or "").lower() for item in buckets["products"].values()}:
            add("products", q, value=q, param="product_category", meta="search as product/category")

    labels = {
        "subjects": "Subjects",
        "products": "Products / Categories",
        "sources": "Source Firms",
        "people": "Analysts / People",
        "metrics": "Metrics",
        "periods": "Periods",
    }
    groups: list[dict[str, Any]] = []
    for key in ("subjects", "products", "sources", "people", "metrics", "periods"):
        items = sorted(buckets[key].values(), key=lambda item: (int(item.get("count") or 0), str(item.get("label") or "").lower()), reverse=True)
        groups.append({"key": key, "label": labels[key], "items": items[:limit]})
    return groups


def _consensus_payload(
    db: Session,
    *,
    subject_id: int | None = None,
    query_text: str = "",
    subject_filter: str = "",
    source_firm_filter: str = "",
    analyst_filter: str = "",
    product_category_filter: str = "",
    metric_filter: str = "",
    period_filter: str = "",
    limit: int = 150,
) -> dict[str, Any]:
    limit_val = max(25, min(int(limit or 150), 500))
    query_text = str(query_text or "").strip()
    subject_filter = str(subject_filter or "").strip()
    source_firm_filter = str(source_firm_filter or "").strip()
    analyst_filter = str(analyst_filter or "").strip()
    product_category_filter = str(product_category_filter or "").strip()
    metric_filter_l = str(metric_filter or "").strip().lower()
    period_filter_l = str(period_filter or "").strip().lower()
    rows_query = (
        db.query(AnalystEvidence)
        .options(
            joinedload(AnalystEvidence.company),
            joinedload(AnalystEvidence.issuer).selectinload(Issuer.identifiers),
            joinedload(AnalystEvidence.subject).joinedload(ResearchSubject.company),
            joinedload(AnalystEvidence.subject).joinedload(ResearchSubject.issuer),
        )
        .outerjoin(Company, AnalystEvidence.company_id == Company.id)
        .outerjoin(Issuer, AnalystEvidence.issuer_id == Issuer.id)
        .outerjoin(ResearchSubject, AnalystEvidence.subject_id == ResearchSubject.id)
        .filter(AnalystEvidence.status != "archived")
    )
    if subject_id is not None:
        rows_query = rows_query.filter(AnalystEvidence.subject_id == int(subject_id))
    if query_text:
        pattern = f"%{query_text}%"
        rows_query = rows_query.filter(
            or_(
                AnalystEvidence.source_name.ilike(pattern),
                AnalystEvidence.outlet.ilike(pattern),
                AnalystEvidence.author.ilike(pattern),
                AnalystEvidence.estimate_metric.ilike(pattern),
                AnalystEvidence.estimate_period.ilike(pattern),
                AnalystEvidence.summary.ilike(pattern),
                Company.name.ilike(pattern),
                Company.ticker.ilike(pattern),
                Issuer.display_name.ilike(pattern),
                Issuer.legal_name.ilike(pattern),
                ResearchSubject.display_name.ilike(pattern),
                ResearchSubject.slug.ilike(pattern),
            )
        )
    if subject_filter:
        pattern = f"%{subject_filter}%"
        rows_query = rows_query.filter(
            or_(
                Company.name.ilike(pattern),
                Company.ticker.ilike(pattern),
                Issuer.display_name.ilike(pattern),
                Issuer.legal_name.ilike(pattern),
                ResearchSubject.display_name.ilike(pattern),
                ResearchSubject.slug.ilike(pattern),
            )
        )
    raw_rows = (
        rows_query.order_by(
            AnalystEvidence.published_at.desc().nullslast(),
            AnalystEvidence.updated_at.desc(),
            AnalystEvidence.id.desc(),
        )
        .limit(max(2_000, min(limit_val * 4, 5_000)))
        .all()
    )
    metric_options = sorted({_consensus_metric(row) for row in raw_rows if _consensus_metric(row)})
    period_options = sorted({_consensus_period(row) for row in raw_rows if _consensus_period(row)})
    filtered_rows: list[AnalystEvidence] = []
    source_firm_filter_l = source_firm_filter.lower()
    analyst_filter_l = analyst_filter.lower()
    product_category_filter_l = product_category_filter.lower()
    for row in raw_rows:
        metric = _consensus_metric(row)
        period = _consensus_period(row)
        if metric_filter_l and metric.lower() != metric_filter_l:
            continue
        if period_filter_l and period.lower() != period_filter_l:
            continue
        if source_firm_filter_l and source_firm_filter_l not in _consensus_source_label(row).lower():
            continue
        if analyst_filter_l and analyst_filter_l not in _consensus_person_label(row).lower():
            continue
        if product_category_filter_l and product_category_filter_l not in _consensus_product_haystack(row):
            continue
        filtered_rows.append(row)

    subject_map: dict[str, dict[str, Any]] = {}
    group_map: dict[tuple[str, ...], dict[str, Any]] = {}
    cohort_map: dict[tuple[str, str, str], dict[str, Any]] = {}
    evidence_rows: list[dict[str, Any]] = []
    non_modelable_count = 0
    for row in filtered_rows:
        subject = _consensus_subject(row)
        source_label = _consensus_source_label(row)
        person_label = _consensus_person_label(row)
        product_category = _consensus_product_category(row)
        metric = _consensus_metric(row)
        display_metric = _consensus_first_text(metric, row.evidence_kind, "evidence")
        period = _consensus_period(row)
        value, unit, currency, value_type = _consensus_value(row)
        source_low, source_high, value_basis = _consensus_reported_range(row, value)
        forecast = _consensus_forecast_payload(row)
        lane = _consensus_lane(row)
        scenario = _consensus_first_text(forecast.get("scenario"), dict(row.payload or {}).get("scenario"))
        period_basis = _consensus_first_text(forecast.get("period_basis"))
        published_at = row.published_at or row.updated_at
        modelable = bool(metric and (value is not None or row.rating or row.price_target is not None))
        owner_key = _consensus_owner_key(row, source_label)
        aggregate_exclusions = _consensus_aggregate_exclusions(
            row,
            subject=subject,
            source_label=source_label,
            metric=metric,
            period=period,
            value=value,
            value_type=value_type,
        )
        aggregate_eligible = not aggregate_exclusions
        if not modelable:
            non_modelable_count += 1
        subject_entry = subject_map.setdefault(
            subject["key"],
            {
                **subject,
                "sources": set(),
                "metrics": set(),
                "evidence_count": 0,
                "estimate_count": 0,
                "latest_at": None,
                "latest_rating": "",
                "latest_target": "",
                "dataset_href": _consensus_dataset_href(subject["label"]),
            },
        )
        subject_entry["evidence_count"] += 1
        subject_entry["sources"].add(source_label)
        if metric:
            subject_entry["metrics"].add(metric)
        if modelable:
            subject_entry["estimate_count"] += 1
        if published_at and (subject_entry["latest_at"] is None or published_at > subject_entry["latest_at"]):
            subject_entry["latest_at"] = published_at
        if row.rating and not subject_entry["latest_rating"]:
            subject_entry["latest_rating"] = row.rating
        if row.price_target is not None and not subject_entry["latest_target"]:
            subject_entry["latest_target"] = _consensus_format_value(row.price_target, unit=unit, currency=currency or row.currency or "")

        if modelable and value is not None:
            group_key = (
                subject["key"],
                subject["label"],
                metric,
                period or "latest",
                str(unit or "").strip().casefold(),
                str(currency or "").strip().upper(),
                str(value_type or "").strip().casefold(),
                lane,
                product_category.casefold(),
                scenario.casefold(),
                period_basis.casefold(),
            )
            group = group_map.setdefault(
                group_key,
                {
                    "subject": subject["label"],
                    "metric": metric,
                    "period": period or "latest",
                    "unit": unit or "",
                    "currency": currency or "",
                    "value_type": value_type or "",
                    "lane": lane,
                    "product_category": product_category,
                    "scenario": scenario,
                    "period_basis": period_basis,
                    "observations_by_owner": {},
                    "excluded_count": 0,
                    "exclusion_reasons": set(),
                    "historical_observation_count": 0,
                    "latest_at": None,
                    "dataset_href": _consensus_dataset_href(subject["label"]),
                },
            )
            if aggregate_eligible:
                observation = {
                    "id": int(row.id),
                    "value": value,
                    "range_low": source_low,
                    "range_high": source_high,
                    "value_basis": value_basis,
                    "source": source_label,
                    "owner_key": owner_key,
                    "published_at": published_at,
                }
                previous = group["observations_by_owner"].get(owner_key)
                previous_sort = (
                    previous.get("published_at") or datetime.min,
                    int(previous.get("id") or 0),
                ) if previous else None
                current_sort = (published_at or datetime.min, int(row.id))
                if previous is None or current_sort > previous_sort:
                    if previous is not None:
                        group["historical_observation_count"] += 1
                    group["observations_by_owner"][owner_key] = observation
                else:
                    group["historical_observation_count"] += 1
            else:
                group["excluded_count"] += 1
                group["exclusion_reasons"].update(aggregate_exclusions)
            if published_at and (group["latest_at"] is None or published_at > group["latest_at"]):
                group["latest_at"] = published_at

        cohort_key = (source_label, _consensus_first_text(row.source_side, "unknown"), _consensus_first_text(row.source_role_class, "unknown"))
        cohort = cohort_map.setdefault(
            cohort_key,
            {
                "source": source_label,
                "source_side": cohort_key[1],
                "role": cohort_key[2],
                "subjects": set(),
                "evidence_count": 0,
                "latest_at": None,
                "trust": _consensus_first_text(row.trust_label, row.evidence_tier, ""),
            },
        )
        cohort["subjects"].add(subject["label"])
        cohort["evidence_count"] += 1
        if published_at and (cohort["latest_at"] is None or published_at > cohort["latest_at"]):
            cohort["latest_at"] = published_at

        evidence_rows.append(
            {
                "detail_index": len(evidence_rows),
                "id": int(row.id),
                "subject": subject,
                "source": source_label,
                "person": person_label,
                "product_category": product_category,
                "metric": display_metric,
                "period": period or "latest",
                "period_basis": _consensus_first_text(_consensus_forecast_payload(row).get("period_basis")),
                "lane": lane,
                "scenario": scenario,
                "value": _consensus_format_value(value, unit=unit, currency=currency, value_type=value_type),
                "reported_range": (
                    f"{_consensus_format_value(source_low, unit=unit, currency=currency, value_type=value_type)} - "
                    f"{_consensus_format_value(source_high, unit=unit, currency=currency, value_type=value_type)}"
                    if source_low is not None and source_high is not None and source_low != source_high else ""
                ),
                "value_basis": value_basis,
                "unit": currency or unit,
                "kind": row.evidence_kind,
                "rating": row.rating or "",
                "target": _consensus_format_value(row.price_target, unit=unit, currency=currency or row.currency or "") if row.price_target is not None else "",
                "confidence": f"{float(row.confidence):.2f}" if row.confidence is not None else "",
                "published_at": published_at.isoformat() if published_at else "",
                "summary": row.summary or "",
                "source_url": row.source_url or "",
                "source_document_href": f"/docs/{int(row.source_document_id)}" if row.source_document_id is not None else "",
                "modelable": modelable,
                "aggregate_eligible": aggregate_eligible,
                "aggregate_included": False,
                "aggregate_exclusions": aggregate_exclusions,
                "aggregate_owner_key": owner_key,
            }
        )

    subject_rows = []
    for entry in subject_map.values():
        latest_at = entry["latest_at"]
        subject_rows.append(
            {
                **{key: value for key, value in entry.items() if key not in {"sources", "metrics", "latest_at"}},
                "source_count": len(entry["sources"]),
                "metric_count": len(entry["metrics"]),
                "metrics_label": ", ".join(sorted(entry["metrics"])[:5]) or "—",
                "latest_at": latest_at.isoformat() if latest_at else "",
            }
        )
    subject_rows.sort(key=lambda row: (row.get("latest_at") or "", int(row.get("evidence_count") or 0)), reverse=True)

    aggregate_included_ids = {
        int(observation["id"])
        for group in group_map.values()
        for observation in (group.get("observations_by_owner") or {}).values()
    }
    for evidence in evidence_rows:
        evidence_id = int(evidence.get("id") or 0)
        evidence["aggregate_included"] = evidence_id in aggregate_included_ids
        if evidence.get("aggregate_eligible") and evidence_id not in aggregate_included_ids:
            evidence["aggregate_eligible"] = False
            evidence["aggregate_exclusions"] = [
                *list(evidence.get("aggregate_exclusions") or []),
                "superseded_by_newer_owner_observation",
            ]

    consensus_rows = []
    for group in group_map.values():
        observations = list((group.get("observations_by_owner") or {}).values())
        values = [float(row["value"]) for row in observations]
        range_lows = [float(row["range_low"]) for row in observations if row.get("range_low") is not None]
        range_highs = [float(row["range_high"]) for row in observations if row.get("range_high") is not None]
        if not values:
            continue
        median_value = _consensus_median(values)
        latest_at = group["latest_at"]
        unit = str(group.get("unit") or "")
        currency = str(group.get("currency") or "")
        value_type = str(group.get("value_type") or "")
        warnings: list[str] = []
        if len(observations) < 2:
            warnings.append("single_estimate_owner")
        if any(row.get("value_basis") == "range_midpoint" for row in observations):
            warnings.append("derived_range_midpoint")
        if int(group.get("historical_observation_count") or 0) > 0:
            warnings.append("historical_observations_superseded")
        if int(group.get("excluded_count") or 0) > 0:
            warnings.append("observations_excluded")
        consensus_rows.append(
            {
                "subject": group["subject"],
                "metric": group["metric"],
                "period": group["period"],
                "unit": unit,
                "currency": currency,
                "value_type": value_type,
                "lane": group.get("lane") or "unknown",
                "product_category": group.get("product_category") or "",
                "scenario": group.get("scenario") or "",
                "period_basis": group.get("period_basis") or "",
                "source_count": len(observations),
                "evidence_count": len(values),
                "observation_count": len(values) + int(group.get("historical_observation_count") or 0) + int(group.get("excluded_count") or 0),
                "historical_observation_count": int(group.get("historical_observation_count") or 0),
                "excluded_observation_count": int(group.get("excluded_count") or 0),
                "exclusion_reasons": sorted(group.get("exclusion_reasons") or []),
                "warnings": warnings,
                "consensus_basis": "median_of_estimate_points_and_range_midpoints",
                "consensus_value": median_value,
                "consensus": _consensus_format_value(median_value, unit=unit, currency=currency, value_type=value_type),
                "range": (
                    f"{_consensus_format_value(min(range_lows), unit=unit, currency=currency, value_type=value_type)} - {_consensus_format_value(max(range_highs), unit=unit, currency=currency, value_type=value_type)}"
                    if range_lows and range_highs and min(range_lows) != max(range_highs)
                    else _consensus_format_value(values[0], unit=unit, currency=currency, value_type=value_type)
                    if values
                    else "—"
                ),
                "latest_at": latest_at.isoformat() if latest_at else "",
                "dataset_href": group["dataset_href"],
            }
        )
    consensus_rows.sort(key=lambda row: (row.get("latest_at") or "", int(row.get("source_count") or 0)), reverse=True)
    matrix_columns, matrix_rows = _consensus_matrix_rows(consensus_rows)

    cohort_rows = []
    for cohort in cohort_map.values():
        latest_at = cohort["latest_at"]
        cohort_rows.append(
            {
                "source": cohort["source"],
                "source_side": cohort["source_side"],
                "role": cohort["role"],
                "subject_count": len(cohort["subjects"]),
                "evidence_count": cohort["evidence_count"],
                "trust": cohort["trust"],
                "latest_at": latest_at.isoformat() if latest_at else "",
            }
        )
    cohort_rows.sort(key=lambda row: (int(row.get("evidence_count") or 0), row.get("latest_at") or ""), reverse=True)
    directional_events = _consensus_directional_events(db, filtered_rows)
    return {
        "query": query_text,
        "subject_filter": subject_filter,
        "source_firm_filter": source_firm_filter,
        "analyst_filter": analyst_filter,
        "product_category_filter": product_category_filter,
        "metric_filter": metric_filter,
        "period_filter": period_filter,
        "limit": limit_val,
        "metric_options": metric_options,
        "period_options": period_options,
        "subject_rows": subject_rows[:80],
        "consensus_rows": consensus_rows[:120],
        "matrix_columns": matrix_columns,
        "matrix_rows": matrix_rows,
        "evidence_rows": evidence_rows[:limit_val],
        "cleanup_rows": [row for row in evidence_rows if not row.get("modelable")][:30],
        "cohort_rows": cohort_rows[:80],
        "directional_events": directional_events,
        "metrics": {
            "subjects": len(subject_rows),
            "consensus_groups": len(consensus_rows),
            "evidence_rows": len(evidence_rows),
            "source_cohorts": len(cohort_rows),
            "non_modelable": non_modelable_count,
            "aggregate_included": len(aggregate_included_ids),
            "aggregate_excluded": sum(1 for row in evidence_rows if row.get("modelable") and not row.get("aggregate_included")),
            "aggregate_warning_groups": sum(1 for row in consensus_rows if row.get("warnings")),
            "directional_events": len(directional_events),
        },
    }


@app.get("/consensus", response_class=HTMLResponse)
def consensus_view(
    request: Request,
    q: str | None = None,
    subject: str | None = None,
    source_firm: str | None = None,
    analyst: str | None = None,
    product_category: str | None = None,
    metric: str | None = None,
    period: str | None = None,
    limit: int = 150,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    payload = _consensus_payload(
        db,
        query_text=str(q or ""),
        subject_filter=str(subject or ""),
        source_firm_filter=str(source_firm or ""),
        analyst_filter=str(analyst or ""),
        product_category_filter=str(product_category or ""),
        metric_filter=str(metric or ""),
        period_filter=str(period or ""),
        limit=limit,
    )
    return _template_response(
        "consensus.html",
        {
            "request": request,
            "title": "Consensus",
            "wrap_class": "wide",
            **payload,
        },
    )


@app.get("/api/consensus/suggest")
def api_consensus_suggest(q: str | None = None, limit: int = 6, db: Session = Depends(get_session)) -> JSONResponse:
    groups = _consensus_suggestion_groups(db, str(q or ""), limit=max(3, min(int(limit or 6), 12)))
    return JSONResponse({"query": str(q or ""), "groups": groups})


@app.post("/api/analyst-evidence")
async def api_analyst_evidence_upsert(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
    _guard_internal_terminal()
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    row = upsert_analyst_evidence(db, dict(payload or {}), actor="codex")
    db.commit()
    return JSONResponse({"ok": True, "analyst_evidence": serialize_analyst_evidence(row)})


@app.post("/api/analyst-evidence/{evidence_id}/relink-subject")
async def api_analyst_evidence_relink_subject(
    evidence_id: int, request: Request, db: Session = Depends(get_session)
) -> JSONResponse:
    _guard_internal_terminal()
    payload = await request.json()

    def execute_relink() -> dict[str, Any]:
        result = relink_analyst_evidence_subject(
            db,
            evidence_id=evidence_id,
            subject_id=int(payload["subject_id"]) if payload.get("subject_id") is not None else None,
            subject_name=payload.get("subject_name"),
            subject_kind=payload.get("subject_kind"),
            estimate_metric=payload.get("estimate_metric"),
            expected_subject_id=payload.get("expected_subject_id"),
            expected_company_id=payload.get("expected_company_id"),
            expected_issuer_id=payload.get("expected_issuer_id"),
            evidence_url=str(payload.get("evidence_url") or ""),
            note=str(payload.get("note") or ""),
            apply=bool(payload.get("apply")),
        )
        if result["applied"]:
            db.commit()
        return result

    try:
        result = await run_in_threadpool(execute_relink)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "relink": result})
