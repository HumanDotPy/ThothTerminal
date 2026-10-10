from __future__ import annotations

from typing import Any

from thoth_terminal.trade import build_trade_series_from_rows, build_trade_summary_document_items, fetch_tabular_url, normalize_trade_config, parse_tabular_payload


def fetch_trade_korea_customs(config: dict[str, Any]) -> dict[str, Any]:
    cfg = normalize_trade_config(config, default_reporter_iso="KR", default_source_label="Korea Customs")
    data_url = str(cfg.get("api_url") or cfg.get("data_url") or "").strip()
    if not data_url:
        raise ValueError("trade_korea_customs requires api_url or data_url")
    headers: dict[str, str] = {}
    if cfg.get("api_key"):
        headers["Authorization"] = str(cfg.get("api_key"))
    service_key = str(cfg.get("service_key") or "").strip()
    params = dict(cfg.get("query_params") or {})
    if service_key and "serviceKey" not in params:
        params["serviceKey"] = service_key
    text, content_type = fetch_tabular_url(url=data_url, params=params or None, headers=headers or None, timeout_s=45.0)
    rows = parse_tabular_payload(
        text,
        content_type=content_type,
        response_format=str(cfg.get("response_format") or "").strip() or None,
        json_items_path=cfg.get("json_items_path") or ["response", "body", "items"],
        csv_delimiter=str(cfg.get("csv_delimiter") or ","),
    )
    series_payloads = build_trade_series_from_rows(rows, cfg=cfg, default_source_url=data_url)
    items = build_trade_summary_document_items(source_name=str(cfg.get("source_label") or "Korea Customs"), cfg=cfg, series_payloads=series_payloads)
    return {"items": items, "cursor": None, "stats": {"trade_series": series_payloads, "source_kind": "trade_korea_customs"}}
