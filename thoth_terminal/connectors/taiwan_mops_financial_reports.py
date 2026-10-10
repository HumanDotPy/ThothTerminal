from __future__ import annotations

import html
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
import urllib3

from thoth_terminal.connectors.official_common import keyword_allowed, stable_external_id
from thoth_terminal.localized_names import resolve_taiwan_company_name
from thoth_terminal.net import DEFAULT_HEADERS


TWSE_OPENAPI_BASE = "https://openapi.twse.com.tw/v1"
TPEX_OPENAPI_BASE = "https://www.tpex.org.tw/openapi/v1"
MOPS_DOC_BASE = "https://doc.twse.com.tw"
MOPS_DOC_QUERY_URL = f"{MOPS_DOC_BASE}/server-java/t57sb01"
_TAIPEI = ZoneInfo("Asia/Taipei")
_UTC = ZoneInfo("UTC")
_FINANCIAL_SUFFIXES = ("basi", "bd", "ci", "fh", "ins", "mim")


def _request_get(url: str, *, timeout_s: float, accept: str) -> requests.Response:
    headers = {**DEFAULT_HEADERS, "Accept": accept}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout_s)
    except requests.exceptions.SSLError:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.get(url, headers=headers, timeout=timeout_s, verify=False)
    resp.raise_for_status()
    return resp


def _request_json(url: str, *, timeout_s: float) -> Any:
    return _request_get(url, timeout_s=timeout_s, accept="application/json,text/csv,*/*").json()


def _request_text(url: str, *, timeout_s: float) -> str:
    resp = _request_get(url, timeout_s=timeout_s, accept="text/html,application/xhtml+xml,*/*")
    if "big5" in str(resp.headers.get("content-type") or "").lower():
        resp.encoding = "big5"
    return resp.text


def _string_set(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    return {str(v).strip() for v in raw_items if str(v).strip()}


def _cfg_map_value(cfg: dict[str, Any] | None, key: str, code: str) -> Any:
    mapping = (cfg or {}).get(key) if isinstance(cfg, dict) else None
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
    return None


def _num(value: object) -> float | None:
    raw = str(value or "").strip().replace(",", "")
    if raw in {"", "-", "None", "null", "nan", "NaN"}:
        return None
    try:
        return float(raw)
    except Exception:
        return None


def _roc_year(value: object) -> int | None:
    raw = "".join(ch for ch in str(value or "").strip() if ch.isdigit())
    if not raw:
        return None
    try:
        year = int(raw)
    except Exception:
        return None
    if year < 1000:
        year += 1911
    return year if 1900 <= year <= 2200 else None


def _roc_date(value: object) -> date | None:
    raw = "".join(ch for ch in str(value or "").strip() if ch.isdigit())
    if len(raw) < 7:
        return None
    try:
        return date(int(raw[:3]) + 1911, int(raw[3:5]), int(raw[5:7]))
    except Exception:
        return None


def _roc_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    try:
        parsed = date.fromisoformat(raw[:10])
        local = datetime(parsed.year, parsed.month, parsed.day, 18, 0, 0, tzinfo=_TAIPEI)
        return local.astimezone(_UTC).replace(tzinfo=None)
    except Exception:
        pass
    match = re.search(r"(?P<y>\d{2,3})[/-](?P<m>\d{1,2})[/-](?P<d>\d{1,2})(?:\s+(?P<h>\d{1,2}):(?P<mi>\d{1,2})(?::(?P<s>\d{1,2}))?)?", raw)
    if not match:
        parsed_date = _roc_date(value)
        if parsed_date is None:
            return None
        local = datetime(parsed_date.year, parsed_date.month, parsed_date.day, 18, 0, 0, tzinfo=_TAIPEI)
        return local.astimezone(_UTC).replace(tzinfo=None)
    try:
        local = datetime(
            int(match.group("y")) + 1911,
            int(match.group("m")),
            int(match.group("d")),
            int(match.group("h") or 18),
            int(match.group("mi") or 0),
            int(match.group("s") or 0),
            tzinfo=_TAIPEI,
        )
        return local.astimezone(_UTC).replace(tzinfo=None)
    except Exception:
        return None


def _quarter_end(fiscal_year: int, quarter: int | None) -> str | None:
    if quarter == 1:
        return f"{fiscal_year}-03-31"
    if quarter == 2:
        return f"{fiscal_year}-06-30"
    if quarter == 3:
        return f"{fiscal_year}-09-30"
    if quarter == 4:
        return f"{fiscal_year}-12-31"
    return None


def _market_endpoint_configs(markets: object) -> list[dict[str, Any]]:
    raw_markets = markets if isinstance(markets, (list, tuple, set)) else [markets or "listed", "otc"]
    out: list[dict[str, Any]] = []
    for value in raw_markets:
        market = str(value or "").strip().lower()
        if market in {"listed", "twse", "l"}:
            out.append(
                {
                    "market": "listed",
                    "exchange": "TWSE",
                    "label": "TWSE listed companies",
                    "income_paths": [f"/opendata/t187ap06_L_{suffix}" for suffix in _FINANCIAL_SUFFIXES],
                    "balance_paths": [f"/opendata/t187ap07_L_{suffix}" for suffix in _FINANCIAL_SUFFIXES],
                    "base": TWSE_OPENAPI_BASE,
                }
            )
        elif market in {"otc", "tpex", "o"}:
            out.append(
                {
                    "market": "otc",
                    "exchange": "TPEX",
                    "label": "TPEx OTC companies",
                    "income_paths": [f"/mopsfin_t187ap06_O_{suffix}" for suffix in _FINANCIAL_SUFFIXES],
                    "balance_paths": [f"/mopsfin_t187ap07_O_{suffix}" for suffix in _FINANCIAL_SUFFIXES],
                    "base": TPEX_OPENAPI_BASE,
                }
            )
    return out or _market_endpoint_configs(["listed", "otc"])


def _company_identity(code: str, local_name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    identity = resolve_taiwan_company_name(company_code=code, local_name=local_name, config=cfg)
    mapped_name = str(_cfg_map_value(cfg, "company_name_by_stock_code", code) or "").strip()
    if mapped_name and not identity.get("company_name_en"):
        identity = {
            **identity,
            "company_name": mapped_name,
            "company_name_en": mapped_name,
            "company_aliases": list(identity.get("company_aliases") or []) + [mapped_name],
            "company_name_source": "source_pack",
        }
    return identity


def _source_identifiers(code: str, exchange: str, source: str) -> list[dict[str, Any]]:
    return [
        {
            "id_type": "TW_STOCK",
            "id_value": code,
            "exchange": exchange,
            "source_key": "company_code",
            "source": source,
            "market": exchange,
            "is_primary": True,
        }
    ] if code else []


def _mapped_company_fields(cfg: dict[str, Any], code: str) -> dict[str, Any]:
    company_id = _cfg_map_value(cfg, "company_id_by_stock_code", code)
    company_slug = str(_cfg_map_value(cfg, "company_slug_by_stock_code", code) or "").strip()
    ticker = str(_cfg_map_value(cfg, "ticker_by_stock_code", code) or "").strip()
    return {
        "company_id": int(company_id) if str(company_id or "").isdigit() else None,
        "resolved_company_id": int(company_id) if str(company_id or "").isdigit() else None,
        "company_slug": company_slug or None,
        "ticker": ticker or code,
    }


def _fetch_statement_rows(
    *,
    cfg: dict[str, Any],
    timeout_s: float,
    company_codes: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows_by_key: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    endpoint_counts: dict[str, int] = {}
    endpoint_errors: list[dict[str, str]] = []
    for market_cfg in _market_endpoint_configs(cfg.get("markets") or ["listed", "otc"]):
        for statement_kind, paths in (("income", market_cfg["income_paths"]), ("balance", market_cfg["balance_paths"])):
            for path in paths:
                url = f"{market_cfg['base']}{path}"
                try:
                    payload = _request_json(url, timeout_s=timeout_s)
                except Exception as exc:
                    endpoint_errors.append(
                        {
                            "market": str(market_cfg["market"]),
                            "statement_kind": statement_kind,
                            "path": path,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    endpoint_counts[path] = 0
                    continue
                raw_rows = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []
                endpoint_counts[path] = len(raw_rows)
                for raw in raw_rows:
                    if not isinstance(raw, dict):
                        continue
                    code = str(_row_value(raw, "公司代號", "SecuritiesCompanyCode", "Code") or "").strip()
                    if not code or (company_codes and code not in company_codes):
                        continue
                    fiscal_year = _roc_year(_row_value(raw, "年度", "Year"))
                    try:
                        quarter = int(str(_row_value(raw, "季別", "Season") or "").strip())
                    except Exception:
                        quarter = 0
                    if fiscal_year is None or quarter not in {1, 2, 3, 4}:
                        continue
                    key = (market_cfg["market"], code, fiscal_year, quarter)
                    row = rows_by_key.setdefault(
                        key,
                        {
                            "market": market_cfg["market"],
                            "exchange": market_cfg["exchange"],
                            "market_label": market_cfg["label"],
                            "code": code,
                            "fiscal_year": fiscal_year,
                            "fiscal_quarter": quarter,
                            "income_row": None,
                            "balance_row": None,
                            "income_url": None,
                            "balance_url": None,
                        },
                    )
                    local_name = str(_row_value(raw, "公司名稱", "CompanyName") or "").strip()
                    if local_name:
                        row["company_name_original"] = local_name
                    report_date = _roc_date(_row_value(raw, "出表日期", "Date"))
                    if report_date is not None:
                        row["report_date"] = report_date.isoformat()
                    row[f"{statement_kind}_row"] = dict(raw)
                    row[f"{statement_kind}_url"] = url
                    row[f"{statement_kind}_path"] = path
    rows = list(rows_by_key.values())
    rows.sort(key=lambda row: (int(row["fiscal_year"]), int(row["fiscal_quarter"]), str(row["code"])), reverse=True)
    stats: dict[str, Any] = {"statement_endpoint_counts": endpoint_counts}
    if endpoint_errors:
        stats["statement_endpoint_errors"] = endpoint_errors
    return rows, stats


def _metric(row: dict[str, Any] | None, *keys: str) -> float | None:
    if not isinstance(row, dict):
        return None
    return _num(_row_value(row, *keys))


def _financial_item(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any] | None:
    code = str(row.get("code") or "").strip()
    income = row.get("income_row") if isinstance(row.get("income_row"), dict) else {}
    balance = row.get("balance_row") if isinstance(row.get("balance_row"), dict) else {}
    local_name = str(row.get("company_name_original") or _row_value(income, "公司名稱", "CompanyName") or _row_value(balance, "公司名稱", "CompanyName") or "").strip()
    identity = _company_identity(code, local_name, cfg)
    company = str(identity.get("company_name") or local_name or code or "Taiwan issuer").strip()
    company_en = str(identity.get("company_name_en") or "").strip()
    aliases = [str(alias).strip() for alias in (identity.get("company_aliases") or []) if str(alias).strip()]
    fiscal_year = int(row["fiscal_year"])
    quarter = int(row["fiscal_quarter"])
    period_end = _quarter_end(fiscal_year, quarter)
    report_date = str(row.get("report_date") or period_end or "").strip()
    published_at = _roc_datetime(report_date) if report_date else None
    filing_type = "TW_MOPS_ANNUAL_FINANCIALS" if quarter == 4 else "TW_MOPS_QUARTERLY_FINANCIALS"
    title = f"{company} FY{fiscal_year} Q{quarter} official financial statements (MOPS)"
    if quarter == 4:
        title = f"{company} FY{fiscal_year} annual official financial statements (MOPS)"
    metrics = {
        "revenue_ntd_thousand": _metric(income, "營業收入", "收入"),
        "gross_profit_ntd_thousand": _metric(income, "營業毛利（毛損）", "營業毛利（毛損）淨額"),
        "operating_income_ntd_thousand": _metric(income, "營業利益（損失）"),
        "net_income_ntd_thousand": _metric(income, "本期淨利（淨損）"),
        "eps_ntd": _metric(income, "基本每股盈餘（元）"),
        "total_assets_ntd_thousand": _metric(balance, "資產總額"),
        "total_liabilities_ntd_thousand": _metric(balance, "負債總額"),
        "total_equity_ntd_thousand": _metric(balance, "權益總額"),
    }
    body = "\n".join(
        part
        for part in [
            title,
            f"Company: {company} ({code})",
            f"Original company name: {local_name}" if local_name and local_name != company else "",
            f"Market: {row.get('market_label')}",
            f"Fiscal period: FY{fiscal_year} Q{quarter}",
            f"Period end: {period_end}" if period_end else "",
            f"Report date: {report_date}" if report_date else "",
            "Official unit for monetary statement values: NT$ thousand",
            f"Revenue: {metrics['revenue_ntd_thousand']}",
            f"Operating income: {metrics['operating_income_ntd_thousand']}",
            f"Net income: {metrics['net_income_ntd_thousand']}",
            f"EPS: {metrics['eps_ntd']}",
            f"Total assets: {metrics['total_assets_ntd_thousand']}",
            f"Total liabilities: {metrics['total_liabilities_ntd_thousand']}",
            f"Total equity: {metrics['total_equity_ntd_thousand']}",
            f"Income statement endpoint: {row.get('income_url')}" if row.get("income_url") else "",
            f"Balance sheet endpoint: {row.get('balance_url')}" if row.get("balance_url") else "",
        ]
        if part
    )
    mapped = _mapped_company_fields(cfg, code)
    exchange = str(row.get("exchange") or "TWSE").strip()
    source_url = str(row.get("income_url") or row.get("balance_url") or "").strip()
    record_url = (
        f"{source_url}#mops-{exchange.lower()}-{code}-fy{fiscal_year}q{quarter}"
        if source_url
        else stable_external_id("taiwan_mops_financial_reports", "financials", exchange, code, fiscal_year, quarter)
    )
    if not keyword_allowed(cfg, code, company, company_en, local_name, *aliases, title, body):
        return None
    return {
        "kind": "securities_filing",
        "external_id": stable_external_id("taiwan_mops_financial_reports", "financials", exchange, code, fiscal_year, quarter),
        "url": record_url,
        "canonical_url": record_url,
        "title": title,
        "author": company,
        "published_at": published_at,
        "raw_text": body,
        "normalized_text": body,
        "extra": {
            "connector": "taiwan_mops_financial_reports",
            "regulator": "MOPS",
            "exchange": exchange,
            "jurisdiction": "TW",
            "filing_type": filing_type,
            "form_type": filing_type,
            "section": "financials",
            "company_doc_type": "financial_report",
            "is_earnings_release": True,
            "company_code": code,
            "stock_code": code,
            **mapped,
            "company_name": company,
            "company_name_en": company_en or None,
            "company_name_original": local_name or None,
            "company_display_name": company,
            "company_aliases": aliases,
            "company_name_source": identity.get("company_name_source"),
            "fiscal_year": fiscal_year,
            "fiscal_quarter": quarter,
            "period_of_report": period_end,
            "filed_date": report_date or None,
            "report_date": report_date or None,
            "currency": "TWD",
            "unit": "NT$ thousand",
            "financial_metrics": metrics,
            "income_statement_url": row.get("income_url"),
            "balance_sheet_url": row.get("balance_url"),
            "income_endpoint_path": row.get("income_path"),
            "balance_endpoint_path": row.get("balance_path"),
            "source_identifiers": _source_identifiers(code, exchange, "taiwan_mops_financial_reports"),
            "source_rows": {
                "income_statement": income or None,
                "balance_sheet": balance or None,
            },
        },
    }


_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", flags=re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", flags=re.IGNORECASE | re.DOTALL)
_READFILE_RE = re.compile(r"readfile2?\(\"([^\"]+)\",\"([^\"]+)\",\"([^\"]+)\"\)", flags=re.IGNORECASE)


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(text).replace("\xa0", " ")).strip()


def _annual_query_url(code: str, roc_year: int) -> str:
    return f"{MOPS_DOC_QUERY_URL}?step=1&colorchg=1&co_id={code}&year={roc_year}&seamon=&mtype=F&"


def _annual_query_years(cfg: dict[str, Any]) -> list[int]:
    raw = cfg.get("annual_report_query_years")
    if isinstance(raw, (list, tuple, set)):
        years = []
        for value in raw:
            try:
                year = int(str(value).strip())
            except Exception:
                continue
            years.append(year - 1911 if year > 1900 else year)
        return sorted({year for year in years if 1 <= year <= 300}, reverse=True)
    current_roc_year = datetime.now(_TAIPEI).year - 1911
    years_back = max(1, min(int(cfg.get("annual_report_years_back") or 3), 6))
    return list(range(current_roc_year, current_roc_year - years_back, -1))


def _parse_annual_report_rows(html_text: str, *, listing_url: str, code: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for tr in _TR_RE.findall(html_text or ""):
        cells_html = _CELL_RE.findall(tr)
        if len(cells_html) < 10:
            continue
        cells = [_strip_html(cell) for cell in cells_html]
        detail = cells[5] if len(cells) > 5 else ""
        if "股東會年報" not in detail:
            continue
        if "年報前十大股東" in detail:
            continue
        match = _READFILE_RE.search(tr)
        if not match:
            continue
        data_year = _roc_year(cells[1] if len(cells) > 1 else "")
        if data_year is None:
            continue
        upload_at = _roc_datetime(cells[9] if len(cells) > 9 else "")
        candidates.append(
            {
                "code": code,
                "fiscal_year": data_year,
                "kind": match.group(1),
                "mops_code": match.group(2),
                "filename": match.group(3),
                "detail": detail,
                "is_english": "英文版" in detail or re.search(r"FE\d+\.pdf$", match.group(3), flags=re.IGNORECASE) is not None,
                "file_size": cells[8] if len(cells) > 8 else None,
                "uploaded_at": upload_at,
                "uploaded_at_raw": cells[9] if len(cells) > 9 else None,
                "listing_url": listing_url,
            }
        )
    candidates.sort(
        key=lambda row: (
            int(row.get("fiscal_year") or 0),
            1 if not row.get("is_english") else 0,
            row.get("uploaded_at") or datetime.min,
        ),
        reverse=True,
    )
    return candidates


def _fetch_latest_annual_report(code: str, *, cfg: dict[str, Any], timeout_s: float) -> dict[str, Any] | None:
    for roc_year in _annual_query_years(cfg):
        listing_url = _annual_query_url(code, roc_year)
        text = _request_text(listing_url, timeout_s=timeout_s)
        rows = _parse_annual_report_rows(text, listing_url=listing_url, code=code)
        if rows:
            preferred = next((row for row in rows if not row.get("is_english")), rows[0])
            alternates = [row for row in rows if row is not preferred]
            preferred["alternate_files"] = [
                {
                    "filename": row.get("filename"),
                    "detail": row.get("detail"),
                    "is_english": row.get("is_english"),
                    "uploaded_at": row.get("uploaded_at").isoformat() if row.get("uploaded_at") else None,
                }
                for row in alternates[:5]
            ]
            return preferred
    return None


def _annual_report_item(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any] | None:
    code = str(row.get("code") or "").strip()
    identity = _company_identity(code, "", cfg)
    company = str(identity.get("company_name") or _cfg_map_value(cfg, "company_name_by_stock_code", code) or code or "Taiwan issuer").strip()
    company_en = str(identity.get("company_name_en") or "").strip()
    aliases = [str(alias).strip() for alias in (identity.get("company_aliases") or []) if str(alias).strip()]
    fiscal_year = int(row.get("fiscal_year") or 0)
    uploaded_at = row.get("uploaded_at") if isinstance(row.get("uploaded_at"), datetime) else None
    period_end = f"{fiscal_year}-12-31"
    title = f"{company} FY{fiscal_year} annual report (MOPS)"
    listing_url = str(row.get("listing_url") or _annual_query_url(code, fiscal_year - 1910)).strip()
    filename = str(row.get("filename") or "").strip()
    detail = str(row.get("detail") or "").strip()
    body = "\n".join(
        part
        for part in [
            title,
            f"Company: {company} ({code})",
            f"Fiscal year: FY{fiscal_year}",
            f"MOPS document detail: {detail}" if detail else "",
            f"Official file: {filename}" if filename else "",
            f"File size: {row.get('file_size')}" if row.get("file_size") else "",
            f"Uploaded at: {row.get('uploaded_at_raw')}" if row.get("uploaded_at_raw") else "",
            f"Source listing: {listing_url}",
            "MOPS resolves the PDF by posting the listed file name to doc.twse.com.tw/server-java/t57sb01.",
        ]
        if part
    )
    if not keyword_allowed(cfg, code, company, company_en, *aliases, title, body):
        return None
    exchange = str(_cfg_map_value(cfg, "exchange_by_stock_code", code) or "TWSE").strip() or "TWSE"
    mapped = _mapped_company_fields(cfg, code)
    return {
        "kind": "securities_filing",
        "external_id": stable_external_id("taiwan_mops_financial_reports", "annual_report", code, fiscal_year, filename),
        "url": listing_url,
        "canonical_url": listing_url,
        "title": title,
        "author": company,
        "published_at": uploaded_at,
        "raw_text": body,
        "normalized_text": body,
        "extra": {
            "connector": "taiwan_mops_financial_reports",
            "regulator": "MOPS",
            "exchange": exchange,
            "jurisdiction": "TW",
            "filing_type": "TW_MOPS_ANNUAL_REPORT",
            "form_type": "TW_MOPS_ANNUAL_REPORT",
            "section": "text_blocks",
            "company_doc_type": "annual_report_text",
            "company_code": code,
            "stock_code": code,
            **mapped,
            "company_name": company,
            "company_name_en": company_en or None,
            "company_display_name": company,
            "company_aliases": aliases,
            "company_name_source": identity.get("company_name_source"),
            "fiscal_year": fiscal_year,
            "period_of_report": period_end,
            "filed_date": uploaded_at.date().isoformat() if uploaded_at else None,
            "mops_document_kind": row.get("kind"),
            "mops_filename": filename or None,
            "mops_detail": detail or None,
            "mops_file_size": row.get("file_size"),
            "mops_uploaded_at_raw": row.get("uploaded_at_raw"),
            "mops_listing_url": listing_url,
            "mops_download_post": {
                "url": MOPS_DOC_QUERY_URL,
                "method": "POST",
                "fields": {"step": "9", "kind": row.get("kind"), "co_id": code, "filename": filename, "colorchg": "1"},
            },
            "alternate_files": row.get("alternate_files") or [],
            "index_url": listing_url,
            "source_identifiers": _source_identifiers(code, exchange, "taiwan_mops_financial_reports"),
        },
    }


def fetch_taiwan_mops_financial_reports(
    config: dict[str, Any],
    *,
    source_cursor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = dict(config or {})
    timeout_s = float(cfg.get("timeout_s") or 30.0)
    company_codes = _string_set(cfg.get("company_codes"))
    max_items = max(0, min(int(cfg.get("max_items_per_run") or 200), 1000))
    max_financial_items = max(0, min(int(cfg.get("max_financial_items_per_run") or max_items), max_items or 1000))
    annual_enabled = cfg.get("annual_reports_enabled", True) is not False

    items: list[dict[str, Any]] = []
    statement_rows, statement_stats = _fetch_statement_rows(cfg=cfg, timeout_s=timeout_s, company_codes=company_codes)
    for row in statement_rows:
        if len(items) >= max_financial_items or (max_items and len(items) >= max_items):
            break
        item = _financial_item(row, cfg)
        if item is not None:
            items.append(item)

    annual_stats: dict[str, Any] = {"checked_codes": [], "missing_codes": [], "document_count": 0}
    next_cursor = dict(source_cursor or {})
    if annual_enabled and company_codes and (not max_items or len(items) < max_items):
        codes = sorted(company_codes)
        start = int(next_cursor.get("annual_code_index") or 0)
        if start < 0 or start >= len(codes):
            start = 0
        limit = max(0, min(int(cfg.get("annual_company_limit_per_run") or 12), len(codes)))
        ordered = codes[start:] + codes[:start]
        checked = 0
        next_index = start
        for code in ordered[:limit]:
            if max_items and len(items) >= max_items:
                break
            checked += 1
            annual_stats["checked_codes"].append(code)
            next_index = (codes.index(code) + 1) % len(codes)
            try:
                annual_row = _fetch_latest_annual_report(code, cfg=cfg, timeout_s=timeout_s)
            except Exception as exc:
                annual_stats.setdefault("errors", []).append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
                continue
            if annual_row is None:
                annual_stats["missing_codes"].append(code)
                continue
            item = _annual_report_item(annual_row, cfg)
            if item is not None:
                items.append(item)
                annual_stats["document_count"] += 1
        if checked:
            next_cursor["annual_code_index"] = next_index

    return {
        "items": items,
        "cursor": next_cursor or None,
        "stats": {
            "source_kind": "taiwan_mops_financial_reports",
            "statement_row_count": len(statement_rows),
            "statement_document_count": len([item for item in items if (item.get("extra") or {}).get("filing_type") in {"TW_MOPS_QUARTERLY_FINANCIALS", "TW_MOPS_ANNUAL_FINANCIALS"}]),
            "annual_report_stats": annual_stats,
            "company_code_count": len(company_codes),
            **statement_stats,
        },
    }
