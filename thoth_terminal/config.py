from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from thoth_terminal.runtime_env import assert_paths_not_on_forbidden_drives, normalize_path, repo_root


_REPO_ROOT = repo_root()
_ENV_FILE = _REPO_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="THOTH_",
        env_file=str(_ENV_FILE),
        extra="ignore",
        populate_by_name=True,
    )

    data_dir: Path = Field(default=Path("data"))
    db_path: Path = Field(default=Path("data/thoth_clean.db"))
    objects_dir: Path = Field(default=Path("data/objects"))
    runtime_state_dir: Path = Field(default=Path("data/runtime"))
    google_oauth_client_secrets_path: Path = Field(default=Path("data/integrations/google/client_secrets.json"))
    google_oauth_token_path: Path = Field(default=Path("data/integrations/google/oauth_token.json"))

    mode: str = Field(default="local_desktop", validation_alias=AliasChoices("THOTH_MODE", "MODE"))
    host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("THOTH_HOST", "THOTH_BIND_HOST", "BIND_HOST", "HOST"),
    )
    port: int = 8750
    allowed_origins: str | None = Field(
        default=None,
        validation_alias=AliasChoices("THOTH_ALLOWED_ORIGINS", "ALLOWED_ORIGINS"),
    )
    require_auth: bool = Field(
        default=True,
        validation_alias=AliasChoices("THOTH_REQUIRE_AUTH", "REQUIRE_AUTH"),
    )
    app_auth_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("THOTH_APP_AUTH_TOKEN", "APP_AUTH_TOKEN"),
    )
    public_build: bool = Field(
        default=False,
        validation_alias=AliasChoices("THOTH_PUBLIC_BUILD", "PUBLIC_BUILD"),
    )
    public_allow_unauthenticated: bool = Field(
        default=False,
        validation_alias=AliasChoices("THOTH_PUBLIC_ALLOW_UNAUTHENTICATED", "PUBLIC_ALLOW_UNAUTHENTICATED"),
    )
    enable_ibkr: bool = Field(
        default=False,
        validation_alias=AliasChoices("THOTH_ENABLE_IBKR", "ENABLE_IBKR"),
    )
    enable_trading: bool = Field(
        default=False,
        validation_alias=AliasChoices("THOTH_ENABLE_TRADING", "ENABLE_TRADING"),
    )
    enable_omori_local_assets: bool = Field(
        default=False,
        validation_alias=AliasChoices("THOTH_ENABLE_OMORI_LOCAL_ASSETS", "ENABLE_OMORI_LOCAL_ASSETS"),
    )
    database_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("THOTH_DATABASE_URL", "DATABASE_URL"),
    )
    duckdb_path: Path = Field(
        default=Path("data/analytics.duckdb"),
        validation_alias=AliasChoices("THOTH_DUCKDB_PATH", "DUCKDB_PATH"),
    )
    duckdb_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("THOTH_DUCKDB_ENABLED", "DUCKDB_ENABLED"),
    )
    duckdb_memory_limit: str = Field(
        default="1GB",
        validation_alias=AliasChoices("THOTH_DUCKDB_MEMORY_LIMIT", "DUCKDB_MEMORY_LIMIT"),
    )
    startup_db_quick_check: bool = Field(
        default=False,
        validation_alias=AliasChoices("THOTH_STARTUP_DB_QUICK_CHECK", "STARTUP_DB_QUICK_CHECK"),
    )
    postgres_connect_timeout_s: int = Field(
        default=5,
        validation_alias=AliasChoices("THOTH_POSTGRES_CONNECT_TIMEOUT_S", "POSTGRES_CONNECT_TIMEOUT_S"),
    )
    postgres_idle_in_transaction_timeout_ms: int = Field(
        default=120_000,
        validation_alias=AliasChoices(
            "THOTH_POSTGRES_IDLE_IN_TRANSACTION_TIMEOUT_MS",
            "POSTGRES_IDLE_IN_TRANSACTION_TIMEOUT_MS",
        ),
    )
    postgres_lock_timeout_ms: int = Field(
        default=30_000,
        validation_alias=AliasChoices("THOTH_POSTGRES_LOCK_TIMEOUT_MS", "POSTGRES_LOCK_TIMEOUT_MS"),
    )
    log_level: str = "INFO"
    runtime_primary_os: str = "windows"
    runtime_writer_policy: str = "windows_single_writer"
    runtime_allow_unsafe_wsl_writes: bool = False
    runtime_enforce_git_workspace: bool = True
    runtime_require_project_venv: bool = True
    forbidden_runtime_drives: str = Field(
        default="D:",
        validation_alias=AliasChoices("THOTH_FORBIDDEN_RUNTIME_DRIVES", "FORBIDDEN_RUNTIME_DRIVES"),
    )

    default_translation_backend: str = "googletrans"
    auto_translate_non_english: bool = True
    auto_translate_title_min_chars: int = 12
    auto_translate_min_confidence: float = 0.55
    ocr_default_backend: str = "tesseract"
    ocr_languages: str = "eng"
    ocr_max_images_per_doc: int = 6
    ocr_max_image_bytes: int = 5_000_000
    ocr_min_chars: int = 20
    document_refinery_enabled: bool = True
    document_refinery_max_input_chars: int = 6000
    document_refinery_max_key_points: int = 3

    # Triage system
    triage_unsave_on_ignore: bool = False
    auto_action_on_export: bool = True

    # Seen tracking
    enable_seen_tracking: bool = True
    seen_dwell_ms: int = 2000
    seen_scroll_window_s: int = 10
    seen_afk_s: int = 30

    # Notifications
    notification_poll_s: int = 20
    notification_sound_rate_s: int = 60
    notification_toast_rate_s: int = 25
    notification_require_idle: bool = True
    notification_idle_s: int = 18
    notification_dedupe_window_s: int = 12
    # Currency normalization
    base_currency: str = "USD"
    fx_enabled: bool = True
    fx_primary_provider: str = "frankfurter"
    fx_fallback_provider: str = "open_er_api"
    fx_update_max_age_hours: int = 36
    fx_required_currencies: list[str] = [
        "USD",
        "EUR",
        "GBP",
        "JPY",
        "CNY",
        "KRW",
        "HKD",
        "CAD",
        "TWD",
    ]

    # Scheduler defaults
    default_active_window_tz: str = "America/New_York"
    web_scheduler_enabled: bool = False
    earnings_nav_enabled: bool = True
    static_assets_version: str = "20260521_consensus_os_12"
    autosave_interval_ms: int = 750
    worker_scheduler_interval_s: int = 60
    worker_poll_interval_s: float = 2.0
    worker_idle_backoff_cap_s: float = 10.0
    worker_max_jobs_per_tick: int = 6
    worker_memory_recycle_mb: int = 2048
    worker_process_memory_limit_mb: int = 3072
    cloud_worker_process_memory_limit_mb: int = 3072
    agent_api_process_memory_limit_mb: int = 1024
    web_process_memory_limit_mb: int = 2048
    runtime_child_memory_poll_s: float = 0.25
    runtime_child_restart_max_delay_s: float = 30.0
    runtime_child_shutdown_timeout_s: float = 8.0
    worker_job_max_attempts: int = 5
    worker_job_stale_seconds: int = 900
    worker_ingest_job_stale_seconds: int = 7200
    worker_requeue_running_jobs_on_startup: bool = True
    # Browser-backed ingestion runs out of process so a wedged Playwright/Edge
    # tree cannot pin the ordinary worker.  The wall clock is intentionally
    # generous; the shorter stall lease is renewed only at meaningful stages.
    browser_ingestion_max_runtime_s: int = 7200
    browser_ingestion_stall_timeout_s: int = 1800
    browser_ingestion_memory_limit_mb: int = 2048
    browser_ingestion_min_available_memory_mb: int = 4096
    browser_ingestion_min_available_memory_percent: float = 15.0
    browser_ingestion_admission_wait_s: int = 600
    browser_ingestion_admission_poll_s: float = 5.0
    browser_failure_quarantine_threshold: int = 8
    browser_failure_quarantine_seconds: int = 86400
    browser_failure_quarantine_max_seconds: int = 604800

    # Capture integrations
    capture_allow_x_origins: bool = True
    x_storage_state_path: Path = Field(default=Path("data/x_storage_state.json"))
    x_browser_channel: str | None = None
    x_user_data_dir: Path | None = None
    x_link_expand_limit: int = 4
    mention_backfill_limit: int = 1000

    # Cloudflare Browser Rendering crawl
    cloudflare_account_id: str | None = None
    cloudflare_api_token: str | None = None
    opendart_api_key: str | None = None
    opendart_api_key_expires_at: str | None = None
    edinetdb_api_key: str | None = None
    edinetdb_api_key_expires_at: str | None = None
    edinet_subscription_key: str | None = None
    edinet_subscription_key_expires_at: str | None = None
    usitc_dataweb_api_key: str | None = None
    usitc_dataweb_api_key_expires_at: str | None = None
    un_comtrade_api_key: str | None = None
    un_comtrade_api_key_expires_at: str | None = None
    sam_gov_api_key: str | None = None
    sam_gov_api_key_expires_at: str | None = None
    api_key_expiry_warning_days: int = 14

    # Local research library / deep-research dropbox
    research_library_inbox_dir: Path = Field(default=Path("research/inbox"))
    research_library_max_files_per_run: int = 50
    research_report_full_refresh_days: int = 90
    research_report_addendum_extension_days: int = 45

    # MCP (Model Context Protocol)
    mcp_auth_token: str | None = None
    muse_owner_agent_token: str | None = None
    # Versioned, cache-stable MCP tool surfaces. ``full`` preserves the legacy
    # operator surface; narrower profiles expose hot schemas plus fixed cold-read
    # discovery/invocation gateways.
    mcp_tool_profile: str = "full"
    chatgpt_app_tool_profile: str = "research_desk_v1"
    chatgpt_app_token: str | None = None
    chatgpt_app_read_only_token: str | None = None
    desktop_control_url: str | None = None
    desktop_control_token: str | None = None
    google_oauth_redirect_base_url: str | None = None

    # Local noninteractive analyst agent
    agent_api_enabled: bool = False
    agent_api_host: str = "0.0.0.0"
    agent_api_port: int = 8765
    agent_api_token: str | None = None
    # Optional second credential for canonical research mutations.  The normal
    # Raider/local-worker token is intentionally proposal-only.
    agent_api_write_token: str | None = None
    agent_lookback_hours: int = 48
    agent_trace_retention_hours: int = 72
    agent_default_model: str = "qwen-3.5-9b"
    agent_llama_base_url: str = "http://127.0.0.1:8080"
    agent_llm_endpoint: str | None = None
    agent_llm_model: str | None = None
    agent_poll_interval_s: float = 120.0
    agent_batch_size: int = 4
    agent_prompt_version: str = "local-analyst-v6-observed-claim-routes"
    agent_auto_write_research_docs: bool = True

    # AI / LLM API settings
    ai_provider: str = "openai"  # openai, anthropic, openrouter, deepseek, nous, local
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-3-5-sonnet-20241022"
    openrouter_api_key: str | None = None
    openrouter_model: str = "openrouter/free"
    openrouter_free_model: str = "openrouter/free"
    openrouter_free_fallback_models: str = (
        "moonshotai/kimi-k2.6:free,"
        "nvidia/nemotron-3-ultra-550b-a55b:free,"
        "nvidia/nemotron-3-super-120b-a12b:free,"
        "deepseek/deepseek-v3.1-nex-n1:free,"
        "qwen/qwen3-next-80b-a3b-instruct:free,"
        "qwen/qwen3-coder:free,"
        "openai/gpt-oss-120b:free"
    )
    openrouter_prefer_free_models: bool = True
    openrouter_enforce_free_only: bool = True
    openrouter_free_retry_limit: int = 4
    deepseek_api_key: str | None = None
    deepseek_model: str = "deepseek-v4-flash"
    nous_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("NOUS_THOTH_TERMINAL_FREE", "THOTH_NOUS_API_KEY", "NOUS_API_KEY"),
    )
    nous_model: str = "Hermes-4.3-36B"
    nous_base_url: str = "https://inference-api.nousresearch.com/v1"
    meta_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MODEL_API_KEY", "THOTH_META_API_KEY", "META_API_KEY"),
    )
    meta_model: str = "muse-spark-1.3-contributor"
    meta_base_url: str = "https://api.meta.ai/v1"
    # Responses is required for prompt_cache_retention. Meta still performs
    # automatic prefix caching on Chat Completions, but only Responses accepts
    # the 24h retention hint.
    meta_api_mode: str = "responses"
    meta_prompt_cache_retention: str = "24h"
    meta_store_responses: bool = False
    artificial_analysis_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ARTIFICIAL_ANALYSIS_API_KEY", "THOTH_ARTIFICIAL_ANALYSIS_API_KEY"),
    )
    artificial_analysis_base_url: str = "https://artificialanalysis.ai/api/v2"
    artificial_analysis_enabled: bool = True
    local_llm_endpoint: str | None = None

    # OpenShell sandbox integration for cloud/API agents
    openshell_enabled: bool = True
    openshell_prefer_wsl: bool = True
    openshell_gateway_name: str = "openshell"
    openshell_require_gateway: bool = True
    openshell_status_timeout_s: float = 3.0
    openshell_default_sandbox_from: str = "python"
    openshell_default_policy_path: Path | None = None

    # Cloud write policy. Cloud agents may draft freely; durable research writes
    # require an explicit operator-approved path and, by default, OpenShell readiness.
    cloud_writes_enabled: bool = True
    cloud_writes_require_openshell: bool = True
    cloud_writes_require_operator_approval: bool = True
    cloud_writes_allow_report_addendum: bool = True
    cloud_writes_allow_candidate_requests: bool = True
    cloud_writes_allow_direct_report_version: bool = False

    # Cloud research automation
    cloud_research_automation_enabled: bool = True
    cloud_research_provider: str = "meta"
    cloud_research_model: str = "muse-spark-1.3-contributor"
    cloud_research_reasoning_effort: str = "xhigh"
    cloud_research_timeout_s: float = 3600.0
    cloud_research_max_input_chars: int = 160000
    cloud_research_max_output_tokens: int = 0
    # Scheduled research is consolidated into one cache-friendly steward. The
    # legacy modes remain callable on demand for compatibility and diagnostics.
    cloud_research_nightly_enabled: bool = False
    cloud_research_nightly_local_times: str = "12:10"
    cloud_research_nightly_lookback_hours: int = 72
    cloud_research_heartbeat_enabled: bool = True
    cloud_research_heartbeat_local_times: str = "08:00,20:00"
    # Provider-aware scheduling keeps discounted-window services such as
    # DeepSeek inside their price window, while flat-rate/rate-limited services
    # such as Meta Contributor distribute large heartbeats across the day.
    cloud_research_schedule_strategy: str = "provider_aware"
    cloud_research_distributed_providers: str = "meta"
    cloud_research_distributed_heartbeat_local_times: str = "08:00,20:00"
    cloud_research_off_peak_providers: str = "deepseek"
    # Provider broker: configured choices remain primary and explicit operator
    # choices remain pinned. Scheduled work may fail over only on health or
    # availability failure; provider-aware price windows defer instead.
    provider_broker_enabled: bool = True
    provider_broker_strategy: str = "configured_first"
    provider_broker_fallback_order: str = "meta,deepseek,nous,openrouter"
    provider_broker_health_lookback_hours: int = 24
    provider_broker_health_sample_size: int = 12
    provider_broker_failure_threshold: int = 2
    cloud_research_heartbeat_interval_hours: int = 12
    cloud_research_heartbeat_lookback_hours: int = 168
    cloud_research_inquisitive_enabled: bool = False
    cloud_research_inquisitive_local_times: str = "12:45,18:30"
    cloud_research_inquisitive_lookback_hours: int = 120
    cloud_research_no_change_gate_enabled: bool = True
    cloud_research_no_change_max_staleness_hours: int = 8
    # Proposal-only local-agent results remain available to the consolidated
    # heartbeat, but should not create one cloud review per ingestion by
    # default. This is an intentionally separate cost/latency control from the
    # scheduled cloud-research automation switch.
    local_agent_cloud_promotion_enabled: bool = False
    cloud_agent_memory_enabled: bool = True
    cloud_agent_memory_context_chars: int = 420000
    cloud_research_stable_state_enabled: bool = True
    cloud_research_stable_state_epoch_hours: int = 24
    cloud_research_stable_state_supply_mode: str = "analytical"
    cloud_research_stable_state_dataset_mode: str = "state_cards"
    cloud_research_stable_state_report_mode: str = "titles"
    cloud_research_stable_state_identity_mode: str = "routing"
    cloud_research_stable_state_include_search_atlas: bool = False
    cloud_research_stable_state_include_roadmap: bool = True
    cloud_research_stable_state_include_memory: bool = True
    cloud_research_volatile_state_enabled: bool = True
    cloud_research_volatile_state_epoch_hours: int = 24
    cloud_research_volatile_state_lookback_hours: int = 168
    cloud_research_volatile_state_max_items: int = 45
    cloud_research_volatile_state_local_limit: int = 160

    local_model_backend: str = "manual"  # manual, auto, vllm, sglang
    local_model_profile: str = "generic"  # generic, triattention
    local_model_host: str = "127.0.0.1"
    local_model_port: int = 8000
    local_model_api_key: str | None = None
    local_model_health_path: str = "/health"
    local_model_model_path: str | None = None
    local_model_served_model_name: str | None = None
    local_model_vllm_dtype: str = "bfloat16"
    local_model_vllm_gpu_memory_utilization: float = 0.90
    local_model_vllm_tensor_parallel_size: int = 1
    local_model_vllm_max_model_len: int = 32768
    local_model_vllm_max_num_batched_tokens: int = 4096
    local_model_vllm_max_num_seqs: int = 16
    local_model_vllm_enable_chunked_prefill: bool = True
    local_model_vllm_enable_prefix_caching: bool = False
    local_model_vllm_enforce_eager: bool = True
    local_model_vllm_trust_remote_code: bool = True
    local_model_triattention_sparse_stats_path: Path | None = None
    local_model_triattention_kv_budget: int = 12000
    local_model_triattention_divide_length: int = 128
    local_model_triattention_window_size: int = 128
    local_model_triattention_pruning_mode: str = "per_head"
    local_model_triattention_protect_prefill: bool = False
    local_model_triattention_enable_kv_compaction: bool = True
    local_model_triattention_enable_block_reclaim: bool = True
    local_model_autostart: bool = False
    local_model_autostart_services: str = "worker"
    local_model_autostart_wait_timeout_s: float = 0.0
    local_model_autostart_probe_timeout_s: float = 2.5

    # Markets / Quotes settings (optional)
    markets_enabled: bool = True
    markets_provider: str = "yahoo"  # yahoo, polygon, alpha_vantage
    markets_api_key: str | None = None
    markets_poll_interval_s: int = 60
    markets_default_watchlist: list[str] = [
        "SPY", "QQQ", "NVDA", "TSM", "AVGO", "AMD", "INTC",
        "MU", "AMAT", "LRCX", "ASML", "KLAC",
    ]

    # Discord bot (optional)
    discord_token: str | None = None
    discord_guild_id: int | None = None
    discord_exec_user_id: int | None = None
    discord_channel_id: int | None = None
    public_base_url: str | None = None
    public_base_url_restrict_to_public_routes: bool = True
    newsletter_enabled: bool = False
    newsletter_from_email: str = "Thoth Research <newsletter@example.com>"
    newsletter_reply_to: str | None = None
    newsletter_public_base_url: str | None = None
    newsletter_signing_secret: str | None = None
    newsletter_smtp_host: str | None = None
    newsletter_smtp_port: int = 587
    newsletter_smtp_username: str | None = None
    newsletter_smtp_password: str | None = None
    newsletter_smtp_starttls: bool = True
    newsletter_smtp_timeout_s: float = 10.0
    discord_baseline_delay_hours: int = 24
    discord_baseline_poll_seconds: int = 60
    discord_perf_cache_seconds: int = 600
    discord_yahoo_bulk_only: bool = True
    discord_watchlist_refresh_enabled: bool = True
    discord_watchlist_refresh_interval_minutes: int = 60
    discord_watchlist_immediate_refresh_delay_seconds: int = 8
    discord_transcript_scan_enabled: bool = False
    discord_transcript_scan_interval_minutes: int = 360
    discord_transcript_scan_immediate_enabled: bool = False
    discord_transcript_scan_lookback_days: int = 120
    transcript_scan_enabled: bool = True
    transcript_scan_interval_minutes: int = 360
    transcript_gap_scan_interval_minutes: int = 10080
    transcript_scan_lookback_days: int = 120
    transcript_gap_scan_lookback_days: int = 420
    transcript_scan_require_official_earnings_material: bool = True
    transcript_scan_max_candidates_per_run: int = 40
    transcript_scan_max_runtime_seconds: int = 240
    transcript_scan_retry_hours: int = 24
    discord_link_collect_enabled: bool = False
    discord_link_collect_interval_minutes: int = 10
    discord_link_collect_max_messages_per_run: int = 300
    discord_link_collect_max_links_per_run: int = 200
    discord_link_scan_channel_id: int | None = None
    discord_link_agentic_ingest_enabled: bool = False
    discord_link_agentic_confidence_min: float = 0.75
    discord_link_agentic_queue_threshold: float = 0.50
    discord_link_bootstrap_mode: str = "24h"
    discord_link_topic_rules_json: str | None = None
    discord_link_macro_proxy_rules_json: str | None = None
    discord_operator_commands_enabled: bool = False
    discord_contributor_role_ids: str | None = None
    discord_moderator_role_ids: str | None = None
    discord_operator_role_ids: str | None = None
    discord_ask_model: str | None = None
    discord_ask_context_limit: int = 8
    discord_ask_timeout_s: float = 30.0
    discord_market_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    discord_desk_enabled: bool = True
    discord_desk_thread_name: str = "Thoth Desk"
    discord_outbox_poll_seconds: int = 10
    discord_outbox_max_send_per_tick: int = 2
    discord_outbox_max_per_hour: int = 12
    discord_desk_digest_midday_enabled: bool = False
    discord_desk_digest_midday_hhmm: str = "12:15"
    discord_desk_digest_midday_lookback_hours: int = 8
    discord_desk_digest_hhmm: str = "21:00"
    discord_desk_digest_tz: str = "America/New_York"
    discord_desk_digest_days: str = "mon,tue,wed,thu,fri"
    discord_desk_digest_lookback_hours: int = 24
    discord_desk_digest_main_notice_enabled: bool = True

    # Agent (optional; uses local LLM endpoint only)
    agent_llm_endpoint: str | None = None
    agent_llm_model: str | None = None
    agent_llm_timeout_s: int = 60
    agent_llm_max_input_chars: int = 24000
    agent_execution_policy: str = "policy_routed"
    agent_preferred_synthesis_engine: str | None = None
    agent_codex_native_enabled: bool = False
    agent_chatgpt_app_enabled: bool = False
    agent_enabled: bool = False
    agent_auto_digest_enabled: bool = False
    agent_auto_watchlist_refresh: bool = False
    agent_context_default_lookback_hours: int = 24
    agent_context_max_items_per_section: int = 25
    agent_context_max_prompt_chars: int = 24000
    agent_daily_master_enabled: bool = True
    agent_daily_master_hhmm: str = "21:00"
    agent_daily_master_tz: str = "America/New_York"

    # SEC EDGAR settings (Rail 1: Statutory Filings)
    sec_edgar_contact_email: str = "admin@example.com"  # Required per SEC fair access
    sec_edgar_rate_limit_rps: float = 9.0  # Stay under 10/sec

    # Entity resolution settings
    openfigi_api_key: str | None = None  # Optional: higher rate limits with key
    gleif_api_enabled: bool = True

    # Dev mode (hides auth/profile features)
    dev_mode: bool = True

    # IBKR Client Portal Gateway (local-only, read-only adapter)
    ibkr_gateway_base_url: str = Field(
        default="https://localhost:5000/v1/api",
        validation_alias=AliasChoices("THOTH_IBKR_GATEWAY_BASE_URL", "IBKR_GATEWAY_BASE_URL"),
    )
    ibkr_clientportal_dir: Path = Field(
        default=Path.home() / "ibkrclientportal",
        validation_alias=AliasChoices("THOTH_IBKR_CLIENTPORTAL_DIR", "IBKR_CLIENTPORTAL_DIR"),
    )
    ibkr_verify_ssl: bool = Field(
        default=False,
        validation_alias=AliasChoices("THOTH_IBKR_VERIFY_SSL", "IBKR_VERIFY_SSL"),
    )
    ibkr_timeout_s: float = Field(
        default=12.0,
        validation_alias=AliasChoices("THOTH_IBKR_TIMEOUT_S", "IBKR_TIMEOUT_S"),
    )
    ibkr_mock_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("THOTH_IBKR_MOCK_ENABLED", "IBKR_MOCK_ENABLED"),
    )
    ibkr_symbol_conids_json: str | None = Field(
        default=None,
        validation_alias=AliasChoices("THOTH_IBKR_SYMBOL_CONIDS_JSON", "IBKR_SYMBOL_CONIDS_JSON"),
    )
    ibkr_default_account_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("THOTH_IBKR_DEFAULT_ACCOUNT_ID", "IBKR_DEFAULT_ACCOUNT_ID"),
    )
    ibkr_auth_backoff_s: float = Field(
        default=60.0,
        validation_alias=AliasChoices("THOTH_IBKR_AUTH_BACKOFF_S", "IBKR_AUTH_BACKOFF_S"),
    )
    ibkr_keep_gateway_running: bool = Field(
        default=False,
        validation_alias=AliasChoices("THOTH_IBKR_KEEP_GATEWAY_RUNNING", "IBKR_KEEP_GATEWAY_RUNNING"),
    )
    ibkr_gateway_presence_connected: bool = Field(
        default=False,
        validation_alias=AliasChoices("THOTH_IBKR_GATEWAY_PRESENCE_CONNECTED", "IBKR_GATEWAY_PRESENCE_CONNECTED"),
    )

    # Local Omori-inspired prototype asset roots. These are gitignored and public-mode forbidden.
    omori_local_asset_root: Path = Field(
        default=Path("local_assets/omori-media-files"),
        validation_alias=AliasChoices("THOTH_OMORI_LOCAL_ASSET_ROOT", "OMORI_LOCAL_ASSET_ROOT"),
    )
    omori_generated_asset_root: Path = Field(
        default=Path("generated_assets/omori-mood-runtime"),
        validation_alias=AliasChoices("THOTH_OMORI_GENERATED_ASSET_ROOT", "OMORI_GENERATED_ASSET_ROOT"),
    )
    omori_public_asset_root: Path = Field(
        default=Path("public/omori-local"),
        validation_alias=AliasChoices("THOTH_OMORI_PUBLIC_ASSET_ROOT", "OMORI_PUBLIC_ASSET_ROOT"),
    )


settings = Settings()
settings.data_dir = normalize_path(settings.data_dir, base_dir=_REPO_ROOT)
settings.db_path = normalize_path(settings.db_path, base_dir=_REPO_ROOT)
settings.duckdb_path = normalize_path(settings.duckdb_path, base_dir=_REPO_ROOT)
settings.objects_dir = normalize_path(settings.objects_dir, base_dir=_REPO_ROOT)
settings.runtime_state_dir = normalize_path(settings.runtime_state_dir, base_dir=_REPO_ROOT)
settings.ibkr_clientportal_dir = normalize_path(settings.ibkr_clientportal_dir, base_dir=_REPO_ROOT)
settings.google_oauth_client_secrets_path = normalize_path(
    settings.google_oauth_client_secrets_path, base_dir=_REPO_ROOT
)
settings.google_oauth_token_path = normalize_path(
    settings.google_oauth_token_path, base_dir=_REPO_ROOT
)
settings.omori_local_asset_root = normalize_path(settings.omori_local_asset_root, base_dir=_REPO_ROOT)
settings.omori_generated_asset_root = normalize_path(settings.omori_generated_asset_root, base_dir=_REPO_ROOT)
settings.omori_public_asset_root = normalize_path(settings.omori_public_asset_root, base_dir=_REPO_ROOT)
if settings.local_model_triattention_sparse_stats_path is not None:
    settings.local_model_triattention_sparse_stats_path = normalize_path(
        settings.local_model_triattention_sparse_stats_path,
        base_dir=_REPO_ROOT,
    )


def ensure_data_dirs() -> None:
    assert_paths_not_on_forbidden_drives(
        {
            "data_dir": settings.data_dir,
            "db_path": settings.db_path,
            "duckdb_path": settings.duckdb_path,
            "objects_dir": settings.objects_dir,
            "runtime_state_dir": settings.runtime_state_dir,
            "google_oauth_token_path": settings.google_oauth_token_path,
        },
        forbidden_drives=settings.forbidden_runtime_drives,
        action="Thoth runtime storage initialization",
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    settings.objects_dir.mkdir(parents=True, exist_ok=True)
    settings.runtime_state_dir.mkdir(parents=True, exist_ok=True)
    settings.google_oauth_token_path.parent.mkdir(parents=True, exist_ok=True)


def bind_host() -> str:
    return str(settings.host or "127.0.0.1").strip() or "127.0.0.1"


def configured_allowed_origins() -> list[str]:
    raw = settings.allowed_origins
    if raw is None:
        return []
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return [str(item).strip() for item in raw if str(item).strip()]
