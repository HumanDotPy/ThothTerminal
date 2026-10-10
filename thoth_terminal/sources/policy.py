from __future__ import annotations

from typing import Any


DEPRECATED_SOURCE_REASON = "deprecated: legacy newsroom source"

_DEPRECATED_NAME_TOKENS = (
    "newsroom",
)

_ALLOWLIST_NAMES = {
    "x_list_analysts",
    "reuters_ai",
    "reuters_taiwan",
    "trendforce_presscenter",
    "trendforce_news",
    "taiwan_mops_monthly_revenue",
    "taiwan_mops_investor_conferences",
    "tpex_infohub_company_pages",
    "opendart_official_filings",
    "opendart_korea_financials",
    "edinet_official_filings",
    "edinetdb_japan_company_financials",
    "tsmc_monthly_revenue",
    "tsmc_quarterly_results",
}

_STRUCTURED_HIGH_QUALITY_CONNECTORS = {
    "sec_edgar",
    "sec_edgar_rss",
    "sec_companyfacts",
    "opendart_disclosures",
    "opendart_financials",
    "edinet_disclosures",
    "edinetdb_financials",
    "taiwan_mops_monthly_revenue",
    "taiwan_mops_investor_conferences",
    "tpex_infohub_company",
    "tsmc_monthly_revenue",
    "tsmc_quarterly",
    "gmail_substack",
    "substack_inbox_browser",
    "note_creator_browser",
}

_NONINTERACTIVE_BROWSER_CONNECTORS = {
    "eurolandir_calendar",
    "ir_calendar",
    "note_creator_browser",
    "substack_inbox_browser",
    "web_list_browser",
}


def allows_interactive_browser(
    name: str | None,
    config: dict[str, Any] | None = None,
) -> bool:
    cfg = config or {}
    connector = str(cfg.get("connector") or "").strip().lower()
    if connector in {"substack_inbox_browser", "note_creator_browser"}:
        return bool(
            cfg.get("allow_interactive_browser")
            or cfg.get("allow_persistent_profile_auth")
            or cfg.get("user_data_dir")
            or cfg.get("browser_user_data_dir")
        )
    return connector == "x_list" and bool(cfg.get("allow_interactive_browser"))


def is_browser_automation_source(config: dict[str, Any] | None) -> bool:
    cfg = config or {}
    connector = str(cfg.get("connector") or "").strip().lower()
    if connector == "x_list":
        return False
    if connector in {"web_list_browser", "substack_inbox_browser", "note_creator_browser"}:
        return True
    return bool(cfg.get("use_browser"))


def is_source_name_policy_deprecated(name: str | None) -> bool:
    source_name = (name or "").strip().lower()
    if not source_name:
        return False
    if source_name in _ALLOWLIST_NAMES:
        return False
    return any(token in source_name for token in _DEPRECATED_NAME_TOKENS)


def is_source_policy_deprecated(
    name: str | None,
    config: dict[str, Any] | None = None,
    *,
    category: str | None = None,
) -> bool:
    source_name = (name or "").strip().lower()
    if not source_name:
        return False
    if source_name in _ALLOWLIST_NAMES:
        return False
    cfg = config or {}
    cat = (category or "").strip().lower()
    if cat == "company":
        return False
    if cfg.get("company_slug"):
        return False
    return is_source_name_policy_deprecated(name)


def should_force_noninteractive_browser(
    name: str | None,
    config: dict[str, Any] | None,
) -> bool:
    cfg = config or {}
    connector = str(cfg.get("connector") or "").strip().lower()
    if allows_interactive_browser(name, cfg):
        return False
    if connector == "x_list":
        return True
    if connector in _NONINTERACTIVE_BROWSER_CONNECTORS:
        return True
    return connector == "web_list" and bool(cfg.get("use_browser"))


def apply_source_deprecation_policy(
    name: str | None,
    config: dict[str, Any] | None,
    *,
    category: str | None = None,
) -> tuple[dict[str, Any], bool]:
    cfg = dict(config or {})
    changed = False
    should_deprecate = is_source_policy_deprecated(name, cfg, category=category)

    if should_deprecate:
        if cfg.get("enabled") is not False:
            cfg["enabled"] = False
            changed = True
        if cfg.get("disabled_reason") != DEPRECATED_SOURCE_REASON:
            cfg["disabled_reason"] = DEPRECATED_SOURCE_REASON
            changed = True
        if cfg.get("deprecated") is not True:
            cfg["deprecated"] = True
            changed = True
        return cfg, changed

    # Clear only policy-owned fields when a source is allowlisted or otherwise not deprecated.
    if cfg.get("disabled_reason") == DEPRECATED_SOURCE_REASON:
        cfg.pop("disabled_reason", None)
        changed = True
    if cfg.get("deprecated") is True:
        cfg.pop("deprecated", None)
        changed = True
    if cfg.get("enabled") is False and not cfg.get("disabled_reason"):
        cfg["enabled"] = True
        changed = True
    return cfg, changed


def apply_noninteractive_browser_policy(
    name: str | None,
    config: dict[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    cfg = dict(config or {})
    changed = False
    if not should_force_noninteractive_browser(name, cfg):
        return cfg, changed

    if cfg.get("headless") is not True:
        cfg["headless"] = True
        changed = True
    if cfg.get("retry_headed_on_fail") is not False:
        cfg["retry_headed_on_fail"] = False
        changed = True
    if cfg.get("retry_headed_on_empty") is not False:
        cfg["retry_headed_on_empty"] = False
        changed = True
    if cfg.get("auto_open_on_auth_fail") is not False:
        cfg["auto_open_on_auth_fail"] = False
        changed = True
    return cfg, changed


def is_source_deprecated_config(config: dict[str, Any] | None) -> bool:
    cfg = config or {}
    return bool(cfg.get("deprecated")) or cfg.get("disabled_reason") == DEPRECATED_SOURCE_REASON
