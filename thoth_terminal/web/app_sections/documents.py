from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


@app.get("/feed", response_class=HTMLResponse)
def feed(
    request: Request,
    q: str | None = None,
    days: int = 10,
    triage: str | None = "NEW",
    saved: int | None = None,
    source_id: str | None = None,
    domain: str | None = None,
    language: str | None = None,
    page: int = 1,
    limit: int = 50,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    triage_norm = (triage or "").upper().strip()
    if triage_norm in {"", "ALL"}:
        triage_norm = ""
    saved_flag = None
    if saved is not None:
        try:
            saved_flag = bool(int(saved))
        except Exception:
            saved_flag = None

    page = max(1, int(page or 1))
    limit = max(10, min(int(limit or 50), 200))

    source_id_val = None
    if source_id is not None and str(source_id).strip() != "":
        try:
            source_id_val = int(source_id)
        except Exception:
            source_id_val = None

    domain = domain or None
    language = language or None
    offset = (page - 1) * limit

    docs = list_documents(
        db,
        query=q,
        days=days,
        triage_state=triage_norm or None,
        saved=saved_flag,
        source_id=source_id_val,
        domain=domain,
        language=language,
        include_attachments=True,
        defer_text=True,
        feed_projection=True,
        exclude_kinds=list(_FEED_EXCLUDED_DOCUMENT_KINDS),
        human_feed_visible_only=True,
        limit=limit + 1,
        offset=offset,
    )
    has_next = len(docs) > limit
    if has_next:
        docs = docs[:limit]
    if saved_flag:
        docs = [d for d in docs if d.triage_state not in {TRIAGE_IGNORED, TRIAGE_ARCHIVED}]
    _annotate_feed_document_excerpts(db, docs)
    for d in docs:
        media_entries = _build_doc_media_entries(d)
        setattr(d, "thumb", None)
        setattr(d, "thumb_url", str(media_entries[0]["src"]) if media_entries else None)
    sources = _feed_source_options(db)
    domains = db.query(Domain).options(load_only(Domain.slug, Domain.name)).order_by(Domain.name.asc()).all()
    languages = sorted({d.language for d in docs if d.language})
    counts = _triage_counts(db)
    has_prev = page > 1
    prev_url = str(request.url.include_query_params(page=page - 1, limit=limit)) if has_prev else None
    next_url = str(request.url.include_query_params(page=page + 1, limit=limit)) if has_next else None

    base_params = {
        "q": q or None,
        "days": days if days else None,
        "source_id": source_id_val,
        "domain": domain,
        "language": language,
    }
    query_keep = _query_string(base_params)
    query_keep_no_days = _query_string({k: v for k, v in base_params.items() if k != "days"})
    query_no_q = _query_string(
        {
            "triage": triage_norm or None,
            "saved": 1 if saved_flag else None,
            "days": days if days else None,
            "source_id": source_id,
            "domain": domain,
            "language": language,
        }
    )
    clear_search_href = f"/feed?{query_no_q}" if query_no_q else "/feed"

    doc_query = _query_string(
        {
            "q": q or None,
            "days": days if days else None,
            "triage": triage_norm or None,
            "saved": 1 if saved_flag else None,
            "source_id": source_id_val,
            "domain": domain,
            "language": language,
            "page": page,
            "limit": limit,
        }
    )

    earnings_rows = []
    show_earnings_widget = False
    earnings_rail_enabled = _cookie_enabled(request, _EARNINGS_FEED_COOKIE, default=False)
    if earnings_rail_enabled:
        now = utcnow()
        future_cutoff = now + timedelta(days=_EARNINGS_FEED_LOOKAHEAD_DAYS)
        earnings_filter = _earnings_title_filter()
        earnings_query = (
            db.query(CompanyDocument, Document, Company)
            .join(Document, Document.id == CompanyDocument.document_id)
            .join(Company, Company.id == CompanyDocument.company_id)
            .filter(CompanyDocument.doc_type.in_(["earnings_event", "calendar_event"]))
            .filter(Document.published_at.isnot(None))
            .filter(Document.published_at >= now - timedelta(days=_EARNINGS_FEED_GRACE_DAYS))
            .filter(Document.published_at <= future_cutoff)
        )
        if earnings_filter is not None:
            earnings_query = earnings_query.filter(earnings_filter)
        earnings_rows = (
            earnings_query.order_by(Document.published_at.asc().nullslast(), Document.ingested_at.desc())
            .limit(40)
            .all()
        )
        if earnings_rows:
            first_dt = earnings_rows[0][1].published_at
            if first_dt:
                season_end = first_dt + timedelta(days=_EARNINGS_FEED_SEASON_DAYS)
                earnings_rows = [
                    row
                    for row in earnings_rows
                    if row[1].published_at and row[1].published_at <= season_end
                ]
            last_dt = earnings_rows[-1][1].published_at if earnings_rows else first_dt
            show_earnings_widget = bool(first_dt and last_dt)
            if show_earnings_widget:
                show_earnings_widget = (
                    now >= first_dt - timedelta(days=_EARNINGS_FEED_LEAD_DAYS)
                    and now <= last_dt + timedelta(days=_EARNINGS_FEED_GRACE_DAYS)
                )
        if not show_earnings_widget:
            earnings_rows = []

    return _template_response(
        "feed.html",
        {
            "request": request,
            "docs": docs,
            "sources": sources,
            "domains": domains,
            "languages": languages,
            "filters": {
                "q": q or "",
                "days": days,
                "triage": triage_norm or "ALL",
                "saved": saved_flag,
                "source_id": source_id_val,
                "domain": domain,
                "language": language,
                "page": page,
                "limit": limit,
            },
            "counts": counts,
            "page": page,
            "limit": limit,
            "has_next": has_next,
            "has_prev": has_prev,
            "prev_url": prev_url,
            "next_url": next_url,
            "query_keep": query_keep,
            "query_keep_no_days": query_keep_no_days,
            "clear_search_href": clear_search_href,
            "doc_query": doc_query,
            "show_earnings_widget": show_earnings_widget,
            "earnings_rows": earnings_rows,
            "earnings_rail_enabled": earnings_rail_enabled,
        },
    )


def _first_human_visible_doc_id_from_candidates(
    db: Session,
    candidates: list[tuple[int, str | None]],
) -> int | None:
    if not candidates:
        return None
    candidate_ids = [int(doc_id) for doc_id, _title in candidates]
    visible_ids = {
        int(row[0])
        for row in db.query(Document.id)
        .filter(Document.id.in_(candidate_ids))
        .filter(human_feed_visible_filter(db))
        .all()
    }
    for doc_id, title in candidates:
        normalized_id = int(doc_id)
        if normalized_id in visible_ids:
            return normalized_id
    return None


def _is_navigation_prewarm(request: Request) -> bool:
    purpose_headers = " ".join(
        str(request.headers.get(name) or "")
        for name in ("purpose", "sec-purpose", "x-moz", "x-purpose", "x-thoth-purpose")
    ).lower()
    return "prefetch" in purpose_headers or "prewarm" in purpose_headers


def _document_has_pdf_capture(doc: Document) -> bool:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    if extra.get("is_pdf") or extra.get("content_kind") == "pdf":
        return True
    for attachment in doc.attachments or []:
        content_type = str(attachment.content_type or "").lower()
        object_path = str(attachment.object_path or "").lower()
        if attachment.kind == "pdf" or content_type.startswith("application/pdf") or object_path.endswith(".pdf"):
            return True
    return False


def _document_display_text(doc: Document, text: str) -> str:
    value = str(text or "")
    if value and value.lstrip().startswith("%PDF-"):
        return (
            "PDF document captured. This item was ingested before PDF extraction support was added, "
            "so the stored text is not readable yet. Re-ingest the source or open the attachment to "
            "view the original file."
        )
    if not value and _document_has_pdf_capture(doc):
        return (
            "PDF document captured. Text extraction is not available for this item yet, but the "
            "original file is attached below."
        )
    return value


def _clip_reader_text(text: str, *, full: bool, total_chars: int | None = None) -> dict[str, object]:
    value = str(text or "")
    total = len(value) if total_chars is None else max(0, int(total_chars))
    if full or total <= _DOC_READER_INITIAL_CHARS:
        return {"text": value, "total_chars": total, "clipped": False}
    clipped = value[:_DOC_READER_INITIAL_CHARS].rstrip()
    clipped += "\n\n[Initial reading view clipped for speed. Load full text to inspect the complete document.]"
    return {"text": clipped, "total_chars": total, "clipped": True}


def _document_reader_payload(doc: Document, *, full: bool = False) -> dict[str, object]:
    translated = bool(doc.translation_en and doc.normalized_text and doc.translation_en != doc.normalized_text)
    primary_raw = doc.translation_en or doc.normalized_text or doc.raw_text or ""
    primary = _clip_reader_text(_document_display_text(doc, primary_raw), full=full)
    original = None
    if translated:
        original = _clip_reader_text(_document_display_text(doc, doc.normalized_text or ""), full=full)
    return {
        "text": primary["text"],
        "total_chars": primary["total_chars"],
        "clipped": primary["clipped"],
        "translation_ready": bool(doc.translation_en),
        "has_translation": translated,
        "original_text": original["text"] if original else None,
        "original_total_chars": original["total_chars"] if original else 0,
        "original_clipped": original["clipped"] if original else False,
        "initial_chars": _DOC_READER_INITIAL_CHARS,
    }


def _document_reader_payload_from_db(db: Session, doc: Document) -> dict[str, object]:
    limit = _DOC_READER_INITIAL_CHARS + 1
    primary_expr = func.coalesce(func.nullif(Document.translation_en, ""), func.nullif(Document.normalized_text, ""), Document.raw_text, "")
    original_expr = func.coalesce(Document.normalized_text, "")
    row = (
        db.query(
            func.substr(primary_expr, 1, limit),
            func.length(primary_expr),
            and_(Document.translation_en.isnot(None), Document.translation_en != ""),
            and_(
                Document.translation_en.isnot(None),
                Document.translation_en != "",
                Document.normalized_text.isnot(None),
                Document.translation_en != Document.normalized_text,
            ),
            func.substr(original_expr, 1, limit),
            func.length(original_expr),
        )
        .filter(Document.id == int(doc.id))
        .first()
    )
    if not row:
        return _document_reader_payload(doc)

    primary_raw = str(row[0] or "")
    primary_display = _document_display_text(doc, primary_raw)
    primary_total = int(row[1] or len(primary_raw))
    if primary_display != primary_raw:
        primary_total = len(primary_display)
    primary = _clip_reader_text(primary_display, full=False, total_chars=primary_total)

    translation_ready = bool(row[2])
    translated = bool(row[3])
    original = None
    if translated:
        original_raw = str(row[4] or "")
        original_display = _document_display_text(doc, original_raw)
        original_total = int(row[5] or len(original_raw))
        if original_display != original_raw:
            original_total = len(original_display)
        original = _clip_reader_text(original_display, full=False, total_chars=original_total)

    return {
        "text": primary["text"],
        "total_chars": primary["total_chars"],
        "clipped": primary["clipped"],
        "translation_ready": translation_ready,
        "has_translation": translated,
        "original_text": original["text"] if original else None,
        "original_total_chars": original["total_chars"] if original else 0,
        "original_clipped": original["clipped"] if original else False,
        "initial_chars": _DOC_READER_INITIAL_CHARS,
    }


def _cached_sec_filing_review(db: Session, doc: Document) -> dict[str, Any] | None:
    key = f"docreview:{_session_cache_scope(db)}:{int(doc.id)}:{doc.content_hash or ''}"
    cached = _DOC_REVIEW_CACHE.get(key)
    if cached is not None:
        review = cached.get("review")
        return review if isinstance(review, dict) else None
    review = build_sec_filing_review(doc)
    _DOC_REVIEW_CACHE.set(key, {"review": review})
    return review


@app.get("/docs/{doc_id}", response_class=HTMLResponse)
def doc_detail(request: Request, doc_id: int, db: Session = Depends(get_session)) -> HTMLResponse:
    is_prewarm = _is_navigation_prewarm(request)
    doc = (
        db.query(Document)
        .options(
            joinedload(Document.source),
            joinedload(Document.attachments),
            defer(Document.raw_text),
            defer(Document.normalized_text),
            defer(Document.translation_en),
        )
        .filter(Document.id == doc_id)
        .first()
    )
    if not doc:
        return HTMLResponse("Not found", status_code=404)
    hydration = {"attempted": False, "reason": "navigation_prewarm" if is_prewarm else "not_attempted"}
    transcript_hydration = {"attempted": False, "reason": "navigation_prewarm" if is_prewarm else "not_attempted"}
    pdf_hydration = {"attempted": False, "reason": "navigation_prewarm" if is_prewarm else "not_attempted"}
    url_hydration = {"attempted": False, "reason": "prior_hydration_ok"}
    if not is_prewarm:
        hydration = maybe_hydrate_sec_filing_document(db, doc)
        transcript_hydration = maybe_hydrate_transcript_document(db, doc)
        pdf_hydration = maybe_hydrate_pdf_attachment_text(db, doc)
        if not any(
            row.get("status") == "ok"
            for row in (hydration, transcript_hydration, pdf_hydration)
            if isinstance(row, dict)
        ):
            url_hydration = maybe_hydrate_url_document_text(db, doc)
        if (
            hydration.get("attempted")
            or transcript_hydration.get("attempted")
            or pdf_hydration.get("attempted")
            or url_hydration.get("attempted")
        ):
            db.commit()
    filing_review = _cached_sec_filing_review(db, doc)
    mentions_in = (
        db.query(DocumentMention, Document)
        .join(Document, Document.id == DocumentMention.source_document_id)
        .filter(DocumentMention.target_document_id == doc.id)
        .order_by(DocumentMention.created_at.desc())
        .limit(50)
        .all()
    )
    mentions_out = (
        db.query(DocumentMention, Document)
        .join(Document, Document.id == DocumentMention.target_document_id)
        .filter(DocumentMention.source_document_id == doc.id)
        .order_by(DocumentMention.created_at.desc())
        .limit(50)
        .all()
    )
    thesis_evidence = (
        db.query(ThesisEvidence, Thesis)
        .join(Thesis, Thesis.id == ThesisEvidence.thesis_id)
        .filter(ThesisEvidence.document_id == doc.id)
        .order_by(ThesisEvidence.created_at.desc())
        .all()
    )
    note_links = (
        db.query(NoteDocument, Note)
        .join(Note, Note.id == NoteDocument.note_id)
        .filter(NoteDocument.document_id == doc.id)
        .order_by(Note.updated_at.desc())
        .limit(50)
        .all()
    )
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    media_entries = _build_doc_media_entries(doc)
    external_links = extract_external_links(extra)
    thread_sections = extract_thread_sections(extra)
    reader_payload = _document_reader_payload_from_db(db, doc)
    article_payload = extra.get("article") if isinstance(extra.get("article"), dict) else None
    if article_payload and article_payload.get("url"):
        article_payload = dict(article_payload)
    else:
        article_payload = None
    try:
        text_blob = str(reader_payload.get("text") or "")
        for u in _extract_urls_from_text(text_blob):
            if u not in external_links:
                external_links.append(u)
    except Exception:
        pass

    qp = request.query_params
    raw_q = qp.get("q") or None
    raw_days = qp.get("days")
    raw_triage = qp.get("triage")
    raw_saved = qp.get("saved")
    raw_source_id = qp.get("source_id")
    raw_domain = qp.get("domain")
    raw_language = qp.get("language")
    raw_page = qp.get("page")
    raw_limit = qp.get("limit")
    raw_nav_prev = qp.get("nav_prev")
    raw_nav_next = qp.get("nav_next")

    days = None
    if raw_days is not None:
        try:
            days = int(raw_days)
        except Exception:
            days = None

    triage_norm = (raw_triage or "").upper().strip()
    if triage_norm in {"", "ALL", "ANY"}:
        triage_norm = ""

    saved_flag = None
    if raw_saved is not None:
        try:
            saved_flag = bool(int(raw_saved))
        except Exception:
            saved_flag = None

    source_id = None
    if raw_source_id is not None:
        try:
            source_id = int(raw_source_id)
        except Exception:
            source_id = None

    page = None
    if raw_page is not None:
        try:
            page = int(raw_page)
        except Exception:
            page = None

    limit = None
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except Exception:
            limit = None

    def _nav_doc_id(raw: object) -> int | None:
        try:
            value = int(str(raw or "").strip())
        except Exception:
            return None
        if value <= 0 or value == int(doc.id):
            return None
        return value

    feed_prev_doc_id = _nav_doc_id(raw_nav_prev)
    feed_next_doc_id = _nav_doc_id(raw_nav_next)

    now = utcnow()
    effective_ts = case(
        (
            (Document.published_at.isnot(None)) & (Document.published_at > now),
            func.coalesce(Document.ingested_at, now),
        ),
        else_=func.coalesce(Document.published_at, Document.ingested_at),
    )

    def _apply_doc_filters(q, *, for_nav: bool = False, human_visible: bool = True):
        if for_nav:
            q = q.filter(~Document.kind.in_(_FEED_EXCLUDED_DOCUMENT_KINDS))
            if human_visible:
                q = q.filter(human_feed_visible_filter(db))
        if days is not None:
            since = utcnow() - timedelta(days=days)
            q = q.filter(effective_ts >= since)
        if triage_norm:
            if for_nav and triage_norm == TRIAGE_NEW:
                q = q.filter(Document.triage_state.in_([TRIAGE_NEW, TRIAGE_OPENED]))
            else:
                q = q.filter(Document.triage_state == triage_norm)
        if saved_flag is not None:
            q = q.filter(Document.saved.is_(bool(saved_flag)))
        if saved_flag:
            q = q.filter(Document.triage_state != TRIAGE_IGNORED)
            q = q.filter(Document.triage_state != TRIAGE_ARCHIVED)
        if source_id is not None:
            q = q.filter(Document.source_id == source_id)
        if raw_domain:
            q = q.join(Document.domains).filter(Domain.slug == raw_domain)
        if raw_language:
            q = q.filter(Document.language == raw_language)
        if raw_q:
            ids = search_document_ids(db, raw_q, limit=5000)
            if ids:
                q = q.filter(Document.id.in_(ids))
            else:
                like = f"%{raw_q.strip()}%"
                q = q.filter(
                    (Document.title.ilike(like))
                    | (Document.normalized_text.ilike(like))
                    | (Document.translation_en.ilike(like))
                )
        return q

    if doc.published_at and doc.published_at > now:
        sort_ts = doc.ingested_at or doc.published_at
    else:
        sort_ts = doc.published_at or doc.ingested_at
    sort_expr = effective_ts

    def _fast_neighbor_id(*, newer: bool) -> int | None:
        candidate_limit = 120
        q = _apply_doc_filters(db.query(Document.id, Document.title), for_nav=True, human_visible=False)
        if newer:
            q = q.filter(or_(sort_expr > sort_ts, and_(sort_expr == sort_ts, Document.id > doc.id)))
            q = q.order_by(sort_expr.asc(), Document.id.asc())
        else:
            q = q.filter(or_(sort_expr < sort_ts, and_(sort_expr == sort_ts, Document.id < doc.id)))
            q = q.order_by(sort_expr.desc(), Document.id.desc())
        rows = [(int(row[0]), row[1]) for row in q.limit(candidate_limit).all()]
        visible_id = _first_human_visible_doc_id_from_candidates(db, rows)
        if visible_id is not None:
            return visible_id
        if len(rows) < candidate_limit:
            return None
        # Fallback preserves correctness if a dense run of generic filings exceeds the bounded window.
        slow_q = _apply_doc_filters(db.query(Document.id), for_nav=True)
        if newer:
            slow_q = slow_q.filter(or_(sort_expr > sort_ts, and_(sort_expr == sort_ts, Document.id > doc.id)))
            slow_q = slow_q.order_by(sort_expr.asc(), Document.id.asc())
        else:
            slow_q = slow_q.filter(or_(sort_expr < sort_ts, and_(sort_expr == sort_ts, Document.id < doc.id)))
            slow_q = slow_q.order_by(sort_expr.desc(), Document.id.desc())
        row = slow_q.first()
        return int(row[0]) if row else None

    # Feed is sorted newest → oldest; align buttons with that mental model:
    # - "Prev" should go to the previous item in that order (newer / above).
    # - "Next" should go to the next item (older / below).
    nav_cache_key = (
        f"docnav:{_session_cache_scope(db)}:{int(doc.id)}:"
        f"{sort_ts.isoformat() if sort_ts else ''}:{raw_q or ''}:{days if days is not None else ''}:"
        f"{triage_norm}:{saved_flag if saved_flag is not None else ''}:{source_id if source_id is not None else ''}:"
        f"{raw_domain or ''}:{raw_language or ''}"
    )
    cached_nav = _DOC_NAV_CACHE.get(nav_cache_key)
    if feed_prev_doc_id is not None or feed_next_doc_id is not None:
        cached_nav = {"prev": feed_prev_doc_id, "next": feed_next_doc_id}
    elif cached_nav is None:
        cached_nav = {"prev": _fast_neighbor_id(newer=True), "next": _fast_neighbor_id(newer=False)}
        _DOC_NAV_CACHE.set(nav_cache_key, cached_nav)
    prev_doc_id = cached_nav.get("prev")
    next_doc_id = cached_nav.get("next")

    doc_query = _query_string(
        {
            "q": raw_q or None,
            "days": days if days is not None else None,
            "triage": triage_norm or None,
            "saved": 1 if saved_flag else None,
            "source_id": source_id,
            "domain": raw_domain,
            "language": raw_language,
            "page": page,
            "limit": limit,
        }
    )
    nav_suffix = f"?{doc_query}" if doc_query else ""
    back_href = f"/feed{nav_suffix}"

    return _template_response(
        "doc.html",
        {
            "request": request,
            "doc": doc,
            "mentions_in": mentions_in,
            "mentions_out": mentions_out,
            "external_links": external_links,
            "media_entries": media_entries,
            "thread_sections": thread_sections,
            "article_payload": article_payload,
            "filing_review": filing_review,
            "reader": reader_payload,
            "thesis_evidence": thesis_evidence,
            "note_links": note_links,
            "prev_doc_id": prev_doc_id,
            "next_doc_id": next_doc_id,
            "back_href": back_href,
            "doc_nav_suffix": nav_suffix,
        },
    )


@app.get("/api/docs/{doc_id}/text")
def api_doc_text(doc_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    doc = (
        db.query(Document)
        .options(joinedload(Document.attachments))
        .filter(Document.id == int(doc_id))
        .first()
    )
    if not doc:
        return JSONResponse({"ok": False, "error": "Document not found"}, status_code=404)
    return JSONResponse({"ok": True, "reader": _document_reader_payload(doc, full=True)})


@app.get("/api/feed/documents/{doc_id}")
def api_feed_document(doc_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    """Read a selected signal without hydration, triage writes, or whole-feed loading."""
    _guard_internal_terminal()
    from thoth_terminal.models import LocalAgentDeepAnalysisTrace, LocalAgentDocumentCompression

    doc = (
        db.query(Document)
        .options(
            joinedload(Document.source), selectinload(Document.attachments),
            selectinload(Document.domains),
            selectinload(Document.entity_links).joinedload(DocumentEntity.entity),
            defer(Document.raw_text), defer(Document.normalized_text), defer(Document.translation_en),
        )
        .filter(Document.id == doc_id)
        .first()
    )
    if doc is None:
        return JSONResponse({"ok": False, "error": "Document not found"}, status_code=404)
    notes = (
        db.query(Note.id, Note.title, NoteDocument.snippet)
        .join(NoteDocument, NoteDocument.note_id == Note.id)
        .filter(NoteDocument.document_id == doc_id)
        .order_by(Note.updated_at.desc()).limit(30).all()
    )
    evidence = (
        db.query(Thesis.id, Thesis.title, ThesisEvidence.polarity, ThesisEvidence.snippet, ThesisEvidence.note)
        .join(ThesisEvidence, ThesisEvidence.thesis_id == Thesis.id)
        .filter(ThesisEvidence.document_id == doc_id)
        .order_by(ThesisEvidence.created_at.desc()).limit(30).all()
    )
    compression = (
        db.query(
            LocalAgentDocumentCompression.model, LocalAgentDocumentCompression.status,
            func.substr(LocalAgentDocumentCompression.summary_short, 1, 6000).label("summary"),
            func.substr(LocalAgentDocumentCompression.summary_detail, 1, 12000).label("detail"),
            LocalAgentDocumentCompression.confidence, LocalAgentDocumentCompression.updated_at,
        ).filter(LocalAgentDocumentCompression.document_id == doc_id)
        .order_by(LocalAgentDocumentCompression.updated_at.desc()).first()
    )
    deep = (
        db.query(
            LocalAgentDeepAnalysisTrace.model, LocalAgentDeepAnalysisTrace.status,
            func.substr(LocalAgentDeepAnalysisTrace.summary_md, 1, 16000).label("summary"),
            LocalAgentDeepAnalysisTrace.updated_at,
        ).filter(LocalAgentDeepAnalysisTrace.document_id == doc_id)
        .order_by(LocalAgentDeepAnalysisTrace.updated_at.desc()).first()
    )
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    return JSONResponse({
        "ok": True, "doc": _doc_payload(doc),
        "reader": _document_reader_payload_from_db(db, doc),
        "entities": [link.entity.canonical_name for link in doc.entity_links if link.entity],
        "domains": [domain.slug for domain in doc.domains],
        "media": _build_doc_media_entries(doc)[:20],
        "attachments": [{"label": att.kind or "Attachment", "href": f"/objects/{att.object_path}"} for att in doc.attachments[:30]],
        "external_links": extract_external_links(extra)[:30],
        "notes": [{"id": row.id, "title": row.title, "snippet": (row.snippet or "")[:2000], "href": f"/notes/{row.id}"} for row in notes],
        "evidence": [{"id": row.id, "title": row.title, "polarity": row.polarity, "snippet": (row.snippet or "")[:2000], "note": (row.note or "")[:2000], "href": f"/theses/{row.id}"} for row in evidence],
        "analysis": [{"kind": kind, "model": row.model, "status": row.status, "text": str(row.summary or ""), "detail": str(getattr(row, "detail", "") or ""), "updated_at": row.updated_at.isoformat()} for kind, row in (("Compression", compression), ("Deep analysis", deep)) if row is not None],
    })


@app.post("/api/feed/documents/{doc_id}/research")
def api_feed_document_research(doc_id: int, payload: dict = Body(default_factory=dict), db: Session = Depends(get_session)) -> JSONResponse:
    """Create canonical research and its source link in one transaction."""
    import hashlib

    _guard_internal_terminal()
    kind = str(payload.get("kind") or "").strip()
    title = str(payload.get("title") or "").strip()
    body = str(payload.get("body") or "").strip()
    request_id = str(payload.get("request_id") or "").strip()
    if kind not in {"note", "thesis"} or not title or not body or not request_id or len(request_id) > 100:
        return JSONResponse({"ok": False, "error": "Kind, title, body, and request ID are required."}, status_code=400)
    if len(title) > 300 or len(body) > 100000:
        return JSONResponse({"ok": False, "error": "Title or research text is too long."}, status_code=400)
    request_id = f"feed:{kind}:{doc_id}:{hashlib.sha256(request_id.encode()).hexdigest()}"
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if doc is None:
        return JSONResponse({"ok": False, "error": "Document not found"}, status_code=404)
    entities = _split_csv(str(payload.get("entities") or ""))
    domains = [item.slug for item in doc.domains]
    try:
        if kind == "thesis":
            if not entities:
                return JSONResponse({"ok": False, "error": "Add at least one entity to seed a thesis."}, status_code=400)
            existing = db.query(Thesis).filter_by(client_request_id=request_id).first()
            if existing is None and not payload.get("force_create"):
                similar = _find_similar_theses(db, title=title, statement=body, entity_tokens=entities, limit=6)
                if similar:
                    return JSONResponse({"ok": False, "error": "Similar theses exist. Review them before creating another.", "similar": [{"id": hit["id"], "title": hit["title"], "href": f"/theses/{hit['id']}"} for hit in similar]}, status_code=409)
            row = create_thesis(db, title=title, statement=body, entity_tokens=entities, domain_slugs=domains, client_request_id=request_id)
            add_thesis_evidence(db, thesis=row, document_id=doc_id, polarity="neutral", note="Seeded from Feed; source relevance requires review.")
            href = f"/theses/{row.id}"
        else:
            row = create_note(db, title=title, body_md=f"[[doc:{doc_id}]]\n\n{body}", entity_tokens=entities, domain_slugs=domains, client_request_id=request_id)
            link_note_document(db, note_id=row.id, document_id=doc_id)
            href = f"/notes/{row.id}"
        db.commit()
    except ValueError as exc:
        db.rollback()
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    _invalidate_terminal_caches("feed:", "home:", "search:")
    return JSONResponse({"ok": True, "id": row.id, "href": href, "kind": kind, "doc": _doc_payload(doc)})


@app.post("/docs/{doc_id}/attachments", response_model=None)
async def doc_attachment_upload(
    doc_id: int,
    file: UploadFile | None = File(None),
) -> Response:
    if not file:
        return HTMLResponse("File required", status_code=400)
    with session_scope() as session:
        doc = session.query(Document).filter_by(id=doc_id).first()
        if not doc:
            return HTMLResponse("Not found", status_code=404)
        data = await file.read()
        ext = Path(file.filename or "").suffix or ""
        if not ext:
            ext = guess_extension(content_type=file.content_type, url=None) or ".bin"
        object_path, sha = put_bytes(data, ext=ext)
        kind = "image" if (file.content_type or "").startswith("image/") else "upload"
        session.add(
            Attachment(
                document_id=doc.id,
                kind=kind,
                original_url=None,
                content_type=file.content_type,
                object_path=object_path,
                sha256=sha,
                created_at=utcnow(),
            )
        )
    return RedirectResponse(url=f"/docs/{doc_id}", status_code=303)


@app.post("/documents/{doc_id}/open")
def doc_open(doc_id: int) -> JSONResponse:
    def _apply():
        with session_scope() as session:
            return mark_opened(session, document_id=int(doc_id), meta={"source": "api:open"})

    doc = _with_db_retry(_apply)
    _invalidate_terminal_caches("feed:")
    return JSONResponse({"ok": True, "doc": _doc_payload(doc)})


@app.post("/documents/{doc_id}/save")
def doc_save(doc_id: int) -> JSONResponse:
    def _apply():
        with session_scope() as session:
            return mark_saved(session, document_id=int(doc_id), meta={"source": "api:save"})

    doc = _with_db_retry(_apply)
    _invalidate_terminal_caches("feed:", "home:", "search:")
    return JSONResponse({"ok": True, "doc": _doc_payload(doc)})


@app.post("/documents/{doc_id}/unsave")
def doc_unsave(doc_id: int) -> JSONResponse:
    def _apply():
        with session_scope() as session:
            return mark_unsaved(session, document_id=int(doc_id), meta={"source": "api:unsave"})

    doc = _with_db_retry(_apply)
    _invalidate_terminal_caches("feed:", "home:", "search:")
    return JSONResponse({"ok": True, "doc": _doc_payload(doc)})


@app.post("/documents/{doc_id}/ignore")
def doc_ignore(doc_id: int) -> JSONResponse:
    def _apply():
        with session_scope() as session:
            return mark_ignored(session, document_id=int(doc_id), meta={"source": "api:ignore"})

    doc = _with_db_retry(_apply)
    _invalidate_terminal_caches("feed:", "home:", "search:")
    return JSONResponse({"ok": True, "doc": _doc_payload(doc)})


@app.post("/documents/{doc_id}/archive")
def doc_archive(doc_id: int) -> JSONResponse:
    def _apply():
        with session_scope() as session:
            return mark_archived(session, document_id=int(doc_id), meta={"source": "api:archive"})

    doc = _with_db_retry(_apply)
    _invalidate_terminal_caches("feed:", "home:", "search:")
    return JSONResponse({"ok": True, "doc": _doc_payload(doc)})


@app.post("/documents/{doc_id}/actioned")
def doc_actioned(doc_id: int) -> JSONResponse:
    def _apply():
        with session_scope() as session:
            return mark_actioned(session, document_id=int(doc_id), meta={"source": "api:actioned"})

    doc = _with_db_retry(_apply)
    _invalidate_terminal_caches("feed:", "home:", "search:")
    return JSONResponse({"ok": True, "doc": _doc_payload(doc)})


@app.post("/documents/{doc_id}/reset_to_new")
def doc_reset(doc_id: int) -> JSONResponse:
    def _apply():
        with session_scope() as session:
            return reset_to_new(session, document_id=int(doc_id), meta={"source": "api:reset"})

    doc = _with_db_retry(_apply)
    _invalidate_terminal_caches("feed:", "home:", "search:")
    return JSONResponse({"ok": True, "doc": _doc_payload(doc)})


@app.post("/documents/{doc_id}/seen")
def doc_seen(doc_id: int) -> JSONResponse:
    if not settings.enable_seen_tracking:
        return JSONResponse({"ok": True, "doc": {"id": int(doc_id)}})
    doc = _seen_with_retry(int(doc_id))
    if doc is None:
        return JSONResponse({"ok": False, "retry": True, "doc": {"id": int(doc_id)}}, status_code=202)
    return JSONResponse({"ok": True, "doc": _doc_payload(doc)})


@app.post("/documents/bulk")
async def documents_bulk(request: Request) -> JSONResponse:
    payload: dict[str, object] = {}
    ct = request.headers.get("content-type", "")
    if ct.startswith("application/json"):
        payload = await request.json()
    else:
        form = await request.form()
        payload = dict(form)

    raw_ids = payload.get("doc_ids") or payload.get("ids") or []
    if isinstance(raw_ids, str):
        doc_ids = [int(i) for i in raw_ids.split(",") if i.strip().isdigit()]
    else:
        doc_ids = [int(i) for i in raw_ids if str(i).isdigit()]

    action = str(payload.get("action") or "").strip()
    if not action:
        return JSONResponse({"ok": False, "error": "Missing action"}, status_code=400)

    valid_actions = {
        "save",
        "unsave",
        "ignore",
        "archive",
        "actioned",
        "reset_to_new",
        "open",
        "seen",
    }
    if action not in valid_actions:
        return JSONResponse({"ok": False, "error": f"Unknown action: {action}"}, status_code=400)

    if action == "seen":
        request.state.skip_terminal_cache_invalidation = True
        if not settings.enable_seen_tracking:
            return JSONResponse(
                {"ok": True, "counts": {}, "processed": len(doc_ids), "failures": []}
            )
        ok, counts, failures = _bulk_seen_with_retry(doc_ids)
        status = 200 if ok else 202
        return JSONResponse({"ok": ok, "counts": counts, "processed": len(doc_ids), "failures": failures}, status_code=status)

    ok, counts, failures = _bulk_apply_with_retry(doc_ids, action)
    _invalidate_terminal_caches("feed:", "home:", "search:")
    status = 200 if ok else 202
    return JSONResponse(
        {"ok": ok, "counts": counts, "processed": len(doc_ids), "failures": failures},
        status_code=status,
    )


@app.get("/documents/{doc_id}/events")
def document_events(doc_id: int, db: Session = Depends(get_session)) -> JSONResponse:
    rows = (
        db.query(DocumentEvent)
        .filter(DocumentEvent.document_id == int(doc_id))
        .order_by(DocumentEvent.event_at.desc())
        .limit(200)
        .all()
    )
    items = [
        {
            "id": r.id,
            "event_type": r.event_type,
            "event_at": r.event_at.isoformat(),
            "meta": r.meta,
        }
        for r in rows
    ]
    return JSONResponse({"items": items})


@app.post("/actions/translate")
def action_translate(
    request: Request,
    ids: str = Form(...),
    backend: str = Form("googletrans"),
    return_to: str | None = Form(None),
) -> RedirectResponse:
    doc_ids = [int(i) for i in ids.split(",") if i.strip().isdigit()]
    with session_scope() as session:
        for doc_id in doc_ids:
            try:
                translate_document(session, document_id=doc_id, backend=backend)
            except Exception:
                continue
    redirect_to = str(return_to or "/").strip() or "/"
    if not redirect_to.startswith("/") or redirect_to.startswith("//"):
        redirect_to = "/"
    return RedirectResponse(url=redirect_to, status_code=303)


@app.post("/actions/export")
def action_export(
    request: Request,
    ids: str = Form(...),
    label: str | None = Form(None),
) -> RedirectResponse:
    doc_ids = [int(i) for i in ids.split(",") if i.strip().isdigit()]
    with session_scope() as session:
        out_dir = export_bundle(session, document_ids=doc_ids, label=label)
    return RedirectResponse(url=f"/exports?path={out_dir.name}", status_code=303)
