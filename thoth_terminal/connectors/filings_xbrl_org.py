from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import requests

from thoth_terminal.connectors.official_common import keyword_allowed, stable_external_id


_BASE_URL = "https://filings.xbrl.org"
_API_BASE_URL = f"{_BASE_URL}/api"
_DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
]


def _clean_lei(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if len(raw) != 20:
        return None
    if not raw.isalnum():
        return None
    return raw


def _abs_url(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return urljoin(_BASE_URL, raw)


def _parse_dt(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _request_json(path_or_url: str, *, params: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    url = path_or_url if path_or_url.startswith("http") else urljoin(_API_BASE_URL + "/", path_or_url.lstrip("/"))
    resp = requests.get(
        url,
        params=params,
        timeout=timeout_s,
        headers={"Accept": "application/vnd.api+json, application/json"},
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ValueError("filings_xbrl_org response was not a JSON object.")
    return payload


def _included_entity_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in payload.get("included") or []:
        if not isinstance(row, dict) or row.get("type") != "entity":
            continue
        key = str(row.get("id") or "").strip()
        if key:
            out[key] = row
    return out


def _entity_payload_for_filing(filing: dict[str, Any], included: dict[str, dict[str, Any]], fallback_lei: str | None) -> dict[str, Any]:
    rel = ((filing.get("relationships") or {}).get("entity") or {}).get("data") or {}
    entity = included.get(str(rel.get("id") or "").strip()) if isinstance(rel, dict) else None
    if isinstance(entity, dict):
        attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        return {
            "name": str(attrs.get("name") or "").strip() or None,
            "identifier": _clean_lei(attrs.get("identifier")) or fallback_lei,
        }
    return {"name": None, "identifier": fallback_lei}


def _item_from_filing(
    filing: dict[str, Any],
    *,
    entity: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    attrs = filing.get("attributes") if isinstance(filing.get("attributes"), dict) else {}
    lei = _clean_lei(entity.get("identifier") or attrs.get("entity_identifier"))
    if not lei:
        return None
    company_name = str(entity.get("name") or config.get("issuer_name_by_lei", {}).get(lei) or lei).strip()
    fxo_id = str(attrs.get("fxo_id") or filing.get("id") or "").strip()
    country = str(attrs.get("country") or "").strip().upper() or None
    period_end = str(attrs.get("period_end") or "").strip() or None
    processed_at = _parse_dt(attrs.get("processed")) or _parse_dt(attrs.get("date_added"))
    title = " ".join(part for part in [company_name, "ESEF/UKSEF annual filing", period_end] if part)
    report_url = _abs_url(attrs.get("report_url"))
    viewer_url = _abs_url(attrs.get("viewer_url"))
    package_url = _abs_url(attrs.get("package_url"))
    json_url = _abs_url(attrs.get("json_url"))
    links = [value for value in [viewer_url, report_url, package_url, json_url] if value]
    if not keyword_allowed(config, title, company_name, lei, period_end, country, fxo_id):
        return None
    extra = {
        "connector": "filings_xbrl_org",
        "section": "filing",
        "regulator": "ESEF",
        "filing_system": "ESEF_UKSEF",
        "filing_type": "ESEF",
        "company_doc_type": "financial_report",
        "company_doc_label": "ESEF/UKSEF annual filing",
        "lei": lei,
        "country": country,
        "period_end": period_end,
        "processed": attrs.get("processed"),
        "date_added": attrs.get("date_added"),
        "sha256": attrs.get("sha256"),
        "fxo_id": fxo_id or None,
        "report_url": report_url,
        "viewer_url": viewer_url,
        "package_url": package_url,
        "json_url": json_url,
        "source_identifiers": [{"id_type": "LEI", "id_value": lei}],
        "payload": attrs,
    }
    company_slug = (config.get("company_slug_by_lei") or {}).get(lei)
    company_id = (config.get("company_id_by_lei") or {}).get(lei)
    ticker = (config.get("ticker_by_lei") or {}).get(lei)
    if company_slug:
        extra["company_slug"] = company_slug
    if company_id is not None:
        extra["resolved_company_id"] = company_id
    if ticker:
        extra["ticker"] = ticker
    text = "\n".join(
        part
        for part in [
            title,
            f"Company: {company_name}",
            f"LEI: {lei}",
            f"Country: {country}" if country else None,
            f"Period end: {period_end}" if period_end else None,
            f"Viewer: {viewer_url}" if viewer_url else None,
            f"Report: {report_url}" if report_url else None,
            f"Package: {package_url}" if package_url else None,
            f"JSON: {json_url}" if json_url else None,
        ]
        if part
    )
    return {
        "kind": "securities_filing",
        "external_id": stable_external_id("filings-xbrl-org", fxo_id or lei, period_end, country),
        "canonical_url": report_url or viewer_url or package_url,
        "url": viewer_url or report_url or package_url,
        "title": title,
        "author": company_name,
        "published_at": processed_at,
        "raw_text": text,
        "normalized_text": text,
        "links": links,
        "extra": extra,
    }


def _selected_leis(config: dict[str, Any], cursor: dict[str, Any]) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    leis = []
    seen: set[str] = set()
    for raw in config.get("leis") or config.get("entity_identifiers") or []:
        lei = _clean_lei(raw)
        if lei and lei not in seen:
            seen.add(lei)
            leis.append(lei)
    per_run = int(config.get("leis_per_run") or config.get("entities_per_run") or len(leis) or 0)
    if per_run <= 0 or per_run >= len(leis):
        return leis, {"lei_index": 0}, {"selected_leis": leis, "lei_count": len(leis), "rotated": False}
    start = int(cursor.get("lei_index") or 0) % len(leis)
    selected = [leis[(start + idx) % len(leis)] for idx in range(per_run)]
    next_index = (start + per_run) % len(leis)
    return selected, {"lei_index": next_index}, {"selected_leis": selected, "lei_count": len(leis), "rotated": True}


def fetch_filings_xbrl_org(config: dict[str, Any], source_cursor: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(config or {})
    cursor = dict(source_cursor or cfg.get("_source_cursor") or {})
    timeout_s = float(cfg.get("timeout_s") or 30.0)
    page_size = max(1, min(int(cfg.get("page_size") or 20), 200))
    max_pages = max(1, int(cfg.get("max_pages") or 1))
    max_items = max(1, int(cfg.get("max_items_per_run") or 200))
    selected_leis, next_cursor, stats = _selected_leis(cfg, cursor)
    selected_lei_set = set(selected_leis)
    items: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    def append_item(item: dict[str, Any] | None) -> None:
        if item is None:
            return
        key = str(item.get("external_id") or item.get("canonical_url") or item.get("url") or "").strip()
        if key and key in seen_keys:
            return
        if key:
            seen_keys.add(key)
        items.append(item)

    for lei in selected_leis:
        page_url: str | None = f"entities/{lei}/filings"
        for _page in range(max_pages):
            if not page_url or len(items) >= max_items:
                break
            params = {"sort": "-processed", "page[size]": page_size} if not page_url.startswith("http") else {}
            try:
                payload = _request_json(
                    page_url,
                    params=params,
                    timeout_s=timeout_s,
                )
            except requests.HTTPError as exc:
                status_code = getattr(exc.response, "status_code", None)
                if status_code == 404:
                    skipped = stats.setdefault("entity_404_leis", [])
                    if isinstance(skipped, list) and len(skipped) < 25:
                        skipped.append(lei)
                    break
                raise
            for filing in payload.get("data") or []:
                if not isinstance(filing, dict):
                    continue
                item = _item_from_filing(filing, entity={"identifier": lei}, config=cfg)
                append_item(item)
                if len(items) >= max_items:
                    break
            page_url = ((payload.get("links") or {}).get("next") or None) if isinstance(payload.get("links"), dict) else None

    include_country_fallback = bool(cfg.get("include_country_fallback", not bool(selected_leis)))
    country_fallback_pages = max(1, int(cfg.get("country_fallback_pages") or cfg.get("max_country_pages") or 1))
    restrict_country_fallback = bool(cfg.get("restrict_country_fallback_to_selected_leis", bool(selected_leis)))
    countries = [
        str(value or "").strip().upper()
        for value in cfg.get("countries") or []
        if include_country_fallback and str(value or "").strip()
    ]
    country_pages_checked = 0
    for country in countries:
        if len(items) >= max_items:
            break
        page_url = "filings"
        for page_idx in range(country_fallback_pages):
            if not page_url or len(items) >= max_items:
                break
            params = (
                {"filter[country]": country, "include": "entity", "sort": "-processed", "page[size]": page_size}
                if page_idx == 0 and not page_url.startswith("http")
                else {}
            )
            payload = _request_json(
                page_url,
                params=params,
                timeout_s=timeout_s,
            )
            country_pages_checked += 1
            included = _included_entity_by_id(payload)
            for filing in payload.get("data") or []:
                if not isinstance(filing, dict):
                    continue
                entity = _entity_payload_for_filing(filing, included, None)
                entity_lei = _clean_lei(entity.get("identifier"))
                if restrict_country_fallback and selected_lei_set and entity_lei not in selected_lei_set:
                    continue
                item = _item_from_filing(filing, entity=entity, config=cfg)
                append_item(item)
                if len(items) >= max_items:
                    break
            page_url = ((payload.get("links") or {}).get("next") or None) if isinstance(payload.get("links"), dict) else None

    stats.update(
        {
            "items_returned": len(items),
            "country_count": len(countries),
            "country_fallback_enabled": include_country_fallback,
            "country_fallback_pages_checked": country_pages_checked,
            "country_fallback_restricted_to_selected_leis": restrict_country_fallback and bool(selected_lei_set),
        }
    )
    return {"items": items, "cursor": next_cursor, "stats": stats}
