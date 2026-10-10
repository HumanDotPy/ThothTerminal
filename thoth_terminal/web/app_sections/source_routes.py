from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


@app.get("/sources/setup", response_class=HTMLResponse)
def source_setup(request: Request) -> HTMLResponse:
    from thoth_terminal.source_starter_packs import starter_pack_payloads

    return _template_response(
        "source_setup.html",
        {"request": request, "packs": starter_pack_payloads()},
    )


@app.post("/sources/setup")
def source_setup_install(
    pack_id: str = Form(...),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    from thoth_terminal.jobs import enqueue_ingest_job
    from thoth_terminal.source_starter_packs import install_starter_pack

    try:
        installed, created = install_starter_pack(db, pack_id=pack_id)
        for source in installed:
            enqueue_ingest_job(db, source_id=int(source.id), priority=1)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _sources_redirect_with_notice(error=str(exc))
    return _sources_redirect_with_notice(
        notice=(
            f"Starter pack ready: {created} source(s) added, "
            f"{len(installed)} connection check(s) queued."
        )
    )


@app.get("/sources", response_class=HTMLResponse)
def sources(
    request: Request,
    include_deprecated: bool = False,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    return _template_response(
        "sources.html",
        {
            "request": request,
            "rows": _source_health_rows(db, include_deprecated=bool(include_deprecated)),
            "include_deprecated": bool(include_deprecated),
            "notice": str(request.query_params.get("notice") or "").strip() or None,
            "error": str(request.query_params.get("error") or "").strip() or None,
        },
    )


@app.get("/sources/new", response_class=HTMLResponse)
def source_new(request: Request) -> HTMLResponse:
    preset_connector = str(request.query_params.get("preset") or "").strip() or None
    preset_url = ""
    if preset_connector == "substack_inbox_browser":
        preset_url = "https://substack.com/inbox?utm_source=user-menu"
    elif preset_connector == "note_creator_browser":
        preset_url = "https://note.com/paul1211"
    return _template_response(
        "source_edit.html",
        {
            "request": request,
            "source": None,
            "preset_connector": preset_connector,
            "preset_url": preset_url,
            "source_profile": None,
        },
    )


_SOURCE_DATA_API_CONNECTORS = {
    "taiwan_mops_monthly_revenue",
    "taiwan_mops_investor_conferences",
    "tpex_infohub_company",
    "twse_openapi",
    "krx_kind_disclosure",
    "hkexnews_title_search",
    "cninfo_announcements",
    "tdnet_company_announcements",
    "edinet_disclosures",
    "edinetdb_financials",
    "opendart_disclosures",
    "opendart_financials",
    "sec_edgar_rss",
    "sec_companyfacts",
    "eu_ted_search",
    "sam_gov_contracts",
    "gsa_calc_ceiling_rates",
    "patentsview_search",
    "trade_usitc_dataweb",
    "trade_un_comtrade",
    "local_research_library",
    "eia_860m",
    "pjm_planning_center",
    "caiso_interconnection_queue",
    "ercot_large_load",
    "ercot_llwg",
    "copernicus_browser",
    "usgs_earthexplorer",
    "usgs_naip",
    "cbp_cross",
    "usitc_hts",
    "eu_taric",
    "fcc_oet_kdb",
    "chips_funding",
}

_SOURCE_NON_ARTICLE_CONNECTORS = {
    "gmail_substack",
    "google_drive_folder",
    "cloudflare_crawl_discovery",
    "taiwan_mops_monthly_revenue",
    "taiwan_mops_investor_conferences",
    "tpex_infohub_company",
    "twse_openapi",
    "krx_kind_disclosure",
    "hkexnews_title_search",
    "cninfo_announcements",
    "tdnet_company_announcements",
    "edinet_disclosures",
    "edinetdb_financials",
    "opendart_disclosures",
    "opendart_financials",
    "sec_edgar_rss",
    "sec_companyfacts",
    "eu_ted_search",
    "sam_gov_contracts",
    "gsa_calc_ceiling_rates",
    "patentsview_search",
    "trade_usitc_dataweb",
    "trade_un_comtrade",
    "local_research_library",
    "eia_860m",
    "pjm_planning_center",
    "caiso_interconnection_queue",
    "ercot_large_load",
    "ercot_llwg",
    "copernicus_browser",
    "usgs_earthexplorer",
    "usgs_naip",
    "cbp_cross",
    "usitc_hts",
    "eu_taric",
    "fcc_oet_kdb",
    "chips_funding",
}

_SOURCE_BROWSER_CONNECTORS = {
    "x_list",
    "web_list_browser",
    "substack_inbox_browser",
    "note_creator_browser",
}

_SOURCE_PAID_READER_CONNECTORS = {
    "substack_inbox_browser",
    "note_creator_browser",
}


_FUTURE_ELECTRONICS_DEFAULT_REPORT_URL = "https://www.futureelectronics.com/resources/market-conditions-report/passives"
_FUTURE_ELECTRONICS_DEFAULT_PRODUCT_LIST_URL = (
    "https://www.futureelectronics.com/c/passives/products?q=%3Arelevance%3AinStockFlag%3Atrue&text="
)


@app.post("/sources/new")
def source_create(
    name: str = Form(...),
    category: str = Form("web"),
    priority: int = Form(2),
    polling_interval_minutes: str | None = Form(None),
    poll_min_minutes: str | None = Form(None),
    poll_max_minutes: str | None = Form(None),
    active_window_start: str | None = Form(None),
    active_window_end: str | None = Form(None),
    active_window_tz: str | None = Form(None),
    connector: str = Form("web_list"),
    url: str = Form(""),
    include_url_regex: str | None = Form(None),
    exclude_url_regex: str | None = Form(None),
    include_keywords: str | None = Form(None),
    exclude_keywords: str | None = Form(None),
    allowed_hosts: str | None = Form(None),
    browser_storage_state_path: str | None = Form(None),
    browser_channel: str | None = Form(None),
    browser_user_data_dir: str | None = Form(None),
    browser_profile_directory: str | None = Form(None),
    browser_slow_mo_ms: str | None = Form(None),
    browser_wait_ms: str | None = Form(None),
    browser_scrolls: str | None = Form(None),
    accept_cookies_selector: str | None = Form(None),
    browser_fetch_articles: bool = Form(False),
    gmail_label_name: str | None = Form(None),
    gmail_query: str | None = Form(None),
    gmail_require_label: bool = Form(True),
    gmail_allow_publications: str | None = Form(None),
    gmail_deny_publications: str | None = Form(None),
    gmail_max_messages_per_run: str | None = Form(None),
    drive_folder_id: str | None = Form(None),
    drive_recursive: bool = Form(True),
    drive_include_shared_drives: bool = Form(True),
    drive_mime_allowlist: str | None = Form(None),
    drive_max_files_per_run: str | None = Form(None),
    drive_max_file_bytes: str | None = Form(None),
    download_article_images: bool = Form(False),
    ocr_enabled: bool = Form(False),
    ocr_backend: str | None = Form(None),
    ocr_languages: str | None = Form(None),
    refresh_existing: bool = Form(False),
    scrolls: int = Form(4),
    include_thread: bool = Form(True),
    max_thread_replies: int = Form(6),
    headless: bool = Form(True),
    include_quoted: bool = Form(True),
    include_articles: bool = Form(True),
    retry_headed_on_fail: bool = Form(True),
    retry_headed_on_empty: bool = Form(True),
    enabled: bool = Form(True),
    disabled_reason: str | None = Form(None),
    freshness_sla_minutes: str | None = Form(None),
    source_family: str | None = Form(None),
    rights_class: str | None = Form("public"),
    acquisition_mode: str | None = Form("connector"),
    cadence_label: str | None = Form(None),
    review_required: bool = Form(False),
    subject_binding_kind: str | None = Form(None),
    site_binding_kind: str | None = Form(None),
    jurisdiction_template_kind: str | None = Form(None),
    vendor_tier: str | None = Form(None),
    profile_notes: str | None = Form(None),
    extra_config_json: str | None = Form(None),
) -> RedirectResponse:
    cfg: dict = {
        "connector": connector,
        "include_url_regex": include_url_regex or None,
        "exclude_url_regex": exclude_url_regex or None,
        "include_keywords": _split_csv(include_keywords),
        "exclude_keywords": _split_csv(exclude_keywords),
        "allowed_hosts": _split_csv(allowed_hosts),
        "max_items_per_run": 25,
        "fetch_article": True,
        "download_article_images": bool(download_article_images),
        "ocr_enabled": bool(ocr_enabled),
        "store_raw_html_on_failure": True,
        "refresh_existing": True if connector == "future_electronics" else bool(refresh_existing),
        "default_domains": [],
        "default_tags": [],
        "default_event_types": [],
    }
    if connector == "rss":
        cfg["feed_url"] = url.strip()
    elif connector == "skhynix_blog_api":
        cfg["api_url"] = url.strip()
    elif connector == "future_electronics":
        requested_url = url.strip()
        cfg.pop("feed_url", None)
        cfg.pop("api_url", None)
        cfg.pop("data_url", None)
        cfg["report_url"] = (
            requested_url
            if "/resources/market-conditions-report/" in requested_url
            else _FUTURE_ELECTRONICS_DEFAULT_REPORT_URL
        )
        cfg["product_list_url"] = (
            requested_url
            if "/c/passives/products" in requested_url
            else _FUTURE_ELECTRONICS_DEFAULT_PRODUCT_LIST_URL
        )
        cfg["list_url"] = cfg["report_url"]
        cfg.setdefault("product_page_size", 100)
        cfg.setdefault("max_product_pages_per_run", 3)
        cfg.setdefault("max_detail_products_per_run", 25)
        cfg.setdefault("fetch_product_details", True)
        cfg.setdefault("download_report_pdf", True)
        cfg.setdefault("emit_product_documents", False)
        cfg.setdefault("dataset_slug", "future-electronics-product-availability")
        cfg.setdefault("refresh_existing", True)
    elif connector.startswith("trade_") or connector in _SOURCE_DATA_API_CONNECTORS:
        cfg.pop("feed_url", None)
        cfg.pop("list_url", None)
        cfg["data_url"] = url.strip()
        cfg["fetch_article"] = False
        cfg["download_article_images"] = False
    elif connector == "cloudflare_crawl_discovery":
        cfg.pop("feed_url", None)
        cfg["start_url"] = url.strip()
        cfg["list_url"] = url.strip()
        cfg["fetch_article"] = False
        cfg["download_article_images"] = False
    elif connector == "gmail_substack":
        cfg.pop("feed_url", None)
        cfg.pop("api_url", None)
        cfg.pop("list_url", None)
        cfg["gmail_label_name"] = (gmail_label_name or "Thoth/Substack").strip() or "Thoth/Substack"
        cfg["gmail_query"] = (gmail_query or "").strip() or None
        cfg["require_label"] = bool(gmail_require_label)
        cfg["allow_publications"] = _split_csv(gmail_allow_publications)
        cfg["deny_publications"] = _split_csv(gmail_deny_publications)
        cfg["store_email_html_attachment"] = True
        cfg["store_inline_images"] = False
        cfg["store_binary_attachments"] = True
        if gmail_max_messages_per_run and str(gmail_max_messages_per_run).strip():
            try:
                cfg["max_messages_per_run"] = int(float(gmail_max_messages_per_run))
            except Exception:
                pass
        cfg["fetch_article"] = False
        cfg["download_article_images"] = False
    elif connector == "google_drive_folder":
        cfg.pop("feed_url", None)
        cfg.pop("api_url", None)
        cfg.pop("list_url", None)
        cfg["folder_id"] = (drive_folder_id or "").strip()
        cfg["recursive"] = bool(drive_recursive)
        cfg["include_shared_drives"] = bool(drive_include_shared_drives)
        cfg["download_files"] = True
        cfg["extract_text"] = True
        cfg["store_binary_attachments"] = True
        cfg["ocr_enabled"] = bool(ocr_enabled)
        mime_allow = _split_csv(drive_mime_allowlist)
        if mime_allow:
            cfg["mime_allowlist"] = mime_allow
        if drive_max_files_per_run and str(drive_max_files_per_run).strip():
            try:
                cfg["max_files_per_run"] = int(float(drive_max_files_per_run))
            except Exception:
                pass
        if drive_max_file_bytes and str(drive_max_file_bytes).strip():
            try:
                cfg["max_file_bytes"] = int(float(drive_max_file_bytes))
            except Exception:
                pass
        cfg["fetch_article"] = False
        cfg["download_article_images"] = False
    else:
        cfg["list_url"] = url.strip()
        if connector in _SOURCE_BROWSER_CONNECTORS:
            cfg["headless"] = bool(headless)
            if browser_storage_state_path and browser_storage_state_path.strip():
                cfg["storage_state_path"] = browser_storage_state_path.strip()
            if browser_channel and browser_channel.strip():
                cfg["browser_channel"] = browser_channel.strip()
            if browser_user_data_dir and browser_user_data_dir.strip():
                cfg["user_data_dir"] = browser_user_data_dir.strip()
            if browser_profile_directory and browser_profile_directory.strip():
                cfg["profile_directory"] = browser_profile_directory.strip()
            if browser_slow_mo_ms and str(browser_slow_mo_ms).strip():
                try:
                    cfg["slow_mo_ms"] = int(float(browser_slow_mo_ms))
                except Exception:
                    pass
        if connector in _SOURCE_PAID_READER_CONNECTORS:
            cfg["require_authenticated_session"] = True
            cfg["skip_paywalled"] = True
            cfg["store_article_html_attachment"] = True
            cfg["browser_download_images"] = True
            cfg["append_image_manifest_to_text"] = True
            cfg["document_kind"] = "subscription_report"
            cfg["max_article_images"] = 40
            cfg["max_images_per_doc"] = 40
            cfg["max_image_bytes"] = 12_000_000
            cfg["refresh_existing"] = True
            cfg["fetch_article"] = True
            if cfg.get("user_data_dir") or cfg.get("browser_user_data_dir"):
                cfg["allow_persistent_profile_auth"] = True
            if connector == "substack_inbox_browser":
                cfg["allowed_hosts"] = _split_csv(allowed_hosts) or ["substack.com", "*.substack.com"]
                if not cfg.get("list_url"):
                    cfg["list_url"] = "https://substack.com/inbox?utm_source=user-menu"
            elif connector == "note_creator_browser":
                cfg["allowed_hosts"] = _split_csv(allowed_hosts) or ["note.com"]
                parsed_path = urlparse(url.strip()).path.strip("/")
                if parsed_path:
                    cfg["creator_slug"] = parsed_path.split("/", 1)[0]
    if connector == "x_list":
        cfg["scrolls"] = int(scrolls)
        cfg["include_thread"] = bool(include_thread)
        cfg["max_thread_replies"] = int(max_thread_replies)
        cfg["include_quoted"] = bool(include_quoted)
        cfg["include_articles"] = bool(include_articles)
    elif connector != "google_drive_folder":
        cfg.pop("folder_id", None)
        cfg.pop("recursive", None)
        cfg.pop("include_shared_drives", None)
        cfg.pop("download_files", None)
        cfg.pop("extract_text", None)
        cfg.pop("max_files_per_run", None)
        cfg.pop("max_file_bytes", None)
        cfg.pop("mime_allowlist", None)
    if connector != "gmail_substack":
        cfg.pop("gmail_label_name", None)
        cfg.pop("gmail_query", None)
        cfg.pop("gmail_label_id", None)
        cfg.pop("require_label", None)
        cfg.pop("allow_publications", None)
        cfg.pop("deny_publications", None)
        cfg.pop("max_messages_per_run", None)
        cfg.pop("store_email_html_attachment", None)
        cfg.pop("store_inline_images", None)
    if ocr_backend and ocr_backend.strip():
        cfg["ocr_backend"] = ocr_backend.strip()
    else:
        cfg.pop("ocr_backend", None)
    if ocr_languages and ocr_languages.strip():
        cfg["ocr_languages"] = ocr_languages.strip()
    else:
        cfg.pop("ocr_languages", None)
    if connector in _SOURCE_BROWSER_CONNECTORS:
        cfg["retry_headed_on_fail"] = bool(retry_headed_on_fail)
        cfg["retry_headed_on_empty"] = bool(retry_headed_on_empty)
    if connector in {"web_list_browser", "substack_inbox_browser", "note_creator_browser"}:
        if browser_wait_ms and str(browser_wait_ms).strip():
            try:
                cfg["browser_wait_ms"] = int(float(browser_wait_ms))
            except Exception:
                pass
        if browser_scrolls and str(browser_scrolls).strip():
            try:
                cfg["browser_scrolls"] = int(float(browser_scrolls))
            except Exception:
                pass
        if accept_cookies_selector and accept_cookies_selector.strip():
            cfg["accept_cookies_selector"] = accept_cookies_selector.strip()
        cfg["browser_fetch_articles"] = bool(browser_fetch_articles)
    if connector in _SOURCE_PAID_READER_CONNECTORS:
        cfg.pop("browser_fetch_articles", None)
        source_family = source_family or "research"
        rights_class = "paid" if not rights_class or rights_class == "public" else rights_class
        acquisition_mode = (
            "authenticated_browser"
            if not acquisition_mode or acquisition_mode in {"connector", "manual_template"}
            else acquisition_mode
        )
        vendor_tier = vendor_tier or "premium"
        profile_notes = profile_notes or "Authenticated paid-reader source. Uses only the user's logged-in session and skips locked/paywalled pages."
    if connector in _SOURCE_NON_ARTICLE_CONNECTORS or connector.startswith("trade_"):
        cfg["fetch_article"] = False
        cfg["download_article_images"] = False
    cfg["enabled"] = bool(enabled)
    if disabled_reason:
        cfg["disabled_reason"] = disabled_reason.strip()
    if freshness_sla_minutes and freshness_sla_minutes.strip():
        try:
            cfg["freshness_sla_minutes"] = int(float(freshness_sla_minutes.strip()))
        except Exception:
            pass
    if jurisdiction_template_kind and jurisdiction_template_kind.strip():
        cfg["jurisdiction_template_kind"] = jurisdiction_template_kind.strip()
    else:
        cfg.pop("jurisdiction_template_kind", None)

    poll_min_s = _parse_minutes(poll_min_minutes)
    poll_max_s = _parse_minutes(poll_max_minutes)
    if poll_min_s:
        cfg["poll_min_seconds"] = poll_min_s
    if poll_max_s:
        cfg["poll_max_seconds"] = poll_max_s
    if not poll_min_s:
        cfg.pop("poll_min_seconds", None)
    if not poll_max_s:
        cfg.pop("poll_max_seconds", None)

    if active_window_start and active_window_start.strip():
        cfg["active_window_start"] = active_window_start.strip()
    else:
        cfg.pop("active_window_start", None)
    if active_window_end and active_window_end.strip():
        cfg["active_window_end"] = active_window_end.strip()
    else:
        cfg.pop("active_window_end", None)
    if active_window_tz and active_window_tz.strip():
        cfg["active_window_tz"] = active_window_tz.strip()
    else:
        cfg.pop("active_window_tz", None)
    if extra_config_json and extra_config_json.strip():
        try:
            extra_cfg = json.loads(extra_config_json)
        except Exception as exc:
            return _sources_redirect_with_notice(error=f"Invalid extra config JSON: {type(exc).__name__}")
        if not isinstance(extra_cfg, dict):
            return _sources_redirect_with_notice(error="Extra config JSON must decode to an object.")
        cfg.update(extra_cfg)
    cfg, _ = apply_noninteractive_browser_policy(name, cfg)
    cfg, _ = apply_source_deprecation_policy(name, cfg, category=category)

    interval_s = None
    if poll_min_s or poll_max_s:
        interval_s = poll_min_s or poll_max_s
    else:
        interval_s = _parse_minutes(polling_interval_minutes)

    if is_cleanup_candidate(name, cfg, category=category):
        return _sources_redirect_with_notice(
            error="Company/newsroom source definitions are retired. Historical coverage stays searchable, but new company-page sources are not added to the active catalog."
        )

    with session_scope() as session:
        src = Source(
            name=name.strip(),
            category=category.strip(),
            priority=int(priority),
            polling_interval_seconds=interval_s,
            config=cfg,
            created_at=utcnow(),
        )
        session.add(src)
        session.flush()
        if not session.query(SourceState).filter_by(source_id=src.id).first():
            session.add(
                SourceState(
                    source_id=src.id,
                    last_run_at=None,
                    last_success_at=None,
                    next_run_at=None,
                    consecutive_failures=0,
                    backoff_until=None,
                    cursor={},
                    updated_at=utcnow(),
                )
            )
        ensure_source_profile(
            session,
            src,
            **_source_profile_payload(
                source_family=source_family,
                rights_class=rights_class,
                acquisition_mode=acquisition_mode,
                cadence_label=cadence_label,
                review_required=review_required,
                subject_binding_kind=subject_binding_kind,
                site_binding_kind=site_binding_kind,
                jurisdiction_template_kind=jurisdiction_template_kind,
                vendor_tier=vendor_tier,
                profile_notes=profile_notes,
            ),
        )
    _invalidate_terminal_caches("sources:", "home:", "search:")
    return RedirectResponse(url="/sources", status_code=303)


@app.get("/sources/{source_id}", response_class=HTMLResponse)
def source_edit(request: Request, source_id: int, db: Session = Depends(get_session)) -> HTMLResponse:
    src = (
        db.query(Source)
        .options(joinedload(Source.state), joinedload(Source.profile))
        .filter_by(id=source_id)
        .first()
    )
    if not src or (src.config or {}).get("deleted"):
        return HTMLResponse("Not found", status_code=404)
    last_run = (
        db.query(IngestionRun)
        .filter_by(source_id=src.id)
        .order_by(IngestionRun.id.desc())
        .first()
    )
    st = src.state
    last_success_local, last_success_utc = _format_dt_pair(st.last_success_at if st else None)
    last_run_local, last_run_utc = _format_dt_pair(st.last_run_at if st else None)
    next_run_local, next_run_utc = _format_dt_pair(st.next_run_at if st else None)
    backoff_local, backoff_utc = _format_dt_pair(st.backoff_until if st else None)

    return _template_response(
        "source_edit.html",
        {
            "request": request,
            "source": src,
            "last_run": last_run,
            "last_success_local": last_success_local,
            "last_success_utc": last_success_utc,
            "last_run_local": last_run_local,
            "last_run_utc": last_run_utc,
            "next_run_local": next_run_local,
            "next_run_utc": next_run_utc,
            "backoff_local": backoff_local,
            "backoff_utc": backoff_utc,
            "source_profile": serialize_source_profile(src.profile),
        },
    )


@app.post("/sources/{source_id}")
def source_update(
    source_id: int,
    category: str = Form("web"),
    priority: int = Form(2),
    polling_interval_minutes: str | None = Form(None),
    poll_min_minutes: str | None = Form(None),
    poll_max_minutes: str | None = Form(None),
    active_window_start: str | None = Form(None),
    active_window_end: str | None = Form(None),
    active_window_tz: str | None = Form(None),
    connector: str = Form("web_list"),
    url: str = Form(""),
    include_url_regex: str | None = Form(None),
    exclude_url_regex: str | None = Form(None),
    include_keywords: str | None = Form(None),
    exclude_keywords: str | None = Form(None),
    allowed_hosts: str | None = Form(None),
    browser_storage_state_path: str | None = Form(None),
    browser_channel: str | None = Form(None),
    browser_user_data_dir: str | None = Form(None),
    browser_profile_directory: str | None = Form(None),
    browser_slow_mo_ms: str | None = Form(None),
    browser_wait_ms: str | None = Form(None),
    browser_scrolls: str | None = Form(None),
    accept_cookies_selector: str | None = Form(None),
    browser_fetch_articles: bool = Form(False),
    gmail_label_name: str | None = Form(None),
    gmail_query: str | None = Form(None),
    gmail_require_label: bool = Form(True),
    gmail_allow_publications: str | None = Form(None),
    gmail_deny_publications: str | None = Form(None),
    gmail_max_messages_per_run: str | None = Form(None),
    drive_folder_id: str | None = Form(None),
    drive_recursive: bool = Form(True),
    drive_include_shared_drives: bool = Form(True),
    drive_mime_allowlist: str | None = Form(None),
    drive_max_files_per_run: str | None = Form(None),
    drive_max_file_bytes: str | None = Form(None),
    max_items_per_run: int = Form(25),
    fetch_article: bool = Form(True),
    download_article_images: bool = Form(False),
    ocr_enabled: bool = Form(False),
    ocr_backend: str | None = Form(None),
    ocr_languages: str | None = Form(None),
    store_raw_html_on_failure: bool = Form(True),
    refresh_existing: bool = Form(False),
    scrolls: int = Form(4),
    include_thread: bool = Form(True),
    max_thread_replies: int = Form(6),
    headless: bool = Form(True),
    include_quoted: bool = Form(True),
    include_articles: bool = Form(True),
    retry_headed_on_fail: bool = Form(True),
    retry_headed_on_empty: bool = Form(True),
    enabled: bool = Form(True),
    disabled_reason: str | None = Form(None),
    freshness_sla_minutes: str | None = Form(None),
    source_family: str | None = Form(None),
    rights_class: str | None = Form("public"),
    acquisition_mode: str | None = Form("connector"),
    cadence_label: str | None = Form(None),
    review_required: bool = Form(False),
    subject_binding_kind: str | None = Form(None),
    site_binding_kind: str | None = Form(None),
    jurisdiction_template_kind: str | None = Form(None),
    vendor_tier: str | None = Form(None),
    profile_notes: str | None = Form(None),
    extra_config_json: str | None = Form(None),
) -> RedirectResponse:
    with session_scope() as session:
        src = session.query(Source).filter_by(id=source_id).first()
        if not src:
            return RedirectResponse(url="/sources", status_code=303)

        cfg = dict(src.config or {})
        cfg["connector"] = connector
        if connector == "rss":
            cfg.pop("list_url", None)
            cfg["feed_url"] = url.strip()
            cfg.pop("api_url", None)
            cfg.pop("data_url", None)
            cfg.pop("start_url", None)
        elif connector == "skhynix_blog_api":
            cfg.pop("list_url", None)
            cfg.pop("feed_url", None)
            cfg["api_url"] = url.strip()
            cfg.pop("data_url", None)
            cfg.pop("start_url", None)
        elif connector == "future_electronics":
            requested_url = url.strip()
            cfg.pop("feed_url", None)
            cfg.pop("api_url", None)
            cfg.pop("data_url", None)
            cfg.pop("start_url", None)
            cfg["report_url"] = (
                requested_url
                if "/resources/market-conditions-report/" in requested_url
                else cfg.get("report_url") or _FUTURE_ELECTRONICS_DEFAULT_REPORT_URL
            )
            cfg["product_list_url"] = (
                requested_url
                if "/c/passives/products" in requested_url
                else cfg.get("product_list_url") or _FUTURE_ELECTRONICS_DEFAULT_PRODUCT_LIST_URL
            )
            cfg["list_url"] = cfg["report_url"]
            cfg.setdefault("product_page_size", 100)
            cfg.setdefault("max_product_pages_per_run", 3)
            cfg.setdefault("max_detail_products_per_run", 25)
            cfg.setdefault("fetch_product_details", True)
            cfg.setdefault("download_report_pdf", True)
            cfg.setdefault("emit_product_documents", False)
            cfg.setdefault("dataset_slug", "future-electronics-product-availability")
        elif connector.startswith("trade_") or connector in _SOURCE_DATA_API_CONNECTORS:
            cfg.pop("list_url", None)
            cfg.pop("feed_url", None)
            cfg["data_url"] = url.strip()
            cfg.pop("api_url", None)
            cfg.pop("start_url", None)
            cfg["fetch_article"] = False
            cfg["download_article_images"] = False
        elif connector == "cloudflare_crawl_discovery":
            cfg.pop("feed_url", None)
            cfg.pop("api_url", None)
            cfg["start_url"] = url.strip()
            cfg["list_url"] = url.strip()
            cfg["fetch_article"] = False
            cfg["download_article_images"] = False
        elif connector == "gmail_substack":
            cfg.pop("list_url", None)
            cfg.pop("feed_url", None)
            cfg.pop("api_url", None)
            cfg["gmail_label_name"] = (gmail_label_name or "Thoth/Substack").strip() or "Thoth/Substack"
            cfg["gmail_query"] = (gmail_query or "").strip() or None
            cfg["require_label"] = bool(gmail_require_label)
            cfg["allow_publications"] = _split_csv(gmail_allow_publications)
            cfg["deny_publications"] = _split_csv(gmail_deny_publications)
            cfg["store_email_html_attachment"] = True
            cfg["store_inline_images"] = False
            cfg["store_binary_attachments"] = True
            if gmail_max_messages_per_run and str(gmail_max_messages_per_run).strip():
                try:
                    cfg["max_messages_per_run"] = int(float(gmail_max_messages_per_run))
                except Exception:
                    pass
            else:
                cfg.pop("max_messages_per_run", None)
            cfg["fetch_article"] = False
            cfg["download_article_images"] = False
        elif connector == "google_drive_folder":
            cfg.pop("list_url", None)
            cfg.pop("feed_url", None)
            cfg.pop("api_url", None)
            cfg["folder_id"] = (drive_folder_id or "").strip()
            cfg["recursive"] = bool(drive_recursive)
            cfg["include_shared_drives"] = bool(drive_include_shared_drives)
            cfg["download_files"] = True
            cfg["extract_text"] = True
            cfg["store_binary_attachments"] = True
            cfg["ocr_enabled"] = bool(ocr_enabled)
            mime_allow = _split_csv(drive_mime_allowlist)
            if mime_allow:
                cfg["mime_allowlist"] = mime_allow
            else:
                cfg.pop("mime_allowlist", None)
            if drive_max_files_per_run and str(drive_max_files_per_run).strip():
                try:
                    cfg["max_files_per_run"] = int(float(drive_max_files_per_run))
                except Exception:
                    pass
            else:
                cfg.pop("max_files_per_run", None)
            if drive_max_file_bytes and str(drive_max_file_bytes).strip():
                try:
                    cfg["max_file_bytes"] = int(float(drive_max_file_bytes))
                except Exception:
                    pass
            else:
                cfg.pop("max_file_bytes", None)
            cfg["fetch_article"] = False
            cfg["download_article_images"] = False
        else:
            cfg.pop("feed_url", None)
            cfg.pop("api_url", None)
            cfg["list_url"] = url.strip()
            if connector in _SOURCE_PAID_READER_CONNECTORS:
                cfg["require_authenticated_session"] = True
                cfg["skip_paywalled"] = True
                cfg["store_article_html_attachment"] = True
                cfg["browser_download_images"] = True
                cfg["append_image_manifest_to_text"] = True
                cfg["document_kind"] = "subscription_report"
                cfg["max_article_images"] = int(cfg.get("max_article_images") or 40)
                cfg["max_images_per_doc"] = int(cfg.get("max_images_per_doc") or 40)
                cfg["max_image_bytes"] = int(cfg.get("max_image_bytes") or 12_000_000)
                cfg["refresh_existing"] = True
                cfg["fetch_article"] = True
                if cfg.get("user_data_dir") or cfg.get("browser_user_data_dir"):
                    cfg["allow_persistent_profile_auth"] = True
                if connector == "substack_inbox_browser":
                    cfg["allowed_hosts"] = _split_csv(allowed_hosts) or ["substack.com", "*.substack.com"]
                elif connector == "note_creator_browser":
                    cfg["allowed_hosts"] = _split_csv(allowed_hosts) or ["note.com"]
                    parsed_path = urlparse(url.strip()).path.strip("/")
                    if parsed_path:
                        cfg["creator_slug"] = parsed_path.split("/", 1)[0]
        if connector in _SOURCE_BROWSER_CONNECTORS:
            cfg["headless"] = bool(headless)
            if browser_storage_state_path and browser_storage_state_path.strip():
                cfg["storage_state_path"] = browser_storage_state_path.strip()
            else:
                cfg.pop("storage_state_path", None)
            if browser_channel and browser_channel.strip():
                cfg["browser_channel"] = browser_channel.strip()
            else:
                cfg.pop("browser_channel", None)
            if browser_user_data_dir and browser_user_data_dir.strip():
                cfg["user_data_dir"] = browser_user_data_dir.strip()
            else:
                cfg.pop("user_data_dir", None)
            if browser_profile_directory and browser_profile_directory.strip():
                cfg["profile_directory"] = browser_profile_directory.strip()
            else:
                cfg.pop("profile_directory", None)
            if browser_slow_mo_ms and str(browser_slow_mo_ms).strip():
                try:
                    cfg["slow_mo_ms"] = int(float(browser_slow_mo_ms))
                except Exception:
                    pass
            else:
                cfg.pop("slow_mo_ms", None)
        else:
            cfg.pop("storage_state_path", None)
            cfg.pop("browser_channel", None)
            cfg.pop("user_data_dir", None)
            cfg.pop("profile_directory", None)
            cfg.pop("slow_mo_ms", None)
            cfg.pop("auto_open_on_auth_fail", None)
            cfg.pop("retry_headed_on_fail", None)
            cfg.pop("retry_headed_on_empty", None)
        if connector == "x_list":
            cfg["scrolls"] = int(scrolls)
            cfg["include_thread"] = bool(include_thread)
            cfg["max_thread_replies"] = int(max_thread_replies)
            cfg["include_quoted"] = bool(include_quoted)
            cfg["include_articles"] = bool(include_articles)
        elif connector != "google_drive_folder":
            cfg.pop("folder_id", None)
            cfg.pop("recursive", None)
            cfg.pop("include_shared_drives", None)
            cfg.pop("download_files", None)
            cfg.pop("extract_text", None)
            cfg.pop("max_files_per_run", None)
            cfg.pop("max_file_bytes", None)
            cfg.pop("mime_allowlist", None)
        if connector != "gmail_substack":
            cfg.pop("gmail_label_name", None)
            cfg.pop("gmail_query", None)
            cfg.pop("gmail_label_id", None)
            cfg.pop("require_label", None)
            cfg.pop("allow_publications", None)
            cfg.pop("deny_publications", None)
            cfg.pop("max_messages_per_run", None)
            cfg.pop("store_email_html_attachment", None)
            cfg.pop("store_inline_images", None)
        if connector in _SOURCE_BROWSER_CONNECTORS:
            cfg["retry_headed_on_fail"] = bool(retry_headed_on_fail)
            cfg["retry_headed_on_empty"] = bool(retry_headed_on_empty)
        if connector in {"web_list_browser", "substack_inbox_browser", "note_creator_browser"}:
            if browser_wait_ms and str(browser_wait_ms).strip():
                try:
                    cfg["browser_wait_ms"] = int(float(browser_wait_ms))
                except Exception:
                    pass
            else:
                cfg.pop("browser_wait_ms", None)
            if browser_scrolls and str(browser_scrolls).strip():
                try:
                    cfg["browser_scrolls"] = int(float(browser_scrolls))
                except Exception:
                    pass
            else:
                cfg.pop("browser_scrolls", None)
            if accept_cookies_selector and accept_cookies_selector.strip():
                cfg["accept_cookies_selector"] = accept_cookies_selector.strip()
            else:
                cfg.pop("accept_cookies_selector", None)
            cfg["browser_fetch_articles"] = bool(browser_fetch_articles)
        else:
            cfg.pop("browser_wait_ms", None)
            cfg.pop("browser_scrolls", None)
            cfg.pop("accept_cookies_selector", None)
            cfg.pop("browser_fetch_articles", None)

        cfg["include_url_regex"] = include_url_regex or None
        cfg["exclude_url_regex"] = exclude_url_regex or None
        cfg["include_keywords"] = _split_csv(include_keywords)
        cfg["exclude_keywords"] = _split_csv(exclude_keywords)
        cfg["allowed_hosts"] = _split_csv(allowed_hosts)
        cfg["max_items_per_run"] = int(max_items_per_run)
        cfg["fetch_article"] = bool(fetch_article)
        cfg["download_article_images"] = bool(download_article_images)
        cfg["ocr_enabled"] = bool(ocr_enabled)
        cfg["store_raw_html_on_failure"] = bool(store_raw_html_on_failure)
        cfg["refresh_existing"] = True if connector == "future_electronics" else bool(refresh_existing)
        if ocr_backend and ocr_backend.strip():
            cfg["ocr_backend"] = ocr_backend.strip()
        else:
            cfg.pop("ocr_backend", None)
        if ocr_languages and ocr_languages.strip():
            cfg["ocr_languages"] = ocr_languages.strip()
        else:
            cfg.pop("ocr_languages", None)
        if connector in _SOURCE_NON_ARTICLE_CONNECTORS or connector.startswith("trade_"):
            cfg["fetch_article"] = False
            cfg["download_article_images"] = False
        if connector in _SOURCE_PAID_READER_CONNECTORS:
            cfg["fetch_article"] = True
            cfg["download_article_images"] = bool(download_article_images)
            cfg["refresh_existing"] = True
            cfg["require_authenticated_session"] = True
            cfg["skip_paywalled"] = True
            cfg["store_article_html_attachment"] = True
            cfg["browser_download_images"] = True
            cfg["append_image_manifest_to_text"] = True
            cfg["document_kind"] = "subscription_report"
            cfg["max_article_images"] = int(cfg.get("max_article_images") or 40)
            cfg["max_images_per_doc"] = int(cfg.get("max_images_per_doc") or 40)
            cfg["max_image_bytes"] = int(cfg.get("max_image_bytes") or 12_000_000)
            if cfg.get("user_data_dir") or cfg.get("browser_user_data_dir"):
                cfg["allow_persistent_profile_auth"] = True
            if connector == "substack_inbox_browser":
                cfg["allowed_hosts"] = _split_csv(allowed_hosts) or ["substack.com", "*.substack.com"]
                if not cfg.get("list_url"):
                    cfg["list_url"] = "https://substack.com/inbox?utm_source=user-menu"
            elif connector == "note_creator_browser":
                cfg["allowed_hosts"] = _split_csv(allowed_hosts) or ["note.com"]
            cfg.pop("browser_fetch_articles", None)
            source_family = source_family or "research"
            rights_class = "paid" if not rights_class or rights_class == "public" else rights_class
            acquisition_mode = (
                "authenticated_browser"
                if not acquisition_mode or acquisition_mode in {"connector", "manual_template"}
                else acquisition_mode
            )
            vendor_tier = vendor_tier or "premium"
            profile_notes = profile_notes or "Authenticated paid-reader source. Uses only the user's logged-in session and skips locked/paywalled pages."

        cfg["enabled"] = bool(enabled)
        if disabled_reason:
            cfg["disabled_reason"] = disabled_reason.strip()
        else:
            cfg.pop("disabled_reason", None)

        if freshness_sla_minutes and freshness_sla_minutes.strip():
            try:
                cfg["freshness_sla_minutes"] = int(float(freshness_sla_minutes.strip()))
            except Exception:
                pass
        else:
            cfg.pop("freshness_sla_minutes", None)
        if jurisdiction_template_kind and jurisdiction_template_kind.strip():
            cfg["jurisdiction_template_kind"] = jurisdiction_template_kind.strip()
        else:
            cfg.pop("jurisdiction_template_kind", None)

        poll_min_s = _parse_minutes(poll_min_minutes)
        poll_max_s = _parse_minutes(poll_max_minutes)
        if poll_min_s:
            cfg["poll_min_seconds"] = poll_min_s
        else:
            cfg.pop("poll_min_seconds", None)
        if poll_max_s:
            cfg["poll_max_seconds"] = poll_max_s
        else:
            cfg.pop("poll_max_seconds", None)

        if active_window_start and active_window_start.strip():
            cfg["active_window_start"] = active_window_start.strip()
        else:
            cfg.pop("active_window_start", None)
        if active_window_end and active_window_end.strip():
            cfg["active_window_end"] = active_window_end.strip()
        else:
            cfg.pop("active_window_end", None)
        if active_window_tz and active_window_tz.strip():
            cfg["active_window_tz"] = active_window_tz.strip()
        else:
            cfg.pop("active_window_tz", None)
        if extra_config_json and extra_config_json.strip():
            try:
                extra_cfg = json.loads(extra_config_json)
            except Exception as exc:
                return _sources_redirect_with_notice(error=f"Invalid extra config JSON: {type(exc).__name__}")
            if not isinstance(extra_cfg, dict):
                return _sources_redirect_with_notice(error="Extra config JSON must decode to an object.")
            cfg.update(extra_cfg)
        cfg.pop("auto_open_on_auth_fail", None)
        cfg, _ = apply_noninteractive_browser_policy(src.name, cfg)
        cfg, _ = apply_source_deprecation_policy(src.name, cfg, category=category)
        if is_cleanup_candidate(src.name, cfg, category=category):
            retire_source_definition(session, src)
            _invalidate_terminal_caches("sources:", "home:", "search:")
            return _sources_redirect_with_notice(
                notice=f"Source {src.name} was retired from the active catalog. Historical documents remain searchable."
            )

        interval_s = None
        if poll_min_s or poll_max_s:
            interval_s = poll_min_s or poll_max_s
        else:
            interval_s = _parse_minutes(polling_interval_minutes)

        src.category = category.strip()
        src.priority = int(priority)
        src.polling_interval_seconds = interval_s
        src.config = cfg
        ensure_source_profile(
            session,
            src,
            **_source_profile_payload(
                source_family=source_family,
                rights_class=rights_class,
                acquisition_mode=acquisition_mode,
                cadence_label=cadence_label,
                review_required=review_required,
                subject_binding_kind=subject_binding_kind,
                site_binding_kind=site_binding_kind,
                jurisdiction_template_kind=jurisdiction_template_kind,
                vendor_tier=vendor_tier,
                profile_notes=profile_notes,
            ),
        )
        if not session.query(SourceState).filter_by(source_id=src.id).first():
            session.add(
                SourceState(
                    source_id=src.id,
                    last_run_at=None,
                    last_success_at=None,
                    next_run_at=None,
                    consecutive_failures=0,
                    backoff_until=None,
                    cursor={},
                    updated_at=utcnow(),
                )
            )

    _invalidate_terminal_caches("sources:", "home:", "search:")
    return RedirectResponse(url="/sources", status_code=303)
