from __future__ import annotations

import json
import os
import secrets
import time
import sys
from typing import Optional
from pathlib import Path

import typer
import requests
from rich.console import Console

from thoth_terminal.config import settings
from thoth_terminal.db import (
    audit_database_files,
    compact_database,
    configured_database_url,
    create_managed_backup,
    cleanup_legacy_db_files,
    database_backend,
    database_space_report,
    get_engine,
    session_scope,
)
from thoth_terminal.db_migration import migrate_sqlite_to_postgres
from thoth_terminal.export import export_bundle
from thoth_terminal.foundation_service import bootstrap_research_base
from thoth_terminal.ingest import ingest_note, ingest_source, _apply_money_mentions
from thoth_terminal.fx import ensure_fx_rates
from thoth_terminal.migrations import init_db
from thoth_terminal.models import Document, IngestionRun, Source, SourceState
from thoth_terminal.research_library_service import ingest_research_library, review_research_library
from thoth_terminal.seed import seed_all
from thoth_terminal.scheduler import (
    _is_within_window,
    _next_window_start_utc,
    _poll_range_seconds,
    _window_config,
    run_scheduler_tick,
)
from thoth_terminal.worker import run_cloud_worker, run_worker
from thoth_terminal.runtime_env import (
    assert_git_workspace,
    configure_logging,
    detect_platform,
    git_workspace_ok,
    normalize_path,
    repo_root,
    require_writer_runtime,
)
from thoth_terminal.local_model_runtime import (
    benchmark_local_model_runtime,
    ensure_local_model_runtime,
    launch_command_as_shell_line,
    probe_local_model_runtime,
    resolve_local_model_runtime,
)
from thoth_terminal.agent.openshell import openshell_runtime_status
from thoth_terminal.runtime_info import get_runtime_identity, list_runtime_states, runtime_fingerprint
from thoth_terminal.runtime_guard import (
    assert_live_runtime_database,
    assert_no_live_runtime_conflict,
    pid_alive as runtime_pid_alive,
)
from thoth_terminal.security import StartupSecurityError, validate_security_posture


app = typer.Typer(
    add_completion=False,
    help="ThothTerminal: local-first research/news terminal.",
    pretty_exceptions_show_locals=False,
)
console = Console()
configure_logging(settings.log_level)


def _print_json_payload(payload: object, *, pretty: bool = False) -> None:
    # Keep CLI JSON usable on legacy Windows consoles when issuer/source names contain non-ASCII text.
    sys.stdout.write(json.dumps(payload, indent=2 if pretty else None, sort_keys=True, ensure_ascii=True))
    sys.stdout.write("\n")


def _format_db_report(report: dict[str, object]) -> list[str]:
    lines: list[str] = []
    active = report.get("active") if isinstance(report, dict) else None
    if isinstance(active, dict):
        url = str(active.get("url") or "")
        if bool(active.get("database_url_configured")) and "://" in url:
            scheme, rest = url.split("://", 1)
            if "@" in rest:
                rest = "***@" + rest.rsplit("@", 1)[-1]
            url = f"{scheme}://{rest}"
        lines.append(
            f"active_backend={active.get('backend')} "
            f"database_url_configured={active.get('database_url_configured')} active_url={url}"
        )
    configured = report.get("configured") if isinstance(report, dict) else None
    if isinstance(configured, dict):
        detail = str(configured.get("integrity_detail") or "unknown")
        lines.append(
            f"configured={configured.get('path')} exists={configured.get('exists')} integrity={detail}"
        )
    legacy_rows = report.get("legacy") if isinstance(report, dict) else []
    if isinstance(legacy_rows, list) and legacy_rows:
        for entry in legacy_rows:
            if not isinstance(entry, dict):
                continue
            lines.append(
                f"legacy={entry.get('path')} exists={entry.get('exists')} integrity={entry.get('integrity_detail')}"
            )
    elif isinstance(legacy_rows, list):
        lines.append("legacy=none")
    unexpected_rows = report.get("unexpected") if isinstance(report, dict) else []
    if isinstance(unexpected_rows, list) and unexpected_rows:
        for entry in unexpected_rows:
            if not isinstance(entry, dict):
                continue
            lines.append(
                f"unexpected={entry.get('path')} exists={entry.get('exists')} integrity={entry.get('integrity_detail')}"
            )
    elif isinstance(unexpected_rows, list):
        lines.append("unexpected=none")
    return lines


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _allow_unsafe_writes() -> bool:
    return bool(getattr(settings, "runtime_allow_unsafe_wsl_writes", False)) or _env_bool(
        "THOTH_RUNTIME_ALLOW_UNSAFE_WRITES", False
    )


def _allow_no_git_gate() -> bool:
    return _env_bool("THOTH_RUNTIME_ALLOW_NO_GIT", False)


def _writer_guard(action: str) -> None:
    require_writer_runtime(
        action=action,
        primary_os=str(getattr(settings, "runtime_primary_os", "windows")),
        writer_policy=str(getattr(settings, "runtime_writer_policy", "windows_single_writer")),
        allow_unsafe_writes=_allow_unsafe_writes(),
    )
    assert_git_workspace(
        enforce=bool(getattr(settings, "runtime_enforce_git_workspace", True)) and (not _allow_no_git_gate()),
        root=repo_root(),
    )


def _live_runtime_start_guard(*, service: str, action: str) -> None:
    try:
        assert_live_runtime_database(action=action)
        assert_no_live_runtime_conflict(service=service, action=action)
    except RuntimeError as exc:
        console.print(f"[red]Refusing to start {action}:[/red] {exc}")
        raise typer.Exit(1) from exc


def _maybe_autostart_local_model(service: str) -> None:
    payload = ensure_local_model_runtime(service=service, require_opt_in=True)
    status = str(payload.get("status") or "").strip().lower()
    if status == "autostart_disabled":
        return
    if status in {"already_ready", "already_running", "launched", "launched_ready"}:
        detail = f"status={status}"
        if payload.get("pid"):
            detail += f" pid={payload.get('pid')}"
        endpoint = ((payload.get("runtime") or {}) if isinstance(payload, dict) else {}).get("endpoint")
        if endpoint:
            detail += f" endpoint={endpoint}"
        console.print(f"[cyan]local model[/cyan] {detail}")
        return
    for warning in payload.get("warnings") or []:
        console.print(f"[yellow]{warning}[/yellow]")
    error = payload.get("error")
    if error:
        console.print(f"[yellow]local model autostart failed[/yellow]: {error}")


def _pid_alive(pid: int | None) -> bool:
    return runtime_pid_alive(pid)


@app.command("version")
def version_cmd(
    json_output: bool = typer.Option(False, "--json", help="Print full runtime identity as JSON."),
) -> None:
    identity = get_runtime_identity()
    if json_output:
        _print_json(identity, pretty=True)
        return
    console.print(runtime_fingerprint())


@app.command("doctor")
def doctor_cmd(
    json_output: bool = typer.Option(False, "--json", help="Print diagnostics as JSON."),
) -> None:
    root = repo_root()
    checks: list[dict[str, object]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    platform_kind = detect_platform()
    check("platform", True, platform_kind)

    enforce_git = bool(getattr(settings, "runtime_enforce_git_workspace", True)) and (not _allow_no_git_gate())
    git_ok, git_detail = git_workspace_ok(root)
    check("git_workspace", (git_ok or (not enforce_git)), git_detail)

    py_path = Path(sys.executable).resolve(strict=False)
    expected_venv = normalize_path(".venv", base_dir=root)
    enforce_venv = bool(getattr(settings, "runtime_require_project_venv", True))
    in_project_venv = (expected_venv in py_path.parents) or (py_path.parent == expected_venv)
    check(
        "python_venv",
        (in_project_venv or (not enforce_venv)),
        f"python={py_path} expected_prefix={expected_venv}",
    )

    db_path = Path(settings.db_path)
    active_db_url = configured_database_url()
    check(
        "active_database",
        True,
        f"backend={database_backend(active_db_url)} database_url_configured={bool(str(settings.database_url or '').strip())}",
    )
    check("db_path_absolute", db_path.is_absolute(), str(db_path))
    check("db_parent_exists", db_path.parent.exists(), str(db_path.parent))

    # The local SQLite file is not the active database when PostgreSQL is
    # configured. Passing it explicitly would force an expensive SQLite
    # integrity check during every PostgreSQL startup preflight.
    db_audit = audit_database_files(
        configured_db_path=db_path if database_backend(active_db_url) == "sqlite" else None,
        data_dir=settings.data_dir,
    )
    configured_db = db_audit.get("configured") if isinstance(db_audit, dict) else None
    configured_ok = True
    configured_detail = "unknown"
    if isinstance(configured_db, dict):
        configured_integrity = configured_db.get("integrity_ok")
        configured_detail = str(configured_db.get("integrity_detail") or "unknown")
        configured_ok = configured_integrity is not False
        if not bool(configured_db.get("exists")):
            configured_detail = "missing (will initialize on startup)"
    check("db_integrity", configured_ok, configured_detail)

    legacy_rows = db_audit.get("legacy") if isinstance(db_audit, dict) else []
    legacy_detail = "none"
    if isinstance(legacy_rows, list) and legacy_rows:
        parts: list[str] = []
        for entry in legacy_rows:
            if not isinstance(entry, dict) or not bool(entry.get("exists")):
                continue
            parts.append(f"{entry.get('path')} ({entry.get('integrity_detail')})")
        if parts:
            legacy_detail = "cleanup recommended: " + "; ".join(parts)
    check("legacy_db_files", True, legacy_detail)

    unexpected_rows = db_audit.get("unexpected") if isinstance(db_audit, dict) else []
    unexpected_detail = "none"
    if isinstance(unexpected_rows, list) and unexpected_rows:
        parts = []
        for entry in unexpected_rows:
            if not isinstance(entry, dict) or not bool(entry.get("exists")):
                continue
            parts.append(f"{entry.get('path')} ({entry.get('integrity_detail')})")
        if parts:
            unexpected_detail = "cleanup recommended: " + "; ".join(parts)
    check("unexpected_db_files", True, unexpected_detail)

    try:
        require_writer_runtime(
            action="doctor writer check",
            primary_os=str(getattr(settings, "runtime_primary_os", "windows")),
            writer_policy=str(getattr(settings, "runtime_writer_policy", "windows_single_writer")),
            allow_unsafe_writes=_allow_unsafe_writes(),
        )
        check("writer_runtime_policy", True, "ok")
    except Exception as exc:
        check("writer_runtime_policy", False, str(exc))

    states = list_runtime_states()
    dead_states: list[str] = []
    for state in states:
        svc = str(state.get("service") or Path(str(state.get("_path") or "")).stem)
        pid_val = state.get("pid")
        try:
            pid = int(pid_val) if pid_val is not None else None
        except Exception:
            pid = None
        if pid is not None and (not _pid_alive(pid)):
            dead_states.append(f"{svc}:{pid}")
    check("runtime_state_files", True, f"{len(states)} file(s)")
    check("runtime_state_pids", len(dead_states) == 0, ", ".join(dead_states) if dead_states else "ok")

    failed = [c for c in checks if not bool(c["ok"])]
    payload = {
        "ok": len(failed) == 0,
        "checks": checks,
        "runtime": get_runtime_identity(),
    }
    if json_output:
        _print_json(payload, pretty=True)
    else:
        for item in checks:
            icon = "[green]OK[/green]" if bool(item["ok"]) else "[red]FAIL[/red]"
            console.print(f"{icon} {item['name']}: {item['detail']}")
    if failed:
        raise typer.Exit(1)


@app.command("openshell-status")
def openshell_status_cmd(
    json_output: bool = typer.Option(False, "--json", help="Print OpenShell status as JSON."),
) -> None:
    payload = openshell_runtime_status()
    if json_output:
        _print_json_payload(payload, pretty=True)
        return
    state = "ready" if payload.get("ready") else str(payload.get("status") or "not_ready")
    console.print(f"[cyan]openshell[/cyan] {state}")
    if payload.get("version"):
        console.print(f"version={payload.get('version')}")
    if payload.get("command"):
        console.print(f"command={payload.get('command')}")
    if payload.get("detail"):
        console.print(str(payload.get("detail")))


@app.command("runtime-status")
def runtime_status_cmd(
    base_url: Optional[str] = typer.Option(None, help="Web base URL for ops checks."),
    json_output: bool = typer.Option(False, "--json", help="Print status as JSON."),
) -> None:
    states = list_runtime_states()
    details: list[dict[str, object]] = []
    for state in states:
        pid_val = state.get("pid")
        try:
            pid = int(pid_val) if pid_val is not None else None
        except Exception:
            pid = None
        details.append(
            {
                "service": state.get("service") or Path(str(state.get("_path") or "")).stem,
                "pid": pid,
                "pid_alive": _pid_alive(pid),
                "started_at_utc": state.get("started_at_utc") or state.get("started_at"),
                "python_executable": state.get("python_executable") or state.get("python"),
                "path": state.get("_path"),
            }
        )

    endpoint = base_url or f"http://{settings.host}:{settings.port}"
    ops_version = None
    ops_health = None
    ops_error = None
    try:
        ops_version = requests.get(f"{endpoint.rstrip('/')}/ops/version", timeout=4).json()
        ops_health = requests.get(f"{endpoint.rstrip('/')}/ops/health", timeout=4).json()
    except Exception as exc:
        ops_error = f"{type(exc).__name__}: {exc}"

    payload = {
        "runtime": get_runtime_identity(),
        "services": details,
        "ops_endpoint": endpoint,
        "ops_version": ops_version,
        "ops_health": ops_health,
        "ops_error": ops_error,
    }
    if json_output:
        _print_json(payload, pretty=True)
        return
    console.print(runtime_fingerprint())
    for svc in details:
        alive = "[green]alive[/green]" if svc.get("pid_alive") else "[red]dead[/red]"
        console.print(f"{alive} {svc.get('service')} pid={svc.get('pid')}")
    if ops_error:
        console.print(f"[yellow]ops check unavailable[/yellow]: {ops_error}")
    else:
        console.print("[green]ops check OK[/green]")


@app.command("init-db")
def init_db_cmd() -> None:
    _writer_guard("init-db")
    init_db()
    from sqlalchemy.engine import make_url

    destination = make_url(settings.database_url).render_as_string(hide_password=True) if settings.database_url else settings.db_path
    console.print(f"[green]DB ready[/green]: {database_backend()} {destination}")


@app.command("migrate-to-postgres")
def migrate_to_postgres_cmd(
    sqlite_path: Path = typer.Option(settings.db_path, "--sqlite-path", help="Source SQLite database path."),
    postgres_url: str = typer.Option(
        "",
        "--postgres-url",
        envvar="THOTH_DATABASE_URL",
        help="Target Postgres URL. Defaults to THOTH_DATABASE_URL.",
    ),
    replace: bool = typer.Option(False, "--replace", help="Delete target table rows before importing."),
    json_output: bool = typer.Option(False, "--json", help="Print migration report as JSON."),
) -> None:
    _writer_guard("migrate-to-postgres")
    result = migrate_sqlite_to_postgres(
        sqlite_path=sqlite_path,
        postgres_url=postgres_url or settings.database_url,
        replace=replace,
    )
    if json_output:
        _print_json(result, pretty=True)
        return
    copied = result.get("copied") if isinstance(result, dict) else []
    rows = sum(int(item.get("rows") or 0) for item in copied if isinstance(item, dict))
    console.print(f"[green]Postgres migration complete[/green]: tables={len(copied)} rows={rows}")


@app.command("duckdb-sync")
def duckdb_sync_cmd(
    json_output: bool = typer.Option(False, "--json", help="Print DuckDB sync report as JSON."),
) -> None:
    from thoth_terminal.duckdb_store import init_duckdb, sync_all_datasets

    init_duckdb()
    with session_scope() as session:
        result = sync_all_datasets(session)
    if json_output:
        _print_json(result, pretty=True)
        return
    console.print(
        f"[green]DuckDB analytic cache cleared; populated on demand[/green]: {settings.duckdb_path} "
        f"versions={result.get('versions', 0)} rows={result.get('rows', 0)}"
    )


@app.command("agentx-sync")
@app.command("inferencex-sync", hidden=True)
def inferencex_sync_cmd(
    dump_dir: Optional[Path] = typer.Option(None, "--dump-dir", help="Use an already-unpacked InferenceX DB dump directory."),
    release_tag: Optional[str] = typer.Option(None, "--release-tag", help="Specific db-dump release tag or date, e.g. db-dump/2026-06-15."),
    cache_dir: Optional[Path] = typer.Option(None, "--cache-dir", help="Directory for downloaded InferenceX dump assets."),
    force_download: bool = typer.Option(False, "--force-download", help="Redownload and re-extract the release dump."),
    yield_every: int = typer.Option(1000, "--yield-every", help="Yield during normalization after this many benchmark rows."),
    pause_ms: int = typer.Option(10, "--pause-ms", help="Milliseconds to sleep at each normalization yield."),
    no_publish: bool = typer.Option(False, "--no-publish", help="Update the draft without publishing a new immutable snapshot."),
    github_token: Optional[str] = typer.Option(None, "--github-token", envvar="GITHUB_TOKEN", help="Optional GitHub token for release downloads."),
    json_output: bool = typer.Option(False, "--json", help="Print sync result as JSON."),
) -> None:
    _writer_guard("inferencex-sync")
    from thoth_terminal.dataset_service import sync_inferencex_upstream_dataset

    with session_scope() as session:
        result = sync_inferencex_upstream_dataset(
            session,
            dump_dir=dump_dir,
            release_tag=release_tag,
            cache_dir=cache_dir,
            github_token=github_token,
            force_download=force_download,
            yield_every=yield_every,
            pause_s=max(0.0, float(pause_ms or 0) / 1000.0),
            publish=not no_publish,
        )
    if json_output:
        _print_json(result, pretty=True)
        return
    verb = "created" if result.get("created") else "updated"
    changed = "changed" if result.get("changed") else "unchanged"
    console.print(
        f"[green]AgentX / InferenceX upstream dataset {verb}[/green]: {result.get('slug')} "
        f"rows={result.get('rows', 0)} columns={result.get('columns', 0)} {changed}"
    )


@app.command("provider-pricing-sync")
def provider_pricing_sync_cmd(
    force: bool = typer.Option(False, "--force", help="Refresh even if the latest published snapshot is under the freshness window."),
    min_refresh_days: int = typer.Option(7, "--min-refresh-days", help="Minimum age before refreshing provider pricing."),
    no_publish: bool = typer.Option(False, "--no-publish", help="Update the draft without publishing a new immutable snapshot."),
    json_output: bool = typer.Option(False, "--json", help="Print sync result as JSON."),
) -> None:
    _writer_guard("provider-pricing-sync")
    from thoth_terminal.dataset_service import sync_provider_pricing_dataset

    with session_scope() as session:
        result = sync_provider_pricing_dataset(
            session,
            force=force,
            min_refresh_days=min_refresh_days,
            publish=not no_publish,
        )
    if json_output:
        _print_json(result, pretty=True)
        return
    if result.get("skipped"):
        console.print(f"[yellow]Provider pricing sync skipped[/yellow]: {result.get('reason')}")
        return
    changed = "changed" if result.get("changed") else "unchanged"
    console.print(
        f"[green]Provider pricing dataset synced[/green]: {result.get('slug')} "
        f"rows={result.get('rows', 0)} columns={result.get('columns', 0)} {changed}"
    )


@app.command("artificial-analysis-sync")
def artificial_analysis_sync_cmd(
    force: bool = typer.Option(False, "--force", help="Refresh even if the latest published snapshot is under the freshness window."),
    min_refresh_days: int = typer.Option(1, "--min-refresh-days", help="Minimum age before refreshing Artificial Analysis data."),
    limit: int | None = typer.Option(None, "--limit", help="Optional maximum language-model rows; defaults to the complete catalog."),
    no_publish: bool = typer.Option(False, "--no-publish", help="Update the draft without publishing a new immutable snapshot."),
    json_output: bool = typer.Option(False, "--json", help="Print sync result as JSON."),
) -> None:
    _writer_guard("artificial-analysis-sync")
    from thoth_terminal.dataset_service import sync_artificial_analysis_dataset

    with session_scope() as session:
        result = sync_artificial_analysis_dataset(
            session,
            force=force,
            min_refresh_days=min_refresh_days,
            limit=limit,
            publish=not no_publish,
        )
    if json_output:
        _print_json(result, pretty=True)
        return
    if result.get("skipped"):
        console.print(f"[yellow]Artificial Analysis sync skipped[/yellow]: {result.get('reason')}")
        return
    changed = "changed" if result.get("changed") else "unchanged"
    console.print(
        f"[green]Artificial Analysis dataset synced[/green]: {result.get('slug')} "
        f"rows={result.get('rows', 0)} columns={result.get('columns', 0)} {changed}"
    )


@app.command("ai-serving-economics-sync")
def ai_serving_economics_sync_cmd(
    benchmark_limit: int = typer.Option(5000, "--benchmark-limit", help="Maximum AgentX trace benchmark rows to carry into the scenario model."),
    price_limit: int = typer.Option(1000, "--price-limit", help="Maximum pricing rows to carry into the scenario model."),
    no_publish: bool = typer.Option(False, "--no-publish", help="Update drafts without publishing immutable snapshots."),
    json_output: bool = typer.Option(False, "--json", help="Print sync result as JSON."),
) -> None:
    _writer_guard("ai-serving-economics-sync")
    from thoth_terminal.dataset_service import sync_ai_serving_economics_datasets

    with session_scope() as session:
        result = sync_ai_serving_economics_datasets(
            session,
            benchmark_limit=benchmark_limit,
            price_limit=price_limit,
            publish=not no_publish,
        )
    if json_output:
        _print_json(result, pretty=True)
        return
    scenarios = result.get("scenarios") if isinstance(result.get("scenarios"), dict) else {}
    frontier = result.get("frontier") if isinstance(result.get("frontier"), dict) else {}
    console.print(
        f"[green]AI serving economics synced[/green]: "
        f"frontier_rows={frontier.get('rows', 0)} scenario_rows={scenarios.get('rows', 0)}"
    )


@app.command("agentx-evaluate")
def agentx_evaluate_cmd(payload_path: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    """Evaluate a canonical/theoretical AgentX configuration JSON against pinned Thoth datasets."""
    from thoth_terminal.agentx_service import evaluate_agentx_configuration
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    with session_scope() as session:
        result = evaluate_agentx_configuration(session, payload)
    _print_json_payload(result, pretty=True)


@app.command("agentx-save")
def agentx_save_cmd(payload_path: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    """Evaluate and publish an immutable AgentX configuration version."""
    _writer_guard("agentx-save")
    from thoth_terminal.agentx_service import save_agentx_configuration
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    with session_scope() as session:
        result = save_agentx_configuration(session, payload)
    _print_json_payload(result, pretty=True)


@app.command("db-audit")
def db_audit_cmd(
    json_output: bool = typer.Option(False, "--json", help="Print database audit as JSON."),
) -> None:
    report = audit_database_files()
    configured = report.get("configured") if isinstance(report, dict) else None
    ok = True
    if isinstance(configured, dict):
        ok = configured.get("integrity_ok") is not False
    payload = {
        "ok": ok,
        "report": report,
    }
    if json_output:
        _print_json(payload, pretty=True)
    else:
        for line in _format_db_report(report):
            console.print(line)
    if not ok:
        raise typer.Exit(1)


@app.command("db-cleanup")
def db_cleanup_cmd(
    json_output: bool = typer.Option(False, "--json", help="Print cleanup result as JSON."),
) -> None:
    _writer_guard("db-cleanup")
    result = cleanup_legacy_db_files()
    if json_output:
        _print_json(result, pretty=True)
        return
    actions = result.get("actions") if isinstance(result, dict) else []
    skipped = result.get("skipped") if isinstance(result, dict) else None
    archive_dir = result.get("archive_dir") if isinstance(result, dict) else None
    if isinstance(actions, list) and actions:
        console.print(f"[green]Archived unmanaged DB artifacts[/green] -> {archive_dir}")
        for action in actions:
            if not isinstance(action, dict):
                continue
            console.print(f"{action.get('kind')}: {action.get('from')} -> {action.get('to')}")
    else:
        console.print(f"[green]No cleanup needed[/green]: {skipped or 'nothing to do'}")


@app.command("db-backup")
def db_backup_cmd(
    json_output: bool = typer.Option(False, "--json", help="Print backup result as JSON."),
) -> None:
    _writer_guard("db-backup")
    result = create_managed_backup()
    if json_output:
        _print_json(result, pretty=True)
        return
    if result.get("created"):
        console.print(f"[green]Created managed DB backup[/green]: {result.get('backup_path')}")
    else:
        console.print(f"[yellow]Backup skipped[/yellow]: {result.get('skipped') or 'nothing to do'}")


@app.command("db-compact")
def db_compact_cmd(
    json_output: bool = typer.Option(False, "--json", help="Print compaction result as JSON."),
    force_vacuum: bool = typer.Option(False, "--force-vacuum", help="Run VACUUM even when little space is reclaimable."),
    backup_first: bool = typer.Option(True, "--backup/--no-backup", help="Create managed backup before VACUUM."),
    min_reclaimable_mb: int = typer.Option(
        128,
        "--min-reclaimable-mb",
        min=0,
        help="Only run VACUUM when at least this much free-page space is reclaimable.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only report current database space usage."),
) -> None:
    _writer_guard("db-compact")
    if dry_run:
        result = {"space": database_space_report()}
    else:
        result = compact_database(
            min_reclaimable_bytes=int(min_reclaimable_mb) * 1024 * 1024,
            force_vacuum=force_vacuum,
            backup_first=backup_first,
        )
    if json_output:
        _print_json(result, pretty=True)
        return
    payload = result.get("space") if dry_run and isinstance(result, dict) else result
    if not isinstance(payload, dict):
        console.print("[yellow]No database space report available[/yellow]")
        return
    before = payload.get("before") if isinstance(payload.get("before"), dict) else payload
    after = payload.get("after") if isinstance(payload.get("after"), dict) else None
    reclaimable = int(before.get("reclaimable_bytes") or 0) if isinstance(before, dict) else 0
    total = int(before.get("total_size_bytes") or before.get("db_size_bytes") or 0) if isinstance(before, dict) else 0
    console.print(
        f"database={before.get('configured_path') if isinstance(before, dict) else settings.db_path} "
        f"total={total} reclaimable={reclaimable}"
    )
    if isinstance(after, dict):
        console.print(
            f"after_total={after.get('total_size_bytes')} after_reclaimable={after.get('reclaimable_bytes')}"
        )
    actions = payload.get("actions") if isinstance(payload, dict) else []
    if actions:
        console.print(f"[green]actions[/green]: {', '.join(str(action) for action in actions)}")
    skipped = payload.get("skipped") if isinstance(payload, dict) else None
    if skipped:
        console.print(f"[yellow]{skipped}[/yellow]")


def _bootstrap_research_base(
    *,
    build_reports: bool,
    build_knowledge: bool,
    limit: int | None = None,
) -> dict[str, int]:
    with session_scope() as session:
        return bootstrap_research_base(
            session,
            build_reports=build_reports,
            build_knowledge=build_knowledge,
            limit=limit,
        )


@app.command("seed")
def seed_cmd(
    bootstrap_reports: bool = typer.Option(
        True,
        "--bootstrap-reports/--no-bootstrap-reports",
        help="Build baseline research reports for company subjects with seeded documents.",
    ),
    bootstrap_knowledge: bool = typer.Option(
        True,
        "--bootstrap-knowledge/--no-bootstrap-knowledge",
        help="Generate baseline primer drafts for seeded company subjects.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Limit the number of company subjects included in research bootstrap.",
    ),
) -> None:
    _writer_guard("seed")
    seed_all(get_engine())
    console.print("[green]Seeded sources and company rails[/green]")
    if bootstrap_reports or bootstrap_knowledge:
        stats = _bootstrap_research_base(
            build_reports=bootstrap_reports,
            build_knowledge=bootstrap_knowledge,
            limit=limit,
        )
        console.print(
            "[green]Bootstrapped research base[/green]: "
            f"subjects={stats['subjects']} reports_new={stats['reports_built']} "
            f"reports_reused={stats['reports_reused']} primer_drafts={stats['knowledge_seeded']} "
            f"transcripts_new={stats.get('transcripts_seeded', 0)} transcripts_reused={stats.get('transcripts_reused', 0)}"
        )


@app.command("bootstrap-ecosystem")
def bootstrap_ecosystem_cmd(
    build_reports: bool = typer.Option(
        True,
        "--build-reports/--no-build-reports",
        help="Build baseline research reports for company subjects with seeded documents.",
    ),
    build_knowledge: bool = typer.Option(
        True,
        "--build-knowledge/--no-build-knowledge",
        help="Generate baseline primer drafts for seeded company subjects.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Limit the number of company subjects included in research bootstrap.",
    ),
) -> None:
    _writer_guard("bootstrap-ecosystem")
    stats = _bootstrap_research_base(
        build_reports=build_reports,
        build_knowledge=build_knowledge,
        limit=limit,
    )
    console.print(
        "[green]Bootstrapped ecosystem[/green]: "
        f"subjects={stats['subjects']} reports_new={stats['reports_built']} "
        f"reports_reused={stats['reports_reused']} primer_drafts={stats['knowledge_seeded']} "
        f"transcripts_new={stats.get('transcripts_seeded', 0)} transcripts_reused={stats.get('transcripts_reused', 0)}"
    )


@app.command("web")
def web_cmd(
    host: str = typer.Option(settings.host, help="Bind host"),
    port: int = typer.Option(settings.port, help="Bind port"),
    reload: bool = typer.Option(False, help="Auto-reload on code changes"),
) -> None:
    _writer_guard("web")
    _live_runtime_start_guard(service="web", action="web")
    try:
        validate_security_posture(settings, bind_host=host)
    except StartupSecurityError as exc:
        console.print(f"[red]Refusing to start web server:[/red] {exc}")
        raise typer.Exit(1) from exc
    _maybe_autostart_local_model("web")
    import uvicorn

    uvicorn.run("thoth_terminal.web.app:app", host=host, port=port, reload=reload)


@app.command("local-agent-api")
def local_agent_api_cmd(
    host: str = typer.Option(settings.agent_api_host, help="Bind host for the token-protected local analyst API."),
    port: int = typer.Option(settings.agent_api_port, help="Bind port for the token-protected local analyst API."),
    token: str | None = typer.Option(
        None,
        "--token",
        envvar="THOTH_AGENT_API_TOKEN",
        help="Bearer token for laptop workers. Prefer THOTH_AGENT_API_TOKEN over passing this on the command line.",
    ),
    reload: bool = typer.Option(False, help="Auto-reload on code changes"),
) -> None:
    _writer_guard("local-agent-api")
    _live_runtime_start_guard(service="local-agent-api", action="local-agent-api")
    effective_token = str(token or settings.agent_api_token or "").strip()
    if not effective_token:
        console.print(
            "[red]Refusing to start local agent API:[/red] "
            "set THOTH_AGENT_API_TOKEN or pass --token."
        )
        raise typer.Exit(1)
    settings.agent_api_enabled = True
    settings.agent_api_token = effective_token
    settings.agent_api_host = host
    settings.agent_api_port = int(port)
    console.print(
        "[green]Starting local analyst agent API[/green]: "
        f"http://{host}:{port} token_required=true"
    )
    import uvicorn

    uvicorn.run("thoth_terminal.web.local_agent_app:app", host=host, port=port, reload=reload)


@app.command("local-agent-eval")
def local_agent_eval_cmd(
    seed: bool = typer.Option(False, "--seed", help="Create or update the built-in local-agent eval cases."),
    import_knowledge: Optional[Path] = typer.Option(
        None,
        "--import-knowledge",
        help="Import Question/A./Correct answer cases from a knowledge-eval text file.",
    ),
    import_workflow_benchmark: Optional[Path] = typer.Option(
        None,
        "--import-workflow-benchmark",
        help="Import one workflow benchmark manifest or a JSON array of manifests.",
    ),
    import_structured_knowledge: Optional[Path] = typer.Option(
        None,
        "--import-structured-knowledge",
        help="Import source-grounded text/image knowledge questions from JSON.",
    ),
    case_id: Optional[int] = typer.Option(None, "--case-id", help="Run one eval case by id."),
    all_cases: bool = typer.Option(False, "--all", help="Run all active eval cases."),
    mode: str = typer.Option(
        "latest_compression",
        "--mode",
        help="Eval source: latest_compression, latest_signal_extraction, document_quality, durable_output, durable_reasoning, pdf_ocr_prep, media_route, stage_plan, bottleneck_report_start, or llama.",
    ),
    model: Optional[str] = typer.Option(None, "--model", help="Configured native thoth-local-text model for --mode llama; defaults to its sole model."),
    prompt_version: Optional[str] = typer.Option(None, "--prompt-version", help="Prompt version label."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    _writer_guard("local-agent-eval")
    init_db(allow_locked=True)
    from thoth_terminal.local_agent_review import (
        run_eval_case,
        seed_default_eval_cases,
        serialize_eval_case,
        serialize_eval_run,
    )
    from thoth_terminal.models import LocalAgentEvalCase, LocalAgentEvalRun

    with session_scope() as session:
        payload: dict[str, object] = {}
        if seed:
            payload["seed"] = seed_default_eval_cases(session)
        if import_knowledge is not None:
            from thoth_terminal.knowledge_eval import import_knowledge_eval_file

            payload["knowledge_import"] = import_knowledge_eval_file(session, path=import_knowledge)
        if import_workflow_benchmark is not None:
            from thoth_terminal.workflow_benchmark_service import import_workflow_benchmark_file

            payload["workflow_benchmark_import"] = import_workflow_benchmark_file(
                session,
                path=import_workflow_benchmark,
            )
        if import_structured_knowledge is not None:
            from thoth_terminal.knowledge_eval import import_structured_knowledge_eval_file

            payload["structured_knowledge_import"] = import_structured_knowledge_eval_file(
                session,
                path=import_structured_knowledge,
            )
        if case_id is not None:
            run = run_eval_case(
                session,
                case_id=int(case_id),
                mode=mode,
                model=model,
                prompt_version=prompt_version,
            )
            payload["runs"] = [serialize_eval_run(run)]
        elif all_cases:
            runs = []
            cases = (
                session.query(LocalAgentEvalCase)
                .filter(LocalAgentEvalCase.active.is_(True))
                .order_by(LocalAgentEvalCase.id.asc())
                .all()
            )
            if str(mode or "").strip().lower() in {"document_quality", "doc_quality"}:
                cases = [case for case in cases if case.document_id is not None]
            if str(mode or "").strip().lower() in {"latest_signal_extraction", "signal_extraction"}:
                cases = [case for case in cases if case.document_id is not None]
            if str(mode or "").strip().lower() in {"durable_output", "durable"}:
                cases = [
                    case
                    for case in cases
                    if isinstance(case.expected_json, dict) and isinstance(case.expected_json.get("durable_output_fixture"), dict)
                ]
            if str(mode or "").strip().lower() in {"durable_reasoning", "durable_e2e"}:
                cases = [
                    case
                    for case in cases
                    if isinstance(case.expected_json, dict) and isinstance(case.expected_json.get("durable_reasoning_fixture"), dict)
                ]
            if str(mode or "").strip().lower() in {"pdf_ocr_prep", "ocr_prep"}:
                cases = [
                    case
                    for case in cases
                    if isinstance(case.expected_json, dict) and isinstance(case.expected_json.get("pdf_ocr_prep_fixture"), dict)
                ]
            if str(mode or "").strip().lower() in {"media_route", "media_routing"}:
                cases = [
                    case
                    for case in cases
                    if isinstance(case.expected_json, dict) and isinstance(case.expected_json.get("media_route_fixture"), dict)
                ]
            if str(mode or "").strip().lower() in {"stage_plan", "staged_plan", "stage_planning"}:
                cases = [
                    case
                    for case in cases
                    if isinstance(case.expected_json, dict) and isinstance(case.expected_json.get("stage_plan_fixture"), dict)
                ]
            if str(mode or "").strip().lower() in {"bottleneck_report_start", "bottleneck_start"}:
                cases = [
                    case
                    for case in cases
                    if isinstance(case.expected_json, dict) and isinstance(case.expected_json.get("bottleneck_report_start_fixture"), dict)
                ]
            for case in cases:
                run = run_eval_case(
                    session,
                    case_id=int(case.id),
                    mode=mode,
                    model=model,
                    prompt_version=prompt_version,
                )
                runs.append(serialize_eval_run(run))
            payload["runs"] = runs
        elif import_knowledge is None and import_workflow_benchmark is None and import_structured_knowledge is None:
            cases = (
                session.query(LocalAgentEvalCase)
                .order_by(LocalAgentEvalCase.updated_at.desc(), LocalAgentEvalCase.id.desc())
                .limit(100)
                .all()
            )
            latest: dict[int, LocalAgentEvalRun] = {}
            for run in (
                session.query(LocalAgentEvalRun)
                .order_by(LocalAgentEvalRun.created_at.desc(), LocalAgentEvalRun.id.desc())
                .all()
            ):
                latest.setdefault(int(run.case_id), run)
            payload["cases"] = [serialize_eval_case(case, latest.get(int(case.id))) for case in cases]

    if json_output:
        _print_json(payload, pretty=True)
        return
    if payload.get("seed"):
        stats = payload["seed"]
        console.print(f"[green]Seeded eval cases[/green]: {stats}")
    if payload.get("knowledge_import"):
        console.print(f"[green]Imported knowledge eval[/green]: {payload['knowledge_import']}")
    for run in payload.get("runs") or []:
        if not isinstance(run, dict):
            continue
        score = run.get("score_value")
        verdict = "PASS" if run.get("passed") else "FAIL"
        console.print(f"{verdict} eval_run={run.get('id')} case={run.get('case_id')} score={score}")
        if run.get("error"):
            console.print(f"[red]{run.get('error')}[/red]")
    if payload.get("cases"):
        for case in payload["cases"]:
            latest = case.get("latest_run") if isinstance(case, dict) else None
            status = "not-run"
            if isinstance(latest, dict):
                status = ("pass" if latest.get("passed") else "fail") + f" {latest.get('score_value')}"
            console.print(f"#{case.get('id')} {case.get('slug')} [{status}]")


@app.command("workflow-benchmark-plan")
def workflow_benchmark_plan_cmd(
    slate_path: Path = typer.Option(
        Path("evals/workflow_benchmark/evaluation-slate-v1.json"),
        "--slate",
        help="Evaluation slate JSON file.",
    ),
    manifests_dir: Path = typer.Option(
        Path("evals/workflow_benchmark/development"),
        "--manifests-dir",
        help="Directory containing workflow benchmark manifests.",
    ),
    configurations_path: Path = typer.Option(
        Path("evals/workflow_benchmark/candidate-configurations-v1.json"),
        "--configurations",
        help="Candidate configuration JSON file.",
    ),
    output_path: Optional[Path] = typer.Option(None, "--output", help="Optional output path for the expanded plan."),
    json_output: bool = typer.Option(False, "--json", help="Print the complete machine-readable plan."),
) -> None:
    from thoth_terminal.workflow_benchmark_runner import build_evaluation_plan, load_json_object

    slate = load_json_object(slate_path)
    config_payload = load_json_object(configurations_path)
    configurations = config_payload.get("configurations")
    if not isinstance(configurations, list):
        raise typer.BadParameter("Configuration file must contain a configurations array.")
    manifests = [load_json_object(path) for path in sorted(manifests_dir.glob("*.json"))]
    structured_cases_by_file: dict[str, list[dict[str, object]]] = {}
    for track in slate.get("tracks") or []:
        if not isinstance(track, dict) or not track.get("structured_case_file"):
            continue
        structured_path = Path(str(track["structured_case_file"]))
        structured_payload = load_json_object(structured_path)
        cases = structured_payload.get("cases")
        if isinstance(cases, list):
            structured_cases_by_file[str(track["structured_case_file"])] = [row for row in cases if isinstance(row, dict)]
    plan = build_evaluation_plan(
        slate=slate,
        manifests=manifests,
        configurations=configurations,
        structured_cases_by_file=structured_cases_by_file,
    )
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(plan, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    if json_output:
        _print_json(plan, pretty=True)
        return
    console.print(
        f"[green]Built workflow benchmark plan[/green]: cells={plan['cell_count']} "
        f"configurations={plan['configuration_count']} statuses={plan['status_counts']}"
    )
    if output_path is not None:
        console.print(f"Wrote {output_path}")


@app.command("desktop")
def desktop_cmd(
    hidden: bool = typer.Option(False, "--hidden", help="Start minimized to the system tray."),
    no_worker: bool = typer.Option(
        False,
        "--no-worker",
        help="Launch the desktop shell without the background worker.",
    ),
) -> None:
    _live_runtime_start_guard(service="desktop", action="desktop")
    try:
        from thoth_terminal.desktop.app import launch_desktop_app
    except ImportError as exc:
        console.print(
            "[red]Desktop dependencies are missing.[/red] "
            'Install them with [bold]pip install -e ".[desktop]"[/bold].'
        )
        raise typer.Exit(1) from exc
    launch_desktop_app(start_hidden=bool(hidden), start_worker=(not bool(no_worker)))


mcp_app = typer.Typer(help="Call the MCP JSON-RPC endpoint exposed by the web server (/mcp).")
app.add_typer(mcp_app, name="mcp")
agent_api_app = typer.Typer(help="Manage revocable per-user keys for the public agent API.")
app.add_typer(agent_api_app, name="agent-api")
local_model_app = typer.Typer(help="Inspect, launch, probe, and benchmark the local model runtime.")
app.add_typer(local_model_app, name="local-model")
rl_app = typer.Typer(help="Generate and score offline RL/verifier datasets from Thoth state.")
app.add_typer(rl_app, name="rl")
profiler_app = typer.Typer(help="Run lightweight opt-in profiler captures.")
app.add_typer(profiler_app, name="profiler")


def _print_json(obj: object, *, pretty: bool = True) -> None:
    text = (
        json.dumps(obj, ensure_ascii=False, indent=2, default=str)
        if pretty
        else json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    )
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is None:
            raise
        buffer.write((text + "\n").encode("utf-8", errors="replace"))


def _load_json(value: str) -> object:
    raw = value.strip()
    if raw.startswith("@"):
        path = Path(raw[1:])
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise typer.BadParameter(f"Failed to read JSON from {path}: {exc}") from exc
    try:
        return json.loads(raw)
    except Exception as exc:
        raise typer.BadParameter(f"Invalid JSON: {exc}") from exc


@profiler_app.command("capture")
def profiler_capture_cmd(
    duration_s: float = typer.Option(30.0, "--duration-s", help="Profiler capture duration."),
    interval_s: float = typer.Option(1.0, "--interval-s", help="Seconds between samples."),
    output: Optional[Path] = typer.Option(None, "--output", help="JSONL output path."),
    device_label: Optional[str] = typer.Option(None, "--device-label", help="Human-readable device label."),
    process_regex: Optional[str] = typer.Option(
        None,
        "--process-regex",
        help="Regex used to select processes. Defaults to Thoth, worker, and model runtimes.",
    ),
    no_events: bool = typer.Option(False, "--no-events", help="Do not query local Thoth DB event tables."),
    event_lookback_s: float = typer.Option(
        120.0,
        "--event-lookback-s",
        help="Initial local-agent/ingestion event lookback window.",
    ),
    event_limit: int = typer.Option(200, "--event-limit", help="Maximum DB events per polling pass."),
    model_state_path: Optional[Path] = typer.Option(
        None,
        "--model-state-path",
        help="Optional model runtime state JSON path.",
    ),
    pretty: bool = typer.Option(True, "--pretty/--compact", help="Pretty-print summary JSON."),
) -> None:
    from thoth_terminal.profiler import ProfilerConfig, run_profiler

    summary = run_profiler(
        ProfilerConfig(
            duration_s=max(0.0, float(duration_s)),
            interval_s=max(0.1, float(interval_s)),
            output_path=output,
            device_label=device_label,
            process_regex=process_regex,
            include_events=not bool(no_events),
            event_lookback_s=max(0.0, float(event_lookback_s)),
            event_limit=max(1, int(event_limit)),
            model_state_path=model_state_path,
        )
    )
    _print_json(summary, pretty=pretty)


@rl_app.command("export")
def rl_export_cmd(
    tasks: str = typer.Option("all", "--tasks", help="Comma-separated task list or all."),
    limit_per_task: int = typer.Option(100, "--limit-per-task", help="Maximum examples to export for each task family."),
    output: Optional[Path] = typer.Option(None, "--output", help="Output JSONL path. Defaults to data/rl/thoth_rl_<stamp>.jsonl."),
    no_baseline: bool = typer.Option(False, "--no-baseline", help="Do not include deterministic baseline completions and scores."),
    pretty: bool = typer.Option(True, help="Pretty-print JSON summary."),
) -> None:
    from thoth_terminal.rl.training_data import generate_training_dataset

    payload = generate_training_dataset(
        tasks=tasks,
        limit_per_task=max(1, int(limit_per_task)),
        output_path=output,
        include_baseline=(not bool(no_baseline)),
    )
    _print_json(payload, pretty=pretty)


@rl_app.command("baseline")
def rl_baseline_cmd(
    input_path: Path = typer.Option(..., "--input", exists=True, file_okay=True, dir_okay=False, help="Input RL JSONL dataset."),
    output: Optional[Path] = typer.Option(None, "--output", help="Optional JSON summary output path."),
    pretty: bool = typer.Option(True, help="Pretty-print JSON summary."),
) -> None:
    from thoth_terminal.rl.training_data import score_baseline_dataset

    payload = score_baseline_dataset(input_path=input_path, output_path=output)
    _print_json(payload, pretty=pretty)


@local_model_app.command("runtime")
def local_model_runtime_cmd(
    pretty: bool = typer.Option(True, help="Pretty-print JSON output."),
) -> None:
    _print_json(resolve_local_model_runtime().as_dict(), pretty=pretty)


@local_model_app.command("launch-cmd")
def local_model_launch_cmd(
    shell: str = typer.Option("powershell", help="Shell flavor: powershell or bash."),
    json_output: bool = typer.Option(False, "--json", help="Print structured JSON instead of the shell line."),
) -> None:
    payload = launch_command_as_shell_line(shell=shell)
    if json_output:
        _print_json(payload, pretty=True)
        return
    line = payload.get("line")
    if not line:
        runtime = payload.get("runtime") if isinstance(payload, dict) else None
        warnings = (runtime or {}).get("warnings") if isinstance(runtime, dict) else []
        for warning in warnings or []:
            console.print(f"[yellow]{warning}[/yellow]")
        raise typer.Exit(1)
    typer.echo(str(line))


@local_model_app.command("probe")
def local_model_probe_cmd(
    timeout_s: float = typer.Option(10.0, help="HTTP timeout seconds."),
    pretty: bool = typer.Option(True, help="Pretty-print JSON output."),
) -> None:
    payload = probe_local_model_runtime(timeout_s=timeout_s)
    _print_json(payload, pretty=pretty)
    if not bool(payload.get("ok")):
        raise typer.Exit(1)


@local_model_app.command("ensure")
def local_model_ensure_cmd(
    service: str = typer.Option("manual", help="Logical launcher name for runtime state."),
    wait_s: float = typer.Option(0.0, help="Seconds to wait for readiness after launch."),
    pretty: bool = typer.Option(True, help="Pretty-print JSON output."),
) -> None:
    payload = ensure_local_model_runtime(
        service=service,
        require_opt_in=False,
        wait_s=max(0.0, float(wait_s)),
    )
    _print_json(payload, pretty=pretty)
    if str(payload.get("status") or "").strip().lower() in {"launch_failed", "not_launch_ready"}:
        raise typer.Exit(1)


@local_model_app.command("benchmark")
def local_model_benchmark_cmd(
    concurrency: int = typer.Option(4, help="Concurrent requests."),
    rounds: int = typer.Option(3, help="How many passes to run over the benchmark cases."),
    timeout_s: float = typer.Option(60.0, help="Per-request timeout seconds."),
    pretty: bool = typer.Option(True, help="Pretty-print JSON output."),
) -> None:
    try:
        payload = benchmark_local_model_runtime(
            concurrency=max(1, int(concurrency)),
            rounds=max(1, int(rounds)),
            timeout_s=float(timeout_s),
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    _print_json(payload, pretty=pretty)
    if not bool(payload.get("ok")):
        raise typer.Exit(1)


def _upsert_env_kv(env_path: Path, key: str, value: str) -> None:
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    lines = [ln for ln in lines if not ln.lstrip().startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mcp_client(base_url: str | None, token: str | None, timeout_s: float):
    from thoth_terminal.mcp_client import ThothMcpClient

    return ThothMcpClient(base_url=base_url, auth_token=token, timeout_s=timeout_s)


def _mcp_guard(fn):
    from thoth_terminal.mcp_client import McpRpcError, McpTransportError

    try:
        return fn()
    except (McpRpcError, McpTransportError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@agent_api_app.command("issue")
def agent_api_issue_cmd(
    principal: str = typer.Argument(..., help="Stable lowercase user identifier."),
    name: str = typer.Option(..., help="User display name."),
    scopes: str = typer.Option("models:read", help="Comma-separated scopes: models:read,evidence:upload,proposals:write."),
    valid_days: int = typer.Option(90, min=1, max=365),
) -> None:
    """Print a new key once; the database retains only its hash."""
    from thoth_terminal.external_agent_access_service import issue_agent_api_key

    _writer_guard("agent-api issue")
    init_db(allow_locked=True)
    with session_scope() as session:
        row, token = issue_agent_api_key(
            session, principal_slug=principal, display_name=name,
            scopes=[part.strip() for part in scopes.split(",") if part.strip()], valid_days=valid_days,
        )
        prefix = row.key_prefix
    typer.echo(f"key_prefix={prefix}")
    typer.echo(f"api_key={token}")


@agent_api_app.command("revoke")
def agent_api_revoke_cmd(prefix: str = typer.Argument(..., help="Key prefix shown when issued.")) -> None:
    from thoth_terminal.external_agent_access_service import revoke_agent_api_key

    _writer_guard("agent-api revoke")
    with session_scope() as session:
        revoked = revoke_agent_api_key(session, key_prefix=prefix)
    typer.echo("revoked" if revoked else "not_found_or_already_revoked")


@agent_api_app.command("list")
def agent_api_list_cmd(principal: str | None = typer.Option(None, help="Filter by principal slug.")) -> None:
    from thoth_terminal.models import ExternalAgentApiKey, ExternalAgentPrincipal

    with session_scope() as session:
        query = session.query(ExternalAgentApiKey, ExternalAgentPrincipal).join(
            ExternalAgentPrincipal, ExternalAgentPrincipal.id == ExternalAgentApiKey.principal_id,
        )
        if principal:
            query = query.filter(ExternalAgentPrincipal.slug == principal.strip().lower())
        rows = query.order_by(ExternalAgentApiKey.created_at.desc()).limit(100).all()
        _print_json_payload([{
            "principal": user.slug, "key_prefix": key.key_prefix,
            "scopes": key.scopes_json, "expires_at": key.expires_at.isoformat() if key.expires_at else None,
            "revoked": key.revoked_at is not None,
        } for key, user in rows], pretty=True)


@mcp_app.command("gen-token")
def mcp_gen_token_cmd(
    env_file: Path = typer.Option(Path(".env"), help="Env file to update"),
    print_only: bool = typer.Option(False, help="Do not write to env file"),
) -> None:
    token = secrets.token_urlsafe(32)
    if not print_only:
        _upsert_env_kv(env_file, "THOTH_MCP_AUTH_TOKEN", token)
    print(token)


@mcp_app.command("ping")
def mcp_ping_cmd(
    base_url: Optional[str] = typer.Option(None, help="Base URL (default: settings host/port)"),
    token: Optional[str] = typer.Option(None, help="Auth token (default: THOTH_MCP_AUTH_TOKEN)"),
    timeout_s: float = typer.Option(10.0, help="HTTP timeout seconds"),
    pretty: bool = typer.Option(True, help="Pretty-print JSON output"),
) -> None:
    client = _mcp_client(base_url, token, timeout_s)
    result = _mcp_guard(client.ping)
    _print_json({"endpoint": client.endpoint, "result": result}, pretty=pretty)


@mcp_app.command("initialize")
def mcp_initialize_cmd(
    base_url: Optional[str] = typer.Option(None, help="Base URL (default: settings host/port)"),
    token: Optional[str] = typer.Option(None, help="Auth token (default: THOTH_MCP_AUTH_TOKEN)"),
    protocol_version: str = typer.Option("2024-11-05", help="MCP protocol version"),
    timeout_s: float = typer.Option(10.0, help="HTTP timeout seconds"),
    pretty: bool = typer.Option(True, help="Pretty-print JSON output"),
) -> None:
    client = _mcp_client(base_url, token, timeout_s)
    result = _mcp_guard(lambda: client.initialize(protocol_version=protocol_version))
    _print_json(
        {"endpoint": client.endpoint, "result": result},
        pretty=pretty,
    )


@mcp_app.command("tools")
def mcp_tools_cmd(
    base_url: Optional[str] = typer.Option(None, help="Base URL (default: settings host/port)"),
    token: Optional[str] = typer.Option(None, help="Auth token (default: THOTH_MCP_AUTH_TOKEN)"),
    timeout_s: float = typer.Option(10.0, help="HTTP timeout seconds"),
    pretty: bool = typer.Option(True, help="Pretty-print JSON output"),
) -> None:
    client = _mcp_client(base_url, token, timeout_s)
    tools = _mcp_guard(client.list_tools)
    _print_json({"endpoint": client.endpoint, "tools": tools}, pretty=pretty)


@mcp_app.command("call")
def mcp_call_cmd(
    tool: str = typer.Argument(..., help="Tool name (see: thoth mcp tools)"),
    args: Optional[str] = typer.Option(None, help="JSON object, or @path/to/file.json"),
    base_url: Optional[str] = typer.Option(None, help="Base URL (default: settings host/port)"),
    token: Optional[str] = typer.Option(None, help="Auth token (default: THOTH_MCP_AUTH_TOKEN)"),
    timeout_s: float = typer.Option(30.0, help="HTTP timeout seconds"),
    pretty: bool = typer.Option(True, help="Pretty-print JSON output"),
) -> None:
    arguments: dict[str, object] = {}
    if args:
        loaded = _load_json(args)
        if not isinstance(loaded, dict):
            raise typer.BadParameter("--args must be a JSON object.")
        arguments = loaded

    client = _mcp_client(base_url, token, timeout_s)
    result = _mcp_guard(lambda: client.call_tool(tool, arguments))
    _print_json(
        {
            "endpoint": client.endpoint,
            "tool": tool,
            "arguments": arguments,
            "result": result,
        },
        pretty=pretty,
    )


@mcp_app.command("resources")
def mcp_resources_cmd(
    base_url: Optional[str] = typer.Option(None, help="Base URL (default: settings host/port)"),
    token: Optional[str] = typer.Option(None, help="Auth token (default: THOTH_MCP_AUTH_TOKEN)"),
    timeout_s: float = typer.Option(10.0, help="HTTP timeout seconds"),
    pretty: bool = typer.Option(True, help="Pretty-print JSON output"),
) -> None:
    client = _mcp_client(base_url, token, timeout_s)
    resources = _mcp_guard(client.list_resources)
    _print_json({"endpoint": client.endpoint, "resources": resources}, pretty=pretty)


@mcp_app.command("read")
def mcp_read_cmd(
    uri: str = typer.Argument(..., help="Resource URI (see: thoth mcp resources)"),
    base_url: Optional[str] = typer.Option(None, help="Base URL (default: settings host/port)"),
    token: Optional[str] = typer.Option(None, help="Auth token (default: THOTH_MCP_AUTH_TOKEN)"),
    timeout_s: float = typer.Option(30.0, help="HTTP timeout seconds"),
    pretty: bool = typer.Option(True, help="Pretty-print JSON output"),
) -> None:
    client = _mcp_client(base_url, token, timeout_s)
    result = _mcp_guard(lambda: client.read_resource(uri))
    _print_json(
        {"endpoint": client.endpoint, "uri": uri, "result": result},
        pretty=pretty,
    )


ingest_app = typer.Typer(help="Ingest content into the database.")
app.add_typer(ingest_app, name="ingest")

discord_app = typer.Typer(help="Discord bot (optional).")
app.add_typer(discord_app, name="discord")

integrations_app = typer.Typer(help="External integrations (OAuth/connectors).")
app.add_typer(integrations_app, name="integrations")

integrations_google_app = typer.Typer(help="Google OAuth integration (Gmail/Drive read-only).")
integrations_app.add_typer(integrations_google_app, name="google")


def _require_discord_dependency() -> None:
    try:
        import discord  # noqa: F401
    except Exception:
        console.print("[red]discord.py not installed.[/red] Run: `pip install -e '.[discord]'`")
        raise typer.Exit(1)


@discord_app.command("bot")
def discord_bot_cmd() -> None:
    """Run the Discord bot (slash commands)."""
    _writer_guard("discord bot")
    _require_discord_dependency()
    from thoth_terminal.discord.bot import run_bot

    run_bot()


@discord_app.command("up")
def discord_up_cmd() -> None:
    """Init DB, sync slash commands, and run the Discord bot."""
    _writer_guard("discord up")
    _require_discord_dependency()
    # Discord is normally launched by the desktop watchdog after the primary
    # runtime has prepared the schema. Do not kill the bot when an active
    # Postgres transaction temporarily blocks an idempotent startup index.
    init_db(allow_locked=True)
    from thoth_terminal.discord.bot import run_bot

    run_bot()


@discord_app.command("sync")
def discord_sync_cmd() -> None:
    """Sync slash commands to THOTH_DISCORD_GUILD_ID and exit."""
    _writer_guard("discord sync")
    _require_discord_dependency()
    from thoth_terminal.discord.bot import sync_once

    sync_once()


@ingest_app.command("note")
def ingest_note_cmd(
    title: str = typer.Option(..., help="Note title"),
    text: str = typer.Option(..., help="Note body"),
) -> None:
    _writer_guard("ingest note")
    with session_scope() as session:
        doc = ingest_note(session, title=title, text=text)
        console.print(f"[green]Saved note[/green] id={doc.id}")


@ingest_app.command("sources")
def ingest_sources_cmd(
    name: Optional[str] = typer.Option(None, help="Ingest only this source name"),
) -> None:
    _writer_guard("ingest sources")
    with session_scope() as session:
        sources = (
            session.query(Source).filter_by(name=name).all()
            if name
            else session.query(Source).order_by(Source.id.asc()).all()
        )
        for src in sources:
            cfg = src.config or {}
            if not cfg.get("connector"):
                console.print(f"[yellow]Skipping {src.name} (missing connector)[/yellow]")
                continue
            res = ingest_source(session, src)
            console.print(
                f"{src.name}: seen={res.seen} new={res.new} updated={res.updated}"
            )


@ingest_app.command("research-library")
def ingest_research_library_cmd(
    library_dir: Optional[str] = typer.Option(None, "--library-dir", help="Folder containing PDFs/Markdown research reports."),
) -> None:
    _writer_guard("ingest research library")
    with session_scope() as session:
        result = ingest_research_library(session, library_dir=library_dir)
        console.print(
            "[green]Research library ingested[/green] "
            f"source={result['source_name']} seen={result['seen']} new={result['new']} updated={result['updated']} "
            f"dir={result['library_dir']}"
        )


@app.command("research-library-review")
def research_library_review_cmd(
    guild_id: Optional[int] = typer.Option(None, "--guild-id", help="Optional Discord guild id for queued agent actions."),
    stale_only: bool = typer.Option(False, "--stale-only/--include-due-soon", help="Only create actions for stale reports, or include reports due soon."),
    review_window_days: int = typer.Option(14, "--review-window-days", help="Due-soon horizon when --include-due-soon is active."),
    limit: int = typer.Option(50, "--limit", help="Maximum reports to inspect."),
) -> None:
    _writer_guard("review research library")
    with session_scope() as session:
        result = review_research_library(
            session,
            guild_id=guild_id,
            stale_only=stale_only,
            review_window_days=review_window_days,
            limit=limit,
        )
        console.print(
            "[green]Research library reviewed[/green] "
            f"checked={result['checked_count']} candidates={result['candidate_count']} actions={result['action_count']}"
        )


@app.command("research-canon-maintenance")
def research_canon_maintenance_cmd(
    universe_slug: Optional[str] = typer.Option(None, "--universe", help="Limit the scan to one active universe slug."),
    create_requests: bool = typer.Option(False, "--create-requests/--no-create-requests", help="Create stable InstrumentRequest rows for critical actionable gaps."),
    create_cards: bool = typer.Option(False, "--create-cards/--no-create-cards", help="Regenerate compact agent canon cards while scanning."),
    stale_after_days: int = typer.Option(120, "--stale-after-days", help="Report stale threshold used by the quality evaluator."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional active universe row limit for smoke runs."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """Run the recurring-safe research coverage/canon maintenance scan."""
    _writer_guard("research canon maintenance")
    from thoth_terminal.models import Base
    from thoth_terminal.migrations import _ensure_research_canon_schema
    from thoth_terminal.research_canon_service import run_research_canon_maintenance

    engine = get_engine()
    Base.metadata.create_all(engine)
    _ensure_research_canon_schema(engine)
    with session_scope() as session:
        result = run_research_canon_maintenance(
            session,
            universe_slug=universe_slug,
            create_requests=create_requests,
            create_cards=create_cards,
            stale_after_days=stale_after_days,
            limit=limit,
        )
        _print_json(result, pretty=pretty)


@app.command("research-campaign-from-universe")
def research_campaign_from_universe_cmd(
    universe_slug: str = typer.Option(..., "--universe", help="Active universe slug to build the campaign from."),
    name: Optional[str] = typer.Option(None, "--name", help="Campaign name."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional item limit for pilot campaigns."),
    preview: bool = typer.Option(True, "--preview/--create", help="Preview items without writing, or create/update the campaign."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """Preview or create a durable research campaign from an active universe."""
    _writer_guard("research campaign from universe")
    from thoth_terminal.models import Base
    from thoth_terminal.migrations import _ensure_research_canon_schema
    from thoth_terminal.research_canon_service import create_campaign_from_universe

    engine = get_engine()
    Base.metadata.create_all(engine)
    _ensure_research_canon_schema(engine)
    with session_scope() as session:
        result = create_campaign_from_universe(
            session,
            universe_slug=universe_slug,
            name=name,
            limit=limit,
            preview=preview,
        )
        _print_json(result, pretty=pretty)


@app.command("research-canon-gaps")
def research_canon_gaps_cmd(
    universe_slug: Optional[str] = typer.Option(None, "--universe", help="Filter by active universe slug."),
    subject_id: Optional[int] = typer.Option(None, "--subject-id", help="Filter by research subject id."),
    gap_kind: Optional[str] = typer.Option(None, "--kind", help="Filter by gap kind."),
    status: Optional[str] = typer.Option("open", "--status", help="Filter by gap status."),
    limit: int = typer.Option(100, "--limit", help="Maximum gaps to return."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """List durable research-canon coverage gaps."""
    from thoth_terminal.research_canon_service import list_coverage_gaps

    with session_scope() as session:
        result = list_coverage_gaps(
            session,
            universe_slug=universe_slug,
            subject_id=subject_id,
            gap_kind=gap_kind,
            status=status,
            limit=limit,
        )
        _print_json_payload(result, pretty=pretty)


@app.command("research-canon-source-pack")
def research_canon_source_pack_cmd(
    subject_id: int = typer.Option(..., "--subject-id", help="Research subject id."),
    discover: bool = typer.Option(False, "--discover/--refresh-only", help="Create stable missing-item requests after refresh."),
    create_requests: bool = typer.Option(True, "--create-requests/--no-create-requests", help="Create InstrumentRequest rows for missing official sources."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """Refresh or discover official source-pack items for a subject."""
    _writer_guard("research canon source pack")
    from thoth_terminal.research_canon_service import discover_source_pack_items, serialize_source_pack, upsert_source_pack

    with session_scope() as session:
        if discover:
            result = discover_source_pack_items(session, subject_id=subject_id, create_requests=create_requests, audit={"source": "cli"})
        else:
            result = {"source_pack": serialize_source_pack(upsert_source_pack(session, subject_id=subject_id, audit={"source": "cli"}))}
        _print_json_payload(result, pretty=pretty)


@app.command("research-canon-source-packs")
def research_canon_source_packs_cmd(
    universe_slug: Optional[str] = typer.Option(None, "--universe", help="Limit to one active universe slug."),
    create_requests: bool = typer.Option(True, "--create-requests/--no-create-requests", help="Create API-first missing-item InstrumentRequest rows."),
    create_gap_requests: bool = typer.Option(True, "--create-gap-requests/--no-create-gap-requests", help="Create parent source-pack coverage-gap requests."),
    create_cards: bool = typer.Option(False, "--create-cards/--no-create-cards", help="Regenerate compact agent canon cards while scanning."),
    materialize_official_documents: bool = typer.Option(True, "--materialize-official-documents/--no-materialize-official-documents", help="Promote already-ingested official/API filings and exchange disclosures before evaluating packs."),
    register_official_sources: bool = typer.Option(True, "--register-official-sources/--no-register-official-sources", help="Register official/API source rails such as active-universe SEC filing feeds before evaluating packs."),
    run_coverage_scan: bool = typer.Option(False, "--coverage-scan/--no-coverage-scan", help="Also run the broader report-quality coverage scan."),
    stale_after_days: int = typer.Option(120, "--stale-after-days", help="Report stale threshold used by the quality evaluator."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional active universe row limit for smoke runs."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """Refresh API-first source-pack places for active-universe companies."""
    _writer_guard("research canon active-universe source packs")
    from thoth_terminal.models import Base
    from thoth_terminal.migrations import _ensure_research_canon_schema
    from thoth_terminal.research_canon_service import discover_active_universe_source_pack_items

    engine = get_engine()
    Base.metadata.create_all(engine)
    _ensure_research_canon_schema(engine)
    with session_scope() as session:
        result = discover_active_universe_source_pack_items(
            session,
            universe_slug=universe_slug,
            create_requests=create_requests,
            create_gap_requests=create_gap_requests,
            create_cards=create_cards,
            materialize_official_documents=materialize_official_documents,
            register_official_sources=register_official_sources,
            run_coverage_scan=run_coverage_scan,
            stale_after_days=stale_after_days,
            limit=limit,
            audit={"source": "cli"},
        )
        _print_json_payload(result, pretty=pretty)


@app.command("research-canon-source-pack-audit")
def research_canon_source_pack_audit_cmd(
    universe_slug: Optional[str] = typer.Option(None, "--universe", help="Limit to one active universe slug."),
    ensure_subjects: bool = typer.Option(False, "--ensure-subjects/--no-ensure-subjects", help="Create missing research subjects before auditing."),
    refresh_source_packs: bool = typer.Option(False, "--refresh-source-packs/--read-only", help="Persist refreshed source-pack rows while auditing."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional active universe row limit for smoke runs."),
    examples_limit: int = typer.Option(12, "--examples-limit", help="Maximum examples per audit bucket."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """Audit active-universe official/API source-pack coverage without generic scraping."""
    if ensure_subjects or refresh_source_packs:
        _writer_guard("research canon source pack audit")
    from thoth_terminal.models import Base
    from thoth_terminal.migrations import _ensure_research_canon_schema
    from thoth_terminal.research_canon_service import audit_active_universe_source_pack_coverage

    engine = get_engine()
    Base.metadata.create_all(engine)
    _ensure_research_canon_schema(engine)
    with session_scope() as session:
        result = audit_active_universe_source_pack_coverage(
            session,
            universe_slug=universe_slug,
            ensure_subjects=ensure_subjects,
            refresh_source_packs=refresh_source_packs,
            limit=limit,
            examples_limit=examples_limit,
        )
        _print_json_payload(result, pretty=pretty)


@app.command("research-canon-source-pack-backfill")
def research_canon_source_pack_backfill_cmd(
    universe_slug: Optional[str] = typer.Option(None, "--universe", help="Limit to one active universe slug."),
    rail: list[str] = typer.Option(None, "--rail", help="Official rail to run: sec, canada, asx, bursa, kazakhstan, sgx, nse, opendart, japan, taiwan, hkex, china, esef. Repeatable."),
    source_name: list[str] = typer.Option(None, "--source-name", help="Official source name to run. Repeatable and constrained to official rails."),
    max_runs_per_source: int = typer.Option(1, "--max-runs-per-source", help="Bounded ingest runs per official source."),
    register_official_sources: bool = typer.Option(True, "--register-official-sources/--no-register-official-sources", help="Refresh active-universe official source configs first."),
    materialize_official_documents: bool = typer.Option(True, "--materialize-official-documents/--no-materialize-official-documents", help="Promote fetched official documents into filing/disclosure rails."),
    materialize_limit: Optional[int] = typer.Option(None, "--materialize-limit", help="Maximum official documents to materialize in this run."),
    refresh_source_packs: bool = typer.Option(True, "--refresh-source-packs/--no-refresh-source-packs", help="Refresh source-pack rows after materialization."),
    include_audit: bool = typer.Option(True, "--audit/--no-audit", help="Include a post-run official coverage audit."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional active universe row limit for smoke runs."),
    resolve_esef_leis: bool = typer.Option(True, "--resolve-esef-leis/--no-resolve-esef-leis", help="Resolve missing ESEF LEIs through GLEIF before registering the ESEF source."),
    esef_lei_query_budget: int = typer.Option(80, "--esef-lei-query-budget", help="Maximum GLEIF lookup queries during ESEF source registration."),
    max_runtime_seconds: Optional[int] = typer.Option(None, "--max-runtime-seconds", help="Stop before starting additional sources/materialization once this runtime budget is exhausted."),
    run_transcript_scan: bool = typer.Option(False, "--run-transcript-scan/--no-run-transcript-scan", help="Run the official-material-gated transcript/call-material scan after the source-pack backfill."),
    transcript_scan_mode: str = typer.Option("gap_check", "--transcript-scan-mode", help="Transcript scan mode to use when --run-transcript-scan is set."),
    transcript_scan_lookback_days: Optional[int] = typer.Option(None, "--transcript-scan-lookback-days", help="Override transcript scan lookback days."),
    transcript_scan_max_candidates: Optional[int] = typer.Option(None, "--transcript-scan-max-candidates", help="Maximum transcript candidates to check after official material exists."),
    transcript_scan_max_runtime_seconds: Optional[int] = typer.Option(None, "--transcript-scan-max-runtime-seconds", help="Maximum transcript scan runtime inside this backfill."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """Run bounded official/API source backfills, materialization, and source-pack refresh."""
    _writer_guard("research canon source pack official backfill")
    from thoth_terminal.models import Base
    from thoth_terminal.migrations import _ensure_research_canon_schema
    from thoth_terminal.research_canon_service import backfill_active_universe_official_source_packs

    engine = get_engine()
    Base.metadata.create_all(engine)
    _ensure_research_canon_schema(engine)
    with session_scope() as session:
        result = backfill_active_universe_official_source_packs(
            session,
            universe_slug=universe_slug,
            rails=rail,
            source_names=source_name,
            max_runs_per_source=max_runs_per_source,
            register_official_sources=register_official_sources,
            materialize_official_documents=materialize_official_documents,
            materialize_limit=materialize_limit,
            refresh_source_packs=refresh_source_packs,
            include_audit=include_audit,
            limit=limit,
            resolve_esef_leis=resolve_esef_leis,
            esef_lei_query_budget=esef_lei_query_budget,
            max_runtime_seconds=max_runtime_seconds,
            run_transcript_scan=run_transcript_scan,
            transcript_scan_mode=transcript_scan_mode,
            transcript_scan_lookback_days=transcript_scan_lookback_days,
            transcript_scan_max_candidates=transcript_scan_max_candidates,
            transcript_scan_max_runtime_seconds=transcript_scan_max_runtime_seconds,
        )
        _print_json_payload(result, pretty=pretty)


@app.command("research-canon-quality")
def research_canon_quality_cmd(
    subject_id: int = typer.Option(..., "--subject-id", help="Research subject id."),
    stale_after_days: int = typer.Option(120, "--stale-after-days", help="Report stale threshold."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """Refresh report quality/readiness for a subject."""
    _writer_guard("research canon quality")
    from thoth_terminal.research_canon_service import serialize_quality, upsert_report_quality

    with session_scope() as session:
        result = {"quality": serialize_quality(upsert_report_quality(session, subject_id=subject_id, stale_after_days=stale_after_days))}
        _print_json_payload(result, pretty=pretty)


@app.command("research-canon-card")
def research_canon_card_cmd(
    subject_id: int = typer.Option(..., "--subject-id", help="Research subject id."),
    generate: bool = typer.Option(False, "--generate/--get", help="Regenerate the card before returning it."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """Get or regenerate an agent canon card."""
    if generate:
        _writer_guard("research canon card")
    from thoth_terminal.research_canon_service import generate_agent_canon_card, get_agent_canon_card

    with session_scope() as session:
        if generate:
            generate_agent_canon_card(session, subject_id=subject_id)
        result = {"agent_canon_card": get_agent_canon_card(session, subject_id=subject_id)}
        _print_json_payload(result, pretty=pretty)


@app.command("research-canon-scenario")
def research_canon_scenario_cmd(
    subject_id: int = typer.Option(..., "--subject-id", help="Research subject id."),
    slug: str = typer.Option(..., "--slug", help="Scenario slug."),
    name: Optional[str] = typer.Option(None, "--name", help="Scenario name."),
    kind: str = typer.Option("custom", "--kind", help="Scenario kind: bear/base/bull/custom/canon."),
    thesis_summary: Optional[str] = typer.Option(None, "--thesis", help="Short thesis summary."),
    selected_canon: bool = typer.Option(False, "--select-canon/--draft-only", help="Select this scenario as canon."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """Create or update a structured subject scenario."""
    _writer_guard("research canon scenario")
    from thoth_terminal.research_canon_service import serialize_scenario, upsert_subject_scenario

    with session_scope() as session:
        row = upsert_subject_scenario(
            session,
            subject_id=subject_id,
            slug=slug,
            name=name,
            scenario_kind=kind,
            selected_canon=selected_canon,
            thesis_summary=thesis_summary,
            audit={"source": "cli"},
        )
        _print_json_payload({"scenario": serialize_scenario(row)}, pretty=pretty)


@app.command("house-view-upsert")
def house_view_upsert_cmd(
    title: str = typer.Option(..., "--title", help="House-view thesis title."),
    statement: str = typer.Option(..., "--statement", help="User-origin thesis statement."),
    subject_id: Optional[int] = typer.Option(None, "--subject-id", help="Optional research subject id."),
    entity: list[str] = typer.Option([], "--entity", help="Entity token to attach; repeat for multiple entities."),
    consensus_view: Optional[str] = typer.Option(None, "--consensus", help="Consensus/sell-side view the house view is responding to."),
    variant_view: Optional[str] = typer.Option(None, "--variant", help="Variant/buy-side view."),
    model_driver: list[str] = typer.Option([], "--model-driver", help="Model driver; repeat for multiple drivers."),
    technology_assumption: list[str] = typer.Option([], "--technology-assumption", help="Technology assumption; repeat for multiple assumptions."),
    invalidation_trigger: list[str] = typer.Option([], "--invalidation", help="Invalidation trigger; repeat for multiple triggers."),
    open_question: list[str] = typer.Option([], "--open-question", help="Open question; repeat for multiple questions."),
    confidence: Optional[float] = typer.Option(None, "--confidence", help="Optional confidence from 0 to 1."),
    time_horizon: Optional[str] = typer.Option(None, "--time-horizon", help="Validity horizon, e.g. 2027-2030 or multi-year."),
    review_cadence_days: int = typer.Option(90, "--review-cadence-days", help="Default review cadence."),
    user_confirmed: bool = typer.Option(False, "--user-confirmed/--proposal-only", help="Required to write a self-authored house-view thesis."),
    idea_origin: str = typer.Option("user", "--idea-origin", help="Idea origin; must be user/operator/self for house-view writes."),
    client_request_id: Optional[str] = typer.Option(None, "--client-request-id", help="Stable idempotency key."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """Create or update a user-confirmed buy-side house-view thesis."""
    _writer_guard("upsert house-view thesis")
    from thoth_terminal.house_view_service import serialize_house_view_thesis, upsert_house_view_thesis

    with session_scope() as session:
        row = upsert_house_view_thesis(
            session,
            title=title,
            statement=statement,
            subject_id=subject_id,
            entity_tokens=entity,
            consensus_view=consensus_view,
            variant_view=variant_view,
            model_drivers=model_driver,
            technology_assumptions=technology_assumption,
            invalidation_triggers=invalidation_trigger,
            open_questions=open_question,
            confidence=confidence,
            time_horizon=time_horizon,
            review_cadence_days=review_cadence_days,
            idea_origin=idea_origin,
            user_confirmed=user_confirmed,
            client_request_id=client_request_id,
            audit={"source": "cli", "command": "house-view-upsert"},
        )
        _print_json({"house_view": serialize_house_view_thesis(row)}, pretty=pretty)


@app.command("house-view-list")
def house_view_list_cmd(
    subject_id: Optional[int] = typer.Option(None, "--subject-id", help="Filter to one research subject id."),
    include_inactive: bool = typer.Option(False, "--include-inactive/--active-only", help="Include inactive/archive statuses."),
    limit: int = typer.Option(50, "--limit", help="Maximum rows."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """List user-origin house-view theses."""
    from thoth_terminal.house_view_service import list_house_view_theses

    with session_scope() as session:
        rows = list_house_view_theses(session, subject_id=subject_id, include_inactive=include_inactive, limit=limit)
        _print_json({"count": len(rows), "house_views": rows}, pretty=pretty)


@app.command("house-view-context")
def house_view_context_cmd(
    subject_id: int = typer.Option(..., "--subject-id", help="Research subject id."),
    mode: str = typer.Option("both", "--mode", help="buy_side, sell_side, consensus, house_view, or both."),
    limit: int = typer.Option(8, "--limit", help="Maximum context rows per rail."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """Return compact buy-side/sell-side context for a subject."""
    from thoth_terminal.house_view_service import build_research_mode_context

    with session_scope() as session:
        _print_json(build_research_mode_context(session, subject_id=subject_id, mode=mode, limit=limit), pretty=pretty)


@app.command("house-view-questions")
def house_view_questions_cmd(
    subject_id: Optional[int] = typer.Option(None, "--subject-id", help="Research subject id."),
    universe_slug: Optional[str] = typer.Option(None, "--universe", help="Active universe slug."),
    theme: Optional[str] = typer.Option(None, "--theme", help="Theme when no subject/universe is supplied."),
    limit: int = typer.Option(10, "--limit", help="Maximum questions."),
    cadence_days: int = typer.Option(90, "--cadence-days", help="Question cooldown/review cadence."),
    create_requests: bool = typer.Option(False, "--create-requests/--preview", help="Create stable InstrumentRequest rows."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """Generate deduped house-view interview questions from consensus/agent context."""
    if create_requests:
        _writer_guard("generate house-view questions")
    from thoth_terminal.house_view_service import generate_house_view_questions

    with session_scope() as session:
        result = generate_house_view_questions(
            session,
            subject_id=subject_id,
            universe_slug=universe_slug,
            theme=theme,
            limit=limit,
            cadence_days=cadence_days,
            create_requests=create_requests,
        )
        _print_json(result, pretty=pretty)


@app.command("source-actor-upsert")
def source_actor_upsert_cmd(
    name: str = typer.Option(..., "--name", help="Display name for the person, firm, publication, account, or data source."),
    actor_type: str = typer.Option("unknown", "--actor-type", help="person, firm, publication, account, data_vendor, research_org, issuer, or unknown."),
    handle: Optional[str] = typer.Option(None, "--handle", help="Optional X/Twitter handle alias."),
    firm_alias: Optional[str] = typer.Option(None, "--firm-alias", help="Optional firm-name alias."),
    side: Optional[str] = typer.Option(None, "--side", help="buy_side, sell_side, specialist_research, independent, media, issuer, or unknown."),
    trust_label: Optional[str] = typer.Option(None, "--trust-label", help="Optional source trust label."),
    notes: Optional[str] = typer.Option(None, "--notes", help="Operator note."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """Create or update a durable source actor and optional aliases."""
    _writer_guard("upsert source actor")
    from thoth_terminal.institutional_model_vocab import ensure_registered_source_actor
    from thoth_terminal.source_identity_service import serialize_source_actor

    with session_scope() as session:
        aliases = [firm_alias] if firm_alias else []
        row = ensure_registered_source_actor(
            session,
            {
                "name": name,
                "actor_type": actor_type,
                "handle": handle,
                "side": side,
                "trust_label": trust_label,
                "notes": notes,
                "aliases": aliases,
                "payload": {"source": "cli"},
            },
            actor="cli",
        )
        session.commit()
        _print_json({"source_actor": serialize_source_actor(row)}, pretty=pretty)


@app.command("source-actors")
def source_actors_cmd(
    query: Optional[str] = typer.Option(None, "--query", help="Display-name search."),
    actor_type: Optional[str] = typer.Option(None, "--actor-type", help="Optional actor-type filter."),
    side: Optional[str] = typer.Option(None, "--side", help="Optional source-side filter."),
    limit: int = typer.Option(50, "--limit", help="Maximum rows."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """List durable source actors."""
    from thoth_terminal.source_identity_service import list_source_actors

    with session_scope() as session:
        rows = list_source_actors(session, query=query, actor_type=actor_type, side=side, limit=limit)
        _print_json({"count": len(rows), "source_actors": rows}, pretty=pretty)


@app.command("source-affiliation-upsert")
def source_affiliation_upsert_cmd(
    firm: str = typer.Option(..., "--firm", help="Organization/firm name."),
    person_name: Optional[str] = typer.Option(None, "--person-name", help="Person/account display name."),
    person_handle: Optional[str] = typer.Option(None, "--person-handle", help="X/Twitter handle for the person/account."),
    role_class: str = typer.Option("analyst", "--role-class", help="research_analyst, analyst, strategist, portfolio_manager, journalist, or unknown."),
    role_label: Optional[str] = typer.Option(None, "--role-label", help="Freeform title/role label."),
    side: str = typer.Option("unknown", "--side", help="sell_side, buy_side, specialist_research, independent, media, issuer, or unknown."),
    start_date: Optional[str] = typer.Option(None, "--start-date", help="YYYY-MM-DD if known."),
    end_date: Optional[str] = typer.Option(None, "--end-date", help="YYYY-MM-DD if known."),
    confidence: Optional[float] = typer.Option(None, "--confidence", help="0-1 confidence."),
    evidence_note: Optional[str] = typer.Option(None, "--evidence-note", help="Why this affiliation is proven."),
    source_url: Optional[str] = typer.Option(None, "--source-url", help="Optional provenance URL."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """Create or update a dated source affiliation. Unseen actors remain unaffiliated until this is called or explicit evidence is ingested."""
    _writer_guard("upsert source affiliation")
    from thoth_terminal.source_identity_service import (
        resolve_source_actor,
        serialize_source_actor,
        serialize_source_affiliation,
        upsert_source_affiliation,
    )

    with session_scope() as session:
        person = resolve_source_actor(
            session,
            name=person_name,
            handle=person_handle,
            actor_type="person" if person_name or person_handle else "unknown",
            actor="cli",
        )
        if person is None:
            raise typer.BadParameter("Provide --person-name or --person-handle.")
        organization = resolve_source_actor(session, name=firm, firm=firm, actor_type="firm", actor="cli")
        if organization is None:
            raise typer.BadParameter("Provide --firm.")
        row = upsert_source_affiliation(
            session,
            person_actor=person,
            organization_actor=organization,
            role_label=role_label,
            role_class=role_class,
            side=side,
            start_date=start_date,
            end_date=end_date,
            confidence=confidence,
            source_url=source_url,
            evidence_note=evidence_note,
            payload={"source": "cli"},
            actor="cli",
        )
        session.commit()
        _print_json(
            {
                "source_affiliation": serialize_source_affiliation(row),
                "person": serialize_source_actor(person),
                "organization": serialize_source_actor(organization),
            },
            pretty=pretty,
        )


@app.command("research-campaign-next")
def research_campaign_next_cmd(
    campaign_id: int = typer.Option(..., "--campaign-id", help="Research campaign id."),
    limit: int = typer.Option(10, "--limit", help="Maximum items to return."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """List next eligible research campaign items."""
    _writer_guard("research campaign next")
    from thoth_terminal.research_canon_service import next_campaign_items

    with session_scope() as session:
        rows = next_campaign_items(session, campaign_id=campaign_id, limit=limit)
        _print_json_payload({"count": len(rows), "items": rows}, pretty=pretty)


@app.command("research-campaign-run")
def research_campaign_run_cmd(
    campaign_id: Optional[int] = typer.Option(None, "--campaign-id", help="Research campaign id."),
    campaign_key: Optional[str] = typer.Option(None, "--campaign-key", help="Research campaign key."),
    limit: int = typer.Option(3, "--limit", help="Maximum campaign items to execute."),
    dry_run: bool = typer.Option(True, "--dry-run/--execute", help="Preview selected items or execute them."),
    force: bool = typer.Option(False, "--force", help="Force report rebuilds."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """Run a bounded set of eligible research campaign items."""
    _writer_guard("research campaign run")
    from thoth_terminal.research_canon_service import execute_campaign_items

    with session_scope() as session:
        result = execute_campaign_items(
            session,
            campaign_id=campaign_id,
            campaign_key=campaign_key,
            limit=limit,
            dry_run=dry_run,
            force=force,
        )
        _print_json_payload(result, pretty=pretty)


@app.command("bottleneck-report-start")
def bottleneck_report_start_cmd(
    model_key: str = typer.Option(..., "--model-key", help="Stable bottleneck model key."),
    title: str = typer.Option(..., "--title", help="Human-readable bottleneck report title."),
    scope_type: str = typer.Option("theme", "--scope-type", help="Scope type, such as theme, company, site, or program."),
    scope_key: Optional[str] = typer.Option(None, "--scope-key", help="Optional scope key."),
    constrained_input: Optional[str] = typer.Option(None, "--constrained-input", help="Constrained input being tested."),
    output_unit: Optional[str] = typer.Option(None, "--output-unit", help="Downstream output unit constrained by the input."),
    supply_chain_id: Optional[int] = typer.Option(None, "--supply-chain-id", help="Optional supply chain id."),
    subject_id: Optional[int] = typer.Option(None, "--subject-id", help="Optional research subject id."),
    summary: Optional[str] = typer.Option(None, "--summary", help="Short scope summary."),
    thesis: Optional[str] = typer.Option(None, "--thesis", help="Initial bottleneck thesis."),
    payload_json: Optional[str] = typer.Option(None, "--payload-json", help="JSON object or @path with model payload details."),
    links_json: Optional[str] = typer.Option(None, "--links-json", help="JSON array or @path of BottleneckModelLink specs."),
    campaign_key: Optional[str] = typer.Option(None, "--campaign-key", help="Optional campaign key."),
    campaign_name: Optional[str] = typer.Option(None, "--campaign-name", help="Optional campaign name."),
    priority: int = typer.Option(8, "--priority", help="Campaign item priority 1-10."),
    execute: bool = typer.Option(False, "--execute/--queue-only", help="Immediately execute one queued bottleneck campaign item."),
    force: bool = typer.Option(False, "--force", help="Force report rebuild if executing."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """Start a user-defined bottleneck report and optionally build the first workspace version."""
    _writer_guard("start bottleneck report")
    from thoth_terminal.models import BottleneckModel, ResearchCampaign, ResearchCampaignItem
    from thoth_terminal.research_canon_service import bottleneck_startup_packet, execute_campaign_items, serialize_campaign_item, start_bottleneck_report

    payload: dict[str, object] | None = None
    if payload_json:
        loaded_payload = _load_json(payload_json)
        if not isinstance(loaded_payload, dict):
            raise typer.BadParameter("--payload-json must be a JSON object or @path to one.")
        payload = dict(loaded_payload)
    links: list[dict[str, object]] | None = None
    if links_json:
        loaded_links = _load_json(links_json)
        if not isinstance(loaded_links, list) or not all(isinstance(item, dict) for item in loaded_links):
            raise typer.BadParameter("--links-json must be a JSON array of objects or @path to one.")
        links = [dict(item) for item in loaded_links]

    with session_scope() as session:
        start_result = start_bottleneck_report(
            session,
            model_key=model_key,
            title=title,
            scope_type=scope_type,
            scope_key=scope_key,
            constrained_input=constrained_input,
            output_unit=output_unit,
            supply_chain_id=supply_chain_id,
            subject_id=subject_id,
            summary=summary,
            thesis=thesis,
            payload=payload,
            links=links,
            campaign_key=campaign_key,
            campaign_name=campaign_name,
            priority=priority,
        )
        execute_result = None
        if execute:
            campaign = start_result.get("campaign") if isinstance(start_result, dict) else {}
            campaign_id = int(campaign.get("id")) if isinstance(campaign, dict) and str(campaign.get("id") or "").isdigit() else None
            execute_result = execute_campaign_items(
                session,
                campaign_id=campaign_id,
                campaign_key=campaign_key,
                limit=1,
                dry_run=False,
                force=force,
            )
            item = session.get(ResearchCampaignItem, int((start_result.get("item") or {}).get("id") or 0))
            campaign_row = session.get(ResearchCampaign, int(campaign_id or 0)) if campaign_id else None
            model = session.get(BottleneckModel, int((start_result.get("bottleneck_model") or {}).get("id") or 0))
            if model is not None:
                start_result["startup_packet"] = bottleneck_startup_packet(session, model=model, campaign=campaign_row, item=item)
                if item is not None:
                    start_result["item"] = serialize_campaign_item(item)
        session.commit()
        _print_json_payload(
            {
                "ok": True,
                "start": start_result,
                "executed": bool(execute),
                "execute_result": execute_result,
            },
            pretty=pretty,
        )


@app.command("research-identity-repair")
def research_identity_repair_cmd(
    instrument_id: int = typer.Option(..., "--instrument-id", help="Research instrument id to relink."),
    issuer_id: Optional[int] = typer.Option(None, "--issuer-id", help="Existing target issuer id."),
    company_id: Optional[int] = typer.Option(None, "--company-id", help="Existing target company id."),
    subject_id: Optional[int] = typer.Option(None, "--subject-id", help="Existing target research subject id."),
    issuer_name: Optional[str] = typer.Option(None, "--issuer-name", help="Target issuer legal/display name if creation is needed."),
    company_name: Optional[str] = typer.Option(None, "--company-name", help="Target company name if creation is needed."),
    ticker: Optional[str] = typer.Option(None, "--ticker", help="Target listing ticker."),
    exchange: Optional[str] = typer.Option(None, "--exchange", help="Target listing exchange."),
    jurisdiction: Optional[str] = typer.Option(None, "--jurisdiction", help="Target issuer jurisdiction."),
    website: Optional[str] = typer.Option(None, "--website", help="Official company website."),
    ir_website: Optional[str] = typer.Option(None, "--ir-website", help="Official investor-relations website."),
    gap_id: Optional[int] = typer.Option(None, "--gap-id", help="Coverage gap id to resolve on apply."),
    gap_key: Optional[str] = typer.Option(None, "--gap-key", help="Coverage gap key to resolve on apply."),
    evidence_url: list[str] = typer.Option([], "--evidence-url", help="Official evidence URL. Repeat for multiple sources."),
    evidence_label: list[str] = typer.Option([], "--evidence-label", help="Optional label for evidence URL at the same position."),
    note: Optional[str] = typer.Option(None, "--note", help="Audit note for the repair."),
    resolve_gap: bool = typer.Option(True, "--resolve-gap/--keep-gap-open", help="Resolve the linked gap after apply."),
    apply: bool = typer.Option(False, "--apply/--preview", help="Apply the repair or only preview the diff."),
    pretty: bool = typer.Option(False, "--pretty/--compact", help="Pretty-print JSON output."),
) -> None:
    """Preview or apply a bounded issuer/company/subject relink for one instrument."""
    if apply:
        _writer_guard("research identity repair")
    from thoth_terminal.research_canon_service import research_identity_repair

    evidence_links = []
    for idx, url in enumerate(evidence_url or []):
        label = evidence_label[idx] if idx < len(evidence_label) else None
        evidence_links.append({"url": url, "label": label or url})
    with session_scope() as session:
        result = research_identity_repair(
            session,
            instrument_id=instrument_id,
            apply=apply,
            issuer_id=issuer_id,
            company_id=company_id,
            subject_id=subject_id,
            issuer_name=issuer_name,
            company_name=company_name,
            ticker=ticker,
            exchange=exchange,
            jurisdiction=jurisdiction,
            website=website,
            ir_website=ir_website,
            evidence_links=evidence_links,
            gap_id=gap_id,
            gap_key=gap_key,
            resolve_gap=resolve_gap,
            note=note,
        )
        _print_json(result, pretty=pretty)


@app.command("research-alias-repair")
def research_alias_repair_cmd(
    canonical_issuer_id: list[int] = typer.Option([], "--canonical-issuer-id", help="Canonical issuer to repair aliases for; repeat for a bounded set. Required for apply."),
    apply: bool = typer.Option(False, "--apply/--preview", help="Apply or preview unambiguous cross-issuer alias removals."),
    expected_fingerprint: Optional[str] = typer.Option(None, "--expected-fingerprint", help="Fingerprint returned by the preview."),
    note: Optional[str] = typer.Option(None, "--note", help="Audit note recorded on each affected subject or identity."),
    pretty: bool = typer.Option(False, "--pretty/--compact"),
) -> None:
    """Repair identity aliases without relinking companies, listings or evidence."""
    if apply:
        _writer_guard("research alias repair")
    from thoth_terminal.identity_alias_service import repair_identity_aliases
    with session_scope() as session:
        result = repair_identity_aliases(session, apply=apply,
            expected_fingerprint=expected_fingerprint, note=note, canonical_issuer_ids=canonical_issuer_id)
        _print_json(result, pretty=pretty)


@app.command("source-status")
def source_status_cmd(
    name: str = typer.Option(..., help="Source name to inspect"),
) -> None:
    with session_scope() as session:
        src = session.query(Source).filter_by(name=name).first()
        if not src:
            console.print(f"[red]Source not found[/red]: {name}")
            return
        state = session.query(SourceState).filter_by(source_id=src.id).first()
        last_run = (
            session.query(IngestionRun)
            .filter_by(source_id=src.id)
            .order_by(IngestionRun.id.desc())
            .first()
        )
        console.print(f"[bold]{src.name}[/bold] (id={src.id})")
        console.print(f"connector: {(src.config or {}).get('connector')}")
        console.print(f"enabled: {(src.config or {}).get('enabled')}")
        console.print(f"interval_s: {src.polling_interval_seconds}")
        if state:
            console.print(f"last_run_at: {state.last_run_at}")
            console.print(f"last_success_at: {state.last_success_at}")
            console.print(f"next_run_at: {state.next_run_at}")
            console.print(f"consecutive_failures: {state.consecutive_failures}")
            console.print(f"backoff_until: {state.backoff_until}")
        else:
            console.print("[yellow]No SourceState row yet[/yellow]")
        if last_run:
            console.print(f"last_status: {last_run.status}")
            console.print(f"last_started: {last_run.started_at}")
            console.print(f"last_finished: {last_run.finished_at}")
            console.print(f"last_error: {last_run.error}")
            console.print(f"last_counts: seen={last_run.items_seen} new={last_run.items_new} updated={last_run.items_updated}")


@app.command("source-schedule")
def source_schedule_cmd(
    name: str = typer.Option(..., help="Source name to inspect scheduling"),
) -> None:
    from datetime import timezone
    from thoth_terminal.db import utcnow
    from zoneinfo import ZoneInfo

    with session_scope() as session:
        src = session.query(Source).filter_by(name=name).first()
        if not src:
            console.print(f"[red]Source not found[/red]: {name}")
            return
        state = session.query(SourceState).filter_by(source_id=src.id).first()

    now = utcnow()
    now_utc = now.replace(tzinfo=timezone.utc)
    min_s, max_s = _poll_range_seconds(src)
    start, end, tz = _window_config(src)
    in_window = None
    now_local = None
    if start and end and tz:
        now_local = now_utc.astimezone(tz)
        in_window = _is_within_window(now_local, start, end)

    console.print(f"[bold]{src.name}[/bold] (id={src.id})")
    console.print(f"connector: {(src.config or {}).get('connector')}")
    console.print(f"poll_range_s: {min_s}..{max_s}")
    if start and end and tz:
        console.print(f"window: {start} -> {end} ({tz.key})")
        console.print(f"now_local: {now_local.strftime('%Y-%m-%d %H:%M:%S')}")
        console.print(f"in_window: {in_window}")
    else:
        console.print("window: none")

    if state:
        def _fmt(dt):
            if not dt:
                return None
            dt_utc = dt.replace(tzinfo=timezone.utc)
            if tz:
                try:
                    dt_local = dt_utc.astimezone(tz)
                    return f"{dt_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC / {dt_local.strftime('%Y-%m-%d %H:%M:%S')} {tz.key}"
                except Exception:
                    return f"{dt_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC"
            return f"{dt_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC"

        console.print(f"last_run_at: {_fmt(state.last_run_at)}")
        console.print(f"next_run_at: {_fmt(state.next_run_at)}")
        console.print(f"backoff_until: {_fmt(state.backoff_until)}")
        console.print(f"consecutive_failures: {state.consecutive_failures}")
    else:
        console.print("[yellow]No SourceState row yet[/yellow]")

    due = True
    reasons = []
    if state and state.backoff_until and state.backoff_until > now:
        due = False
        reasons.append(f"backoff_until {state.backoff_until}")
    if state and state.next_run_at and state.next_run_at > now:
        due = False
        reasons.append(f"next_run_at {state.next_run_at}")
    if start and end and tz and in_window is False:
        due = False
        next_window = _next_window_start_utc(now_utc, start, tz).replace(tzinfo=None)
        reasons.append(f"outside_window next_window {next_window}")
    console.print(f"due_now: {due}")
    if reasons:
        console.print("reasons:")
        for r in reasons:
            console.print(f"  - {r}")


@app.command("backfill-money")
def backfill_money_cmd(
    days: int = typer.Option(30, help="Only process docs within N days (0 = all)"),
    source_id: Optional[int] = typer.Option(None, help="Limit to a source id"),
    limit: int = typer.Option(2000, help="Max docs to process per run"),
) -> None:
    _writer_guard("backfill-money")
    with session_scope() as session:
        ensure_fx_rates(session, base=str(settings.base_currency or "USD"))
        q = session.query(Document)
        if days and days > 0:
            from datetime import timedelta
            from sqlalchemy import func
            from thoth_terminal.db import utcnow

            since = utcnow() - timedelta(days=int(days))
            q = q.filter(func.coalesce(Document.published_at, Document.ingested_at) >= since)
        if source_id is not None:
            q = q.filter(Document.source_id == int(source_id))
        docs = q.order_by(Document.id.desc()).limit(int(limit)).all()
        processed = 0
        for doc in docs:
            src = session.query(Source).filter_by(id=int(doc.source_id)).first()
            cfg = dict(src.config or {}) if src else {}
            _apply_money_mentions(session, doc, cfg=cfg)
            processed += 1
        console.print(f"[green]Backfilled money mentions[/green] docs={processed}")


@app.command("run")
def run_scheduler_cmd(
    interval_s: int = typer.Option(30, help="Scheduler tick interval seconds"),
) -> None:
    _writer_guard("run")
    _live_runtime_start_guard(service="worker", action="run")
    console.print("[yellow]run is now a worker loop (scheduler + jobs).[/yellow]")
    console.print("[cyan]Worker running[/cyan] (Ctrl+C to stop)")
    run_worker(scheduler_interval_s=int(interval_s))


@app.command("worker")
def worker_cmd() -> None:
    _writer_guard("worker")
    _live_runtime_start_guard(service="worker", action="worker")
    console.print("[cyan]Worker running[/cyan] (Ctrl+C to stop)")
    run_worker()


@app.command("cloud-worker")
def cloud_worker_cmd() -> None:
    _writer_guard("cloud-worker")
    _live_runtime_start_guard(service="cloud-worker", action="cloud-worker")
    console.print("[cyan]Cloud worker running[/cyan] (Ctrl+C to stop)")
    run_cloud_worker()


@app.command("export")
def export_cmd(
    ids: str = typer.Option(..., help="Comma-separated document IDs"),
    label: Optional[str] = typer.Option(None, help="Bundle label"),
) -> None:
    _writer_guard("export")
    doc_ids = [int(i) for i in ids.split(",") if i.strip().isdigit()]
    with session_scope() as session:
        out = export_bundle(session, document_ids=doc_ids, label=label)
    console.print(f"[green]Exported[/green] {out}")


@app.command("show")
def show_cmd(doc_id: int) -> None:
    with session_scope() as session:
        doc = session.query(Document).filter_by(id=doc_id).first()
        if not doc:
            raise typer.Exit(1)
        console.print(f"[bold]{doc.title or doc.url or doc_id}[/bold]")
        console.print(doc.translation_en or doc.normalized_text)


@app.command("x-login")
def x_login_cmd(
    url: str = typer.Option("https://x.com/i/flow/login", help="Login URL (or your list URL)"),
    storage: Optional[str] = typer.Option(None, help="Storage state path (default: data/x_storage_state.json)"),
    wait_s: int = typer.Option(900, help="Seconds to wait before auto-saving session"),
    channel: Optional[str] = typer.Option(None, help="Browser channel (e.g. msedge, chrome)"),
    user_data_dir: Optional[str] = typer.Option(None, help="Persistent profile directory"),
    slow_mo_ms: int = typer.Option(0, help="Slow Playwright actions (ms)"),
) -> None:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        console.print("[red]Playwright not installed.[/red] Run: pip install playwright && python -m playwright install chromium")
        raise typer.Exit(1)

    storage_path = Path(storage) if storage else settings.x_storage_state_path
    storage_path.parent.mkdir(parents=True, exist_ok=True)

    console.print("[cyan]Opening browser for X login...[/cyan]")
    console.print(
        "Log in (Google sign-in is fine). When you see your home/list, return here and press Enter."
    )
    console.print(f"If you don't press Enter, it will wait up to {wait_s}s before saving.")

    launch_kwargs: dict[str, object] = {"headless": False}
    if channel:
        launch_kwargs["channel"] = channel
    if slow_mo_ms and slow_mo_ms > 0:
        launch_kwargs["slow_mo"] = int(slow_mo_ms)

    with sync_playwright() as p:
        browser = None
        if user_data_dir:
            context = p.chromium.launch_persistent_context(user_data_dir, **launch_kwargs)
        else:
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context()

        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_navigation_timeout(120_000)
        page.set_default_timeout(120_000)
        page.goto(url, wait_until="domcontentloaded")
        if sys.stdin.isatty():
            try:
                input()
            except EOFError:
                time.sleep(max(0, int(wait_s)))
        else:
            time.sleep(max(0, int(wait_s)))
        context.storage_state(path=str(storage_path))
        context.close()
        if browser:
            browser.close()

    console.print(f"[green]Saved X session[/green] at {storage_path}")


@app.command("browser-login")
def browser_login_cmd(
    url: str = typer.Option(..., help="Login or protected URL"),
    storage: str = typer.Option("data/browser_storage_state.json", help="Storage state output path"),
    wait_s: int = typer.Option(900, help="Seconds to wait before auto-saving session"),
    channel: Optional[str] = typer.Option(None, help="Browser channel (e.g. msedge, chrome)"),
    user_data_dir: Optional[str] = typer.Option(None, help="Persistent profile directory"),
    slow_mo_ms: int = typer.Option(0, help="Slow Playwright actions (ms)"),
) -> None:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        console.print(
            "[red]Playwright not installed.[/red] Run: pip install playwright && python -m playwright install chromium"
        )
        raise typer.Exit(1)

    storage_path = Path(storage)
    storage_path.parent.mkdir(parents=True, exist_ok=True)

    console.print("[cyan]Opening browser for login...[/cyan]")
    console.print("Complete any cookie/CF challenge, then return here and press Enter.")
    console.print(f"If you don't press Enter, it will wait up to {wait_s}s before saving.")

    launch_kwargs: dict[str, object] = {"headless": False}
    if channel:
        launch_kwargs["channel"] = channel
    if slow_mo_ms and slow_mo_ms > 0:
        launch_kwargs["slow_mo"] = int(slow_mo_ms)

    with sync_playwright() as p:
        browser = None
        if user_data_dir:
            context = p.chromium.launch_persistent_context(user_data_dir, **launch_kwargs)
        else:
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context()

        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_navigation_timeout(120_000)
        page.set_default_timeout(120_000)
        page.goto(url, wait_until="domcontentloaded")
        if sys.stdin.isatty():
            try:
                input()
            except EOFError:
                time.sleep(max(0, int(wait_s)))
        else:
            time.sleep(max(0, int(wait_s)))
        context.storage_state(path=str(storage_path))
        context.close()
        if browser:
            browser.close()

    console.print(f"[green]Saved storage state[/green] at {storage_path}")


@integrations_google_app.command("status")
def integrations_google_status_cmd(
    pretty: bool = typer.Option(True, help="Pretty-print JSON output"),
) -> None:
    from thoth_terminal.google_oauth import status as google_status

    try:
        payload = {"ok": True, "status": google_status().as_dict()}
    except Exception as exc:
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    _print_json(payload, pretty=pretty)
    if not payload.get("ok"):
        raise typer.Exit(1)


@integrations_google_app.command("connect")
def integrations_google_connect_cmd(
    open_browser: bool = typer.Option(True, "--open-browser/--no-open-browser", help="Open browser automatically"),
    pretty: bool = typer.Option(True, help="Pretty-print JSON output"),
) -> None:
    _writer_guard("integrations google connect")
    from thoth_terminal.google_oauth import connect_cli

    try:
        st = connect_cli(open_browser=bool(open_browser))
        _print_json({"ok": True, "status": st.as_dict()}, pretty=pretty)
    except Exception as exc:
        _print_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, pretty=pretty)
        raise typer.Exit(1) from exc


@integrations_google_app.command("disconnect")
def integrations_google_disconnect_cmd(
    pretty: bool = typer.Option(True, help="Pretty-print JSON output"),
) -> None:
    _writer_guard("integrations google disconnect")
    from thoth_terminal.google_oauth import disconnect as google_disconnect

    try:
        removed = bool(google_disconnect())
        _print_json({"ok": True, "removed": removed}, pretty=pretty)
    except Exception as exc:
        _print_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, pretty=pretty)
        raise typer.Exit(1) from exc


@integrations_google_app.command("test")
def integrations_google_test_cmd(
    service: str = typer.Option(..., "--service", help="gmail or drive"),
    pretty: bool = typer.Option(True, help="Pretty-print JSON output"),
) -> None:
    svc = (service or "").strip().lower()
    from thoth_terminal.google_oauth import drive_test as google_drive_test
    from thoth_terminal.google_oauth import gmail_test as google_gmail_test

    try:
        if svc == "gmail":
            result = google_gmail_test()
        elif svc == "drive":
            result = google_drive_test()
        else:
            raise typer.BadParameter("--service must be gmail or drive")
        _print_json({"ok": True, "service": svc, "result": result}, pretty=pretty)
    except typer.BadParameter:
        raise
    except Exception as exc:
        _print_json({"ok": False, "service": svc, "error": f"{type(exc).__name__}: {exc}"}, pretty=pretty)
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    app()
