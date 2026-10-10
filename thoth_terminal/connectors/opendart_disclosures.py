from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import requests

from thoth_terminal.config import settings
from thoth_terminal.connectors.official_common import keyword_allowed, recent_date_window, stable_external_id
from thoth_terminal.net import DEFAULT_HEADERS


OPENDART_BASE = "https://engopendart.fss.or.kr/engapi"
DART_VIEWER_BASE = "https://englishdart.fss.or.kr/dsbh001/main.do"


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    return [str(v).strip() for v in raw if str(v).strip()]


def _rotate(values: list[str], *, start: int, count: int) -> tuple[list[str], int]:
    if not values or count <= 0:
        return [], start
    out: list[str] = []
    idx = max(0, int(start or 0))
    for _ in range(min(count, len(values))):
        out.append(values[idx % len(values)])
        idx += 1
    return out, idx % len(values)


def _date_yyyymmdd(value: datetime) -> str:
    return value.strftime("%Y%m%d")


def _parse_filing_dt(value: object) -> datetime | None:
    raw = "".join(ch for ch in str(value or "").strip() if ch.isdigit())
    if len(raw) < 8:
        return None
    try:
        return datetime.strptime(raw[:8], "%Y%m%d")
    except Exception:
        return None


def _filing_type_from_report_name(report_name: str) -> str | None:
    text = re.sub(r"\s+", " ", str(report_name or "").strip().lower())
    if not text:
        return None
    if "semiannual report" in text or "semi-annual report" in text or "half-year" in text or "half year" in text:
        return "DART_11012"
    if "annual report" in text and "semiannual" not in text and "quarterly" not in text:
        return "DART_11011"
    if "quarterly report" in text:
        if re.search(r"\.(?:09|9)\)", text):
            return "DART_11014"
        return "DART_11013"
    return None


def _opendart_get(path: str, params: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
    resp = requests.get(
        f"{OPENDART_BASE}/{path.lstrip('/')}",
        params=params,
        headers={**DEFAULT_HEADERS, "Accept": "application/json"},
        timeout=timeout_s,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        return {}
    status = str(payload.get("status") or "").strip()
    if status and status not in {"000", "013"}:
        message = str(payload.get("message") or "OpenDART request failed").strip()
        raise ValueError(f"OpenDART {status}: {message}")
    return payload


def fetch_opendart_disclosures(config: dict[str, Any], *, source_cursor: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(config or {})
    cursor = dict(source_cursor or {})
    api_key = str(cfg.get("api_key") or getattr(settings, "opendart_api_key", None) or "").strip()
    if not api_key:
        raise ValueError("OpenDART requires THOTH_OPENDART_API_KEY or source config api_key.")

    timeout_s = float(cfg.get("timeout_s") or 30.0)
    start_dt, end_dt = recent_date_window(cfg, default_days=7)
    corp_codes = _string_list(cfg.get("corp_codes") or cfg.get("corp_code"))
    company_id_by_corp_code = cfg.get("company_id_by_corp_code") if isinstance(cfg.get("company_id_by_corp_code"), dict) else {}
    company_slug_by_corp_code = cfg.get("company_slug_by_corp_code") if isinstance(cfg.get("company_slug_by_corp_code"), dict) else {}
    ticker_by_corp_code = cfg.get("ticker_by_corp_code") if isinstance(cfg.get("ticker_by_corp_code"), dict) else {}
    company_name_by_corp_code = cfg.get("company_name_by_corp_code") if isinstance(cfg.get("company_name_by_corp_code"), dict) else {}
    corp_classes = _string_list(cfg.get("corp_cls") or cfg.get("corp_classes"))
    disclosure_types = _string_list(cfg.get("pblntf_ty") or cfg.get("disclosure_types"))
    detail_types = _string_list(cfg.get("pblntf_detail_ty") or cfg.get("detail_types"))
    sort = str(cfg.get("sort") or "date").strip() or "date"
    sort_mth = str(cfg.get("sort_mth") or "desc").strip() or "desc"
    page_count = max(1, min(int(cfg.get("page_count") or 100), 100))
    max_pages = max(1, min(int(cfg.get("max_pages") or 3), 20))
    limit = max(1, min(int(cfg.get("max_items_per_run") or 100), 1000))

    base_params: dict[str, Any] = {
        "crtfc_key": api_key,
        "bgn_de": _date_yyyymmdd(start_dt),
        "end_de": _date_yyyymmdd(end_dt),
        "last_reprt_at": str(cfg.get("last_reprt_at") or "Y"),
        "sort": sort,
        "sort_mth": sort_mth,
        "page_count": str(page_count),
    }
    if disclosure_types:
        base_params["pblntf_ty"] = ",".join(disclosure_types)
    if detail_types:
        base_params["pblntf_detail_ty"] = ",".join(detail_types)
    if corp_classes:
        base_params["corp_cls"] = ",".join(corp_classes)

    next_cursor = dict(cursor)
    if bool(cfg.get("rotate_corp_codes")) and corp_codes:
        corp_code_queries, next_idx = _rotate(
            corp_codes,
            start=int(cursor.get("corp_code_index") or 0),
            count=max(1, int(cfg.get("max_corp_codes_per_run") or 8)),
        )
        next_cursor["corp_code_index"] = next_idx
        next_cursor["corp_code_count"] = len(corp_codes)
    else:
        corp_code_queries = corp_codes or [""]
    per_corp_limit = limit
    if corp_codes:
        per_corp_limit = max(1, (limit + len(corp_code_queries) - 1) // len(corp_code_queries))
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    fetched_rows = 0

    for corp_code in corp_code_queries:
        if len(items) >= limit:
            break
        corp_items = 0
        for page_no in range(1, max_pages + 1):
            if corp_items >= per_corp_limit:
                break
            params = dict(base_params)
            params["page_no"] = str(page_no)
            if corp_code:
                params["corp_code"] = corp_code
            payload = _opendart_get("list.json", params, timeout_s=timeout_s)
            rows = payload.get("list") if isinstance(payload.get("list"), list) else []
            if not rows:
                break
            fetched_rows += len(rows)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                rcept_no = str(row.get("rcept_no") or "").strip()
                if rcept_no and rcept_no in seen:
                    continue
                corp_name = str(row.get("corp_name") or row.get("corp_eng_name") or "").strip()
                corp_eng_name = str(row.get("corp_eng_name") or "").strip()
                corp_code_v = str(row.get("corp_code") or corp_code or "").strip()
                stock_code = str(row.get("stock_code") or "").strip()
                report_nm = str(row.get("report_nm") or "").strip()
                filer = str(row.get("flr_nm") or "").strip()
                rm = str(row.get("rm") or "").strip()
                if rcept_no:
                    seen.add(rcept_no)
                published_at = _parse_filing_dt(row.get("rcept_dt"))
                viewer_url = f"{DART_VIEWER_BASE}?rcpNo={rcept_no}" if rcept_no else f"{OPENDART_BASE}/list.json"
                mapped_company_name = str(company_name_by_corp_code.get(corp_code_v) or corp_name or "").strip()
                title_company = mapped_company_name or corp_name
                if not keyword_allowed(cfg, corp_name, mapped_company_name, title_company, stock_code, report_nm, filer, rm):
                    continue
                title = f"{title_company} - {report_nm}" if title_company and report_nm else (report_nm or title_company or "OpenDART filing")
                filing_type = _filing_type_from_report_name(report_nm)
                extra = {
                    "connector": "opendart_disclosures",
                    "regulator": "OpenDART",
                    "country": "KR",
                    "filing_type": filing_type,
                    "rcept_no": rcept_no or None,
                    "corp_code": corp_code_v or None,
                    "corp_name": title_company or None,
                    "corp_eng_name": corp_eng_name or None,
                    "stock_code": stock_code or str(ticker_by_corp_code.get(corp_code_v) or "").strip() or None,
                    "corp_cls": str(row.get("corp_cls") or "").strip() or None,
                    "report_nm": report_nm or None,
                    "flr_nm": filer or None,
                    "rcept_dt": str(row.get("rcept_dt") or "").strip() or None,
                    "rm": rm or None,
                    "viewer_url": viewer_url,
                    "api_base": OPENDART_BASE,
                    "source_identifiers": [
                        {
                            "id_type": "DART_CORP",
                            "id_value": corp_code_v,
                            "source_key": "corp_code",
                            "source": "opendart_disclosures",
                            "market": "KR",
                            "is_primary": True,
                        },
                        {
                            "id_type": "KR_STOCK",
                            "id_value": stock_code or str(ticker_by_corp_code.get(corp_code_v) or "").strip(),
                            "exchange": "KRX",
                            "source_key": "stock_code",
                            "source": "opendart_disclosures",
                            "market": "KRX",
                        },
                    ],
                }
                if company_slug_by_corp_code.get(corp_code_v):
                    extra["company_slug"] = company_slug_by_corp_code[corp_code_v]
                if company_id_by_corp_code.get(corp_code_v) is not None:
                    extra["company_id"] = company_id_by_corp_code[corp_code_v]
                    extra["resolved_company_id"] = company_id_by_corp_code[corp_code_v]
                    extra["identity_resolution"] = {"source": "opendart_source_config", "confidence": 0.98}
                normalized = "\n".join(
                    part
                    for part in [
                        title,
                        f"Company: {title_company}" if title_company else "",
                        f"Stock code: {stock_code}" if stock_code else "",
                        f"Corporation code: {corp_code_v}" if corp_code_v else "",
                        f"Filing number: {rcept_no}" if rcept_no else "",
                        f"Filed date: {row.get('rcept_dt')}" if row.get("rcept_dt") else "",
                        f"Disclosure report: {report_nm}" if report_nm else "",
                        f"Filer: {filer}" if filer else "",
                        f"Remarks: {rm}" if rm else "",
                    ]
                    if part
                )
                items.append(
                    {
                        "kind": "securities_filing",
                        "external_id": rcept_no or stable_external_id("opendart_disclosures", corp_name, report_nm, row.get("rcept_dt")),
                        "url": viewer_url,
                        "canonical_url": viewer_url,
                        "title": title,
                        "author": title_company or filer or "OpenDART",
                        "published_at": published_at,
                        "raw_text": normalized,
                        "normalized_text": normalized,
                        "extra": extra,
                    }
                )
                corp_items += 1
                if len(items) >= limit or corp_items >= per_corp_limit:
                    break
            if len(items) >= limit or corp_items >= per_corp_limit or len(rows) < page_count:
                break

    stats = {
        "count": len(items),
        "fetched_rows": fetched_rows,
        "source_kind": "opendart_disclosures",
        "start_date": _date_yyyymmdd(start_dt),
        "end_date": _date_yyyymmdd(end_dt),
        "corp_codes_queried": [code for code in corp_code_queries if code],
    }
    return {
        "items": items,
        "cursor": next_cursor if bool(cfg.get("rotate_corp_codes")) and corp_codes else None,
        "stats": stats,
    }
