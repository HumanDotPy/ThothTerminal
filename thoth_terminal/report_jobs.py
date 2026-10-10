from __future__ import annotations

from datetime import datetime
from typing import Any

from thoth_terminal.db import session_scope
from thoth_terminal.discord import outbox
from thoth_terminal.jobs import enqueue_job
from thoth_terminal.research_report_service import build_research_report, get_research_report_state, run_report_freshness_scan


JOB_KIND_RESEARCH_REPORT_BUILD = "research_report_build"
JOB_KIND_RESEARCH_REPORT_FRESHNESS_SCAN = "research_report_freshness_scan"


def enqueue_report_build(
    session,
    *,
    subject: str | int,
    force: bool = False,
    trigger_kind: str = "manual",
    trigger_label: str | None = None,
    window_days: int | None = None,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 3,
    notify_discord: dict[str, Any] | None = None,
):
    return enqueue_job(
        session,
        kind=JOB_KIND_RESEARCH_REPORT_BUILD,
        payload={
            "subject": subject,
            "force": bool(force),
            "trigger_kind": str(trigger_kind or "manual"),
            "trigger_label": str(trigger_label or "").strip() or None,
            "window_days": int(window_days) if window_days is not None else None,
            "notify_discord": (dict(notify_discord) if isinstance(notify_discord, dict) else None),
        },
        dedupe_key=(dedupe_key.strip() if isinstance(dedupe_key, str) and dedupe_key.strip() else None),
        priority=4,
        run_after=run_after,
        max_attempts=int(max_attempts),
    )


def enqueue_report_freshness_scan(
    session,
    *,
    dedupe_key: str | None = "research_report_freshness_scan",
    run_after: datetime | None = None,
    max_attempts: int = 2,
):
    return enqueue_job(
        session,
        kind=JOB_KIND_RESEARCH_REPORT_FRESHNESS_SCAN,
        payload={},
        dedupe_key=dedupe_key,
        priority=3,
        run_after=run_after,
        max_attempts=int(max_attempts),
    )


def handle_research_report_build(payload: dict[str, Any]) -> None:
    subject = payload.get("subject")
    if subject is None or str(subject).strip() == "":
        return
    notify_cfg = dict(payload.get("notify_discord") or {}) if isinstance(payload.get("notify_discord"), dict) else None
    state: dict[str, Any] | None = None
    result_reason: str | None = None
    created_version = False
    error_message: str | None = None
    try:
        with session_scope() as session:
            result = build_research_report(
                session,
                subject=subject,
                force=bool(payload.get("force")),
                trigger_kind=str(payload.get("trigger_kind") or "worker"),
                trigger_label=str(payload.get("trigger_label") or "").strip() or None,
                window_days=int(payload.get("window_days")) if str(payload.get("window_days") or "").isdigit() else None,
            )
            created_version = bool(result.created_version)
            result_reason = str(result.reason or "").strip() or None
            state = get_research_report_state(session, subject=subject)
    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        if notify_cfg:
            _notify_discord_report_build(
                notify_cfg,
                state=None,
                created_version=False,
                result_reason=None,
                error_message=error_message,
                subject_label=str(subject),
            )
        raise
    if notify_cfg:
        _notify_discord_report_build(
            notify_cfg,
            state=state,
            created_version=created_version,
            result_reason=result_reason,
            error_message=None,
            subject_label=str(subject),
        )


def _notify_discord_report_build(
    notify_cfg: dict[str, Any],
    *,
    state: dict[str, Any] | None,
    created_version: bool,
    result_reason: str | None,
    error_message: str | None,
    subject_label: str,
) -> None:
    guild_id = int(notify_cfg.get("guild_id") or 0)
    channel_id = int(notify_cfg.get("channel_id") or 0) or None
    thread_id = int(notify_cfg.get("thread_id") or 0) or None
    if not guild_id or not channel_id:
        return

    announce_in_main = bool(notify_cfg.get("announce_in_main")) if notify_cfg.get("announce_in_main") is not None else bool(thread_id)
    dedupe_prefix = str(notify_cfg.get("dedupe_key_prefix") or "").strip() or None
    report = dict((state or {}).get("report") or {})
    version = dict((state or {}).get("version") or {}) if isinstance((state or {}).get("version"), dict) else {}
    coverage = dict((state or {}).get("coverage") or {})
    artifacts = dict((state or {}).get("artifacts") or {})
    freshness = dict((state or {}).get("freshness") or {})
    subject = dict((state or {}).get("subject") or {})
    display_name = str(subject.get("display_name") or report.get("display_name") or subject_label).strip() or subject_label
    kind = str(subject.get("kind") or report.get("subject_kind") or "").strip()
    primary_ticker = str(subject.get("primary_ticker") or report.get("primary_ticker") or "").strip()
    latest_version = int(version.get("version")) if version.get("version") is not None else None
    report_url = str((state or {}).get("report_url") or "").strip()
    artifact_links = {
        str(key): str(value)
        for key, value in (artifacts.get("links") or {}).items()
        if str(key).strip() and str(value).strip()
    }
    gaps = [str(row).strip() for row in (coverage.get("gaps") or []) if str(row).strip()]
    metric_rows = [dict(row) for row in (coverage.get("metrics") or []) if isinstance(row, dict)]
    coverage_bits: list[str] = []
    for label in ("Primers", "Teardowns", "Theses", "Notes", "Transcripts", "Transcript summaries"):
        for row in metric_rows:
            if str(row.get("label") or "").strip().lower() != label.lower():
                continue
            try:
                coverage_bits.append(f"{label}: {int(row.get('value'))}")
            except Exception:
                pass
            break

    if error_message:
        status_line = f"Build failed: {error_message}"
        color = 0xEF4444
    elif created_version:
        status_line = f"Published new report version v{latest_version}."
        color = 0x22C55E
    elif result_reason == "unchanged":
        suffix = f" v{latest_version}" if latest_version is not None else ""
        status_line = f"No new report version published. Latest{suffix} remains current because the fingerprint did not change."
        color = 0x60A5FA
    elif result_reason == "fresh":
        suffix = f" v{latest_version}" if latest_version is not None else ""
        status_line = f"No new report version published. Latest{suffix} is still inside the freshness window."
        color = 0xF59E0B
    else:
        suffix = f" v{latest_version}" if latest_version is not None else ""
        status_line = f"Report build finished. Latest{suffix} is available."
        color = 0x60A5FA

    freshness_state = str(freshness.get("state") or "").strip()
    freshness_bits: list[str] = []
    if kind:
        freshness_bits.append(f"Type: {kind}")
    if primary_ticker:
        freshness_bits.append(f"Ticker: ${primary_ticker}")
    if freshness_state:
        freshness_bits.append(f"Freshness: {freshness_state}")

    export_lines: list[str] = []
    if report_url:
        export_lines.append(f"Report: {report_url}")
    for artifact_type in ("pdf", "markdown", "json", "ai_clean_markdown"):
        link = artifact_links.get(artifact_type)
        if link:
            export_lines.append(f"{artifact_type.upper()}: {link}")

    thread_lines: list[str] = [f"**Research Report — {display_name}**", status_line]
    if freshness_bits:
        thread_lines.append(" • ".join(freshness_bits))
    if export_lines:
        thread_lines.append("")
        thread_lines.extend(export_lines[:4])
    if gaps:
        thread_lines.append("")
        thread_lines.append("Top gaps:")
        thread_lines.extend([f"• {gap}" for gap in gaps[:3]])

    embed = {
        "title": f"Research Report — {display_name}"[:256],
        "description": status_line[:4000],
        "color": color,
        "fields": [],
        "footer": {"text": "Generated automatically"},
    }
    if freshness_bits:
        embed["fields"].append({"name": "Subject", "value": "\n".join(freshness_bits)[:1024], "inline": False})
    if coverage_bits:
        embed["fields"].append({"name": "Coverage", "value": " • ".join(coverage_bits)[:1024], "inline": False})
    if gaps:
        embed["fields"].append({"name": "Top Gaps", "value": "\n".join([f"• {gap}" for gap in gaps[:3]])[:1024], "inline": False})
    if export_lines:
        embed["fields"].append({"name": "Links", "value": "\n".join(export_lines[:4])[:1024], "inline": False})
    if thread_id and announce_in_main:
        notice_lines = [
            f"Research report update for {display_name} is live in <#{int(thread_id)}>.",
            status_line,
        ]
        if report_url:
            notice_lines.append(f"Report: {report_url}")
        embed = outbox.add_notice_after_post(
            embed,
            channel_id=int(channel_id),
            lines=notice_lines,
            dedupe_key=(f"{dedupe_prefix}:notice" if dedupe_prefix else None),
            priority=6,
        ) or embed

    thread_content = "\n".join(thread_lines).strip() or None
    outbox.create_outbox_item(
        guild_id=guild_id,
        channel_id=int(channel_id),
        thread_id=int(thread_id) if thread_id else None,
        content=thread_content,
        embed_json=embed,
        dedupe_key=(f"{dedupe_prefix}:post" if dedupe_prefix else None),
        priority=5,
        created_by_kind="agent",
        created_by_user_id=int(notify_cfg.get("requested_by_user_id")) if notify_cfg.get("requested_by_user_id") else None,
        max_attempts=5,
    )


def handle_research_report_freshness_scan(_payload: dict[str, Any]) -> None:
    with session_scope() as session:
        for row in run_report_freshness_scan(session):
            report_id = row.get("report_id")
            if report_id is None:
                continue
            enqueue_report_build(
                session,
                subject=int(report_id),
                force=False,
                trigger_kind="freshness_scan",
                trigger_label=str(row.get("reason") or "stale"),
                dedupe_key=f"research_report_build:{int(report_id)}",
            )
