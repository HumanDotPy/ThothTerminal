from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterable

import requests

from thoth_terminal.config import settings
from thoth_terminal.net import DEFAULT_HEADERS
from thoth_terminal.trade.common import (
    build_trade_series_from_rows,
    build_trade_summary_document_items,
    normalize_trade_config,
)


UN_COMTRADE_FULL_URL = "https://comtradeapi.un.org/data/v1/get/C/M/HS"
UN_COMTRADE_PREVIEW_URL = "https://comtradeapi.un.org/public/v1/preview/C/M/HS"

_COUNTRY_CODES = {
    "WORLD": 0,
    "WLD": 0,
    "W00": 0,
    "0": 0,
    "US": 842,
    "USA": 842,
    "CN": 156,
    "CHN": 156,
    "TW": 490,
    "TWN": 490,
    "JP": 392,
    "JPN": 392,
    "HK": 344,
    "HKG": 344,
    "MY": 458,
    "MYS": 458,
    "KR": 410,
    "KOR": 410,
    "SG": 702,
    "SGP": 702,
    "VN": 704,
    "VNM": 704,
    "TH": 764,
    "THA": 764,
    "PH": 608,
    "PHL": 608,
    "DE": 276,
    "DEU": 276,
    "NL": 528,
    "NLD": 528,
}

_FLOW_CODES = {
    "import": "M",
    "imports": "M",
    "m": "M",
    "export": "X",
    "exports": "X",
    "x": "X",
}


def _as_list(value: object, *, fallback: Iterable[object] = ()) -> list[str]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list | tuple | set):
        items = [str(part).strip() for part in value if str(part).strip()]
    else:
        items = [str(part).strip() for part in fallback if str(part).strip()]
    return items


def _country_code(value: object) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw)
    key = raw.upper()
    if key not in _COUNTRY_CODES:
        raise ValueError(f"Unsupported UN Comtrade country code {raw!r}; pass reporter_code/partner_code for countries not in the built-in map.")
    return int(_COUNTRY_CODES[key])


def _country_scope(value: object) -> str:
    raw = str(value or "").strip()
    if not raw or raw.lower() in {"world", "all"} or raw.upper() in {"W00", "WLD"} or raw == "0":
        return "world"
    return raw.upper()


def _add_months(month: date, delta: int) -> date:
    idx = month.year * 12 + (month.month - 1) + int(delta)
    year, month_idx = divmod(idx, 12)
    return date(year, month_idx + 1, 1)


def _periods(cfg: dict[str, Any]) -> list[str]:
    raw_periods = _as_list(cfg.get("periods"))
    if raw_periods:
        return [period.replace("-", "")[:6] for period in raw_periods if period.replace("-", "")[:6].isdigit()]
    lookback = max(1, min(int(cfg.get("lookback_periods") or 12), 60))
    lag_months = max(0, int(cfg.get("lag_months") or 2))
    end_month = _add_months(datetime.now(UTC).date().replace(day=1), -lag_months)
    start_month = _add_months(end_month, -(lookback - 1))
    periods: list[str] = []
    cur = start_month
    while cur <= end_month:
        periods.append(f"{cur.year:04d}{cur.month:02d}")
        cur = _add_months(cur, 1)
    return periods


def _code_batches(codes: list[str], batch_size: int) -> list[list[str]]:
    size = max(1, min(int(batch_size or 8), 20))
    return [codes[idx : idx + size] for idx in range(0, len(codes), size)]


def _comtrade_headers(cfg: dict[str, Any], api_key: str | None) -> dict[str, str]:
    headers = {**DEFAULT_HEADERS, **dict(cfg.get("headers") or {})}
    if api_key:
        headers["Ocp-Apim-Subscription-Key"] = api_key
    return headers


def _extract_rows(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("data")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    return []


def _row_period(row: dict[str, Any]) -> tuple[int | None, int | None]:
    raw_period = str(row.get("period") or "").strip()
    if len(raw_period) >= 6 and raw_period[:6].isdigit():
        return int(raw_period[:4]), int(raw_period[4:6])
    year = row.get("refYear") or row.get("year")
    month = row.get("refMonth") or row.get("month")
    try:
        return int(str(year).strip()), int(str(month).strip())
    except Exception:
        return None, None


def _normalize_rows(rows: list[dict[str, Any]], *, source_ref: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        year, month = _row_period(row)
        if not year or not month:
            continue
        normalized = {
            "year": year,
            "month": month,
            "hs_code": row.get("cmdCode"),
            "value": row.get("primaryValue") or row.get("cifvalue") or row.get("fobvalue"),
            "quantity": row.get("qty") or row.get("altQty"),
            "source_ref": source_ref,
            "reporter": row.get("reporterDesc") or row.get("reporterISO"),
            "partner": row.get("partnerDesc") or row.get("partnerISO"),
        }
        out.append(normalized)
    return out


def _fetch_comtrade_rows(
    *,
    api_url: str,
    cfg: dict[str, Any],
    api_key: str | None,
    reporter_code: int,
    partner_code: int,
    flow_code: str,
    period: str,
    cmd_codes: list[str],
) -> list[dict[str, Any]]:
    params = {
        "cmdCode": ",".join(cmd_codes),
        "flowCode": flow_code,
        "reporterCode": str(reporter_code),
        "partnerCode": str(partner_code),
        "partner2Code": str(cfg.get("partner2_code") or "0"),
        "period": period,
        "motCode": str(cfg.get("mode_of_transport_code") or cfg.get("motCode") or "0"),
        "customsCode": str(cfg.get("customs_code") or cfg.get("customsCode") or "C00"),
        "includeDesc": str(bool(cfg.get("include_desc", True))).lower(),
        "maxRecords": str(int(cfg.get("max_records") or 50_000)),
        "format": "json",
    }
    resp = requests.get(
        api_url,
        params=params,
        headers=_comtrade_headers(cfg, api_key),
        timeout=float(cfg.get("timeout_s") or 60.0),
        verify=bool(cfg.get("verify_ssl", True)),
    )
    resp.raise_for_status()
    return _extract_rows(resp.json())


def fetch_trade_un_comtrade(config: dict[str, Any]) -> dict[str, Any]:
    default_reporter = _as_list((config or {}).get("reporter_isos"), fallback=[(config or {}).get("reporter_iso") or "JP"])[0]
    cfg = normalize_trade_config(config, default_reporter_iso=default_reporter, default_source_label="UN Comtrade")
    api_key = str(cfg.get("api_key") or getattr(settings, "un_comtrade_api_key", None) or "").strip() or None
    allow_preview = bool(cfg.get("allow_public_preview", True))
    preview_mode = bool(cfg.get("preview_mode")) or not api_key
    if preview_mode and not allow_preview:
        raise ValueError("trade_un_comtrade requires THOTH_UN_COMTRADE_API_KEY unless allow_public_preview is true.")
    api_url = str(cfg.get("api_url") or (UN_COMTRADE_PREVIEW_URL if preview_mode else UN_COMTRADE_FULL_URL)).strip()
    reporters = _as_list(cfg.get("reporter_isos"), fallback=[cfg.get("reporter_iso")])
    partners = _as_list(cfg.get("partner_isos") or cfg.get("partner_scopes"), fallback=[cfg.get("partner_iso") or cfg.get("partner_scope") or "world"])
    periods = _periods(cfg)
    hs_codes = [str(code).strip() for code in (cfg.get("hs_codes") or []) if str(code).strip()]
    if not hs_codes:
        raise ValueError("trade_un_comtrade requires themes or hs_codes.")
    flow_code = _FLOW_CODES.get(str(cfg.get("flow") or "import").strip().lower(), "M")
    max_requests = max(1, int(cfg.get("max_requests_per_run") or 120))
    request_count = 0
    all_series: list[dict[str, Any]] = []
    total_rows = 0

    for reporter in reporters:
        reporter_code = _country_code(cfg.get("reporter_code") if len(reporters) == 1 and cfg.get("reporter_code") else reporter)
        reporter_iso = _country_scope(reporter)
        if reporter_iso == "world":
            raise ValueError("UN Comtrade reporter cannot be world.")
        for partner in partners:
            partner_code = _country_code(cfg.get("partner_code") if len(partners) == 1 and cfg.get("partner_code") else partner)
            partner_scope = _country_scope(partner)
            pair_rows: list[dict[str, Any]] = []
            for period in periods:
                for batch in _code_batches(hs_codes, int(cfg.get("cmd_batch_size") or 8)):
                    if request_count >= max_requests:
                        break
                    rows = _fetch_comtrade_rows(
                        api_url=api_url,
                        cfg=cfg,
                        api_key=api_key,
                        reporter_code=reporter_code,
                        partner_code=partner_code,
                        flow_code=flow_code,
                        period=period,
                        cmd_codes=batch,
                    )
                    request_count += 1
                    source_ref = f"UN Comtrade {period} reporter={reporter_code} partner={partner_code} flow={flow_code}"
                    pair_rows.extend(_normalize_rows(rows, source_ref=source_ref))
                if request_count >= max_requests:
                    break
            total_rows += len(pair_rows)
            single_cfg = {
                **cfg,
                "reporter_iso": reporter_iso,
                "partner_scope": partner_scope,
                "flow": "export" if flow_code == "X" else "import",
                "value_field": "value",
                "quantity_field": "quantity",
                "source_ref_field": "source_ref",
                "source_label": str(cfg.get("source_label") or "UN Comtrade").strip() or "UN Comtrade",
            }
            all_series.extend(build_trade_series_from_rows(pair_rows, cfg=single_cfg, default_source_url=api_url))
            if request_count >= max_requests:
                break
        if request_count >= max_requests:
            break

    items = build_trade_summary_document_items(source_name=str(cfg.get("source_label") or "UN Comtrade"), cfg=cfg, series_payloads=all_series)
    return {
        "items": items,
        "cursor": None,
        "stats": {
            "row_count": total_rows,
            "request_count": request_count,
            "preview_mode": preview_mode,
            "trade_series": all_series,
            "source_kind": "trade_un_comtrade",
            "truncated_by_request_cap": request_count >= max_requests,
        },
    }
