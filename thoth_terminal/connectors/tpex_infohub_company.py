from __future__ import annotations

from datetime import datetime
from typing import Any

import requests
import urllib3

from thoth_terminal.connectors.official_common import stable_external_id
from thoth_terminal.localized_names import resolve_taiwan_company_name
from thoth_terminal.net import DEFAULT_HEADERS

TPEX_INFOHUB_BASE = "https://info.tpex.org.tw"
TPEX_STKINFO_URL = f"{TPEX_INFOHUB_BASE}/api/stkInfo"


def _request_company_payload(query: str, *, timeout_s: float) -> dict[str, Any]:
    headers = {
        **DEFAULT_HEADERS,
        "Accept": "application/json,text/plain,*/*",
        "Referer": f"{TPEX_INFOHUB_BASE}/IIH/zh/company.html?stkNo={query}",
    }
    try:
        resp = requests.post(TPEX_STKINFO_URL, params={"query": query}, headers=headers, timeout=timeout_s)
    except requests.exceptions.SSLError:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.post(
            TPEX_STKINFO_URL,
            params={"query": query},
            headers=headers,
            timeout=timeout_s,
            verify=False,
        )
    resp.raise_for_status()
    payload = resp.json()
    return dict(payload) if isinstance(payload, dict) else {}


def _cfg_map_value(cfg: dict[str, Any], key: str, code: str) -> Any:
    mapping = cfg.get(key)
    if not isinstance(mapping, dict):
        return None
    return mapping.get(code) or mapping.get(str(code).strip().upper())


def _roc_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            pass
    parts = raw.replace("-", "/").split()
    date_part = parts[0] if parts else ""
    time_part = parts[1] if len(parts) > 1 else "00:00"
    chunks = date_part.split("/")
    if len(chunks) != 3:
        return None
    try:
        year = int(chunks[0])
        if year < 1000:
            year += 1911
        month = int(chunks[1])
        day = int(chunks[2])
        if len(time_part.split(":")) == 2:
            time_part = f"{time_part}:00"
        hour, minute, second = [int(part) for part in time_part.split(":")[:3]]
        return datetime(year, month, day, hour, minute, second)
    except Exception:
        return None


def _taiwan_result_period_for_event(dt: datetime | None) -> tuple[int | None, int | None]:
    if dt is None:
        return None, None
    if dt.month <= 3:
        return dt.year - 1, 4
    if dt.month <= 6:
        return dt.year, 1
    if dt.month <= 9:
        return dt.year, 2
    return dt.year, 3


def _stock_code_list(config: dict[str, Any]) -> list[str]:
    raw = config.get("company_codes") or config.get("stock_codes") or []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    out: list[str] = []
    seen: set[str] = set()
    values = raw if isinstance(raw, list) else []
    for value in values:
        code = "".join(ch for ch in str(value or "").strip() if ch.isalnum())
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _section_rows(payload: dict[str, Any], section: str) -> list[dict[str, Any]]:
    rows = payload.get(section)
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def fetch_tpex_infohub_company(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    company_codes = _stock_code_list(cfg)
    max_items = max(1, min(int(cfg.get("max_items_per_run") or 80), 500))
    max_codes = max(1, min(int(cfg.get("max_company_codes_per_run") or len(company_codes) or 1), 250))
    sections_raw = cfg.get("sections") or ["conferences"]
    sections = [str(section).strip() for section in sections_raw if str(section).strip()] if isinstance(sections_raw, list) else ["conferences"]
    timeout_s = float(cfg.get("timeout_s") or 30.0)

    items: list[dict[str, Any]] = []
    queried = 0
    empty = 0
    for code in company_codes[:max_codes]:
        queried += 1
        payload = _request_company_payload(code, timeout_s=timeout_s)
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        company_local = str(info.get("name") or "").strip()
        short_name = str(info.get("shortName") or "").strip()
        website = str(info.get("websiteUrl") or "").strip()
        identity = resolve_taiwan_company_name(company_code=code, local_name=company_local or short_name, config=cfg)
        company_display = str(
            _cfg_map_value(cfg, "company_name_by_stock_code", code)
            or identity.get("company_name")
            or company_local
            or short_name
            or code
        ).strip()
        company_en = str(identity.get("company_name_en") or "").strip()
        company_aliases = [str(alias).strip() for alias in identity.get("company_aliases") or [] if str(alias).strip()]
        mapped_company_id = _cfg_map_value(cfg, "company_id_by_stock_code", code)
        mapped_company_slug = str(_cfg_map_value(cfg, "company_slug_by_stock_code", code) or "").strip()
        mapped_ticker = str(_cfg_map_value(cfg, "ticker_by_stock_code", code) or "").strip()

        before_code_items = len(items)
        for section in sections:
            for row in _section_rows(payload, section):
                title = str(row.get("title") or row.get("主旨") or "").strip()
                link = str(row.get("link") or row.get("url") or "").strip()
                stock = str(row.get("stock") or f"{code} {short_name or company_local}".strip()).strip()
                raw_dt = str(row.get("datetime") or row.get("date") or "").strip()
                published_at = _roc_datetime(raw_dt)
                fiscal_year, fiscal_quarter = _taiwan_result_period_for_event(published_at) if section == "conferences" else (None, None)
                material_title = title or f"{stock} {section}".strip()
                normalized = "\n".join(
                    part
                    for part in [
                        f"Company: {company_local}" if company_local else "",
                        f"Display company: {company_display}" if company_display else "",
                        f"Code: {code}",
                        f"Section: {section}",
                        f"Datetime: {raw_dt}" if raw_dt else "",
                        f"Stock: {stock}" if stock else "",
                        f"Title: {material_title}",
                        f"URL: {link}" if link else "",
                    ]
                    if part
                )
                external_id = stable_external_id("tpex_infohub_company", code, section, raw_dt, link, material_title)
                extra: dict[str, Any] = {
                    "connector": "tpex_infohub_company",
                    "exchange": "TPEX",
                    "jurisdiction": "TW",
                    "api_url": TPEX_STKINFO_URL,
                    "source_section": section,
                    "stock_code": code,
                    "ticker": mapped_ticker or code,
                    "company_id": mapped_company_id if str(mapped_company_id or "").isdigit() else None,
                    "resolved_company_id": mapped_company_id if str(mapped_company_id or "").isdigit() else None,
                    "company_slug": mapped_company_slug or None,
                    "company_name": company_display or company_local or None,
                    "company_name_en": company_en or None,
                    "company_name_original": company_local or None,
                    "company_aliases": company_aliases,
                    "company_name_source": identity.get("company_name_source"),
                    "website_url": website or None,
                    "source_row": dict(row),
                    "source_identifiers": [
                        {
                            "id_type": "TW_STOCK",
                            "id_value": code,
                            "exchange": "TPEX",
                            "source_key": "stock_code",
                            "source": "tpex_infohub_company",
                            "market": "TPEX",
                            "is_primary": True,
                        }
                    ],
                }
                if section == "conferences":
                    extra.update(
                        {
                            "is_call_material": True,
                            "artifact_type": "presentation",
                            "artifact_subtype": "earnings",
                            "company_doc_type": "investor_presentation",
                            "company_doc_label": material_title,
                            "fiscal_year": fiscal_year,
                            "fiscal_quarter": fiscal_quarter,
                        }
                    )
                items.append(
                    {
                        "kind": "exchange_disclosure",
                        "external_id": external_id,
                        "url": link or f"{TPEX_INFOHUB_BASE}/IIH/zh/company.html?stkNo={code}",
                        "canonical_url": link or f"{TPEX_INFOHUB_BASE}/IIH/zh/company.html?stkNo={code}",
                        "title": material_title,
                        "author": company_display or company_local or "Taipei Exchange",
                        "published_at": published_at,
                        "raw_text": normalized,
                        "normalized_text": normalized,
                        "extra": extra,
                    }
                )
                if len(items) >= max_items:
                    break
            if len(items) >= max_items:
                break
        if len(items) == before_code_items:
            empty += 1
        if len(items) >= max_items:
            break

    return {
        "items": items,
        "cursor": None,
        "stats": {
            "count": len(items),
            "source_kind": "tpex_infohub_company",
            "queried_company_codes": queried,
            "empty_company_codes": empty,
            "sections": sections,
        },
    }
