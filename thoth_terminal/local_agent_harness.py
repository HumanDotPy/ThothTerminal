"""Run-linked local model turns through the owned Harness, without write tools."""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Literal
from uuid import UUID, NAMESPACE_URL, uuid5
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.config import settings
from thoth_terminal.local_agent_service import finish_run, record_action, start_run
from thoth_terminal.models import InteractiveAgentOperation, LocalAgentAction, LocalAgentRun
from thoth_terminal.web.deepseek_harness_service import HarnessRemoteError, HarnessUnavailable, harness_service


class IntakeRequest(BaseModel):
    request_id: UUID
    run_id: int | None = Field(default=None, gt=0)
    worker_id: str = Field(min_length=1, max_length=128)
    role: Literal["text", "vision", "ocr"]
    model: str = Field(min_length=1, max_length=255)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=8)
    max_tokens: int = Field(ge=1, le=32768)
    temperature: float = Field(default=0.1, ge=0, le=2)
    timeout_s: float = Field(default=240, ge=1, le=900)


def prompt_parts(request: IntakeRequest) -> list[dict[str, str]]:
    system, user, images = [], [], []
    for message in request.messages:
        role, content = message.get("role"), message.get("content")
        if role not in {"system", "user"}:
            raise ValueError("Ingestion accepts only system instructions and current user evidence.")
        if isinstance(content, str):
            (system if role == "system" else user).append(content)
            continue
        if role != "user" or not isinstance(content, list):
            raise ValueError("Invalid ingestion message content.")
        for part in content:
            if not isinstance(part, dict):
                raise ValueError("Invalid ingestion content part.")
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                user.append(part["text"])
            elif part.get("type") == "image_url" and request.role != "text":
                image = part.get("image_url")
                if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                    raise ValueError("Invalid ingestion image part.")
                url = image["url"]
                match = re.fullmatch(r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/]+={0,2})", url)
                if not match or len(match[2]) > 12 * 1024 * 1024:
                    raise ValueError("Send a bounded inline image; external image URLs are disabled.")
                images.append({"type": "image", "mediaType": match[1], "data": match[2], "name": f"evidence-{len(images) + 1}"})
            else:
                raise ValueError("Unsupported ingestion content part.")
    system_text, user_text = "\n\n".join(system), "\n\n".join(user)
    if len(system_text.encode("utf-8")) > 200 * 1024 or len(user_text) > 2_000_000 or len(images) > 4:
        raise ValueError("Ingestion input exceeds its bounded request size.")
    if not user_text.strip() and not images:
        raise ValueError("Send current user evidence for ingestion.")
    envelope = {"thoth_intake": {"system_prompt": system_text, "max_tokens": request.max_tokens,
                                 "temperature": request.temperature}, "input": user_text}
    return [{"type": "text", "text": json.dumps(envelope, ensure_ascii=False)}, *images]


def execute_intake(db: Session, request: IntakeRequest) -> dict[str, Any]:
    parts = prompt_parts(request)
    payload = request.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    key = f"local-harness:{request.request_id}"
    previous = db.query(InteractiveAgentOperation).filter_by(operation_key=key).first()
    if previous:
        if previous.request_hash != digest:
            raise ValueError("This request ID belongs to a different ingestion input.")
        receipt = previous.receipt
        action = db.get(LocalAgentAction, receipt["action_id"])
        if action.status == "success":
            return dict(action.response_json)
        raise HarnessUnavailable("This request is already recorded; inspect its run before retrying with a new ID.")
    run = db.get(LocalAgentRun, request.run_id) if request.run_id else None
    if request.run_id and (not run or run.worker_id != request.worker_id or run.status != "running"):
        raise ValueError("Ingestion requires this worker's active Thoth run.")
    standalone = run is None
    if standalone:
        run = start_run(db, run_type="harness_inference", worker_id=request.worker_id, model_name=request.model)
    session_id = str(uuid5(NAMESPACE_URL, f"thoth:intake:{run.id}:{request.worker_id}:{request.request_id}"))
    metadata = {"request_id": str(request.request_id), "session_id": session_id, "provider": f"thoth-local-{request.role}",
                "model": request.model, "role": request.role, "input_hash": digest}
    action = record_action(db, run_id=run.id, action_kind="harness_inference", status="running",
                           tool_name="harness.local_ingestion", request_json=metadata)
    operation = InteractiveAgentOperation(operation_key=key, request_hash=digest,
                                         receipt={"action_id": action.id, "run_id": run.id, "session_id": session_id}, created_at=utcnow())
    db.add(operation)
    try:
        db.commit()  # Release the DB connection before the model turn.
    except IntegrityError:
        db.rollback()
        raise HarnessUnavailable("This ingestion request is already being executed.") from None
    action_id, run_id = action.id, run.id
    db.commit()
    started = time.monotonic()
    local_time = datetime.now(ZoneInfo(str(getattr(settings, "default_active_window_tz", "America/New_York"))))
    try:
        result = harness_service.intake_turn({"sessionId": session_id, "requestId": session_id,
            "provider": metadata["provider"], "model": request.model, "content": parts,
            "timeoutSeconds": request.timeout_s,
            "title": f"Local {request.role} · run {run_id} · {local_time:%Y-%m-%d %H:%M %Z}"})
        response = {"ok": True, **result, "run_id": run_id, "action_id": action_id}
        usage, timing = result.get("tokenUsage") or {}, result.get("sessionStats") or {}
        inputs = [usage.get(key) for key in ("uncachedInputTokens", "cacheReadTokens", "cacheWriteTokens")]
        response["model_metrics"] = {
            "input_tokens": sum(value or 0 for value in inputs) if any(value is not None for value in inputs) else None,
            "cached_input_tokens": usage.get("cacheReadTokens"), "output_tokens": usage.get("outputTokens"),
            "latency_ms": timing.get("llmMs"), "generation_ms": timing.get("decodeMs"),
            "time_to_first_token_ms": timing.get("ttftMs"),
        }
        if len(str(response.get("text", ""))) > 256_000:
            raise HarnessUnavailable("Harness output exceeded the ingestion result bound.")
        action = db.get(LocalAgentAction, action_id)
        action.status, action.response_json = "success", {**response, "latency_ms": (time.monotonic() - started) * 1000}
        if standalone:
            finish_run(db, run_id=run_id, status="success", summary="Local Harness inference completed.")
        db.commit()
        return dict(action.response_json)
    except (HarnessUnavailable, HarnessRemoteError) as exc:
        db.rollback()
        action = db.get(LocalAgentAction, action_id)
        action.status, action.error = "error", str(exc)[:1000]
        action.response_json = {"code": getattr(exc, "code", "harness_unavailable"), "session_id": session_id,
                                "latency_ms": (time.monotonic() - started) * 1000}
        if standalone:
            finish_run(db, run_id=run_id, status="error", error=action.error)
        db.commit()
        raise
