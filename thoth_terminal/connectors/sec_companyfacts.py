from __future__ import annotations

from datetime import datetime, UTC
from typing import Any

from thoth_terminal.connectors.official_common import safe_json_dumps, stable_external_id
from thoth_terminal.connectors.sec_edgar import get_company_facts


SEC_CONCEPTS = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
    "SalesRevenueNet": "revenue",
    "Revenues": "revenue",
    "GrossProfit": "gross_profit",
    "OperatingIncomeLoss": "operating_income",
    "NetIncomeLoss": "net_income",
    "EarningsPerShareDiluted": "diluted_eps",
    "EarningsPerShareBasic": "eps",
    "PaymentsToAcquirePropertyPlantAndEquipment": "capex",
    "ResearchAndDevelopmentExpense": "rnd_expenses",
    "NetCashProvidedByUsedInOperatingActivities": "cf_operating",
    "Assets": "total_assets",
    "Liabilities": "total_liabilities",
    "StockholdersEquity": "net_assets",
}


def _list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    return [str(v).strip() for v in raw if str(v).strip()]


def _normalize_cik(value: object) -> str | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if 1 <= len(digits) <= 10:
        return digits.zfill(10)
    return None


def _map_lookup(mapping: object, cik: str) -> object | None:
    if not isinstance(mapping, dict):
        return None
    cik_padded = _normalize_cik(cik)
    if cik_padded is None:
        return None
    keys = [cik_padded, str(int(cik_padded))]
    for key in keys:
        if key in mapping:
            return mapping.get(key)
    return None


def _rotate(values: list[str], *, start: int, count: int) -> tuple[list[str], int]:
    if not values or count <= 0:
        return [], start
    out: list[str] = []
    idx = max(0, int(start or 0))
    for _ in range(min(count, len(values))):
        out.append(values[idx % len(values)])
        idx += 1
    return out, idx % len(values)


def _latest_units(concept_payload: dict[str, Any], *, max_points: int) -> list[dict[str, Any]]:
    units = concept_payload.get("units") if isinstance(concept_payload.get("units"), dict) else {}
    rows: list[dict[str, Any]] = []
    for unit, values in units.items():
        if not isinstance(values, list):
            continue
        ordered = sorted(
            [dict(v) for v in values if isinstance(v, dict) and v.get("val") is not None],
            key=lambda row: str(row.get("filed") or row.get("end") or ""),
            reverse=True,
        )
        for row in ordered[:max_points]:
            row["unit"] = unit
            rows.append(row)
    return rows


def fetch_sec_companyfacts(
    config: dict[str, Any],
    *,
    source_cursor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = dict(config or {})
    cursor = dict(source_cursor or {})
    ciks = _list(cfg.get("ciks") or cfg.get("cik"))
    if not ciks:
        raise ValueError("SEC companyfacts requires ciks.")
    ciks = [cik for cik in (_normalize_cik(raw) for raw in ciks) if cik]
    if not ciks:
        raise ValueError("SEC companyfacts requires valid ciks.")
    concepts = _list(cfg.get("concepts")) or list(SEC_CONCEPTS.keys())
    max_points = max(1, min(int(cfg.get("max_points_per_concept") or 12), 40))
    limit = max(1, min(int(cfg.get("max_items_per_run") or 100), 500))
    ciks_per_run = max(1, min(int(cfg.get("ciks_per_run") or cfg.get("requests_per_run") or 8), 100))
    selected_ciks, next_idx = _rotate(
        ciks,
        start=int(cursor.get("cik_index") or 0),
        count=min(ciks_per_run, limit),
    )
    cursor["cik_index"] = next_idx
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for cik_padded in selected_ciks:
        if len(items) >= limit:
            break
        try:
            payload = get_company_facts(cik_padded)
        except Exception as exc:
            errors.append({"cik": cik_padded, "error": f"{type(exc).__name__}: {exc}"})
            continue
        entity_name = str(payload.get("entityName") or cik).strip()
        facts = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
        us_gaap = facts.get("us-gaap") if isinstance(facts.get("us-gaap"), dict) else {}
        rows: list[dict[str, Any]] = []
        for concept in concepts:
            concept_payload = us_gaap.get(concept)
            if not isinstance(concept_payload, dict):
                continue
            metric_key = SEC_CONCEPTS.get(concept, concept)
            for point in _latest_units(concept_payload, max_points=max_points):
                rows.append(
                    {
                        "taxonomy": "us-gaap",
                        "concept": concept,
                        "metric_key": metric_key,
                        "label": concept_payload.get("label") or metric_key,
                        "description": concept_payload.get("description"),
                        **point,
                    }
                )
        title = f"{entity_name} SEC companyfacts"
        text = safe_json_dumps(rows)
        items.append(
            {
                "kind": "securities_filing",
                "external_id": stable_external_id("sec_companyfacts", cik_padded),
                "url": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json",
                "canonical_url": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json",
                "title": title,
                "author": entity_name,
                "published_at": datetime.now(UTC).replace(tzinfo=None),
                "raw_text": f"{title}\n{text}",
                "normalized_text": f"{title}\n{text}",
                "extra": {
                    "connector": "sec_companyfacts",
                    "regulator": "SEC",
                    "country": "US",
                    "section": "companyfacts",
                    "cik": cik_padded,
                    "company_slug": _map_lookup(cfg.get("company_slug_by_cik"), cik_padded),
                    "company_id": _map_lookup(cfg.get("company_id_by_cik"), cik_padded),
                    "ticker": _map_lookup(cfg.get("ticker_by_cik"), cik_padded),
                    "company_name": entity_name,
                    "payload": rows,
                },
            }
        )
    return {
        "items": items,
        "cursor": cursor,
        "stats": {
            "count": len(items),
            "source_kind": "sec_companyfacts",
            "selected_ciks": selected_ciks,
            "cik_count": len(ciks),
            "errors": errors,
        },
    }
