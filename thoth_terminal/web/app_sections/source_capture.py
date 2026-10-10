from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


@app.post("/sources/{source_id}/playwright/test")
def source_playwright_test(
    source_id: int, db: Session = Depends(get_session)
) -> JSONResponse:
    src = db.query(Source).filter_by(id=source_id).first()
    if not src or (src.config or {}).get("deleted"):
        return JSONResponse({"ok": False, "error": "Source not found"}, status_code=404)
    cfg = dict(src.config or {})
    connector = cfg.get("connector")
    try:
        from thoth_terminal.browser_ingestion_runtime import run_browser_ingestion

        if connector == "x_list":
            result = run_browser_ingestion(probe_x_list, cfg)
        elif connector == "web_list_browser":
            result = run_browser_ingestion(probe_web_list_browser, cfg)
        elif connector == "substack_inbox_browser":
            probe_cfg = dict(cfg)
            probe_cfg["_probe_site"] = "substack"
            result = run_browser_ingestion(probe_paid_reader_browser, probe_cfg)
        elif connector == "note_creator_browser":
            probe_cfg = dict(cfg)
            probe_cfg["_probe_site"] = "note"
            result = run_browser_ingestion(probe_paid_reader_browser, probe_cfg)
        else:
            return JSONResponse(
                {"ok": False, "error": "Connector does not support Playwright testing"},
                status_code=400,
            )
        payload = {"ok": True}
        payload.update(result or {})
        return JSONResponse(payload)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})


@app.post("/sources/{source_id}/run")
def source_run(source_id: int) -> RedirectResponse:
    def _enqueue() -> None:
        with session_scope() as session:
            src = session.query(Source).filter_by(id=source_id).first()
            if src and not (src.config or {}).get("deleted"):
                enqueue_ingest_job(session, source_id=src.id)

    try:
        _with_db_retry(_enqueue, attempts=6, delay_s=0.25)
        _invalidate_terminal_caches("sources:", "home:")
    except Exception as exc:
        logging.getLogger("thoth.web").exception("Failed to enqueue source run: %s", exc)
    return RedirectResponse(url="/sources", status_code=303)


@app.post("/sources/{source_id}/delete")
def source_delete(source_id: int) -> RedirectResponse:
    def _delete_source() -> None:
        with session_scope() as session:
            src = session.query(Source).filter_by(id=source_id).first()
            if not src:
                return
            retire_source_definition(session, src)

    try:
        _with_db_retry(_delete_source)
        _invalidate_terminal_caches("sources:", "home:", "search:")
        return _sources_redirect_with_notice(notice=f"Source #{int(source_id)} removed from the active catalog.")
    except Exception as exc:
        logging.getLogger("thoth.web").exception("Failed to delete source: %s", exc)
        return _sources_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.post("/sources/cleanup/company-pages")
def sources_cleanup_company_pages() -> RedirectResponse:
    try:
        with session_scope() as session:
            removed_rows = cleanup_legacy_company_sources(session)
        _invalidate_terminal_caches("sources:", "home:", "search:")
        return _sources_redirect_with_notice(notice=f"Removed {len(removed_rows)} company/newsroom source definitions.")
    except Exception as exc:
        logging.getLogger("thoth.web").exception("Failed to bulk-clean company sources: %s", exc)
        return _sources_redirect_with_notice(error=f"{type(exc).__name__}: {exc}")


@app.get("/capture", response_class=HTMLResponse)
def capture(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/agents/activity#capture", status_code=303)


def _capture_priority_rank(value: str | None) -> tuple[str, int, bool]:
    key = str(value or "").strip().lower()
    if key in {"front", "urgent", "high"}:
        return "front", 50, True
    if key in {"next", "priority"}:
        return "next", 25, True
    return "backlog", 1, False


def _mark_document_for_agent_intake(doc: Document, *, priority_value: str | None) -> None:
    priority_key, priority_rank, pin_to_head = _capture_priority_rank(priority_value)
    now = utcnow()
    extra = dict(doc.extra or {})
    extra.update(
        {
            "capture_destination": "agent_intake",
            "agent_priority": priority_key,
            "agent_priority_rank": priority_rank,
            "agent_priority_set": bool(pin_to_head),
        }
    )
    doc.extra = extra
    doc.triage_state = "NEW"
    doc.triage_updated_at = now
    doc.actioned_at = None
    doc.importance = max(int(doc.importance or 0), priority_rank)
    if pin_to_head:
        doc.saved = True
        doc.saved_at = doc.saved_at or now


@app.post("/capture")
async def capture_post(
    title: str = Form(""),
    text: str = Form(""),
    url: str = Form(""),
    author: str = Form(""),
    rights_class: str = Form("public"),
    acquisition_mode: str = Form("manual_review"),
    language_hint: str = Form(""),
    agent_priority: str = Form(""),
    file: UploadFile | None = File(None),
) -> RedirectResponse:
    # Store as tweet when capturing X/Twitter URLs; otherwise keep as internal note.
    with session_scope() as session:
        doc = None
        if url and _is_x_url(url):
            payload: dict[str, object] = {"url": url}
            if title:
                payload["title"] = title
            if text:
                payload["text"] = text
            if not text:
                oembed = fetch_oembed_tweet(url)
                if oembed:
                    payload.update(oembed)
            doc = ingest_tweet(
                session,
                payload=payload,
                defer_heavy=True,
                enqueue_postprocess=enqueue_postprocess_job,
            )
            if doc is not None:
                _mark_document_for_agent_intake(doc, priority_value=agent_priority)
        if doc is None:
            clean_url = str(url or "").strip()
            rights_v = str(rights_class or "public").strip() or "public"
            acquisition_v = str(acquisition_mode or "manual_review").strip() or "manual_review"
            extra = {
                "capture_url": clean_url or None,
                "rights_class": rights_v,
                "acquisition_mode": acquisition_v,
                "entitlement_preserving_capture": rights_v != "public" or acquisition_v in {"manual_review", "authenticated_browser"},
            }
            doc = ingest_note(
                session,
                title=title or "(note)",
                text=text or clean_url or "",
                url=clean_url or None,
                author=author or None,
                language_hint=language_hint or None,
                extra=extra,
            )
            _mark_document_for_agent_intake(doc, priority_value=agent_priority)
        session.flush()

        if file:
            data = await file.read()
            ext = Path(file.filename or "").suffix or ""
            if not ext:
                ext = ".bin"
            object_path, sha = put_bytes(data, ext=ext)
            session.add(
                Attachment(
                    document_id=doc.id,
                    kind="upload",
                    original_url=None,
                    content_type=file.content_type,
                    object_path=object_path,
                    sha256=sha,
                    created_at=utcnow(),
                )
            )

    return RedirectResponse(url="/agents/activity#capture", status_code=303)


@app.post("/api/capture/tweets")
async def capture_tweets(request: Request) -> JSONResponse:
    payload: dict[str, object] = {}
    raw = await request.body()
    if raw:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {}
    if not payload:
        try:
            form = await request.form()
            payload = dict(form)
        except Exception:
            payload = {}

    items = payload.get("items") or payload.get("tweets") or []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except Exception:
            items = []
    if isinstance(items, dict):
        items = [items]
    if not items and payload:
        if isinstance(payload, dict) and (payload.get("url") or payload.get("text")):
            items = [payload]

    if not isinstance(items, list):
        return JSONResponse({"ok": False, "error": "Invalid payload"}, status_code=400)

    ids: list[int] = []
    failures: list[str] = []
    items_new = 0
    items_updated = 0

    async def _ingest_with_retry(
        item: dict[str, object], attempts: int = 4
    ) -> tuple[int | None, bool | None, str | None]:
        delay = 0.2
        key = item.get("url") or item.get("tweet_id") or item.get("id") or "unknown"
        for attempt in range(attempts):
            try:
                with session_scope() as session:
                    started_at = utcnow()
                    doc = ingest_tweet(
                        session,
                        payload=item,
                        defer_heavy=True,
                        enqueue_postprocess=enqueue_postprocess_job,
                    )
                    if doc is None:
                        return None, None, None
                    is_new = False
                    if doc.ingested_at:
                        try:
                            is_new = doc.ingested_at >= started_at
                        except Exception:
                            is_new = False
                    return int(doc.id), is_new, None
            except (DatabaseBusyError, OperationalError) as exc:
                if isinstance(exc, OperationalError) and not _is_db_locked(exc):
                    raise
                if attempt >= attempts - 1:
                    return None, None, f"{key}: db_locked"
                await asyncio.sleep(delay)
                delay = min(delay * 2, 2.0)
            except Exception as exc:
                return None, None, f"{key}: {type(exc).__name__}"
        return None, None, f"{key}: db_locked"

    for item in items:
        if not isinstance(item, dict):
            continue
        doc_id, is_new, err = await _ingest_with_retry(item)
        if doc_id is not None:
            ids.append(doc_id)
            if is_new:
                items_new += 1
            else:
                items_updated += 1
        if err:
            failures.append(err)

    if items:
        with session_scope() as session:
            src = session.query(Source).filter_by(name="x_capture").first()
            if src:
                now = utcnow()
                extra: dict[str, object] = {}
                if items_new > 0:
                    extra["notify"] = {"message": f"X capture: {items_new} new tweet(s)"}
                if failures:
                    extra["failures"] = failures
                status = "success"
                error = None
                if failures and not ids:
                    status = "error"
                    error = failures[0]
                run = IngestionRun(
                    source_id=src.id,
                    started_at=now,
                    finished_at=now,
                    status=status,
                    error=error,
                    items_seen=len(items),
                    items_new=items_new,
                    items_updated=items_updated,
                    extra=extra,
                )
                session.add(run)

    return JSONResponse({"ok": True, "ingested": len(ids), "ids": ids, "failures": failures})


@app.get("/api/capture/check")
def capture_check(url: str) -> JSONResponse:
    raw = (url or "").strip()
    if not raw:
        return JSONResponse({"ok": False, "detail": "url required"}, status_code=400)
    if not raw.startswith("http"):
        return JSONResponse({"ok": False, "detail": "invalid url"}, status_code=400)
    try:
        resp = requests.head(raw, allow_redirects=True, timeout=6)
        status = resp.status_code
        if status >= 400:
            resp = requests.get(raw, allow_redirects=True, timeout=8)
            status = resp.status_code
        ok = status < 400
        return JSONResponse(
            {
                "ok": ok,
                "status": status,
                "final_url": resp.url,
                "content_type": resp.headers.get("content-type"),
            }
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=200)


@app.post("/actions/export-note")
def action_export_note(note_id: int = Form(...), label: str | None = Form(None)) -> RedirectResponse:
    with session_scope() as session:
        out_dir = export_note_bundle(session, note_id=int(note_id), label=label)
    return RedirectResponse(url=f"/exports?path={out_dir.name}", status_code=303)


@app.post("/actions/export-thesis")
def action_export_thesis(thesis_id: int = Form(...), label: str | None = Form(None)) -> RedirectResponse:
    with session_scope() as session:
        out_dir = export_thesis_dossier(session, thesis_id=int(thesis_id), label=label)
    return RedirectResponse(url=f"/exports?path={out_dir.name}", status_code=303)
