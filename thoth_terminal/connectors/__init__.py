from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import threading

from thoth_terminal.browser_ingestion_runtime import run_browser_ingestion

from thoth_terminal.connectors.gmail_substack import fetch_gmail_substack
from thoth_terminal.connectors.google_drive_folder import fetch_google_drive_folder
from thoth_terminal.connectors.gsa_calc_ceiling_rates import fetch_gsa_calc_ceiling_rates
from thoth_terminal.connectors.institutional_public_records import (
    fetch_caiso_interconnection_queue,
    fetch_cbp_cross,
    fetch_chips_funding,
    fetch_copernicus_browser,
    fetch_eia_860m,
    fetch_ercot_large_load,
    fetch_ercot_llwg,
    fetch_eu_taric,
    fetch_fcc_oet_kdb,
    fetch_pjm_planning_center,
    fetch_usgs_earthexplorer,
    fetch_usgs_naip,
    fetch_usitc_hts,
)
from thoth_terminal.connectors.rss import fetch_rss
from thoth_terminal.connectors.cloudflare_crawl_discovery import fetch_cloudflare_crawl_discovery
from thoth_terminal.connectors.asx_announcements import fetch_asx_announcements
from thoth_terminal.connectors.bursa_announcements import fetch_bursa_announcements
from thoth_terminal.connectors.cninfo_announcements import fetch_cninfo_announcements
from thoth_terminal.connectors.edinet_disclosures import fetch_edinet_disclosures
from thoth_terminal.connectors.edinetdb_financials import fetch_edinetdb_financials
from thoth_terminal.connectors.eu_ted_search import fetch_eu_ted_search
from thoth_terminal.connectors.future_electronics import fetch_future_electronics
from thoth_terminal.connectors.filings_xbrl_org import fetch_filings_xbrl_org
from thoth_terminal.connectors.hkexnews_title_search import fetch_hkexnews_title_search
from thoth_terminal.connectors.ipo_calendar import (
    fetch_asx_ipo_calendar,
    fetch_euronext_ipo_calendar,
    fetch_hkex_ipo_calendar,
    fetch_jpx_ipo_calendar,
    fetch_krx_ipo_calendar,
    fetch_nasdaq_ipo_calendar,
    fetch_sec_ipo_filings,
    fetch_twse_ipo_calendar,
)
from thoth_terminal.connectors.kazakhstan_announcements import fetch_kazakhstan_announcements
from thoth_terminal.connectors.krx_kind_disclosure import fetch_krx_kind_disclosure
from thoth_terminal.connectors.local_research_library import fetch_local_research_library
from thoth_terminal.connectors.patentsview_search import fetch_patentsview_search
from thoth_terminal.connectors.nse_corporate_announcements import fetch_nse_corporate_announcements
from thoth_terminal.connectors.opendart_disclosures import fetch_opendart_disclosures
from thoth_terminal.connectors.opendart_financials import fetch_opendart_financials
from thoth_terminal.connectors.paid_reader_browser import (
    fetch_note_creator_browser,
    fetch_substack_inbox_browser,
)
from thoth_terminal.connectors.sam_gov_contracts import fetch_sam_gov_contracts
from thoth_terminal.connectors.tdnet_company_announcements import fetch_tdnet_company_announcements
from thoth_terminal.connectors.taiwan_mops_financial_reports import fetch_taiwan_mops_financial_reports
from thoth_terminal.connectors.taiwan_mops_investor_conferences import fetch_taiwan_mops_investor_conferences
from thoth_terminal.connectors.taiwan_mops_monthly_revenue import fetch_taiwan_mops_monthly_revenue
from thoth_terminal.connectors.trade_usitc_dataweb import fetch_trade_usitc_dataweb
from thoth_terminal.connectors.tpex_infohub_company import fetch_tpex_infohub_company
from thoth_terminal.connectors.twse_openapi import fetch_twse_openapi
from thoth_terminal.connectors.web_list import fetch_web_list
from thoth_terminal.connectors.web_list_browser import fetch_web_list_browser
from thoth_terminal.connectors.skhynix_blog_api import fetch_skhynix_blog_api
from thoth_terminal.connectors.sgx_announcements import fetch_sgx_announcements
from thoth_terminal.connectors.trade_eurostat_comext import fetch_trade_eurostat_comext
from thoth_terminal.connectors.trade_japan_trade_stats import fetch_trade_japan_trade_stats
from thoth_terminal.connectors.trade_korea_customs import fetch_trade_korea_customs
from thoth_terminal.connectors.trade_taiwan_customs import fetch_trade_taiwan_customs
from thoth_terminal.connectors.trade_un_comtrade import fetch_trade_un_comtrade
from thoth_terminal.connectors.trade_us_census import fetch_trade_us_census
from thoth_terminal.connectors.tsmc_monthly_revenue import fetch_tsmc_monthly_revenue
from thoth_terminal.connectors.tsmc_quarterly import fetch_tsmc_quarterly
from thoth_terminal.connectors.x_list import fetch_x_list
from thoth_terminal.connectors.sec_edgar import fetch_sec_edgar
from thoth_terminal.connectors.sec_companyfacts import fetch_sec_companyfacts
from thoth_terminal.connectors.sec_edgar_rss import fetch_sec_edgar_rss
from thoth_terminal.connectors.sedarplus_search import fetch_sedarplus_search

logger = logging.getLogger("thoth.connectors")

_playwright_serial_lock = threading.Lock()


def shutdown_playwright_pool(*, wait: bool = False, cancel_futures: bool = True) -> None:
    # Compatibility hook retained for desktop shutdown. Browser connector
    # processes are one-shot and owned by run_browser_ingestion, so there is no
    # resident executor to drain.
    del wait, cancel_futures


def _run_playwright(fn, cfg):
    # One isolated browser tree at a time. The child runner enforces memory,
    # progress, and wall-clock limits and kills the whole tree on failure.
    source_id = cfg.get("_source_id") if isinstance(cfg, dict) else None
    source_name = cfg.get("_source_name") if isinstance(cfg, dict) else None
    connector = cfg.get("connector") if isinstance(cfg, dict) else None
    started = time.perf_counter()
    logger.info(
        "playwright_fetch_start fn=%s connector=%s source_id=%s source=%s headless=%s channel=%s persistent_profile=%s",
        getattr(fn, "__name__", str(fn)),
        connector,
        source_id,
        source_name,
        cfg.get("headless") if isinstance(cfg, dict) else None,
        cfg.get("browser_channel") if isinstance(cfg, dict) else None,
        bool((cfg.get("user_data_dir") or cfg.get("browser_user_data_dir")) if isinstance(cfg, dict) else False),
    )
    try:
        with _playwright_serial_lock:
            return run_browser_ingestion(fn, dict(cfg or {}))
    finally:
        logger.info(
            "playwright_fetch_done fn=%s connector=%s source_id=%s source=%s elapsed_s=%.2f",
            getattr(fn, "__name__", str(fn)),
            connector,
            source_id,
            source_name,
            time.perf_counter() - started,
        )


_LEGACY_DISABLED = {
    "ir_calendar",
    "eurolandir_calendar",
    "tsmc_calendar",
}


@dataclass(frozen=True)
class ConnectorFetchResult:
    items: list[dict]
    cursor: dict[str, Any] | None = None
    stats: dict[str, Any] | None = None


def _normalize_fetch_result(raw: object) -> ConnectorFetchResult:
    if isinstance(raw, list):
        items = [it for it in raw if isinstance(it, dict)]
        return ConnectorFetchResult(items=items, cursor=None, stats=None)
    if isinstance(raw, dict):
        items_val = raw.get("items")
        cursor_val = raw.get("cursor")
        stats_val = raw.get("stats")
        if not isinstance(items_val, list):
            raise ValueError("Connector fetch result dict must include list field 'items'.")
        items = [it for it in items_val if isinstance(it, dict)]
        cursor = dict(cursor_val) if isinstance(cursor_val, dict) else None
        stats = dict(stats_val) if isinstance(stats_val, dict) else None
        return ConnectorFetchResult(items=items, cursor=cursor, stats=stats)
    raise ValueError("Connector fetch returned unsupported payload.")


def fetch_items_result(connector: str, *, source_config: dict, source_cursor: dict | None = None) -> ConnectorFetchResult:
    if connector in _LEGACY_DISABLED:
        logger.info("Skipping legacy connector %s (disabled in new earnings/company pipeline).", connector)
        return ConnectorFetchResult(items=[], cursor=dict(source_cursor or {}), stats={"skipped": True, "reason": "legacy_disabled"})
    if connector == "rss":
        return _normalize_fetch_result(fetch_rss(source_config))
    if connector == "web_list":
        return _normalize_fetch_result(fetch_web_list(source_config))
    if connector == "web_list_browser":
        cfg = dict(source_config or {})
        cfg["use_browser"] = True
        return _normalize_fetch_result(_run_playwright(fetch_web_list_browser, cfg))
    if connector == "skhynix_blog_api":
        return _normalize_fetch_result(fetch_skhynix_blog_api(source_config))
    if connector == "future_electronics":
        return _normalize_fetch_result(fetch_future_electronics(source_config, source_cursor=source_cursor))
    if connector == "filings_xbrl_org":
        return _normalize_fetch_result(fetch_filings_xbrl_org(source_config, source_cursor=source_cursor))
    if connector == "x_list":
        return _normalize_fetch_result(_run_playwright(fetch_x_list, source_config))
    if connector == "tsmc_monthly_revenue":
        return _normalize_fetch_result(fetch_tsmc_monthly_revenue(source_config))
    if connector == "taiwan_mops_monthly_revenue":
        return _normalize_fetch_result(fetch_taiwan_mops_monthly_revenue(source_config))
    if connector == "taiwan_mops_financial_reports":
        return _normalize_fetch_result(fetch_taiwan_mops_financial_reports(source_config, source_cursor=source_cursor))
    if connector == "taiwan_mops_investor_conferences":
        return _normalize_fetch_result(fetch_taiwan_mops_investor_conferences(source_config, source_cursor=source_cursor))
    if connector == "tpex_infohub_company":
        return _normalize_fetch_result(fetch_tpex_infohub_company(source_config))
    if connector == "tsmc_quarterly":
        return _normalize_fetch_result(fetch_tsmc_quarterly(source_config))
    if connector == "sec_edgar":
        cfg = dict(source_config or {})
        if source_cursor:
            cfg["_source_cursor"] = dict(source_cursor)
        return _normalize_fetch_result(fetch_sec_edgar(cfg))
    if connector == "sec_edgar_rss":
        return _normalize_fetch_result(fetch_sec_edgar_rss(source_config))
    if connector == "sedarplus_search":
        return _normalize_fetch_result(fetch_sedarplus_search(source_config, source_cursor=source_cursor))
    if connector == "sec_ipo_filings":
        return _normalize_fetch_result(fetch_sec_ipo_filings(source_config))
    if connector == "nasdaq_ipo_calendar":
        return _normalize_fetch_result(fetch_nasdaq_ipo_calendar(source_config))
    if connector == "asx_ipo_calendar":
        return _normalize_fetch_result(fetch_asx_ipo_calendar(source_config))
    if connector == "euronext_ipo_calendar":
        return _normalize_fetch_result(fetch_euronext_ipo_calendar(source_config))
    if connector == "jpx_ipo_calendar":
        return _normalize_fetch_result(fetch_jpx_ipo_calendar(source_config))
    if connector == "hkex_ipo_calendar":
        return _normalize_fetch_result(fetch_hkex_ipo_calendar(source_config))
    if connector == "krx_ipo_calendar":
        return _normalize_fetch_result(fetch_krx_ipo_calendar(source_config))
    if connector == "twse_ipo_calendar":
        return _normalize_fetch_result(fetch_twse_ipo_calendar(source_config))
    if connector == "sec_companyfacts":
        return _normalize_fetch_result(fetch_sec_companyfacts(source_config, source_cursor=source_cursor))
    if connector == "gmail_substack":
        return _normalize_fetch_result(fetch_gmail_substack(source_config, source_cursor=source_cursor))
    if connector == "substack_inbox_browser":
        cfg = dict(source_config or {})
        if source_cursor:
            cfg["_source_cursor"] = dict(source_cursor)
        return _normalize_fetch_result(_run_playwright(fetch_substack_inbox_browser, cfg))
    if connector == "note_creator_browser":
        return _normalize_fetch_result(_run_playwright(fetch_note_creator_browser, source_config))
    if connector == "google_drive_folder":
        return _normalize_fetch_result(fetch_google_drive_folder(source_config, source_cursor=source_cursor))
    if connector == "local_research_library":
        return _normalize_fetch_result(fetch_local_research_library(source_config))
    if connector == "trade_us_census":
        return _normalize_fetch_result(fetch_trade_us_census(source_config))
    if connector == "trade_taiwan_customs":
        return _normalize_fetch_result(fetch_trade_taiwan_customs(source_config))
    if connector == "trade_korea_customs":
        return _normalize_fetch_result(fetch_trade_korea_customs(source_config))
    if connector == "trade_japan_trade_stats":
        return _normalize_fetch_result(fetch_trade_japan_trade_stats(source_config))
    if connector == "trade_eurostat_comext":
        return _normalize_fetch_result(fetch_trade_eurostat_comext(source_config))
    if connector == "trade_usitc_dataweb":
        return _normalize_fetch_result(fetch_trade_usitc_dataweb(source_config))
    if connector == "trade_un_comtrade":
        return _normalize_fetch_result(fetch_trade_un_comtrade(source_config))
    if connector == "cloudflare_crawl_discovery":
        return _normalize_fetch_result(fetch_cloudflare_crawl_discovery(source_config))
    if connector == "cninfo_announcements":
        cfg = dict(source_config or {})
        if source_cursor:
            cfg["_source_cursor"] = dict(source_cursor)
        return _normalize_fetch_result(fetch_cninfo_announcements(cfg))
    if connector == "asx_announcements":
        return _normalize_fetch_result(fetch_asx_announcements(source_config, source_cursor=source_cursor))
    if connector == "bursa_announcements":
        return _normalize_fetch_result(fetch_bursa_announcements(source_config, source_cursor=source_cursor))
    if connector == "kazakhstan_announcements":
        return _normalize_fetch_result(fetch_kazakhstan_announcements(source_config, source_cursor=source_cursor))
    if connector == "sgx_announcements":
        return _normalize_fetch_result(fetch_sgx_announcements(source_config, source_cursor=source_cursor))
    if connector == "nse_corporate_announcements":
        return _normalize_fetch_result(fetch_nse_corporate_announcements(source_config, source_cursor=source_cursor))
    if connector == "twse_openapi":
        return _normalize_fetch_result(fetch_twse_openapi(source_config))
    if connector == "krx_kind_disclosure":
        return _normalize_fetch_result(fetch_krx_kind_disclosure(source_config, source_cursor=source_cursor))
    if connector == "hkexnews_title_search":
        cfg = dict(source_config or {})
        if source_cursor:
            cfg["_source_cursor"] = dict(source_cursor)
        return _normalize_fetch_result(fetch_hkexnews_title_search(cfg))
    if connector == "tdnet_company_announcements":
        return _normalize_fetch_result(fetch_tdnet_company_announcements(source_config, source_cursor=source_cursor))
    if connector == "edinet_disclosures":
        return _normalize_fetch_result(fetch_edinet_disclosures(source_config))
    if connector == "edinetdb_financials":
        return _normalize_fetch_result(fetch_edinetdb_financials(source_config, source_cursor=source_cursor))
    if connector == "opendart_disclosures":
        return _normalize_fetch_result(fetch_opendart_disclosures(source_config, source_cursor=source_cursor))
    if connector == "opendart_financials":
        return _normalize_fetch_result(fetch_opendart_financials(source_config, source_cursor=source_cursor))
    if connector == "eu_ted_search":
        return _normalize_fetch_result(fetch_eu_ted_search(source_config))
    if connector == "sam_gov_contracts":
        return _normalize_fetch_result(fetch_sam_gov_contracts(source_config))
    if connector == "gsa_calc_ceiling_rates":
        return _normalize_fetch_result(fetch_gsa_calc_ceiling_rates(source_config))
    if connector == "patentsview_search":
        return _normalize_fetch_result(fetch_patentsview_search(source_config))
    if connector == "eia_860m":
        return _normalize_fetch_result(fetch_eia_860m(source_config))
    if connector == "pjm_planning_center":
        return _normalize_fetch_result(fetch_pjm_planning_center(source_config))
    if connector == "caiso_interconnection_queue":
        return _normalize_fetch_result(fetch_caiso_interconnection_queue(source_config))
    if connector == "ercot_large_load":
        return _normalize_fetch_result(fetch_ercot_large_load(source_config))
    if connector == "ercot_llwg":
        return _normalize_fetch_result(fetch_ercot_llwg(source_config))
    if connector == "copernicus_browser":
        return _normalize_fetch_result(fetch_copernicus_browser(source_config))
    if connector == "usgs_earthexplorer":
        return _normalize_fetch_result(fetch_usgs_earthexplorer(source_config))
    if connector == "usgs_naip":
        return _normalize_fetch_result(fetch_usgs_naip(source_config))
    if connector == "cbp_cross":
        return _normalize_fetch_result(fetch_cbp_cross(source_config))
    if connector == "usitc_hts":
        return _normalize_fetch_result(fetch_usitc_hts(source_config))
    if connector == "eu_taric":
        return _normalize_fetch_result(fetch_eu_taric(source_config))
    if connector == "fcc_oet_kdb":
        return _normalize_fetch_result(fetch_fcc_oet_kdb(source_config))
    if connector == "chips_funding":
        return _normalize_fetch_result(fetch_chips_funding(source_config))
    raise ValueError(f"Unknown connector: {connector}")


def fetch_items(connector: str, *, source_config: dict) -> list[dict]:
    return fetch_items_result(connector, source_config=source_config, source_cursor=None).items
