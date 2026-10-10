from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

import requests

from thoth_terminal.config import settings
from thoth_terminal.connectors.official_common import keyword_allowed, safe_json_dumps, stable_external_id
from thoth_terminal.net import DEFAULT_HEADERS


EDINETDB_BASE = "https://edinetdb.jp/v1"


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


def _request_json(path: str, *, api_key: str | None, params: dict[str, Any] | None = None, timeout_s: float = 30.0) -> Any:
    headers = {**DEFAULT_HEADERS, "Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    resp = requests.get(f"{EDINETDB_BASE}/{path.lstrip('/')}", params=params or {}, headers=headers, timeout=timeout_s)
    resp.raise_for_status()
    return resp.json()


def _first_list(payload: Any, *keys: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in keys:
                val = data.get(key)
                if isinstance(val, list):
                    return val
        for key in keys:
            val = payload.get(key)
            if isinstance(val, list):
                return val
    return []


def _unwrap_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload and isinstance(payload.get("data"), (dict, list)):
        return payload["data"]
    return payload


def _edinet_code(company: dict[str, Any]) -> str:
    return str(company.get("edinetCode") or company.get("edinet_code") or company.get("code") or "").strip()


def _company_query_parts(company: dict[str, Any]) -> list[str]:
    stock_code = _normalize_jp_stock_code(_first_str(company, "secCode", "sec_code", "stockCode", "stock_code"))
    parts = [
        _edinet_code(company),
        stock_code,
        _first_str(company, "secCode", "sec_code", "stockCode", "stock_code"),
    ]
    for key in [
        "nameEn",
        "name_en",
        "companyNameEn",
        "company_name_en",
        "filerName",
        "filer_name",
        "name",
        "nameJa",
        "name_ja",
        "company_name",
        "companyName",
    ]:
        parts.append(_first_str(company, key))
    return [str(part or "").strip() for part in parts if str(part or "").strip()]


def _query_matches_company(query: str, company: dict[str, Any]) -> bool:
    raw_query = str(query or "").strip()
    if not raw_query:
        return False
    normalized_query = _normalize_jp_stock_code(raw_query)
    query_fold = raw_query.casefold()
    for part in _company_query_parts(company):
        if raw_query == part or query_fold == part.casefold():
            return True
        if normalized_query and normalized_query == _normalize_jp_stock_code(part):
            return True
        if len(query_fold) >= 3 and query_fold in part.casefold():
            return True
    return False


def _resolve_queries_from_company_list(
    cfg: dict[str, Any],
    queries: list[str],
    *,
    api_key: str | None,
    timeout_s: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not queries:
        return [], []
    payload = _request_json(
        "companies",
        api_key=api_key,
        params={"page": int(cfg.get("company_list_page") or 1), "per_page": int(cfg.get("company_list_per_page") or 5000)},
        timeout_s=timeout_s,
    )
    company_rows = _first_list(payload, "companies", "results", "items")
    matched: list[dict[str, Any]] = []
    unresolved: list[str] = []
    limit_per_query = max(1, int(cfg.get("company_list_match_limit") or cfg.get("search_limit") or 5))
    for query in queries:
        query_matches = [row for row in company_rows if isinstance(row, dict) and _query_matches_company(query, row)]
        if query_matches:
            matched.extend(query_matches[:limit_per_query])
        else:
            unresolved.append(query)
    return matched, unresolved


def _resolve_companies(cfg: dict[str, Any], *, api_key: str | None, timeout_s: float) -> list[dict[str, Any]]:
    explicit = _list(cfg.get("edinet_codes") or cfg.get("codes"))
    companies: list[dict[str, Any]] = [{"edinetCode": code} for code in explicit]
    queries = _list(cfg.get("queries") or cfg.get("search_queries"))
    unresolved_queries = queries
    if queries and bool(cfg.get("resolve_queries_via_company_list", True)):
        matched, unresolved_queries = _resolve_queries_from_company_list(
            cfg,
            queries,
            api_key=api_key,
            timeout_s=timeout_s,
        )
        companies.extend(matched)
    fallback_query_search = bool(cfg.get("fallback_query_search", not bool(cfg.get("source_pack_targeted_backfill"))))
    for query in unresolved_queries if fallback_query_search else []:
        payload = _request_json(
            "search",
            api_key=api_key,
            params={"q": query, "limit": int(cfg.get("search_limit") or 5)},
            timeout_s=timeout_s,
        )
        for row in _first_list(payload, "companies", "results", "data"):
            if not isinstance(row, dict):
                continue
            companies.append(row)
    deduped: dict[str, dict[str, Any]] = {}
    for row in companies:
        code = _edinet_code(row)
        if code:
            deduped.setdefault(code, dict(row, edinetCode=code))
    return list(deduped.values())


def _rotate(values: list[str], *, start: int, count: int) -> tuple[list[str], int]:
    if not values or count <= 0:
        return [], start
    out: list[str] = []
    idx = max(0, int(start or 0))
    for _ in range(min(count, len(values))):
        out.append(values[idx % len(values)])
        idx += 1
    return out, idx % len(values)


def _optional_quota(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return max(0, int(value))
    except Exception:
        return None


def _retry_after_seconds(response: requests.Response | None) -> int | None:
    if response is None:
        return None
    raw = str(response.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(60, int(float(raw)))
    except Exception:
        return None


def _first_str(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _normalize_jp_stock_code(value: object) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    raw = raw.removesuffix(".T").removesuffix(".JP")
    raw = "".join(ch for ch in raw if ch.isalnum())
    if raw.endswith("0"):
        candidate = raw[:-1]
        if candidate and re.fullmatch(r"[0-9]{4}[A-Z]?|[0-9]{3}[A-Z]", candidate):
            raw = candidate
    return raw if re.fullmatch(r"[0-9]{4}[A-Z]?|[0-9]{3}[A-Z]", raw) else ""


def _company_filter_parts(company: dict[str, Any], *, code: str, company_name: str, stock_code: str) -> list[object]:
    keys = [
        "secCode",
        "sec_code",
        "stockCode",
        "stock_code",
        "nameEn",
        "name_en",
        "companyNameEn",
        "company_name_en",
        "filerName",
        "filer_name",
        "name",
        "nameJa",
        "name_ja",
        "company_name",
        "companyName",
        "industry",
        "sector",
        "business",
        "description",
    ]
    return [company_name, code, stock_code, *(_first_str(company, key) for key in keys)]


def _selected_company_config(cfg: dict[str, Any], cursor: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    priority_codes = _list(cfg.get("priority_edinet_codes") or cfg.get("priority_codes"))
    priority_queries = _list(cfg.get("priority_queries") or cfg.get("unresolved_edinet_queries"))
    tracked_codes = _list(cfg.get("tracked_edinet_codes"))
    market_codes = _list(cfg.get("market_edinet_codes"))
    tracked_queries = _list(cfg.get("tracked_queries"))
    market_queries = _list(cfg.get("market_queries"))
    if not any([priority_codes, priority_queries, tracked_codes, market_codes, tracked_queries, market_queries]):
        return cfg, cursor, {"tracked_selected": 0, "market_selected": 0}

    companies_per_run = max(1, int(cfg.get("companies_per_run") or 4))
    tracked_quota = int(cfg.get("tracked_companies_per_run") or max(1, companies_per_run // 2))
    market_quota = max(0, companies_per_run - tracked_quota)
    priority_quota = _optional_quota(cfg.get("priority_companies_per_run"))
    if priority_quota is None:
        priority_quota = tracked_quota if (priority_codes or priority_queries) else 0
    priority_code_quota = _optional_quota(cfg.get("priority_codes_per_run"))
    priority_query_quota = _optional_quota(cfg.get("priority_queries_per_run"))
    tracked_code_quota = _optional_quota(cfg.get("tracked_codes_per_run"))
    tracked_query_quota = _optional_quota(cfg.get("tracked_queries_per_run"))
    market_code_quota = _optional_quota(cfg.get("market_codes_per_run"))
    market_query_quota = _optional_quota(cfg.get("market_queries_per_run"))

    selected_priority_codes, next_priority_code_idx = _rotate(
        priority_codes,
        start=int(cursor.get("priority_code_index") or 0),
        count=priority_code_quota if priority_code_quota is not None else priority_quota,
    )
    remaining_priority = max(0, priority_quota - len(selected_priority_codes))
    selected_priority_queries, next_priority_query_idx = _rotate(
        priority_queries,
        start=int(cursor.get("priority_query_index") or 0),
        count=priority_query_quota if priority_query_quota is not None else remaining_priority,
    )
    selected_tracked_codes, next_tracked_code_idx = _rotate(
        tracked_codes,
        start=int(cursor.get("tracked_code_index") or 0),
        count=tracked_code_quota if tracked_code_quota is not None else tracked_quota,
    )
    remaining_tracked = max(0, tracked_quota - len(selected_tracked_codes))
    selected_tracked_queries, next_tracked_query_idx = _rotate(
        tracked_queries,
        start=int(cursor.get("tracked_query_index") or 0),
        count=tracked_query_quota if tracked_query_quota is not None else remaining_tracked,
    )
    selected_market_codes, next_market_code_idx = _rotate(
        market_codes,
        start=int(cursor.get("market_code_index") or 0),
        count=market_code_quota if market_code_quota is not None else market_quota,
    )
    remaining_market = max(0, market_quota - len(selected_market_codes))
    selected_market_queries, next_market_query_idx = _rotate(
        market_queries,
        start=int(cursor.get("market_query_index") or 0),
        count=market_query_quota if market_query_quota is not None else remaining_market,
    )

    selected = dict(cfg)
    selected["edinet_codes"] = selected_priority_codes + selected_tracked_codes + selected_market_codes
    selected["queries"] = selected_priority_queries + selected_tracked_queries + selected_market_queries
    next_cursor = dict(cursor)
    next_cursor.update(
        {
            "priority_code_index": next_priority_code_idx,
            "priority_query_index": next_priority_query_idx,
            "tracked_code_index": next_tracked_code_idx,
            "tracked_query_index": next_tracked_query_idx,
            "market_code_index": next_market_code_idx,
            "market_query_index": next_market_query_idx,
        }
    )
    stats = {
        "priority_selected": len(selected_priority_codes) + len(selected_priority_queries),
        "priority_code_selected": len(selected_priority_codes),
        "priority_query_selected": len(selected_priority_queries),
        "tracked_selected": len(selected_tracked_codes) + len(selected_tracked_queries),
        "market_selected": len(selected_market_codes) + len(selected_market_queries),
        "tracked_code_selected": len(selected_tracked_codes),
        "tracked_query_selected": len(selected_tracked_queries),
        "market_code_selected": len(selected_market_codes),
        "market_query_selected": len(selected_market_queries),
    }
    return selected, next_cursor, stats


def _name(company: dict[str, Any], fallback: str) -> str:
    return str(
        company.get("filerName")
        or company.get("filer_name")
        or company.get("nameEn")
        or company.get("name_en")
        or company.get("companyNameEn")
        or company.get("company_name_en")
        or company.get("company_name")
        or company.get("companyName")
        or company.get("name")
        or company.get("nameJa")
        or company.get("name_ja")
        or fallback
    ).strip()


def _doc(
    *,
    code: str,
    company_name: str,
    company_meta: dict[str, Any] | None = None,
    section: str,
    payload: Any,
    url: str,
    title_suffix: str,
    published_at: datetime | None = None,
) -> dict[str, Any]:
    meta = dict(company_meta or {})
    sec_code = str(meta.get("secCode") or meta.get("sec_code") or meta.get("stockCode") or meta.get("stock_code") or "").strip()
    stock_code = _normalize_jp_stock_code(sec_code)
    jcn = str(meta.get("JCN") or meta.get("jcn") or "").strip()
    name_en = _first_str(meta, "nameEn", "name_en", "companyNameEn", "company_name_en")
    text = safe_json_dumps(payload)
    title = f"{company_name} - {title_suffix}"
    return {
        "kind": "securities_filing",
        "external_id": stable_external_id("edinetdb_financials", code, section, text[:1000]),
        "url": url,
        "canonical_url": url,
        "title": title,
        "author": company_name,
        "published_at": published_at,
        "raw_text": f"{title}\n{text}",
        "normalized_text": f"{title}\n{text}",
        "extra": {
            "connector": "edinetdb_financials",
            "regulator": "EDINET",
            "provider": "EDINET DB",
            "country": "JP",
            "edinet_code": code,
            "sec_code": sec_code or None,
            "sec_code_raw": sec_code or None,
            "stock_code": stock_code or None,
            "jcn": jcn or None,
            "company_name": company_name,
            "company_name_en": name_en or None,
            "section": section,
            "payload": payload,
            "api_url": url,
            "source_identifiers": [
                {
                    "id_type": "EDINET",
                    "id_value": code,
                    "source_key": "edinet_code",
                    "source": "edinetdb_financials",
                    "market": "JP",
                    "is_primary": True,
                },
                {
                    "id_type": "JP_STOCK",
                    "id_value": stock_code,
                    "exchange": "JPX",
                    "source_key": "stock_code",
                    "source": "edinetdb_financials",
                    "market": "JPX",
                },
                {
                    "id_type": "JCN",
                    "id_value": jcn,
                    "source_key": "jcn",
                    "source": "edinetdb_financials",
                    "market": "JP",
                },
            ],
        },
    }


def fetch_edinetdb_financials(config: dict[str, Any], *, source_cursor: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(config or {})
    cursor = dict(source_cursor or {})
    cfg, next_cursor, selection_stats = _selected_company_config(cfg, cursor)
    api_key = str(cfg.get("api_key") or getattr(settings, "edinetdb_api_key", None) or "").strip()
    timeout_s = float(cfg.get("timeout_s") or 30.0)
    limit = max(1, min(int(cfg.get("max_items_per_run") or 100), 500))
    include_sections = set(_list(cfg.get("sections")) or ["company", "financials", "earnings", "segments", "text_blocks", "analysis"])
    items: list[dict[str, Any]] = []
    rate_limited = False
    retry_after_seconds = None
    try:
        companies = _resolve_companies(cfg, api_key=api_key or None, timeout_s=timeout_s)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            companies = []
            rate_limited = True
            retry_after_seconds = _retry_after_seconds(exc.response)
        else:
            raise

    explicit_codes = {code.casefold() for code in _list(cfg.get("edinet_codes"))}
    for company in companies:
        if len(items) >= limit or rate_limited:
            break
        code = str(company.get("edinetCode") or "").strip()
        if not code:
            continue
        company_payload: Any = company
        if "company" in include_sections:
            try:
                fetched_payload = _unwrap_data(_request_json(f"companies/{quote(code)}", api_key=api_key or None, timeout_s=timeout_s))
                if isinstance(fetched_payload, dict):
                    company_payload = {**company, **fetched_payload}
                else:
                    company_payload = fetched_payload
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 429:
                    rate_limited = True
                    retry_after_seconds = retry_after_seconds or _retry_after_seconds(exc.response)
                    break
                raise
            except Exception:
                company_payload = company
        if rate_limited:
            break
        if not isinstance(company_payload, dict):
            company_payload = company
        company_name = _name(company_payload, code)
        stock_code = _normalize_jp_stock_code(
            _first_str(company_payload, "secCode", "sec_code", "stockCode", "stock_code")
        )
        if code.casefold() not in explicit_codes and not keyword_allowed(
            cfg,
            *_company_filter_parts(company_payload, code=code, company_name=company_name, stock_code=stock_code),
        ):
            continue
        base_url = f"{EDINETDB_BASE}/companies/{quote(code)}"
        if "company" in include_sections:
            items.append(_doc(code=code, company_name=company_name, company_meta=company_payload if isinstance(company_payload, dict) else company, section="company", payload=company_payload, url=base_url, title_suffix="EDINET DB company profile"))
            if len(items) >= limit:
                break
        section_specs = [
            ("financials", "financials", {"years": int(cfg.get("years") or 6), "period": str(cfg.get("period") or "annual")}, "financial time series"),
            ("earnings", "earnings", {"limit": int(cfg.get("earnings_limit") or 8), "include_qualitative_text": bool(cfg.get("include_qualitative_text", True))}, "earnings disclosures"),
            ("segments", "segments", {}, "segment financials"),
            ("text_blocks", "text-blocks", {"full": bool(cfg.get("full_text_blocks", False))}, "annual report text blocks"),
            ("analysis", "analysis", {}, "financial health analysis"),
            ("ratios", "ratios", {}, "financial ratios"),
            ("expenses", "expenses", {}, "SGA expense breakdown"),
            ("facilities", "facilities", {}, "facility disclosures"),
        ]
        for section, path, params, title_suffix in section_specs:
            if len(items) >= limit:
                break
            if section not in include_sections:
                continue
            try:
                payload = _unwrap_data(_request_json(f"companies/{quote(code)}/{path}", api_key=api_key or None, params=params, timeout_s=timeout_s))
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    continue
                if exc.response is not None and exc.response.status_code == 429:
                    rate_limited = True
                    retry_after_seconds = retry_after_seconds or _retry_after_seconds(exc.response)
                    break
                raise
            items.append(
                _doc(
                    code=code,
                    company_name=company_name,
                    company_meta=company_payload if isinstance(company_payload, dict) else company,
                    section=section,
                    payload=payload,
                    url=f"{base_url}/{path}",
                    title_suffix=title_suffix,
                )
            )

    cursor_advanced = not rate_limited
    stats = {
        "count": min(len(items), limit),
        "company_count": len(companies),
        "rate_limited": rate_limited,
        "cursor_advanced": cursor_advanced,
        "source_kind": "edinetdb_financials",
        **selection_stats,
    }
    if retry_after_seconds is not None:
        stats["retry_after_seconds"] = retry_after_seconds
    return {
        "items": items[:limit],
        "cursor": next_cursor if cursor_advanced else cursor,
        "stats": stats,
    }
