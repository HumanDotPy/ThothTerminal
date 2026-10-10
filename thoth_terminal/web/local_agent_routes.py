from __future__ import annotations

import json
import logging
import secrets
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from thoth_terminal.web.json_response import JSONResponse
from sqlalchemy.orm import Session

from thoth_terminal.config import settings
from thoth_terminal.desktop.prefs import local_ingestion_enabled
from thoth_terminal.db import database_backend, get_session
from thoth_terminal.decision_spine_service import (
    cached_company_decision_payload,
    company_decision_payload,
    list_investment_catalysts,
    resolve_company_or_issuer_from_payload,
    serialize_analyst_evidence,
    serialize_catalyst,
    serialize_memo,
    sync_decision_spine,
    upsert_analyst_evidence,
    upsert_decision_evidence_link,
    upsert_investment_catalyst,
    upsert_investment_memo,
)
from thoth_terminal.market_context_service import build_agent_market_context
from thoth_terminal.models import AnalystEvidence, LocalAgentRun, ResearchSubject
from thoth_terminal.jobs import enqueue_cloud_research_automation_job
from thoth_terminal.instrument_service import (
    create_instrument_request,
    fulfill_instrument_request,
    get_instrument_request,
    instrument_request_markdown,
    list_instrument_requests,
    review_instrument_request,
    run_instrument_request,
    serialize_instrument_request,
    serialize_instrument_request_summary,
)
from thoth_terminal.runtime_info import get_runtime_identity
from thoth_terminal.local_agent_service import (
    claim_media_transcription_jobs,
    cleanup_local_agent_traces,
    complete_media_transcription,
    create_claim,
    create_error_compression_for_model_failure,
    create_or_update_compression,
    create_or_update_deep_analysis_trace,
    create_unresolved_entity,
    daily_summary_context,
    durable_candidate_context,
    document_analysis_readiness,
    enqueue_pending_media_transcription_jobs,
    finish_run,
    backfill_legacy_media_classifications,
    backfill_media_ocr_translations,
    get_document_batch_plan,
    get_document_backlog_plan,
    get_document_for_agent,
    get_next_documents,
    institutional_model_context,
    list_deep_analysis_candidates,
    list_durable_review_candidates,
    get_research_doc,
    list_translation_overrides,
    local_agent_runtime_stage_decision,
    local_agent_stage_plan,
    local_agent_run_health,
    media_attachment_file,
    list_stale_running_runs,
    list_watchlists,
    recent_actions,
    recent_runs,
    record_action,
    requeue_media_transcription_jobs,
    search_entities,
    search_objects_for_agent,
    search_research_docs,
    serialize_claim,
    serialize_compression,
    serialize_deep_analysis_trace,
    serialize_run,
    serialize_write_diff,
    start_run,
    stop_stale_running_runs,
    suggest_translation_override,
    upsert_daily_summary,
    write_research_destination,
)


logger = logging.getLogger("thoth.local_agent_routes")


def _reject_disabled_local_ingestion() -> None:
    raise HTTPException(status_code=409, detail={
        "code": "local_ingestion_disabled",
        "message": "Local models for ingestion are disabled in Settings.",
    })


def _require_local_work_admission(request: Request, db: Session, *, run_id: int | None = None,
                                  payload: dict[str, Any] | None = None) -> None:
    """Scope the ingestion preference to local workers, preserving external agent use."""
    if local_ingestion_enabled():
        return
    payload = payload or {}
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    provider = str(request.headers.get("x-thoth-agent-inference-provider")
                   or payload.get("inference_provider") or stats.get("inference_provider") or "").lower()
    if provider == "local":
        _reject_disabled_local_ingestion()
    if run_id is None:
        raw_run_id = request.headers.get("x-thoth-agent-run-id")
        if str(raw_run_id or "").isdigit():
            run_id = int(raw_run_id)
    run = db.get(LocalAgentRun, run_id) if run_id else None
    if run and (run.stats_json or {}).get("inference_provider") == "local":
        _reject_disabled_local_ingestion()


def _auth_enabled() -> bool:
    return bool(getattr(settings, "agent_api_enabled", False))


def _agent_token_configured() -> bool:
    return bool(str(getattr(settings, "agent_api_token", "") or "").strip())


def _agent_write_token() -> str:
    return str(getattr(settings, "agent_api_write_token", "") or "").strip()


def _extract_token(request: Request) -> str:
    auth = str(request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return str(request.headers.get("x-thoth-agent-token") or "").strip()


def _require_agent_auth(request: Request) -> None:
    if not _auth_enabled():
        raise HTTPException(status_code=403, detail="Local analyst agent API is disabled.")
    expected = str(getattr(settings, "agent_api_token", "") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Local analyst agent API token is not configured.")
    provided = _extract_token(request)
    write_token = _agent_write_token()
    valid_worker = bool(provided and secrets.compare_digest(provided, expected))
    valid_writer = bool(provided and write_token and secrets.compare_digest(provided, write_token))
    if not valid_worker and not valid_writer:
        raise HTTPException(status_code=401, detail="Unauthorized local analyst agent request.")


def _has_canonical_write_access(request: Request) -> bool:
    provided = _extract_token(request)
    expected = _agent_write_token()
    return bool(provided and expected and secrets.compare_digest(provided, expected))


def _require_canonical_write_auth(request: Request) -> None:
    _require_agent_auth(request)
    if not _has_canonical_write_access(request):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "local_agent_proposal_only",
                "message": "This local-agent credential may submit proposals and operational state but cannot mutate canonical research state.",
            },
        )


def _enqueue_deepseek_promotion_review(
    db: Session,
    *,
    document_id: int,
    compression_id: int,
    run_id: int | None,
) -> int | None:
    """Hand a proposal-only local result to the existing cloud review lane."""
    try:
        job = enqueue_cloud_research_automation_job(
            db,
            mode="heartbeat",
            lookback_hours=max(1, int(getattr(settings, "cloud_research_heartbeat_lookback_hours", 168) or 168)),
            provider="deepseek",
            model=str(getattr(settings, "deepseek_model", "deepseek-v4-flash") or "deepseek-v4-flash"),
            objective=(
                "Review the quarantined local-agent durable proposal for "
                f"document {int(document_id)} / compression {int(compression_id)}. "
                "Verify every proposed claim against that source document, treat unrelated retrieved context only as duplicate/conflict context, "
                "and promote only source-grounded novel facts through canonical Thoth rails."
            ),
            promotion_document_id=int(document_id),
            promotion_compression_id=int(compression_id),
            dedupe_key=f"deepseek:local-proposal:{int(compression_id)}",
            max_attempts=3,
        )
        return int(job.id)
    except Exception:
        logger.exception("Could not enqueue DeepSeek promotion review for compression %s", compression_id)
        return None


def _require_document_analysis_ready(db: Session, document_id: int) -> dict[str, Any]:
    try:
        readiness = document_analysis_readiness(db, document_id=int(document_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not readiness["ready"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "document_prep_incomplete",
                "message": "Document analysis cannot be written until text, translation, and signal media preparation are complete.",
                **readiness,
            },
        )
    return readiness


def _payload_run_id(payload: dict[str, Any], request: Request) -> int | None:
    raw = payload.get("run_id") or request.headers.get("x-thoth-agent-run-id")
    return int(raw) if str(raw or "").isdigit() else None


def _short_trace_summary(summary_md: object, error: object = None) -> str:
    text = " ".join(str(summary_md or error or "").strip().split())
    if not text:
        return "Deep-analysis trace recorded."
    return text[:237].rstrip() + "..." if len(text) > 240 else text


def _dry_run_requested(request: Request) -> bool:
    raw = str(request.query_params.get("dry_run") or request.query_params.get("dryRun") or "").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _allow_remote_symbol_lookup(ticker: object) -> bool:
    value = str(ticker or "").strip()
    if not value or len(value) > 24 or any(ch.isspace() for ch in value):
        return False
    if not all(ch.isalnum() or ch in ".-/" for ch in value):
        return False
    return True


def _agent_resolution_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    ticker = out.get("ticker") or out.get("symbol")
    out.setdefault("allow_remote_symbol_lookup", _allow_remote_symbol_lookup(ticker))
    return out


async def _json_payload(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return dict(payload) if isinstance(payload, dict) else {}


def _json_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _query_int_list(value: object) -> list[int]:
    raw_items = value if isinstance(value, list) else str(value or "").replace(";", ",").split(",")
    out: list[int] = []
    for raw in raw_items:
        text = str(raw or "").strip()
        if text.isdigit():
            parsed = int(text)
            if parsed not in out:
                out.append(parsed)
        if len(out) >= 50:
            break
    return out


def _json_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _decision_target_search_suggestions(db: Session, target: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    query = target.get("query") if isinstance(target.get("query"), dict) else target
    label = _clean_text(target.get("label") or target.get("name") or target.get("subject") or "")
    ticker = _clean_text(target.get("ticker") or query.get("ticker") or query.get("symbol") or "")
    terms: list[str] = []
    for value in (label, ticker):
        if value and value.lower() not in {term.lower() for term in terms}:
            terms.append(value)
    if label:
        first_token = label.split()[0]
        if len(first_token) >= 3 and first_token.lower() not in {term.lower() for term in terms}:
            terms.append(first_token)
    suggestions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for term in terms:
        for hit in search_entities(db, query=term, limit=limit, allow_provider_fallback=False):
            key = (str(hit.get("kind") or ""), str(hit.get("id") or hit.get("label") or hit.get("ticker") or ""))
            if key in seen:
                continue
            seen.add(key)
            row = dict(hit)
            row["matched_query"] = term
            suggestions.append(row)
            if len(suggestions) >= limit:
                return suggestions
    return suggestions


def _decision_target_unresolved_payload(db: Session, target: dict[str, Any], *, index: int | None = None) -> dict[str, Any]:
    query = target.get("query") if isinstance(target.get("query"), dict) else target
    label = _clean_text(target.get("label") or target.get("name") or target.get("subject") or "")
    ticker = _clean_text(target.get("ticker") or query.get("ticker") or query.get("symbol") or "")
    parts = []
    if label:
        parts.append(f"label={label}")
    if ticker:
        parts.append(f"ticker={ticker}")
    if query.get("company_id") is not None:
        parts.append(f"company_id={query.get('company_id')}")
    if query.get("issuer_id") is not None:
        parts.append(f"issuer_id={query.get('issuer_id')}")
    target_text = ", ".join(parts) or "empty target"
    return {
        "index": index,
        "target": target,
        "status": "unresolved",
        "reason": "not_tracked_or_unresolved_identity",
        "message": f"No tracked company or issuer matched decision-context target ({target_text}).",
        "search_suggestions": _decision_target_search_suggestions(db, target, limit=5),
    }


def _confidence(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _instrument_request_payload(item: dict[str, Any], *, document_id: int | None, run_id: int | None) -> dict[str, Any]:
    title = str(item.get("title") or item.get("request_title") or "").strip()
    summary = str(item.get("summary") or item.get("rationale") or item.get("missing_rail") or "").strip()
    target_entities = [str(v).strip() for v in _json_list(item.get("target_entities")) if str(v).strip()]
    target_themes = [str(v).strip() for v in _json_list(item.get("target_themes")) if str(v).strip()]
    desired_outputs = [str(v).strip() for v in _json_list(item.get("desired_outputs")) if str(v).strip()]
    evidence_bundle = [dict(v) for v in _json_list(item.get("evidence_bundle")) if isinstance(v, dict)]
    if document_id is not None and not any(int(row.get("document_id") or 0) == int(document_id) for row in evidence_bundle if str(row.get("document_id") or "").isdigit()):
        evidence_bundle.append({"document_id": int(document_id), "role": "source_document"})
    draft_payload = dict(item.get("draft_payload") or {}) if isinstance(item.get("draft_payload"), dict) else {}
    if run_id is not None:
        draft_payload.setdefault("local_agent_run_id", int(run_id))
    if document_id is not None:
        draft_payload.setdefault("source_document_id", int(document_id))
    linked_objects = dict(item.get("linked_objects") or {}) if isinstance(item.get("linked_objects"), dict) else {}
    if document_id is not None:
        linked_objects.setdefault("document_id", int(document_id))
    return {
        "title": title,
        "intent": str(item.get("intent") or "missing_research_rail").strip() or "missing_research_rail",
        "summary": summary or None,
        "target_entities": target_entities or None,
        "target_themes": target_themes or None,
        "desired_outputs": desired_outputs or None,
        "evidence_bundle": evidence_bundle or None,
        "provenance_summary": str(item.get("provenance_summary") or "").strip() or None,
        "evidence_tier": str(item.get("evidence_tier") or "").strip() or None,
        "status": str(item.get("status") or "review").strip().lower() or "review",
        "priority": int(item.get("priority")) if str(item.get("priority") or "").isdigit() else 6,
        "automation_source": str(item.get("automation_source") or "local_agent_missing_rail").strip() or "local_agent_missing_rail",
        "source_kind": str(item.get("source_kind") or "").strip() or None,
        "draft_payload": draft_payload or None,
        "linked_objects": linked_objects or None,
    }


def _analysis_result_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("result", "signal_extraction", "compression"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload


def _signal_extraction_write_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("signal_extraction", "compression", "result"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload


def _signal_extraction_response(row: Any) -> dict[str, Any]:
    serialized = serialize_compression(row)
    return {
        "ok": True,
        "compression_id": int(row.id),
        "signal_extraction_id": int(row.id),
        "compression": serialized,
        "signal_extraction": dict(serialized),
    }


def _local_agent_payload_allows_duplicate_context(payload: dict[str, Any]) -> bool:
    routing = _json_dict(payload.get("routing"))
    nested = _json_dict(payload.get("payload"))
    values = {
        str(payload.get("novelty") or "").strip().lower(),
        str(payload.get("action") or "").strip().lower(),
        str(routing.get("novelty") or "").strip().lower(),
        str(routing.get("action") or "").strip().lower(),
        str(nested.get("novelty") or "").strip().lower(),
        str(nested.get("action") or "").strip().lower(),
    }
    if any(value in {"update", "material_update", "conflict", "contradiction", "correction", "revision"} for value in values):
        return True
    return bool(payload.get("duplicate_context_override") or nested.get("duplicate_context_override"))


def _local_agent_duplicate_gate_context(
    db: Session,
    payload: dict[str, Any],
    *,
    document_id: int | None,
) -> dict[str, Any] | None:
    if _local_agent_payload_allows_duplicate_context(payload):
        return None
    evidence_key = str(payload.get("evidence_key") or "").strip()
    if evidence_key and db.query(AnalystEvidence.id).filter_by(evidence_key=evidence_key).first() is not None:
        return None
    context = durable_candidate_context(
        db,
        document_id=document_id,
        payload={
            "result": {
                "decision_targets": [
                    {
                        key: payload.get(key)
                        for key in ("ticker", "company_id", "issuer_id", "subject_id", "subject", "company")
                        if payload.get(key) is not None
                    }
                ],
                "analyst_evidence": [payload],
                "model_delta_candidates": [payload],
            }
        },
        limit=6,
    )
    duplicate_risk = _json_dict(context.get("duplicate_risk"))
    if str(duplicate_risk.get("level") or "").strip().lower() == "high" or str(context.get("recommended_action") or "").strip().lower() == "hold_for_duplicate_review":
        if _local_agent_duplicate_context_is_distinct_forecast_series(context, payload):
            return None
        return context
    return None


def _local_agent_duplicate_context_is_distinct_forecast_series(context: dict[str, Any], payload: dict[str, Any]) -> bool:
    forecast_payload = _json_dict(payload.get("forecast") or payload.get("projection"))
    incoming_metric = str(
        payload.get("metric_key")
        or payload.get("estimate_metric")
        or forecast_payload.get("metric_key")
        or forecast_payload.get("metric")
        or forecast_payload.get("metric_name")
        or ""
    ).strip()
    incoming_period = str(
        payload.get("forecast_period")
        or payload.get("estimate_period")
        or payload.get("target_period")
        or forecast_payload.get("period")
        or ""
    ).strip()
    incoming_value = str(
        payload.get("forecast_value")
        if payload.get("forecast_value") is not None
        else payload.get("estimate_value")
        if payload.get("estimate_value") is not None
        else payload.get("forecast_status")
        or forecast_payload.get("value")
        or forecast_payload.get("forecast_value")
        or forecast_payload.get("status")
        or ""
    ).strip()
    if not incoming_metric or not (incoming_period or incoming_value):
        return False
    blocking_rows = []
    comparable_rows = []
    for row in [
        *_json_list(context.get("existing_analyst_evidence")),
        *_json_list(context.get("existing_institutional_evidence")),
        *_json_list(context.get("existing_institutional_observations")),
    ]:
        if not isinstance(row, dict):
            continue
        if str(row.get("duplicate_risk") or "").strip().lower() != "high":
            continue
        match = _json_dict(row.get("match"))
        comparable_rows.append(match)
        if bool(match.get("metric_match")) and (bool(match.get("period_match")) or bool(match.get("value_match"))):
            blocking_rows.append(match)
    if blocking_rows:
        return all(bool(match.get("distinct_estimate_owner")) for match in blocking_rows)
    return bool(comparable_rows)


def _raise_local_agent_duplicate_gate(context: dict[str, Any]) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "message": "Duplicate durable analyst evidence requires explicit update/conflict/correction metadata before materialization.",
            "duplicate_risk": context.get("duplicate_risk"),
            "recommended_action": context.get("recommended_action"),
            "existing_analyst_evidence": context.get("existing_analyst_evidence", [])[:3],
            "existing_institutional_observations": context.get("existing_institutional_observations", [])[:3],
        },
    )


def _model_delta_estimate_owner(item: dict[str, Any]) -> str:
    attribution = _json_dict(item.get("source_attribution"))
    underlying = _json_dict(attribution.get("underlying_source"))
    return _clean_text(
        item.get("estimate_owner")
        or item.get("underlying_firm")
        or item.get("source_firm")
        or item.get("research_firm")
        or item.get("institution")
        or item.get("firm")
        or item.get("source_owner")
        or underlying.get("firm")
        or underlying.get("name")
    )


def _preserve_estimate_owner(payload: dict[str, Any], owner: str) -> None:
    if not owner:
        return
    payload.setdefault("estimate_owner", owner)
    payload.setdefault("underlying_firm", owner)
    if not payload.get("source_name") and not payload.get("observed_source_name"):
        payload["source_name"] = owner
    attribution = _json_dict(payload.get("source_attribution"))
    underlying = _json_dict(attribution.get("underlying_source"))
    underlying.setdefault("firm", owner)
    underlying.setdefault("name", owner)
    attribution["underlying_source"] = underlying
    payload["source_attribution"] = attribution


def _model_delta_candidate_to_analyst_evidence(item: dict[str, Any]) -> dict[str, Any] | None:
    metric_key = _clean_text(item.get("canonical_metric_key") or item.get("metric_key"))
    if not metric_key or bool(item.get("requires_resolution")):
        return None
    value_number = item.get("value_number")
    if value_number is None:
        value_number = item.get("forecast_value")
    value_text = _clean_text(item.get("value_text") or item.get("forecast_status"))
    if value_number is None and not value_text:
        return None
    summary = _clean_text(item.get("summary") or item.get("rationale"))
    if not summary:
        subject = _clean_text(item.get("raw_subject_text") or item.get("canonical_subject_key") or item.get("subject"))
        period = _clean_text(item.get("period") or item.get("forecast_period"))
        summary = f"{subject} {metric_key} {value_number if value_number is not None else value_text} {period}".strip()
    forecast = {
        "metric_key": metric_key,
        "metric_value_type": item.get("metric_value_type"),
        "value": value_number,
        "value_text": value_text or None,
        "forecast_status": value_text or None,
        "unit": item.get("unit"),
        "currency": item.get("currency"),
        "product_family": item.get("product_family"),
        "segment": item.get("segment"),
        "period": item.get("period") or item.get("forecast_period"),
        "period_basis": item.get("period_basis"),
        "period_year": item.get("period_year"),
        "period_quarter": item.get("period_quarter"),
        "period_half": item.get("period_half"),
        "as_of_date": item.get("as_of_date"),
        "scenario": item.get("scenario"),
    }
    payload = {
        **item,
        "evidence_kind": "estimate",
        "action": item.get("action") or "claim",
        "summary": summary,
        "metric_key": metric_key,
        "estimate_metric": metric_key,
        "estimate_period": item.get("period") or item.get("forecast_period"),
        "estimate_value": value_number,
        "forecast_value": value_number,
        "forecast_status": value_text or None,
        "estimate_unit": item.get("unit"),
        "currency": item.get("currency"),
        "forecast": {key: value for key, value in forecast.items() if value not in (None, "", [])},
        "payload": {
            **(item.get("payload") if isinstance(item.get("payload"), dict) else {}),
            "model_delta_candidate": dict(item),
        },
    }
    _preserve_estimate_owner(payload, _model_delta_estimate_owner(item))
    return {key: value for key, value in payload.items() if value not in (None, "", [])}


def _analysis_analyst_evidence_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = _analysis_result_payload(payload)
    sources: list[dict[str, Any]] = [dict(row) for row in _json_list(payload.get("analyst_evidence")) if isinstance(row, dict)]
    if result is not payload:
        sources.extend(dict(row) for row in _json_list(result.get("analyst_evidence")) if isinstance(row, dict))
    model_delta_sources: list[dict[str, Any]] = [
        dict(row) for row in _json_list(payload.get("model_delta_candidates")) if isinstance(row, dict)
    ]
    if result is not payload:
        model_delta_sources.extend(dict(row) for row in _json_list(result.get("model_delta_candidates")) if isinstance(row, dict))
    for item in model_delta_sources:
        converted = _model_delta_candidate_to_analyst_evidence(item)
        if converted is not None:
            converted.setdefault("extraction_method", "model_delta_candidate_conversion")
            sources.append(converted)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in sources:
        key = json.dumps(item, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _route_numberish(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace(",", "").replace("$", "").replace("%", "")
    try:
        return float(text)
    except ValueError:
        return None


def _analysis_analyst_evidence_is_modelable(payload: dict[str, Any]) -> bool:
    forecast = payload.get("forecast") if isinstance(payload.get("forecast"), dict) else {}
    if any(
        _route_numberish(payload.get(field)) is not None
        for field in (
            "price_target",
            "prior_price_target",
            "forecast_value",
            "prior_forecast_value",
            "estimate_value",
            "prior_estimate_value",
            "actual_value",
        )
    ):
        return True
    if any(_route_numberish(forecast.get(field)) is not None for field in ("value", "forecast_value", "prior_value", "actual_value")):
        return True
    if _clean_text(payload.get("rating")) or _clean_text(payload.get("prior_rating")):
        return True
    metric_key = _clean_text(
        payload.get("metric_key")
        or payload.get("estimate_metric")
        or forecast.get("metric_key")
        or forecast.get("metric")
        or forecast.get("metric_name")
    )
    if not metric_key:
        return False
    status = _clean_text(payload.get("forecast_status") or forecast.get("forecast_status") or forecast.get("status") or payload.get("value_text"))
    if any(
        _clean_text(payload.get(field) or forecast.get(field))
        for field in (
            "target_date",
            "actual_date",
            "start_date",
            "end_date",
            "forecast_start_date",
            "forecast_end_date",
            "forecast_period",
            "estimate_period",
            "period",
        )
    ) and status:
        return True
    metric_type = _clean_text(payload.get("metric_value_type") or forecast.get("metric_value_type")).lower()
    metric_text = metric_key.lower()
    if not status:
        return False
    if any(ch.isdigit() for ch in status):
        return True
    if metric_type in {"status", "event", "date"}:
        return True
    return any(token in metric_text for token in ("status", "qualified", "qualification", "delivery", "start", "lead_time", "availability", "allocation"))


def _analysis_claim_payloads(
    payload: dict[str, Any],
    *,
    run_id: int | None,
    document_id: int,
    compression_id: int,
) -> list[dict[str, Any]]:
    if "claims" in payload:
        explicit = [dict(item) for item in _json_list(payload.get("claims")) if isinstance(item, dict)]
        out: list[dict[str, Any]] = []
        for row in explicit:
            row.setdefault("run_id", run_id)
            row.setdefault("document_id", document_id)
            row.setdefault("compression_id", compression_id)
            row.setdefault("payload", dict(row))
            out.append(row)
        return out

    result = _analysis_result_payload(payload)
    entity_refs = [
        str(item.get("name") or "").strip()
        for item in _json_list(result.get("entity_refs")) if isinstance(item, dict)
    ]
    rows: list[dict[str, Any]] = []
    for claim in _json_list(result.get("atomic_claims")):
        if not isinstance(claim, dict):
            continue
        claim_text = str(claim.get("claim_text") or "").strip()
        if not claim_text:
            continue
        entities = [str(item or "").strip() for item in _json_list(claim.get("entities")) if str(item or "").strip()]
        entities = list(dict.fromkeys(entities or [item for item in entity_refs if item] or ["unresolved"]))
        for entity in entities:
            rows.append(
                {
                    "run_id": run_id,
                    "compression_id": compression_id,
                    "document_id": document_id,
                    "entity_type": "unknown",
                    "entity_name": entity,
                    "claim_kind": str(claim.get("claim_kind") or "business_update"),
                    "claim_text": claim_text,
                    "confidence": _confidence(claim.get("confidence")),
                    "routing_reason": "local llama.cpp compression",
                    "payload": dict(claim),
                }
            )
    return rows


def _analysis_unresolved_payloads(
    payload: dict[str, Any],
    *,
    run_id: int | None,
    document_id: int,
) -> list[dict[str, Any]]:
    if "unresolved_entities" in payload:
        source = [dict(item) for item in _json_list(payload.get("unresolved_entities")) if isinstance(item, dict)]
    else:
        result = _analysis_result_payload(payload)
        source = [dict(item) for item in _json_list(result.get("unresolved_entities")) if isinstance(item, dict)]
    rows: list[dict[str, Any]] = []
    for item in source:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        rows.append(
            {
                "run_id": run_id,
                "document_id": document_id,
                "name": name,
                "entity_type": item.get("entity_type"),
                "rationale": item.get("rationale"),
                "confidence": _confidence(item.get("confidence")),
                "candidate_payload": item.get("candidate_payload") if isinstance(item.get("candidate_payload"), dict) else item,
            }
        )
    return rows


def _analysis_research_write_payloads(
    payload: dict[str, Any],
    *,
    run_id: int | None,
    document_id: int,
) -> list[dict[str, Any]]:
    if "research_writes" in payload:
        source = [dict(item) for item in _json_list(payload.get("research_writes")) if isinstance(item, dict)]
    else:
        result = _analysis_result_payload(payload)
        source = [dict(item) for item in _json_list(result.get("research_writes")) if isinstance(item, dict)]
    rows: list[dict[str, Any]] = []
    for item in source:
        destination = item.get("destination") if isinstance(item.get("destination"), dict) else {}
        subject = str(item.get("subject") or destination.get("subject") or "").strip()
        append_md = str(item.get("append_md") or "").strip()
        if not destination and subject:
            destination = {"type": "research_report", "subject": subject}
        if not destination or not append_md:
            continue
        row = dict(item)
        row["run_id"] = run_id
        row["destination"] = destination
        row["append_md"] = append_md
        row.setdefault("source_document_ids", [document_id])
        row.setdefault("write_summary", f"Document {document_id} update")
        rows.append(row)
    return rows


def _decision_evidence_link_payload(
    *,
    source_payload: dict[str, Any],
    document_id: int,
    target_type: str,
    target_id: int | None,
) -> dict[str, Any] | None:
    if target_id is None:
        return None
    payload = {
        "target_type": target_type,
        "target_id": target_id,
        "evidence_object_type": "document",
        "evidence_object_id": document_id,
        "relation": "source",
        "confidence": _confidence(source_payload.get("confidence")),
        "summary": source_payload.get("summary") or source_payload.get("title") or source_payload.get("write_summary"),
        "evidence_tier": source_payload.get("evidence_tier"),
        "source_kind": source_payload.get("source_kind"),
        "source_url": source_payload.get("source_url"),
    }
    for key in ("company_id", "issuer_id", "ticker"):
        if source_payload.get(key) is not None:
            payload[key] = source_payload.get(key)
    return payload


def register_local_agent_routes(app: FastAPI) -> None:
    @app.post("/api/local-agent/harness/completions")
    async def local_agent_harness_completion(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_agent_auth(request)
        if not local_ingestion_enabled():
            _reject_disabled_local_ingestion()
        from pydantic import ValidationError
        from starlette.concurrency import run_in_threadpool
        from thoth_terminal.local_agent_harness import IntakeRequest, execute_intake
        from thoth_terminal.web.deepseek_harness_service import HarnessRemoteError, HarnessUnavailable

        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 24 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Ingestion request exceeds 24 MiB.")
            body.extend(chunk)
        try:
            payload = IntakeRequest.model_validate_json(body)
        except ValidationError:
            raise HTTPException(status_code=422, detail="Invalid bounded Harness ingestion request.") from None
        try:
            result = await run_in_threadpool(execute_intake, db, payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except HarnessRemoteError as exc:
            raise HTTPException(status_code=502, detail={"code": exc.code, "message": str(exc)}) from exc
        except HarnessUnavailable as exc:
            raise HTTPException(status_code=503, detail={"code": "harness_unavailable", "message": str(exc)}) from exc
        return JSONResponse(result)

    @app.get("/api/local-agent/health")
    def local_agent_health(db: Session = Depends(get_session)) -> JSONResponse:
        run_health = local_agent_run_health(db, stale_after_minutes=60, limit=5)
        return JSONResponse(
            {
                "ok": True,
                "enabled": _auth_enabled(),
                "local_ingestion_enabled": local_ingestion_enabled(),
                "token_configured": _agent_token_configured(),
                "lookback_hours": int(getattr(settings, "agent_lookback_hours", 48) or 48),
                "trace_retention_hours": int(getattr(settings, "agent_trace_retention_hours", 72) or 72),
                "default_model": getattr(settings, "agent_default_model", None),
                "prompt_version": getattr(settings, "agent_prompt_version", None),
                "database_backend": database_backend(),
                "database_url_configured": bool(str(getattr(settings, "database_url", "") or "").strip()),
                "runtime": get_runtime_identity(),
                "running_runs": run_health["running_runs"],
                "stale_running_runs": run_health["stale_running_runs"],
                "stale_run_after_minutes": run_health["stale_after_minutes"],
            }
        )

    @app.get("/api/local-agent/runs/stale")
    def local_agent_stale_runs(
        request: Request,
        stale_after_minutes: int = 60,
        limit: int = 20,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        rows = list_stale_running_runs(db, stale_after_minutes=stale_after_minutes, limit=limit)
        return JSONResponse({"ok": True, "stale_after_minutes": max(1, min(int(stale_after_minutes or 60), 24 * 60)), "runs": rows})

    @app.post("/api/local-agent/runs/stale/stop")
    def local_agent_stop_stale_runs(
        request: Request,
        stale_after_minutes: int = 60,
        limit: int = 20,
        dry_run: bool = True,
        confirm: str | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        if not bool(dry_run) and str(confirm or "").strip() != "stop_stale_runs":
            raise HTTPException(status_code=400, detail="Set confirm=stop_stale_runs to stop stale local-agent runs.")
        result = stop_stale_running_runs(
            db,
            stale_after_minutes=stale_after_minutes,
            limit=limit,
            dry_run=bool(dry_run),
        )
        db.commit()
        return JSONResponse({"ok": True, **result})

    @app.post("/api/local-agent/runs/start")
    async def local_agent_start_run(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        _require_local_work_admission(request, db, payload=payload)
        stats = dict(payload.get("stats") or {}) if isinstance(payload.get("stats"), dict) else {}
        provider = request.headers.get("x-thoth-agent-inference-provider") or payload.get("inference_provider")
        if provider:
            stats["inference_provider"] = str(provider).lower()
        try:
            cleanup_local_agent_traces(db)
        except Exception as exc:
            db.rollback()
            logger.warning("local-agent trace cleanup skipped before run start: %s", exc)
        row = start_run(
            db,
            run_type=str(payload.get("run_type") or "feed_triage"),
            model_profile=payload.get("model_profile"),
            model_name=payload.get("model_name"),
            prompt_version=payload.get("prompt_version"),
            worker_id=payload.get("worker_id"),
            stats=stats or None,
        )
        db.commit()
        return JSONResponse({"ok": True, "run": serialize_run(row)})

    @app.post("/api/local-agent/runs/{run_id}/finish")
    async def local_agent_finish_run(run_id: int, request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        row = finish_run(
            db,
            run_id=int(run_id),
            status=str(payload.get("status") or "success"),
            summary=payload.get("summary"),
            stats=payload.get("stats") if isinstance(payload.get("stats"), dict) else {},
            error=payload.get("error"),
        )
        db.commit()
        return JSONResponse({"ok": True, "run": serialize_run(row)})

    @app.get("/api/local-agent/documents/next")
    def local_agent_next_documents(
        request: Request,
        limit: int = 4,
        lookback_hours: int | None = None,
        include_filings: bool = False,
        include_opened: bool = False,
        include_product_snapshots: bool = False,
        defer_pending_media: bool = True,
        queue_strategy: str = "newest",
        prompt_version: str | None = None,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        _require_local_work_admission(request, db, run_id=run_id)
        docs = get_next_documents(
            db,
            limit=limit,
            lookback_hours=lookback_hours,
            include_filings=bool(include_filings),
            include_opened=bool(include_opened),
            include_product_snapshots=bool(include_product_snapshots),
            defer_pending_media=bool(defer_pending_media),
            queue_strategy=queue_strategy,
            prompt_version=prompt_version,
        )
        record_action(
            db,
            run_id=run_id,
            action_kind="document_queue_read",
            tool_name="agent_get_next_documents",
            endpoint="/api/local-agent/documents/next",
            request_json={
                "limit": limit,
                "lookback_hours": lookback_hours,
                "include_filings": bool(include_filings),
                "include_opened": bool(include_opened),
                "include_product_snapshots": bool(include_product_snapshots),
                "defer_pending_media": bool(defer_pending_media),
                "queue_strategy": queue_strategy,
                "prompt_version": prompt_version,
            },
            response_json={"count": len(docs), "document_ids": [row.get("id") for row in docs]},
        )
        db.commit()
        return JSONResponse({"ok": True, "documents": docs})

    @app.get("/api/local-agent/documents/batch-plan")
    def local_agent_document_batch_plan(
        request: Request,
        limit: int = 4,
        lookback_hours: int | None = None,
        include_filings: bool = False,
        include_opened: bool = False,
        include_product_snapshots: bool = False,
        defer_pending_media: bool = True,
        max_input_tokens: int = 32_000,
        max_documents_per_batch: int = 6,
        queue_strategy: str = "newest",
        prompt_version: str | None = None,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        _require_local_work_admission(request, db, run_id=run_id)
        plan = get_document_batch_plan(
            db,
            limit=limit,
            lookback_hours=lookback_hours,
            include_filings=bool(include_filings),
            include_opened=bool(include_opened),
            include_product_snapshots=bool(include_product_snapshots),
            defer_pending_media=bool(defer_pending_media),
            max_input_tokens=max_input_tokens,
            max_documents_per_batch=max_documents_per_batch,
            queue_strategy=queue_strategy,
            prompt_version=prompt_version,
        )
        record_action(
            db,
            run_id=run_id,
            action_kind="document_batch_plan_read",
            tool_name="agent_get_document_batch_plan",
            endpoint="/api/local-agent/documents/batch-plan",
            request_json={
                "limit": limit,
                "lookback_hours": lookback_hours,
                "include_filings": bool(include_filings),
                "include_opened": bool(include_opened),
                "include_product_snapshots": bool(include_product_snapshots),
                "defer_pending_media": bool(defer_pending_media),
                "max_input_tokens": max_input_tokens,
                "max_documents_per_batch": max_documents_per_batch,
                "queue_strategy": queue_strategy,
                "prompt_version": prompt_version,
            },
            response_json={
                "documents_planned": plan.get("documents_planned"),
                "batches": len(plan.get("batches") or []),
                "batch_document_ids": [row.get("document_ids") for row in plan.get("batches", [])],
            },
        )
        db.commit()
        return JSONResponse({"ok": True, "plan": plan})

    @app.get("/api/local-agent/stages/plan")
    def local_agent_stages_plan(
        request: Request,
        media_limit: int = 2,
        signal_limit: int = 4,
        durable_limit: int = 2,
        lookback_hours: int | None = None,
        queue_strategy: str = "newest",
        prompt_version: str | None = None,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        _require_local_work_admission(request, db, run_id=run_id)
        plan = local_agent_stage_plan(
            db,
            media_limit=media_limit,
            signal_limit=signal_limit,
            durable_limit=durable_limit,
            lookback_hours=lookback_hours,
            queue_strategy=queue_strategy,
            prompt_version=prompt_version,
        )
        record_action(
            db,
            run_id=run_id,
            action_kind="stage_plan_read",
            tool_name="agent_get_stage_plan",
            endpoint="/api/local-agent/stages/plan",
            request_json={
                "media_limit": media_limit,
                "signal_limit": signal_limit,
                "durable_limit": durable_limit,
                "lookback_hours": lookback_hours,
                "queue_strategy": queue_strategy,
                "prompt_version": prompt_version,
            },
            response_json={
                "stages": [row.get("stage") for row in plan.get("stages", [])],
                "batch_documents_planned": (plan.get("batch_plan") or {}).get("documents_planned"),
                "signal_lookback_hours": plan.get("signal_lookback_hours"),
                "lookback_expanded": (plan.get("lookback_expansion") or {}).get("expanded")
                if isinstance(plan.get("lookback_expansion"), dict)
                else None,
            },
        )
        db.commit()
        return JSONResponse({"ok": True, "plan": plan})

    @app.get("/api/local-agent/stages/decision")
    def local_agent_stages_decision(
        request: Request,
        media_limit: int = 2,
        signal_limit: int = 4,
        durable_limit: int = 2,
        lookback_hours: int | None = None,
        queue_strategy: str = "newest",
        prompt_version: str | None = None,
        recent_run_limit: int = 8,
        min_consecutive_text_zero_runs: int = 2,
        document_ids: str | None = None,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        _require_local_work_admission(request, db, run_id=run_id)
        parsed_document_ids = _query_int_list(document_ids)
        decision = local_agent_runtime_stage_decision(
            db,
            media_limit=media_limit,
            signal_limit=signal_limit,
            durable_limit=durable_limit,
            lookback_hours=lookback_hours,
            queue_strategy=queue_strategy,
            prompt_version=prompt_version,
            recent_run_limit=recent_run_limit,
            min_consecutive_text_zero_runs=min_consecutive_text_zero_runs,
            document_ids=parsed_document_ids,
        )
        record_action(
            db,
            run_id=run_id,
            action_kind="stage_decision_read",
            tool_name="agent_get_stage_decision",
            endpoint="/api/local-agent/stages/decision",
            request_json={
                "media_limit": media_limit,
                "signal_limit": signal_limit,
                "durable_limit": durable_limit,
                "lookback_hours": lookback_hours,
                "queue_strategy": queue_strategy,
                "prompt_version": prompt_version,
                "recent_run_limit": recent_run_limit,
                "min_consecutive_text_zero_runs": min_consecutive_text_zero_runs,
                "document_ids": parsed_document_ids,
            },
            response_json={
                "recommendation": decision.get("recommendation"),
                "recommended_stage": decision.get("recommended_stage"),
                "recommended_model_stage": decision.get("recommended_model_stage"),
                "reason": decision.get("reason"),
                "error_code": decision.get("error_code"),
                "media_state_code": decision.get("media_state_code"),
                "pending_media_jobs": decision.get("pending_media_jobs"),
                "media_actionable_jobs": decision.get("media_actionable_jobs"),
                "media_ocr_pending_jobs": decision.get("media_ocr_pending_jobs"),
                "media_vlm_pending_jobs": decision.get("media_vlm_pending_jobs"),
                "media_retry_delayed_jobs": decision.get("media_retry_delayed_jobs"),
                "media_retry_delayed_nonblocking": decision.get("media_retry_delayed_nonblocking"),
                "model_work_state": decision.get("model_work_state"),
                "text_starved_runs": decision.get("text_starved_runs"),
                "ready_signal_documents_estimate": decision.get("ready_signal_documents_estimate"),
                "fresh_window_complete": decision.get("fresh_window_complete"),
            },
        )
        db.commit()
        return JSONResponse({"ok": True, "decision": decision})

    @app.post("/api/local-agent/documents/batch")
    async def local_agent_documents_batch(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        raw_ids = payload.get("document_ids") or payload.get("ids") or []
        document_ids: list[int] = []
        for value in _json_list(raw_ids):
            if str(value or "").isdigit():
                doc_id = int(value)
                if doc_id not in document_ids:
                    document_ids.append(doc_id)
            if len(document_ids) >= 50:
                break
        if not document_ids:
            raise HTTPException(status_code=400, detail="document_ids is required.")
        try:
            max_text_chars = max(1000, min(int(payload.get("max_text_chars") or 24000), 128000))
        except Exception:
            max_text_chars = 24000
        documents: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for document_id in document_ids:
            try:
                documents.append(
                    get_document_for_agent(
                        db,
                        document_id=document_id,
                        run_id=run_id,
                        max_text_chars=max_text_chars,
                    )
                )
            except Exception as exc:
                errors.append({"document_id": document_id, "error": f"{type(exc).__name__}: {exc}"})
        record_action(
            db,
            run_id=run_id,
            action_kind="document_batch_read",
            tool_name="agent_get_documents_batch",
            endpoint="/api/local-agent/documents/batch",
            request_json={"document_ids": document_ids, "max_text_chars": max_text_chars},
            response_json={"count": len(documents), "errors": len(errors), "document_ids": [row.get("id") for row in documents]},
        )
        db.commit()
        return JSONResponse({"ok": True, "documents": documents, "errors": errors})

    @app.get("/api/local-agent/documents/backlog/plan")
    def local_agent_document_backlog_plan(
        request: Request,
        lookback_hours: int | None = None,
        include_filings: bool = False,
        include_opened: bool = True,
        include_product_snapshots: bool = False,
        sample_limit: int = 12,
        batch_size: int = 4,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        plan = get_document_backlog_plan(
            db,
            lookback_hours=lookback_hours,
            include_filings=bool(include_filings),
            include_opened=bool(include_opened),
            include_product_snapshots=bool(include_product_snapshots),
            sample_limit=sample_limit,
            batch_size=batch_size,
        )
        record_action(
            db,
            run_id=run_id,
            action_kind="document_backlog_plan_read",
            tool_name="agent_get_document_backlog_plan",
            endpoint="/api/local-agent/documents/backlog/plan",
            request_json={
                "lookback_hours": lookback_hours,
                "include_filings": bool(include_filings),
                "include_opened": bool(include_opened),
                "include_product_snapshots": bool(include_product_snapshots),
                "sample_limit": sample_limit,
                "batch_size": batch_size,
            },
            response_json={
                "total_documents": plan.get("total_documents"),
                "estimated_batches": plan.get("estimated_batches"),
                "sample_document_ids": [row.get("id") for row in plan.get("sample_documents", [])],
            },
        )
        db.commit()
        return JSONResponse({"ok": True, "plan": plan})

    @app.get("/api/local-agent/documents/{document_id}")
    def local_agent_document(
        document_id: int,
        request: Request,
        run_id: int | None = None,
        max_text_chars: int = 24000,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        doc = get_document_for_agent(db, document_id=int(document_id), run_id=run_id, max_text_chars=max_text_chars)
        record_action(
            db,
            run_id=run_id,
            action_kind="document_read",
            tool_name="agent_get_document",
            endpoint=f"/api/local-agent/documents/{document_id}",
            document_id=int(document_id),
            response_json={"document_id": int(document_id), "text_chars": len(str(doc.get("text") or ""))},
        )
        db.commit()
        return JSONResponse({"ok": True, "document": doc})

    @app.get("/api/local-agent/media/transcriptions/next")
    def local_agent_next_media_transcriptions(
        request: Request,
        limit: int = 4,
        enqueue_limit: int = 100,
        render_readable_pdf_pages: bool = False,
        stale_after_minutes: int = 60,
        document_ids: str | None = None,
        media_lane: str | None = None,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        _require_local_work_admission(request, db, run_id=run_id)
        target_document_ids = _query_int_list(document_ids)
        enqueue_result = enqueue_pending_media_transcription_jobs(
            db,
            limit=enqueue_limit,
            render_readable_pdf_pages=bool(render_readable_pdf_pages),
            document_ids=target_document_ids,
        )
        jobs = claim_media_transcription_jobs(
            db,
            run_id=run_id,
            limit=limit,
            stale_after_minutes=stale_after_minutes,
            document_ids=target_document_ids,
            media_lane=media_lane,
        )
        record_action(
            db,
            run_id=run_id,
            action_kind="media_transcription_queue_read",
            tool_name="agent_get_media_transcription_jobs",
            endpoint="/api/local-agent/media/transcriptions/next",
            request_json={
                "limit": limit,
                "enqueue_limit": enqueue_limit,
                "render_readable_pdf_pages": bool(render_readable_pdf_pages),
                "stale_after_minutes": stale_after_minutes,
                "document_ids": target_document_ids,
                "media_lane": media_lane,
            },
            response_json={
                "count": len(jobs),
                "job_ids": [row.get("job_id") for row in jobs],
                "document_ids": [row.get("document_id") for row in jobs],
                "attachment_ids": [row.get("attachment_id") for row in jobs],
                "enqueue": enqueue_result,
            },
        )
        db.commit()
        return JSONResponse({"ok": True, "enqueue": enqueue_result, "jobs": jobs})

    @app.post("/api/local-agent/media/transcriptions/requeue")
    async def local_agent_media_transcriptions_requeue(
        request: Request,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        raw_document_ids = payload.get("document_ids") or payload.get("documentIds") or []
        document_ids = [
            int(value)
            for value in (raw_document_ids if isinstance(raw_document_ids, list) else [raw_document_ids])
            if str(value).isdigit()
        ]
        dry_run = bool(payload.get("dry_run") or payload.get("dryRun"))
        include_success = bool(payload.get("include_success") or payload.get("includeSuccess"))
        campaign_id = str(payload.get("campaign_id") or payload.get("campaignId") or "").strip() or None
        reason = str(payload.get("reason") or "").strip()
        try:
            result = requeue_media_transcription_jobs(
                db,
                document_ids=document_ids,
                reason=reason,
                campaign_id=campaign_id,
                include_success=include_success,
                additional_attempts=int(payload.get("additional_attempts") or payload.get("additionalAttempts") or 3),
                limit=int(payload.get("limit") or 1000),
                dry_run=dry_run,
            )
            enqueue_result = None
            if not dry_run:
                enqueue_result = enqueue_pending_media_transcription_jobs(
                    db,
                    limit=int(payload.get("enqueue_limit") or payload.get("enqueueLimit") or 1000),
                    render_readable_pdf_pages=bool(
                        payload.get("render_readable_pdf_pages") or payload.get("renderReadablePdfPages")
                    ),
                    document_ids=document_ids,
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        response = {**result, "enqueue": enqueue_result}
        record_action(
            db,
            run_id=run_id,
            action_kind="media_transcription_requeue",
            status="planned" if dry_run else "success",
            tool_name="agent_requeue_media_transcriptions",
            endpoint="/api/local-agent/media/transcriptions/requeue",
            request_json={
                "document_ids": document_ids,
                "campaign_id": campaign_id,
                "reason": reason,
                "dry_run": dry_run,
                "include_success": include_success,
            },
            response_json=response,
            summary=(
                f"{'Planned' if dry_run else 'Requeued'} {result.get('requeued_jobs', 0)} "
                "media transcription job(s)."
            ),
        )
        db.commit()
        return JSONResponse({"ok": True, "result": response})

    @app.get("/api/local-agent/media/attachments/{attachment_id}/bytes")
    def local_agent_media_attachment_bytes(
        attachment_id: int,
        request: Request,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> FileResponse:
        _require_agent_auth(request)
        try:
            attachment, path = media_attachment_file(db, attachment_id=int(attachment_id))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        record_action(
            db,
            run_id=run_id,
            action_kind="media_attachment_bytes_read",
            tool_name="agent_get_media_attachment_bytes",
            endpoint=f"/api/local-agent/media/attachments/{attachment_id}/bytes",
            document_id=int(attachment.document_id),
            response_json={
                "attachment_id": int(attachment.id),
                "content_type": attachment.content_type,
                "sha256": attachment.sha256,
            },
        )
        db.commit()
        return FileResponse(path, media_type=attachment.content_type or "application/octet-stream")

    @app.post("/api/local-agent/media/transcriptions")
    async def local_agent_media_transcription_write(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        try:
            result = complete_media_transcription(db, payload, run_id=run_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        record_action(
            db,
            run_id=run_id,
            action_kind="media_transcription_write",
            status=str(result.get("status") or "success"),
            tool_name="agent_write_media_transcription",
            endpoint="/api/local-agent/media/transcriptions",
            document_id=int(result["document_id"]) if str(result.get("document_id") or "").isdigit() else None,
            request_json={
                "job_id": payload.get("job_id"),
                "document_id": payload.get("document_id"),
                "attachment_id": payload.get("attachment_id"),
                "backend": payload.get("backend"),
                "model": payload.get("model"),
                "text_chars": len(str(payload.get("text") or "")),
            },
            response_json=result,
            error=str(result.get("error") or "") or None,
        )
        db.commit()
        return JSONResponse({"ok": True, "result": result})

    @app.post("/api/local-agent/media/classifications/backfill")
    async def local_agent_media_classification_backfill(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        limit = int(payload.get("limit") or 100)
        scan_limit = int(payload.get("scan_limit") or payload.get("scanLimit") or max(1000, limit * 20))
        offset = int(payload.get("offset") or 0)
        result = backfill_legacy_media_classifications(db, limit=limit, scan_limit=scan_limit, offset=offset)
        record_action(
            db,
            run_id=run_id,
            action_kind="media_classification_backfill",
            tool_name="agent_backfill_media_classifications",
            endpoint="/api/local-agent/media/classifications/backfill",
            request_json={"limit": limit, "scan_limit": scan_limit, "offset": offset},
            response_json=result,
            summary=f"Backfilled {result.get('updated_rows', 0)} media classification row(s).",
        )
        db.commit()
        return JSONResponse({"ok": True, "result": result})

    @app.post("/api/local-agent/media/translations/backfill")
    async def local_agent_media_translation_backfill(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        limit = int(payload.get("limit") or 100)
        scan_limit = int(payload.get("scan_limit") or payload.get("scanLimit") or max(1000, limit * 20))
        offset = int(payload.get("offset") or 0)
        raw_document_ids = payload.get("document_ids") or payload.get("documentIds") or []
        document_ids = [
            int(value)
            for value in (raw_document_ids if isinstance(raw_document_ids, list) else [raw_document_ids])
            if str(value).isdigit()
        ]
        result = backfill_media_ocr_translations(
            db,
            limit=limit,
            scan_limit=scan_limit,
            offset=offset,
            document_ids=document_ids,
        )
        record_action(
            db,
            run_id=run_id,
            action_kind="media_translation_backfill",
            tool_name="agent_backfill_media_translations",
            endpoint="/api/local-agent/media/translations/backfill",
            request_json={"limit": limit, "scan_limit": scan_limit, "offset": offset, "document_ids": document_ids},
            response_json=result,
            summary=f"Backfilled {result.get('translated_rows', 0)} media translation row(s).",
        )
        db.commit()
        return JSONResponse({"ok": True, "result": result})

    @app.get("/api/local-agent/watchlists")
    def local_agent_watchlists(request: Request, run_id: int | None = None, db: Session = Depends(get_session)) -> JSONResponse:
        _require_agent_auth(request)
        rows = list_watchlists(db)
        record_action(
            db,
            run_id=run_id,
            action_kind="watchlist_read",
            tool_name="agent_get_watchlists",
            endpoint="/api/local-agent/watchlists",
            response_json={"count": len(rows)},
        )
        db.commit()
        return JSONResponse({"ok": True, "watchlists": rows})

    @app.get("/api/local-agent/decision/context")
    def local_agent_decision_context(
        request: Request,
        company_id: int | None = None,
        issuer_id: int | None = None,
        ticker: str | None = None,
        sync: bool = False,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        company, issuer = resolve_company_or_issuer_from_payload(
            db,
            _agent_resolution_payload({"company_id": company_id, "issuer_id": issuer_id, "ticker": ticker}),
        )
        if company is None and issuer is None:
            raise HTTPException(status_code=404, detail="Company or issuer not found.")
        stats: dict[str, int | bool] = {"skipped": True, "read_only": True}
        if sync:
            stats = sync_decision_spine(
                db,
                company_id=int(company.id) if company is not None else None,
                issuer_id=int(issuer.id) if issuer is not None else None,
            )
        context = cached_company_decision_payload(db, company=company, issuer=issuer, read_only=not sync)
        if not context and not sync:
            stats = sync_decision_spine(
                db,
                company_id=int(company.id) if company is not None else None,
                issuer_id=int(issuer.id) if issuer is not None else None,
            )
            context = company_decision_payload(db, company=company, issuer=issuer, read_only=False)
        record_action(
            db,
            run_id=run_id,
            action_kind="decision_context_read",
            tool_name="agent_get_decision_context",
            endpoint="/api/local-agent/decision/context",
            request_json={"company_id": company_id, "issuer_id": issuer_id, "ticker": ticker, "sync": bool(sync)},
            response_json={
                "subject": (context.get("subject") or {}).get("subject_key"),
                "coverage_score": context.get("coverage_score"),
                "sync": stats,
            },
        )
        db.commit()
        return JSONResponse({"ok": True, "context": context, "sync": stats})

    @app.get("/api/local-agent/calendar")
    def local_agent_calendar(
        request: Request,
        company_id: int | None = None,
        issuer_id: int | None = None,
        ticker: str | None = None,
        days_back: int = 30,
        days_forward: int = 365,
        limit: int = 100,
        sync: bool = False,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        company, issuer = resolve_company_or_issuer_from_payload(
            db,
            _agent_resolution_payload({"company_id": company_id, "issuer_id": issuer_id, "ticker": ticker}),
        )
        if company is None and issuer is None:
            raise HTTPException(status_code=404, detail="Company or issuer not found.")
        if sync:
            sync_decision_spine(
                db,
                company_id=int(company.id) if company is not None else None,
                issuer_id=int(issuer.id) if issuer is not None else None,
            )
        rows = list_investment_catalysts(
            db,
            company_id=int(company.id) if company is not None else None,
            issuer_id=int(issuer.id) if issuer is not None else None,
            days_back=max(1, int(days_back or 30)),
            days_forward=max(1, int(days_forward or 365)),
            limit=max(1, min(int(limit or 100), 500)),
        )
        record_action(
            db,
            run_id=run_id,
            action_kind="investment_calendar_read",
            tool_name="agent_get_investment_calendar",
            endpoint="/api/local-agent/calendar",
            request_json={"company_id": company_id, "issuer_id": issuer_id, "ticker": ticker, "sync": bool(sync)},
            response_json={"count": len(rows)},
        )
        db.commit()
        return JSONResponse({"ok": True, "catalysts": rows})

    @app.post("/api/local-agent/decision/context-batch")
    async def local_agent_decision_context_batch(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        targets = [dict(item) for item in _json_list(payload.get("targets")) if isinstance(item, dict)]
        if not targets:
            raise HTTPException(status_code=400, detail="targets is required.")
        days_back = max(1, min(int(payload.get("days_back") or 30), 3650))
        days_forward = max(1, min(int(payload.get("days_forward") or 365), 3650))
        limit = max(1, min(int(payload.get("limit") or 100), 500))
        sync = bool(payload.get("sync", False))
        results: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for index, target in enumerate(targets[:20]):
            query = target.get("query") if isinstance(target.get("query"), dict) else target
            try:
                subject_id = query.get("subject_id")
                if str(subject_id or "").isdigit():
                    subject = db.query(ResearchSubject).filter_by(id=int(subject_id), status="active").first()
                    if subject is None:
                        unresolved.append(_decision_target_unresolved_payload(db, target, index=index))
                        continue
                    if subject.company_id is None and subject.issuer_id is None:
                        results.append({
                            "target": target,
                            "resolved": {"subject_id": int(subject.id)},
                            "context": {"subject": {"id": int(subject.id), "subject_key": subject.subject_key, "display_name": subject.display_name}},
                            "calendar": [],
                            "sync": {"skipped": True, "read_only": True},
                        })
                        continue
                resolution_payload = dict(query)
                for source_key, dest_key in (
                    ("label", "name"),
                    ("name", "name"),
                    ("entity_name", "entity_name"),
                    ("company_name", "company_name"),
                    ("issuer_name", "issuer_name"),
                ):
                    if target.get(source_key) not in (None, "") and resolution_payload.get(dest_key) in (None, ""):
                        resolution_payload[dest_key] = target.get(source_key)
                company, issuer = resolve_company_or_issuer_from_payload(db, _agent_resolution_payload(resolution_payload))
                if company is None and issuer is None:
                    unresolved.append(_decision_target_unresolved_payload(db, target, index=index))
                    continue
                stats: dict[str, int | bool] = {"skipped": True, "read_only": True}
                if sync:
                    stats = sync_decision_spine(
                        db,
                        company_id=int(company.id) if company is not None else None,
                        issuer_id=int(issuer.id) if issuer is not None else None,
                    )
                context = cached_company_decision_payload(db, company=company, issuer=issuer, read_only=not sync)
                if not context and not sync:
                    stats = sync_decision_spine(
                        db,
                        company_id=int(company.id) if company is not None else None,
                        issuer_id=int(issuer.id) if issuer is not None else None,
                    )
                    context = company_decision_payload(db, company=company, issuer=issuer, read_only=False)
                calendar = list_investment_catalysts(
                    db,
                    company_id=int(company.id) if company is not None else None,
                    issuer_id=int(issuer.id) if issuer is not None else None,
                    days_back=days_back,
                    days_forward=days_forward,
                    limit=limit,
                )
                results.append(
                    {
                        "target": target,
                        "resolved": {
                            "company_id": int(company.id) if company is not None else None,
                            "issuer_id": int(issuer.id) if issuer is not None else None,
                        },
                        "context": context,
                        "calendar": calendar,
                        "sync": stats,
                    }
                )
            except Exception as exc:
                errors.append({"index": index, "target": target, "error": f"{type(exc).__name__}: {exc}"})
        record_action(
            db,
            run_id=run_id,
            action_kind="decision_context_batch_read",
            tool_name="agent_get_decision_context_batch",
            endpoint="/api/local-agent/decision/context-batch",
            request_json={"targets": len(targets), "sync": sync, "days_back": days_back, "days_forward": days_forward, "limit": limit},
            response_json={"count": len(results), "unresolved": len(unresolved), "errors": len(errors)},
        )
        db.commit()
        return JSONResponse({"ok": True, "results": results, "unresolved": unresolved, "errors": errors})

    @app.get("/api/local-agent/market-context")
    def local_agent_market_context(
        request: Request,
        base_currency: str | None = None,
        live_quotes: bool = False,
        max_items: int = 30,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        context = build_agent_market_context(
            db,
            base_currency=base_currency,
            live_quotes=bool(live_quotes),
            max_items=max(1, min(int(max_items or 30), 100)),
        )
        record_action(
            db,
            run_id=run_id,
            action_kind="market_context_read",
            tool_name="agent_get_market_context",
            endpoint="/api/local-agent/market-context",
            request_json={"base_currency": base_currency, "live_quotes": bool(live_quotes), "max_items": max_items},
            response_json={
                "fx_base": (context.get("fx") or {}).get("base"),
                "watchlist_count": len((context.get("market_watchlist") or {}).get("items") or []),
                "futures_count": len(context.get("futures") or []),
            },
        )
        db.commit()
        return JSONResponse({"ok": True, "context": context})

    @app.get("/api/local-agent/institutional/model-context")
    def local_agent_institutional_model_context(
        request: Request,
        document_id: int | None = None,
        q: str | None = None,
        subject_id: int | None = None,
        max_source_actors: int = 12,
        max_subjects: int = 8,
        max_state_vectors: int = 8,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        context = institutional_model_context(
            db,
            document_id=document_id,
            query=q,
            subject_id=subject_id,
            max_source_actors=max(1, min(int(max_source_actors or 12), 30)),
            max_subjects=max(1, min(int(max_subjects or 8), 20)),
            max_state_vectors=max(1, min(int(max_state_vectors or 8), 20)),
        )
        record_action(
            db,
            run_id=run_id,
            action_kind="institutional_model_context_read",
            tool_name="agent_get_institutional_model_context",
            endpoint="/api/local-agent/institutional/model-context",
            document_id=document_id,
            request_json={
                "document_id": document_id,
                "q": q,
                "subject_id": subject_id,
                "max_source_actors": max_source_actors,
                "max_subjects": max_subjects,
                "max_state_vectors": max_state_vectors,
            },
            response_json={
                "source_actor_matches": len(context.get("source_actor_matches") or []),
                "subject_matches": len(context.get("subject_matches") or []),
                "state_vectors": len(context.get("state_vectors") or []),
                "metric_count": len(context.get("metric_vocabulary") or []),
            },
        )
        db.commit()
        return JSONResponse({"ok": True, "context": context})

    @app.post("/api/local-agent/catalysts")
    async def local_agent_upsert_catalyst(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_canonical_write_auth(request)
        payload = await _json_payload(request)
        payload = _agent_resolution_payload(payload)
        run_id = _payload_run_id(payload, request)
        dry_run = _dry_run_requested(request)
        row = upsert_investment_catalyst(db, payload=payload, actor="local_agent")
        deduped = bool(getattr(row, "_thoth_deduped", False))
        created = bool(getattr(row, "_thoth_created", False))
        result = {"ok": True, "catalyst": serialize_catalyst(row), "created": created, "deduped": deduped}
        if dry_run:
            db.rollback()
            result["dry_run"] = True
            return JSONResponse(result)
        record_action(
            db,
            run_id=run_id,
            action_kind="investment_catalyst_write_deduped" if deduped else "investment_catalyst_write",
            tool_name="agent_upsert_investment_catalyst",
            endpoint="/api/local-agent/catalysts",
            destination_type="investment_catalyst",
            destination_id=int(row.id),
            confidence=row.confidence,
            summary=row.title,
            response_json={"catalyst_id": int(row.id), "catalyst_key": row.catalyst_key, "created": created, "deduped": deduped},
        )
        db.commit()
        return JSONResponse(result)

    @app.post("/api/local-agent/investment-memos")
    async def local_agent_upsert_memo(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_canonical_write_auth(request)
        payload = await _json_payload(request)
        payload = _agent_resolution_payload(payload)
        run_id = _payload_run_id(payload, request)
        dry_run = _dry_run_requested(request)
        row = upsert_investment_memo(db, payload=payload, actor="local_agent")
        result = {"ok": True, "memo": serialize_memo(row)}
        if dry_run:
            db.rollback()
            result["dry_run"] = True
            return JSONResponse(result)
        record_action(
            db,
            run_id=run_id,
            action_kind="investment_memo_write",
            tool_name="agent_upsert_investment_memo",
            endpoint="/api/local-agent/investment-memos",
            destination_type="investment_memo",
            destination_id=int(row.id),
            summary=row.title,
            response_json={"memo_id": int(row.id), "memo_key": row.memo_key},
        )
        db.commit()
        return JSONResponse(result)

    @app.post("/api/local-agent/analyst-evidence")
    async def local_agent_upsert_analyst_evidence(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_canonical_write_auth(request)
        payload = await _json_payload(request)
        payload = _agent_resolution_payload(payload)
        run_id = _payload_run_id(payload, request)
        dry_run = _dry_run_requested(request)
        if dry_run:
            payload["_dry_run"] = True
        existing_id = None
        if dry_run and str(payload.get("evidence_key") or "").strip():
            existing_id = (
                db.query(AnalystEvidence.id)
                .filter_by(evidence_key=str(payload.get("evidence_key")).strip())
                .scalar()
            )
        document_id = int(payload["source_document_id"]) if str(payload.get("source_document_id") or "").isdigit() else None
        duplicate_context = _local_agent_duplicate_gate_context(db, payload, document_id=document_id)
        if duplicate_context is not None:
            _raise_local_agent_duplicate_gate(duplicate_context)
        try:
            row = upsert_analyst_evidence(db, payload=payload, actor="local_agent")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = {"ok": True, "analyst_evidence": serialize_analyst_evidence(row)}
        if dry_run:
            row_id = int(row.id)
            row_type = type(row)
            db.rollback()
            if existing_id is None:
                leaked = db.get(row_type, row_id)
                if leaked is not None:
                    db.delete(leaked)
                    db.commit()
            result["dry_run"] = True
            return JSONResponse(result)
        record_action(
            db,
            run_id=run_id,
            action_kind="analyst_evidence_write",
            tool_name="agent_upsert_analyst_evidence",
            endpoint="/api/local-agent/analyst-evidence",
            destination_type="analyst_evidence",
            destination_id=int(row.id),
            confidence=row.confidence,
            summary=row.summary,
            response_json={"analyst_evidence_id": int(row.id), "evidence_key": row.evidence_key},
        )
        db.commit()
        return JSONResponse(result)

    @app.post("/api/local-agent/evidence-links")
    async def local_agent_upsert_evidence_link(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_canonical_write_auth(request)
        payload = await _json_payload(request)
        payload = _agent_resolution_payload(payload)
        run_id = _payload_run_id(payload, request)
        dry_run = _dry_run_requested(request)
        row = upsert_decision_evidence_link(db, payload=payload, actor="local_agent")
        result = {"ok": True, "id": int(row.id)}
        if dry_run:
            db.rollback()
            result["dry_run"] = True
            return JSONResponse(result)
        record_action(
            db,
            run_id=run_id,
            action_kind="decision_evidence_link_write",
            tool_name="agent_upsert_decision_evidence_link",
            endpoint="/api/local-agent/evidence-links",
            destination_type=row.target_type,
            destination_id=int(row.target_id),
            confidence=row.confidence,
            summary=row.summary,
            response_json={"evidence_link_id": int(row.id)},
        )
        db.commit()
        return JSONResponse(result)

    @app.get("/api/local-agent/entities/search")
    def local_agent_entities_search(
        request: Request,
        q: str,
        limit: int = 12,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        rows = search_entities(db, query=q, limit=limit)
        record_action(
            db,
            run_id=run_id,
            action_kind="entity_search",
            tool_name="agent_search_entities",
            endpoint="/api/local-agent/entities/search",
            request_json={"q": q, "limit": limit},
            response_json={"count": len(rows)},
        )
        db.commit()
        return JSONResponse({"ok": True, "results": rows})

    @app.get("/api/local-agent/objects/search")
    def local_agent_objects_search(
        request: Request,
        q: str,
        kind: str = "all",
        limit: int = 12,
        include_neighborhoods: bool = False,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        payload = search_objects_for_agent(
            db,
            query=q,
            kind=kind,
            limit=limit,
            include_neighborhoods=include_neighborhoods,
        )
        record_action(
            db,
            run_id=run_id,
            action_kind="object_search",
            tool_name="agent_object_search",
            endpoint="/api/local-agent/objects/search",
            request_json={"q": q, "kind": kind, "limit": limit, "include_neighborhoods": include_neighborhoods},
            response_json={"count": payload.get("count")},
        )
        db.commit()
        return JSONResponse({"ok": True, "results": payload})

    @app.get("/api/local-agent/research-docs/search")
    def local_agent_research_docs_search(
        request: Request,
        q: str,
        limit: int = 12,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        rows = search_research_docs(db, query=q, limit=limit)
        record_action(
            db,
            run_id=run_id,
            action_kind="research_doc_search",
            tool_name="agent_search_research_docs",
            endpoint="/api/local-agent/research-docs/search",
            request_json={"q": q, "limit": limit},
            response_json={"count": len(rows)},
        )
        db.commit()
        return JSONResponse({"ok": True, "results": rows})

    @app.get("/api/local-agent/research-docs/{destination_type}/{destination_id}")
    def local_agent_get_research_doc(
        destination_type: str,
        destination_id: int,
        request: Request,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        doc = get_research_doc(db, destination_type=destination_type, destination_id=int(destination_id))
        record_action(
            db,
            run_id=run_id,
            action_kind="research_doc_read",
            tool_name="agent_get_research_doc",
            endpoint=f"/api/local-agent/research-docs/{destination_type}/{destination_id}",
            destination_type=destination_type,
            destination_id=int(destination_id),
            response_json={"content_chars": len(str(doc.get("content_md") or ""))},
        )
        db.commit()
        return JSONResponse({"ok": True, "document": doc})

    @app.post("/api/local-agent/durable/context")
    async def local_agent_durable_context(
        request: Request,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        raw_document_id = payload.get("document_id")
        document_id = int(raw_document_id) if str(raw_document_id or "").isdigit() else None
        context = durable_candidate_context(
            db,
            document_id=document_id,
            payload=payload,
            limit=int(payload.get("limit") or 8) if str(payload.get("limit") or "").isdigit() else 8,
        )
        record_action(
            db,
            run_id=run_id,
            document_id=document_id,
            action_kind="durable_candidate_context_read",
            tool_name="agent_durable_candidate_context",
            endpoint="/api/local-agent/durable/context",
            request_json={
                "document_id": document_id,
                "candidate_writes": len(_json_list(payload.get("candidate_writes") or payload.get("research_writes"))),
                "durable_routes": len(_json_list(payload.get("durable_routes"))),
            },
            response_json={
                "query_terms": context.get("query_terms"),
                "object_searches": len(_json_list(context.get("object_searches"))),
                "prior_write_diffs": len(_json_list(context.get("prior_write_diffs"))),
                "recommended_action": context.get("recommended_action"),
            },
        )
        db.commit()
        return JSONResponse({"ok": True, "context": context})

    @app.get("/api/local-agent/translation-overrides")
    def local_agent_translation_overrides(
        request: Request,
        q: str = "",
        approved: bool | None = None,
        limit: int = 50,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        rows = list_translation_overrides(db, query=q, approved=approved, limit=limit)
        record_action(
            db,
            run_id=run_id,
            action_kind="translation_override_read",
            tool_name="agent_get_translation_overrides",
            endpoint="/api/local-agent/translation-overrides",
            request_json={"q": q, "approved": approved, "limit": limit},
            response_json={"count": len(rows)},
        )
        db.commit()
        return JSONResponse({"ok": True, "overrides": rows})

    @app.get("/api/local-agent/instrument-requests")
    def local_agent_list_instrument_requests(
        request: Request,
        q: str = "",
        status: str = "open",
        scope: str = "all",
        limit: int = 50,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        safe_limit = max(1, min(int(limit or 50), 100))
        rows = list_instrument_requests(
            db,
            query=str(q or "").strip() or None,
            status=str(status or "open").strip() or "open",
            scope=str(scope or "all").strip() or "all",
            limit=safe_limit,
            include_payloads=False,
        )
        record_action(
            db,
            run_id=run_id,
            action_kind="instrument_request_read",
            tool_name="agent_list_instrument_requests",
            endpoint="/api/local-agent/instrument-requests",
            request_json={"q": q, "status": status, "scope": scope, "limit": safe_limit},
            response_json={"count": len(rows)},
        )
        db.commit()
        return JSONResponse({"ok": True, "count": len(rows), "instrument_requests": [serialize_instrument_request_summary(row) for row in rows]})

    @app.get("/api/local-agent/instrument-requests/{request_id}")
    def local_agent_get_instrument_request(
        request_id: int,
        request: Request,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        row = get_instrument_request(db, int(request_id))
        if row is None:
            raise HTTPException(status_code=404, detail="Instrument request not found.")
        payload = serialize_instrument_request(row)
        payload["markdown"] = instrument_request_markdown(row)
        record_action(
            db,
            run_id=run_id,
            action_kind="instrument_request_read",
            tool_name="agent_get_instrument_request",
            endpoint=f"/api/local-agent/instrument-requests/{int(request_id)}",
            request_json={"id": int(request_id)},
            response_json={"status": payload.get("status"), "request_key": payload.get("request_key")},
        )
        db.commit()
        return JSONResponse({"ok": True, "instrument_request": payload})

    @app.post("/api/local-agent/instrument-requests")
    async def local_agent_create_instrument_request(
        request: Request,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_canonical_write_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        raw_document_id = payload.get("document_id")
        document_id = int(raw_document_id) if str(raw_document_id or "").isdigit() else None
        item = payload.get("request") if isinstance(payload.get("request"), dict) else payload
        request_payload = _instrument_request_payload(dict(item), document_id=document_id, run_id=run_id)
        if not str(request_payload.get("title") or "").strip():
            raise HTTPException(status_code=400, detail="title is required.")
        row = create_instrument_request(db, **request_payload)
        record_action(
            db,
            run_id=run_id,
            document_id=document_id,
            action_kind="instrument_request_write",
            status="success",
            tool_name="agent_create_instrument_request",
            endpoint="/api/local-agent/instrument-requests",
            summary=str(request_payload.get("summary") or request_payload.get("title") or "").strip(),
            request_json={"title": request_payload.get("title"), "intent": request_payload.get("intent")},
            response_json={"instrument_request_id": int(row.id), "request_key": row.request_key},
        )
        db.commit()
        return JSONResponse({"ok": True, "instrument_request": serialize_instrument_request(row)})

    @app.post("/api/local-agent/instrument-requests/{request_id}/run")
    async def local_agent_run_instrument_request(
        request_id: int,
        request: Request,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_canonical_write_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        try:
            row = run_instrument_request(
                db,
                int(request_id),
                draft_payload_patch=dict(payload.get("draft_payload_patch") or {}) if isinstance(payload.get("draft_payload_patch"), dict) else None,
                linked_objects_patch=dict(payload.get("linked_objects_patch") or {}) if isinstance(payload.get("linked_objects_patch"), dict) else None,
                run_summary=str(payload.get("run_summary") or "").strip() or None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        record_action(
            db,
            run_id=run_id,
            action_kind="instrument_request_run",
            status="success",
            tool_name="agent_run_instrument_request",
            endpoint=f"/api/local-agent/instrument-requests/{int(request_id)}/run",
            summary=str(payload.get("run_summary") or "").strip() or None,
            request_json={"instrument_request_id": int(request_id)},
            response_json={"instrument_request_id": int(row.id), "status": row.status},
        )
        db.commit()
        return JSONResponse({"ok": True, "instrument_request": serialize_instrument_request(row)})

    @app.post("/api/local-agent/instrument-requests/{request_id}/review")
    async def local_agent_review_instrument_request(
        request_id: int,
        request: Request,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_canonical_write_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        try:
            row = review_instrument_request(
                db,
                int(request_id),
                decision=str(payload.get("decision") or "").strip(),
                reviewer_note=str(payload.get("reviewer_note") or "").strip() or None,
                live_payload=dict(payload.get("live_payload") or {}) if isinstance(payload.get("live_payload"), dict) else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        record_action(
            db,
            run_id=run_id,
            action_kind="instrument_request_review",
            status="success",
            tool_name="agent_review_instrument_request",
            endpoint=f"/api/local-agent/instrument-requests/{int(request_id)}/review",
            summary=str(payload.get("reviewer_note") or "").strip() or None,
            request_json={"instrument_request_id": int(request_id), "decision": payload.get("decision")},
            response_json={"instrument_request_id": int(row.id), "status": row.status},
        )
        db.commit()
        return JSONResponse({"ok": True, "instrument_request": serialize_instrument_request(row)})

    @app.post("/api/local-agent/instrument-requests/{request_id}/fulfill")
    async def local_agent_fulfill_instrument_request(
        request_id: int,
        request: Request,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_canonical_write_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        try:
            row = fulfill_instrument_request(
                db,
                int(request_id),
                fulfillment_summary=str(payload.get("fulfillment_summary") or "").strip(),
                fulfilled_by=str(payload.get("fulfilled_by") or "").strip() or None,
                live_payload_patch=dict(payload.get("live_payload_patch") or {}) if isinstance(payload.get("live_payload_patch"), dict) else None,
                linked_objects_patch=dict(payload.get("linked_objects_patch") or {}) if isinstance(payload.get("linked_objects_patch"), dict) else None,
                evidence_bundle_patch=[dict(v) for v in _json_list(payload.get("evidence_bundle_patch")) if isinstance(v, dict)],
                verification=dict(payload.get("verification") or {}) if isinstance(payload.get("verification"), dict) else None,
                close_linked_gaps=bool(payload.get("close_linked_gaps", True)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        record_action(
            db,
            run_id=run_id,
            action_kind="instrument_request_fulfillment",
            status="success",
            tool_name="agent_fulfill_instrument_request",
            endpoint=f"/api/local-agent/instrument-requests/{int(request_id)}/fulfill",
            summary=str(payload.get("fulfillment_summary") or "").strip() or None,
            request_json={"instrument_request_id": int(request_id), "fulfilled_by": payload.get("fulfilled_by")},
            response_json={"instrument_request_id": int(row.id), "status": row.status, "request_key": row.request_key},
        )
        db.commit()
        return JSONResponse({"ok": True, "instrument_request": serialize_instrument_request(row)})

    @app.post("/api/local-agent/compressions")
    async def local_agent_create_compression(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        document_id = payload.get("document_id")
        if not str(document_id or "").isdigit():
            raise HTTPException(status_code=400, detail="document_id is required.")
        _require_document_analysis_ready(db, int(document_id))
        compression_payload = _signal_extraction_write_payload(payload)
        row = create_or_update_compression(db, document_id=int(document_id), payload=compression_payload, run_id=run_id)
        record_action(
            db,
            run_id=run_id,
            action_kind="document_compression_write",
            tool_name="agent_create_document_compression",
            endpoint="/api/local-agent/compressions",
            document_id=int(document_id),
            confidence=row.confidence,
            summary=row.summary_short,
            request_json={"document_id": int(document_id)},
            response_json={"compression_id": int(row.id)},
        )
        db.commit()
        return JSONResponse(_signal_extraction_response(row))

    @app.post("/api/local-agent/signal-extractions")
    async def local_agent_create_signal_extraction(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        document_id = payload.get("document_id")
        if not str(document_id or "").isdigit():
            raise HTTPException(status_code=400, detail="document_id is required.")
        _require_document_analysis_ready(db, int(document_id))
        extraction_payload = _signal_extraction_write_payload(payload)
        row = create_or_update_compression(db, document_id=int(document_id), payload=extraction_payload, run_id=run_id)
        record_action(
            db,
            run_id=run_id,
            action_kind="signal_extraction_write",
            tool_name="agent_create_signal_extraction",
            endpoint="/api/local-agent/signal-extractions",
            document_id=int(document_id),
            confidence=row.confidence,
            summary=row.summary_short,
            request_json={"document_id": int(document_id)},
            response_json={"signal_extraction_id": int(row.id), "compression_id": int(row.id)},
        )
        db.commit()
        return JSONResponse(_signal_extraction_response(row))

    @app.post("/api/local-agent/documents/{document_id}/analysis-result")
    async def local_agent_document_analysis_result(
        document_id: int,
        request: Request,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        _require_document_analysis_ready(db, int(document_id))
        compression_payload = _signal_extraction_write_payload(payload)
        compression = create_or_update_compression(
            db,
            document_id=int(document_id),
            payload=compression_payload,
            run_id=run_id,
        )
        canonical_write_access = _has_canonical_write_access(request)
        cloud_review_job_id: int | None = None
        counts = {
            "claims_created": 0,
            "claims_deduped": 0,
            "unresolved_created": 0,
            "research_writes": 0,
            "research_write_errors": 0,
            "dataset_rows": 0,
            "dataset_materializations": 0,
            "supply_chain_metric_bindings": 0,
            "instrument_request_fulfillments": 0,
            "analyst_evidence": 0,
            "analyst_evidence_duplicate_skipped": 0,
            "analyst_evidence_skipped_not_modelable": 0,
            "catalysts": 0,
            "investment_memos": 0,
            "evidence_links": 0,
            "research_requests": 0,
            "errors": 0,
        }
        errors: list[dict[str, Any]] = []

        for claim_payload in (_analysis_claim_payloads(
            payload,
            run_id=run_id,
            document_id=int(document_id),
            compression_id=int(compression.id),
        ) if canonical_write_access else []):
            try:
                claim = create_claim(db, payload=claim_payload, run_id=run_id)
                if bool(getattr(claim, "_thoth_deduped", False)):
                    counts["claims_deduped"] += 1
                else:
                    counts["claims_created"] += 1
            except Exception as exc:
                counts["errors"] += 1
                errors.append({"kind": "claim", "error": f"{type(exc).__name__}: {exc}"})

        # Unresolved entities are review candidates, not promoted research facts.
        for unresolved_payload in _analysis_unresolved_payloads(payload, run_id=run_id, document_id=int(document_id)):
            try:
                create_unresolved_entity(db, payload=unresolved_payload, run_id=run_id)
                counts["unresolved_created"] += 1
            except Exception as exc:
                counts["errors"] += 1
                errors.append({"kind": "unresolved_entity", "error": f"{type(exc).__name__}: {exc}"})

        for write_payload in (_analysis_research_write_payloads(payload, run_id=run_id, document_id=int(document_id)) if canonical_write_access else []):
            action = record_action(
                db,
                run_id=run_id,
                action_kind="research_doc_write_attempt",
                status="running",
                tool_name="agent_write_research_doc",
                endpoint=f"/api/local-agent/documents/{document_id}/analysis-result",
                document_id=int(document_id),
                request_json={
                    "destination": write_payload.get("destination"),
                    "source_document_ids": write_payload.get("source_document_ids"),
                },
            )
            try:
                write_diff = write_research_destination(
                    db,
                    destination=write_payload.get("destination") if isinstance(write_payload.get("destination"), dict) else {},
                    payload=write_payload,
                    run_id=run_id,
                    action_id=int(action.id),
                )
                action.status = "success"
                action.destination_type = write_diff.destination_type
                action.destination_id = write_diff.destination_id
                action.summary = write_diff.write_summary
                action.confidence = write_diff.confidence
                action.response_json = {"write_diff_id": int(write_diff.id), "after_version_id": write_diff.after_version_id}
                counts["research_writes"] += 1
            except Exception as exc:
                action.status = "error"
                action.error = str(exc)
                counts["research_write_errors"] += 1
                counts["errors"] += 1
                errors.append({"kind": "research_write", "error": f"{type(exc).__name__}: {exc}"})

        durable_write_specs = (
            ("dataset_rows", "dataset_rows", "agent_write_dataset_rows", "dataset_or_data_series"),
            ("dataset_materializations", "dataset_materializations", "agent_materialize_dataset_family", "dataset_or_data_series"),
            ("supply_chain_metric_bindings", "supply_chain_metric_bindings", "agent_bind_supply_chain_metric", "supply_chain_metric_binding"),
        )
        for field, count_key, tool_name, destination_type in durable_write_specs:
            for item in ([dict(row) for row in _json_list(payload.get(field)) if isinstance(row, dict)] if canonical_write_access else []):
                item_payload = dict(item)
                item_payload.setdefault("source_document_ids", [int(document_id)])
                if "source_document_id" in item_payload and "source_document_ids" not in item_payload:
                    item_payload["source_document_ids"] = [item_payload["source_document_id"]]
                destination = item_payload.get("destination") if isinstance(item_payload.get("destination"), dict) else {}
                destination = dict(destination)
                destination.setdefault("type", destination_type)
                if field == "dataset_rows":
                    raw_dataset_id = item_payload.get("dataset_id") or item_payload.get("id")
                    if str(raw_dataset_id or "").isdigit():
                        destination.setdefault("id", int(raw_dataset_id))
                elif field == "dataset_materializations":
                    for key in ("slug", "title", "family", "category", "source_kind"):
                        if item_payload.get(key) not in (None, ""):
                            destination.setdefault(key, item_payload.get(key))
                    if item_payload.get("dataset_family") not in (None, ""):
                        destination.setdefault("family", item_payload.get("dataset_family"))
                elif field == "supply_chain_metric_bindings":
                    raw_chain_id = item_payload.get("supply_chain_id") or item_payload.get("chain_id") or item_payload.get("id")
                    if str(raw_chain_id or "").isdigit():
                        destination.setdefault("id", int(raw_chain_id))
                action = record_action(
                    db,
                    run_id=run_id,
                    action_kind=f"{field}_write_attempt",
                    status="running",
                    tool_name=tool_name,
                    endpoint=f"/api/local-agent/documents/{document_id}/analysis-result",
                    document_id=int(document_id),
                    request_json={"destination": destination, "source_document_ids": item_payload.get("source_document_ids")},
                )
                try:
                    write_diff = write_research_destination(
                        db,
                        destination=destination,
                        payload=item_payload,
                        run_id=run_id,
                        action_id=int(action.id),
                    )
                    action.status = "success"
                    action.destination_type = write_diff.destination_type
                    action.destination_id = write_diff.destination_id
                    action.summary = write_diff.write_summary
                    action.confidence = write_diff.confidence
                    action.response_json = {"write_diff_id": int(write_diff.id), "destination_type": write_diff.destination_type}
                    counts[count_key] += 1
                except Exception as exc:
                    action.status = "error"
                    action.error = str(exc)
                    counts["errors"] += 1
                    errors.append({"kind": field, "error": f"{type(exc).__name__}: {exc}"})

        for item in (_analysis_analyst_evidence_payloads(payload) if canonical_write_access else []):
            try:
                item_payload = _agent_resolution_payload(item)
                item_payload.setdefault("source_document_id", int(document_id))
                if not _analysis_analyst_evidence_is_modelable(item_payload):
                    counts["analyst_evidence_skipped_not_modelable"] += 1
                    continue
                duplicate_context = _local_agent_duplicate_gate_context(db, item_payload, document_id=int(document_id))
                if duplicate_context is not None:
                    counts["analyst_evidence_duplicate_skipped"] += 1
                    continue
                row = upsert_analyst_evidence(db, payload=item_payload, actor="local_agent")
                counts["analyst_evidence"] += 1
                link_payload = _decision_evidence_link_payload(
                    source_payload=item,
                    document_id=int(document_id),
                    target_type="analyst_evidence",
                    target_id=int(row.id),
                )
                if link_payload is not None:
                    upsert_decision_evidence_link(db, payload=_agent_resolution_payload(link_payload), actor="local_agent")
                    counts["evidence_links"] += 1
            except Exception as exc:
                counts["errors"] += 1
                errors.append({"kind": "analyst_evidence", "error": f"{type(exc).__name__}: {exc}"})

        for item in ([dict(row) for row in _json_list(payload.get("catalysts")) if isinstance(row, dict)] if canonical_write_access else []):
            try:
                row = upsert_investment_catalyst(db, payload=_agent_resolution_payload(item), actor="local_agent")
                counts["catalysts"] += 1
                link_payload = _decision_evidence_link_payload(
                    source_payload=item,
                    document_id=int(document_id),
                    target_type="investment_catalyst",
                    target_id=int(row.id),
                )
                if link_payload is not None:
                    upsert_decision_evidence_link(db, payload=_agent_resolution_payload(link_payload), actor="local_agent")
                    counts["evidence_links"] += 1
            except Exception as exc:
                counts["errors"] += 1
                errors.append({"kind": "catalyst", "error": f"{type(exc).__name__}: {exc}"})

        for item in ([dict(row) for row in _json_list(payload.get("investment_memos")) if isinstance(row, dict)] if canonical_write_access else []):
            try:
                row = upsert_investment_memo(db, payload=_agent_resolution_payload(item), actor="local_agent")
                counts["investment_memos"] += 1
                link_payload = _decision_evidence_link_payload(
                    source_payload=item,
                    document_id=int(document_id),
                    target_type="investment_memo",
                    target_id=int(row.id),
                )
                if link_payload is not None:
                    upsert_decision_evidence_link(db, payload=_agent_resolution_payload(link_payload), actor="local_agent")
                    counts["evidence_links"] += 1
            except Exception as exc:
                counts["errors"] += 1
                errors.append({"kind": "investment_memo", "error": f"{type(exc).__name__}: {exc}"})

        for item in ([dict(row) for row in _json_list(payload.get("evidence_links")) if isinstance(row, dict)] if canonical_write_access else []):
            try:
                row = upsert_decision_evidence_link(db, payload=_agent_resolution_payload(item), actor="local_agent")
                counts["evidence_links"] += 1
            except Exception as exc:
                counts["errors"] += 1
                errors.append({"kind": "evidence_link", "error": f"{type(exc).__name__}: {exc}"})

        for item in ([dict(row) for row in _json_list(payload.get("research_requests")) if isinstance(row, dict)] if canonical_write_access else []):
            try:
                request_payload = _instrument_request_payload(item, document_id=int(document_id), run_id=run_id)
                if not str(request_payload.get("title") or "").strip():
                    raise ValueError("title is required")
                create_instrument_request(db, **request_payload)
                counts["research_requests"] += 1
            except Exception as exc:
                counts["errors"] += 1
                errors.append({"kind": "research_request", "error": f"{type(exc).__name__}: {exc}"})

        for item in ([dict(row) for row in _json_list(payload.get("instrument_request_fulfillments")) if isinstance(row, dict)] if canonical_write_access else []):
            try:
                raw_request_id = item.get("instrument_request_id") or item.get("request_id") or item.get("id")
                if not str(raw_request_id or "").isdigit():
                    raise ValueError("instrument_request_id is required")
                row = fulfill_instrument_request(
                    db,
                    int(raw_request_id),
                    fulfillment_summary=str(item.get("fulfillment_summary") or item.get("summary") or "").strip(),
                    fulfilled_by=str(item.get("fulfilled_by") or "local_agent").strip() or "local_agent",
                    live_payload_patch=dict(item.get("live_payload_patch") or {}) if isinstance(item.get("live_payload_patch"), dict) else None,
                    linked_objects_patch=dict(item.get("linked_objects_patch") or {}) if isinstance(item.get("linked_objects_patch"), dict) else None,
                    evidence_bundle_patch=[dict(v) for v in _json_list(item.get("evidence_bundle_patch")) if isinstance(v, dict)],
                    verification=dict(item.get("verification") or {}) if isinstance(item.get("verification"), dict) else None,
                    close_linked_gaps=bool(item.get("close_linked_gaps", True)),
                )
                record_action(
                    db,
                    run_id=run_id,
                    document_id=int(document_id),
                    action_kind="instrument_request_fulfillment",
                    status="success",
                    tool_name="agent_fulfill_instrument_request",
                    endpoint=f"/api/local-agent/documents/{document_id}/analysis-result",
                    summary=str(item.get("fulfillment_summary") or item.get("summary") or "").strip(),
                    request_json={"instrument_request_id": int(raw_request_id)},
                    response_json={"instrument_request_id": int(row.id), "status": row.status},
                )
                counts["instrument_request_fulfillments"] += 1
            except Exception as exc:
                counts["errors"] += 1
                errors.append({"kind": "instrument_request_fulfillment", "error": f"{type(exc).__name__}: {exc}"})

        if (
            not canonical_write_access
            and bool(getattr(settings, "local_agent_cloud_promotion_enabled", False))
            and compression.document_disposition not in {"skip", "noop"}
        ):
            cloud_review_job_id = _enqueue_deepseek_promotion_review(
                db,
                document_id=int(document_id),
                compression_id=int(compression.id),
                run_id=run_id,
            )

        record_action(
            db,
            run_id=run_id,
            action_kind="analysis_proposal_write" if not canonical_write_access else "analysis_result_write",
            status="error" if errors else "success",
            tool_name="agent_write_analysis_result",
            endpoint=f"/api/local-agent/documents/{document_id}/analysis-result",
            document_id=int(document_id),
            confidence=compression.confidence,
            summary=compression.summary_short,
            request_json={"document_id": int(document_id)},
            response_json={
                "compression_id": int(compression.id),
                "signal_extraction_id": int(compression.id),
                "permission_mode": "proposal_only" if not canonical_write_access else "canonical_writer",
                "cloud_review_job_id": cloud_review_job_id,
                **counts,
            },
            error="; ".join(str(row.get("error") or "") for row in errors[:3]) if errors else None,
        )
        db.commit()
        response = _signal_extraction_response(compression)
        response.update(
            {
                "counts": counts,
                "errors": errors,
                "permission_mode": "proposal_only" if not canonical_write_access else "canonical_writer",
                "cloud_review_job_id": cloud_review_job_id,
            }
        )
        return JSONResponse(
            response
        )

    @app.get("/api/local-agent/deep-analysis/next")
    def local_agent_next_deep_analysis(
        request: Request,
        hours: int = 48,
        limit: int = 3,
        max_text_chars: int = 12000,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        _require_local_work_admission(request, db, run_id=run_id)
        candidates = list_deep_analysis_candidates(
            db,
            hours=hours,
            limit=limit,
            max_text_chars=max_text_chars,
        )
        record_action(
            db,
            run_id=run_id,
            action_kind="deep_analysis_queue_read",
            tool_name="agent_get_deep_analysis_candidates",
            endpoint="/api/local-agent/deep-analysis/next",
            request_json={"hours": hours, "limit": limit, "max_text_chars": max_text_chars},
            response_json={"count": len(candidates)},
        )
        db.commit()
        return JSONResponse({"ok": True, "candidates": candidates})

    @app.get("/api/local-agent/durable-review/next")
    def local_agent_next_durable_review(
        request: Request,
        hours: int = 72,
        limit: int = 2,
        max_text_chars: int = 12000,
        document_ids: str | None = None,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        _require_local_work_admission(request, db, run_id=run_id)
        target_document_ids = _query_int_list(document_ids)
        candidates = list_durable_review_candidates(
            db,
            hours=hours,
            limit=limit,
            max_text_chars=max_text_chars,
            document_ids=target_document_ids,
        )
        record_action(
            db,
            run_id=run_id,
            action_kind="durable_review_queue_read",
            tool_name="agent_get_durable_review_candidates",
            endpoint="/api/local-agent/durable-review/next",
            request_json={"hours": hours, "limit": limit, "max_text_chars": max_text_chars, "document_ids": target_document_ids},
            response_json={"count": len(candidates)},
        )
        db.commit()
        return JSONResponse({"ok": True, "candidates": candidates})

    @app.post("/api/local-agent/compression-traces")
    async def local_agent_create_compression_trace(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        compression_id = payload.get("compression_id") or payload.get("signal_extraction_id")
        if not str(compression_id or "").isdigit():
            raise HTTPException(status_code=400, detail="compression_id or signal_extraction_id is required.")
        trace_payload = payload.get("trace") if isinstance(payload.get("trace"), dict) else payload
        try:
            row = create_or_update_deep_analysis_trace(
                db,
                compression_id=int(compression_id),
                payload=trace_payload,
                run_id=run_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        record_action(
            db,
            run_id=run_id,
            action_kind="compression_deep_analysis_write",
            tool_name="agent_create_compression_trace",
            endpoint="/api/local-agent/compression-traces",
            document_id=int(row.document_id),
            destination_type="local_agent_document_compression",
            destination_id=int(row.compression_id),
            confidence=row.signal_score,
            summary=_short_trace_summary(row.summary_md, row.error),
            request_json={"compression_id": int(row.compression_id), "analysis_mode": row.analysis_mode},
            response_json={"trace_id": int(row.id), "status": row.status, "signal_level": row.signal_level},
            error=row.error if row.status == "error" else None,
        )
        db.commit()
        return JSONResponse({"ok": True, "trace": serialize_deep_analysis_trace(row, include_payload=False)})

    @app.post("/api/local-agent/signal-extraction-traces")
    async def local_agent_create_signal_extraction_trace(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        signal_extraction_id = payload.get("signal_extraction_id") or payload.get("compression_id")
        if not str(signal_extraction_id or "").isdigit():
            raise HTTPException(status_code=400, detail="signal_extraction_id is required.")
        trace_payload = payload.get("trace") if isinstance(payload.get("trace"), dict) else payload
        try:
            row = create_or_update_deep_analysis_trace(
                db,
                compression_id=int(signal_extraction_id),
                payload=trace_payload,
                run_id=run_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        record_action(
            db,
            run_id=run_id,
            action_kind="signal_extraction_deep_analysis_write",
            tool_name="agent_create_signal_extraction_trace",
            endpoint="/api/local-agent/signal-extraction-traces",
            document_id=int(row.document_id),
            destination_type="local_agent_document_compression",
            destination_id=int(row.compression_id),
            confidence=row.signal_score,
            summary=_short_trace_summary(row.summary_md, row.error),
            request_json={"signal_extraction_id": int(row.compression_id), "compression_id": int(row.compression_id), "analysis_mode": row.analysis_mode},
            response_json={"trace_id": int(row.id), "status": row.status, "signal_level": row.signal_level},
            error=row.error if row.status == "error" else None,
        )
        db.commit()
        return JSONResponse({"ok": True, "trace": serialize_deep_analysis_trace(row, include_payload=False)})

    @app.post("/api/local-agent/claims")
    async def local_agent_create_claim(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_canonical_write_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        row = create_claim(db, payload=payload, run_id=run_id)
        deduped = bool(getattr(row, "_thoth_deduped", False))
        record_action(
            db,
            run_id=run_id,
            action_kind="claim_write_deduped" if deduped else "claim_write",
            tool_name="agent_attach_claim",
            endpoint="/api/local-agent/claims",
            document_id=row.document_id,
            destination_type=row.destination_type,
            destination_id=row.destination_id,
            confidence=row.confidence,
            summary=row.claim_text[:240],
            response_json={"claim_id": int(row.id), "deduped": deduped},
        )
        db.commit()
        return JSONResponse({"ok": True, "claim": serialize_claim(row), "created": not deduped, "deduped": deduped})

    @app.post("/api/local-agent/research-docs/write")
    async def local_agent_write_doc(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_canonical_write_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        destination = payload.get("destination") if isinstance(payload.get("destination"), dict) else {}
        action = record_action(
            db,
            run_id=run_id,
            action_kind="research_doc_write_attempt",
            status="running",
            tool_name="agent_write_research_doc",
            endpoint="/api/local-agent/research-docs/write",
            request_json={"destination": destination, "source_document_ids": payload.get("source_document_ids")},
        )
        try:
            row = write_research_destination(db, destination=destination, payload=payload, run_id=run_id, action_id=int(action.id))
        except ValueError as exc:
            action.status = "error"
            action.error = str(exc)
            db.commit()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            action.status = "error"
            action.error = str(exc)
            db.commit()
            raise
        action.status = "success"
        action.destination_type = row.destination_type
        action.destination_id = row.destination_id
        action.summary = row.write_summary
        action.confidence = row.confidence
        action.response_json = {"write_diff_id": int(row.id), "after_version_id": row.after_version_id}
        db.commit()
        return JSONResponse({"ok": True, "write": serialize_write_diff(row)})

    @app.post("/api/local-agent/unresolved-entities")
    async def local_agent_unresolved_entity(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        row = create_unresolved_entity(db, payload=payload, run_id=run_id)
        record_action(
            db,
            run_id=run_id,
            action_kind="unresolved_entity_write",
            tool_name="agent_create_unresolved_entity",
            endpoint="/api/local-agent/unresolved-entities",
            document_id=row.document_id,
            confidence=row.confidence,
            summary=row.name,
            response_json={"unresolved_entity_id": int(row.id)},
        )
        db.commit()
        return JSONResponse({"ok": True, "id": int(row.id)})

    @app.post("/api/local-agent/translation-overrides")
    async def local_agent_translation_override(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        row = suggest_translation_override(db, payload=payload, run_id=run_id)
        record_action(
            db,
            run_id=run_id,
            action_kind="translation_override_write",
            tool_name="agent_suggest_translation_override",
            endpoint="/api/local-agent/translation-overrides",
            document_id=row.first_seen_document_id,
            confidence=row.confidence,
            summary=f"{row.source_text} -> {row.canonical_text}",
            response_json={"translation_override_id": int(row.id)},
        )
        db.commit()
        return JSONResponse({"ok": True, "id": int(row.id)})

    @app.post("/api/local-agent/daily-summaries")
    async def local_agent_daily_summary(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_canonical_write_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        row = upsert_daily_summary(db, payload=payload, run_id=run_id)
        record_action(
            db,
            run_id=run_id,
            action_kind="daily_summary_write",
            tool_name="agent_create_daily_summary",
            endpoint="/api/local-agent/daily-summaries",
            summary=row.title,
            response_json={"daily_summary_id": int(row.id), "summary_date": row.summary_date.isoformat()},
        )
        db.commit()
        return JSONResponse({"ok": True, "id": int(row.id)})

    @app.get("/api/local-agent/daily-summary/context")
    def local_agent_daily_summary_context(
        request: Request,
        hours: int = 24,
        limit: int = 250,
        run_id: int | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        context = daily_summary_context(db, hours=hours, limit=limit)
        record_action(
            db,
            run_id=run_id,
            action_kind="daily_summary_context_read",
            tool_name="agent_get_daily_summary_context",
            endpoint="/api/local-agent/daily-summary/context",
            request_json={"hours": hours, "limit": limit},
            response_json={
                "compressions": len(context.get("compressions") or []),
                "claims": len(context.get("claims") or []),
                "writes": len(context.get("writes") or []),
            },
        )
        db.commit()
        return JSONResponse({"ok": True, "context": context})

    @app.post("/api/local-agent/errors")
    async def local_agent_record_error(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        action = record_action(
            db,
            run_id=run_id,
            action_kind=str(payload.get("action_kind") or "agent_error"),
            status="error",
            tool_name=str(payload.get("tool_name") or "agent_record_error"),
            endpoint="/api/local-agent/errors",
            document_id=int(payload.get("document_id")) if str(payload.get("document_id") or "").isdigit() else None,
            summary=payload.get("summary"),
            request_json=payload,
            error=str(payload.get("error") or "agent error"),
        )
        document_id = int(payload.get("document_id")) if str(payload.get("document_id") or "").isdigit() else None
        error_compression = create_error_compression_for_model_failure(
            db,
            document_id=document_id,
            run_id=run_id,
            error=str(payload.get("error") or ""),
            payload=payload,
        )
        db.commit()
        return JSONResponse(
            {
                "ok": True,
                "action": {"id": int(action.id)},
                "error_compression": serialize_compression(error_compression) if error_compression is not None else None,
            }
        )

    @app.post("/api/local-agent/actions")
    async def local_agent_record_action(request: Request, db: Session = Depends(get_session)) -> JSONResponse:
        _require_agent_auth(request)
        payload = await _json_payload(request)
        run_id = _payload_run_id(payload, request)
        document_id = int(payload.get("document_id")) if str(payload.get("document_id") or "").isdigit() else None
        destination_id = int(payload.get("destination_id")) if str(payload.get("destination_id") or "").isdigit() else None
        confidence = None
        if payload.get("confidence") is not None and str(payload.get("confidence")).strip():
            try:
                confidence = float(payload.get("confidence"))
            except (TypeError, ValueError):
                confidence = None
        if _dry_run_requested(request):
            return JSONResponse(
                {
                    "ok": True,
                    "dry_run": True,
                    "action": {
                        "id": 0,
                        "action_kind": str(payload.get("action_kind") or "agent_action"),
                        "document_id": document_id,
                    },
                }
            )
        action = record_action(
            db,
            run_id=run_id,
            action_kind=str(payload.get("action_kind") or "agent_action"),
            status=str(payload.get("status") or "success"),
            tool_name=str(payload.get("tool_name") or "agent_record_action"),
            endpoint=str(payload.get("endpoint") or "/api/local-agent/actions"),
            document_id=document_id,
            destination_type=str(payload.get("destination_type") or "").strip()[:64] or None,
            destination_id=destination_id,
            summary=payload.get("summary"),
            confidence=confidence,
            request_json=payload.get("request") if isinstance(payload.get("request"), dict) else {},
            response_json=payload.get("response") if isinstance(payload.get("response"), dict) else {},
            error=str(payload.get("error") or "").strip() or None,
        )
        db.commit()
        return JSONResponse({"ok": True, "action": {"id": int(action.id)}})

    @app.get("/api/local-agent/actions/recent")
    def local_agent_recent_actions(
        request: Request,
        hours: int = 72,
        limit: int = 200,
        include_payloads: bool = False,
        action_kind: str | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        return JSONResponse(
            {
                "ok": True,
                "actions": recent_actions(
                    db,
                    hours=hours,
                    limit=limit,
                    include_payloads=include_payloads,
                    action_kind=action_kind,
                ),
            }
        )

    @app.get("/api/local-agent/runs/recent")
    def local_agent_recent_runs(
        request: Request,
        hours: int = 72,
        limit: int = 100,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _require_agent_auth(request)
        return JSONResponse({"ok": True, "runs": recent_runs(db, hours=hours, limit=limit)})
