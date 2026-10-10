from __future__ import annotations

from typing import Any

from thoth_terminal.trade import (
    build_trade_series_from_rows,
    build_trade_summary_document_items,
    normalize_trade_config,
    parse_tabular_payload,
    fetch_tabular_url,
    resolve_taiwan_dataset_url,
)


def fetch_trade_taiwan_customs(config: dict[str, Any]) -> dict[str, Any]:
    cfg = normalize_trade_config(config, default_reporter_iso="TW", default_source_label="Taiwan Customs")
    data_url = str(cfg.get("data_url") or cfg.get("csv_url") or "").strip()
    if not data_url and cfg.get("dataset_id"):
        data_url = str(resolve_taiwan_dataset_url(cfg.get("dataset_id")) or "").strip()
    if not data_url:
        raise ValueError("trade_taiwan_customs requires data_url/csv_url or dataset_id")
    text, content_type = fetch_tabular_url(url=data_url, timeout_s=40.0)
    rows = parse_tabular_payload(
        text,
        content_type=content_type,
        response_format=str(cfg.get("response_format") or "").strip() or None,
        json_items_path=cfg.get("json_items_path"),
        csv_delimiter=str(cfg.get("csv_delimiter") or ","),
    )
    series_payloads = build_trade_series_from_rows(rows, cfg=cfg, default_source_url=data_url)
    items = build_trade_summary_document_items(source_name=str(cfg.get("source_label") or "Taiwan Customs"), cfg=cfg, series_payloads=series_payloads)
    return {"items": items, "cursor": None, "stats": {"trade_series": series_payloads, "source_kind": "trade_taiwan_customs"}}
