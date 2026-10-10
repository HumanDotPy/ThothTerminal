from __future__ import annotations

from datetime import datetime
from typing import Any

import requests
import urllib3

from thoth_terminal.connectors.official_common import keyword_allowed, stable_external_id
from thoth_terminal.localized_names import resolve_taiwan_company_name
from thoth_terminal.net import DEFAULT_HEADERS

TWSE_OPENAPI_BASE = "https://openapi.twse.com.tw/v1"


def _request_twse_json(api_url: str, *, timeout_s: float) -> Any:
    try:
        resp = requests.get(
            api_url,
            headers={**DEFAULT_HEADERS, "Accept": "application/json,text/csv,*/*"},
            timeout=timeout_s,
        )
    except requests.exceptions.SSLError:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.get(
            api_url,
            headers={**DEFAULT_HEADERS, "Accept": "application/json,text/csv,*/*"},
            timeout=timeout_s,
            verify=False,
        )
    resp.raise_for_status()
    return resp.json()


def _roc_date_to_iso(value: object) -> tuple[str | None, datetime | None]:
    raw = str(value or "").strip()
    if not raw or len(raw) < 7 or not raw.isdigit():
        return None, None
    try:
        year = int(raw[:3]) + 1911
        month = int(raw[3:5])
        day = int(raw[5:7])
        dt = datetime(year, month, day)
        return dt.date().isoformat(), dt
    except Exception:
        return None, None


def _roc_time_fragment(value: object) -> str | None:
    raw = "".join(ch for ch in str(value or "").strip() if ch.isdigit())
    if not raw:
        return None
    raw = raw.rjust(6, "0")[-6:]
    return f"{raw[:2]}:{raw[2:4]}:{raw[4:6]}"


def _cfg_map_value(cfg: dict[str, Any], key: str, code: str) -> Any:
    mapping = cfg.get(key)
    if not isinstance(mapping, dict):
        return None
    return mapping.get(code) or mapping.get(str(code).strip().upper())


def _row_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row:
            return row.get(key)
    stripped = {str(key).strip(): value for key, value in row.items()}
    for key in keys:
        normalized = str(key).strip()
        if normalized in stripped:
            return stripped.get(normalized)
    compact = {str(key).replace(" ", ""): value for key, value in row.items()}
    for key in keys:
        normalized = str(key).replace(" ", "")
        if normalized in compact:
            return compact.get(normalized)
    return None


def fetch_twse_openapi(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    api_url = str(cfg.get("api_url") or cfg.get("data_url") or "").strip()
    endpoint_path = str(cfg.get("endpoint_path") or "").strip()
    if not api_url:
        if not endpoint_path:
            endpoint_path = "/opendata/t187ap04_L"
        api_url = f"{TWSE_OPENAPI_BASE}{endpoint_path}"
    payload = _request_twse_json(api_url, timeout_s=float(cfg.get("timeout_s") or 30.0))
    rows = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []

    title_field = str(cfg.get("title_field") or "主旨 ").strip() or "主旨 "
    body_field = str(cfg.get("body_field") or "說明").strip() or "說明"
    url_field = str(cfg.get("url_field") or "Url").strip()
    company_field = str(cfg.get("company_field") or "公司名稱").strip() or "公司名稱"
    code_field = str(cfg.get("code_field") or "公司代號").strip() or "公司代號"
    date_field = str(cfg.get("date_field") or "發言日期").strip() or "發言日期"
    time_field = str(cfg.get("time_field") or "發言時間").strip() or "發言時間"
    max_items = max(1, min(int(cfg.get("max_items_per_run") or 50), 500))

    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(_row_value(row, title_field, "主旨 ", "主旨", "Title") or "").strip()
        body = str(_row_value(row, body_field, "說明", "Details") or "").strip()
        company = str(_row_value(row, company_field, "公司名稱") or "").strip()
        code = str(_row_value(row, code_field, "公司代號", "Code") or "").strip()
        company_identity = resolve_taiwan_company_name(company_code=code, local_name=company, config=cfg)
        company_display = str(company_identity.get("company_name") or company or code or "").strip()
        company_en = str(company_identity.get("company_name_en") or "").strip()
        mapped_company_name = str(_cfg_map_value(cfg, "company_name_by_stock_code", code) or "").strip()
        mapped_company_id = _cfg_map_value(cfg, "company_id_by_stock_code", code)
        mapped_company_slug = str(_cfg_map_value(cfg, "company_slug_by_stock_code", code) or "").strip()
        mapped_ticker = str(_cfg_map_value(cfg, "ticker_by_stock_code", code) or "").strip()
        if mapped_company_name and (not company_display or company_display == code or company_display == company):
            company_display = mapped_company_name
        company_aliases = [
            str(alias).strip()
            for alias in (company_identity.get("company_aliases") or [])
            if str(alias).strip()
        ]
        if not title:
            title = str(row.get("Title") or "").strip()
        if not keyword_allowed(cfg, title, body, company, company_en, company_display, *company_aliases, code):
            continue
        iso_date, published_at = _roc_date_to_iso(_row_value(row, date_field, "發言日期", "Date"))
        if published_at and time_field:
            time_frag = _roc_time_fragment(_row_value(row, time_field, "發言時間"))
            if time_frag:
                try:
                    published_at = datetime.strptime(f"{published_at.date().isoformat()} {time_frag}", "%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
        url = str(_row_value(row, url_field, "Url") or "").strip() if url_field else ""
        normalized = "\n".join(
            part
            for part in [
                f"Company: {company}" if company else "",
                f"Display company: {company_display}" if company_display and company_display != company else "",
                f"Code: {code}" if code else "",
                f"Title: {title}" if title else "",
                body,
            ]
            if part
        ).strip()
        external_id = stable_external_id("twse_openapi", api_url, iso_date or "", code, company, title)
        items.append(
            {
                "kind": "exchange_disclosure",
                "external_id": external_id,
                "url": url or api_url,
                "canonical_url": url or api_url,
                "title": title or (company_display or company or "TWSE record"),
                "author": company_display or company or "TWSE",
                "published_at": published_at,
                "raw_text": normalized,
                "normalized_text": normalized,
                "extra": {
                    "connector": "twse_openapi",
                    "exchange": "TWSE",
                    "api_url": api_url,
                    "dataset_path": endpoint_path or None,
                    "stock_code": code or None,
                    "ticker": mapped_ticker or code or None,
                    "company_id": mapped_company_id if str(mapped_company_id or "").isdigit() else None,
                    "resolved_company_id": mapped_company_id if str(mapped_company_id or "").isdigit() else None,
                    "company_slug": mapped_company_slug or None,
                    "company_name": company_display or company or None,
                    "company_name_en": company_en or None,
                    "company_name_original": company or None,
                    "company_aliases": company_aliases,
                    "company_name_source": company_identity.get("company_name_source"),
                    "source_identifiers": [
                        {
                            "id_type": "TW_STOCK",
                            "id_value": code,
                            "exchange": "TWSE",
                            "source_key": "stock_code",
                            "source": "twse_openapi",
                            "market": "TWSE",
                            "is_primary": True,
                        }
                    ]
                    if code
                    else [],
                    "source_row": dict(row),
                },
            }
        )
        if len(items) >= max_items:
            break
    return {"items": items, "cursor": None, "stats": {"count": len(items), "source_kind": "twse_openapi"}}
