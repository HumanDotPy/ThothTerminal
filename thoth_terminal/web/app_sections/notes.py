from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


@app.get("/notes", response_class=HTMLResponse)
def notes(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    note_type: str | None = None,
    pinned: int | None = None,
    domain: str | None = None,
    tag: str | None = None,
    entity: str | None = None,
    days: int | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    items = list_notes(
        db,
        query=q,
        status=status or None,
        note_type=note_type or None,
        pinned=pinned,
        domain=domain or None,
        tag=tag or None,
        entity=entity or None,
        days=days,
        limit=200,
    )
    filter_options = _cached_note_filter_options(db)
    return _template_response(
        "notes.html",
        {
            "request": request,
            "items": items,
            "filters": {
                "q": q or "",
                "status": status,
                "note_type": note_type,
                "pinned": pinned,
                "domain": domain,
                "tag": tag,
                "entity": entity,
                "days": days,
            },
            "note_statuses": NOTE_STATUSES,
            "note_types": NOTE_TYPES,
            "domains": filter_options["domains"],
            "tags": filter_options["tags"],
        },
    )


@app.get("/notes/new", response_class=HTMLResponse)
def note_new(
    request: Request,
    from_doc: int | None = None,
    from_thesis: int | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    prefill: dict[str, object] = {
        "title": "New note",
        "body_md": "",
        "note_type": "general",
        "importance": 3,
        "entities": "",
        "domains": "",
        "tags": "",
        "event_types": "",
    }

    if from_doc:
        doc = db.query(Document).filter_by(id=int(from_doc)).first()
        prefill["title"] = (doc.title if doc else None) or f"Note on doc {from_doc}"
        prefill["body_md"] = f"[[doc:{from_doc}]]\n\n"
    elif from_thesis:
        th = db.query(Thesis).filter_by(id=int(from_thesis)).first()
        prefill["title"] = (th.title if th else None) or f"Note on thesis {from_thesis}"
        prefill["body_md"] = f"[[thesis:{from_thesis}]]\n\n"

    return _template_response(
        "note_new.html",
        {
            "request": request,
            "note_types": NOTE_TYPES,
            "prefill": prefill,
            "link": {"from_doc": from_doc, "from_thesis": from_thesis},
        },
    )


@app.post("/notes/new")
def note_create(
    title: str = Form(...),
    body_md: str = Form(""),
    note_type: str = Form("general"),
    status: str = Form("active"),
    importance: int = Form(3),
    pinned: str = Form("false"),
    entities: str | None = Form(None),
    domains: str | None = Form(None),
    tags: str | None = Form(None),
    event_types: str | None = Form(None),
    from_doc: int | None = Form(None),
    from_thesis: int | None = Form(None),
) -> RedirectResponse:
    pinned_bool = str(pinned).strip().lower() in ("1", "true", "yes", "y", "on")
    with session_scope() as session:
        note = create_note(
            session,
            title=title,
            body_md=body_md or "",
            note_type=note_type,
            status=status,
            importance=int(importance),
            pinned=pinned_bool,
            entity_tokens=_split_csv(entities),
            domain_slugs=_split_csv(domains)[:2],
            tag_slugs=_split_csv(tags),
            event_type_slugs=_split_csv(event_types),
        )
        session.flush()

        if from_doc:
            try:
                link_note_document(session, note_id=note.id, document_id=int(from_doc))
            except Exception:
                pass
        if from_thesis:
            try:
                link_note_thesis(session, note_id=note.id, thesis_id=int(from_thesis), relation="context")
            except Exception:
                pass

    return RedirectResponse(url=f"/notes/{note.id}", status_code=303)


def _load_note(db: Session, note_id: int) -> Note | None:
    return (
        db.query(Note)
        .options(
            selectinload(Note.entities),
            selectinload(Note.domains),
            selectinload(Note.tags),
            selectinload(Note.event_types),
            selectinload(Note.thesis_links).joinedload(NoteThesis.thesis),
            selectinload(Note.document_links).joinedload(NoteDocument.document).joinedload(Document.source),
            selectinload(Note.proposition_links)
            .joinedload(NoteProposition.proposition)
            .selectinload(Proposition.observations),
        )
        .filter(Note.id == int(note_id))
        .first()
    )


@app.get("/notes/{note_id}", response_class=HTMLResponse)
def note_detail(request: Request, note_id: int, db: Session = Depends(get_session)) -> HTMLResponse:
    ensure_dataset_tables(db)
    _ensure_supply_chain_tables(db)
    note = _load_note(db, note_id)
    if not note:
        return HTMLResponse("Not found", status_code=404)
    preview_html = render_markdown_to_html(db, note.body_md or "")
    extra = note.extra if isinstance(note.extra, dict) else {}
    note_attachments = extra.get("attachments") or []
    linked_dataset_rows = (
        db.query(DatasetLink, Dataset)
        .join(Dataset, DatasetLink.dataset_id == Dataset.id)
        .filter(DatasetLink.object_type == "note", DatasetLink.object_id == int(note.id))
        .order_by(Dataset.updated_at.desc(), DatasetLink.id.desc())
        .all()
    )
    linked_supply_chain_rows = (
        db.query(SupplyChainLink, SupplyChain)
        .join(SupplyChain, SupplyChainLink.chain_id == SupplyChain.id)
        .filter(SupplyChainLink.object_type == "note", SupplyChainLink.object_id == int(note.id))
        .order_by(SupplyChain.updated_at.desc(), SupplyChainLink.id.desc())
        .all()
    )
    linked_stage_rows = [serialize_product_stage_entry(row) for row in list_product_stage_entries_linked_to_note(db, int(note.id))]
    linked_charts = db.query(Chart).filter_by(note_id=int(note.id)).order_by(Chart.updated_at.desc()).all()
    return _template_response(
        "note_detail.html",
        {
            "request": request,
            "note": note,
            "preview_html": preview_html,
            "note_attachments": note_attachments,
            "note_types": NOTE_TYPES,
            "note_statuses": NOTE_STATUSES,
            "note_link_relations": NOTE_LINK_RELATIONS,
            "note_prop_relations": NOTE_PROP_RELATIONS,
            "linked_dataset_rows": linked_dataset_rows,
            "linked_supply_chain_rows": linked_supply_chain_rows,
            "linked_stage_rows": linked_stage_rows,
            "linked_charts": linked_charts,
        },
    )


@app.post("/notes/{note_id}/edit")
def note_edit(
    note_id: int,
    note_type: str = Form("general"),
    status: str = Form("active"),
    importance: int = Form(3),
    entities: str | None = Form(None),
    domains: str | None = Form(None),
    tags: str | None = Form(None),
    event_types: str | None = Form(None),
) -> RedirectResponse:
    with session_scope() as session:
        update_note(
            session,
            note_id=int(note_id),
            note_type=note_type,
            status=status,
            importance=int(importance),
            entities_csv=entities,
            domains_csv=domains,
            tags_csv=tags,
            event_types_csv=event_types,
        )
    return RedirectResponse(url=f"/notes/{note_id}", status_code=303)


@app.post("/notes/{note_id}/pin")
def note_pin(note_id: int) -> RedirectResponse:
    with session_scope() as session:
        update_note(session, note_id=int(note_id), pinned=True)
    return RedirectResponse(url=f"/notes/{note_id}", status_code=303)


@app.post("/notes/{note_id}/unpin")
def note_unpin(note_id: int) -> RedirectResponse:
    with session_scope() as session:
        update_note(session, note_id=int(note_id), pinned=False)
    return RedirectResponse(url=f"/notes/{note_id}", status_code=303)


@app.post("/notes/{note_id}/archive")
def note_archive(note_id: int) -> RedirectResponse:
    with session_scope() as session:
        update_note(session, note_id=int(note_id), status="archived")
    return RedirectResponse(url=f"/notes/{note_id}", status_code=303)


@app.post("/notes/{note_id}/unarchive")
def note_unarchive(note_id: int) -> RedirectResponse:
    with session_scope() as session:
        update_note(session, note_id=int(note_id), status="active")
    return RedirectResponse(url=f"/notes/{note_id}", status_code=303)


@app.post("/notes/{note_id}/link/thesis")
def note_link_thesis(note_id: int, thesis_id: int = Form(...), relation: str = Form("context")) -> RedirectResponse:
    with session_scope() as session:
        link_note_thesis(session, note_id=int(note_id), thesis_id=int(thesis_id), relation=relation)
    return RedirectResponse(url=f"/notes/{note_id}", status_code=303)


@app.post("/notes/{note_id}/unlink/thesis")
def note_unlink_thesis(note_id: int, thesis_id: int = Form(...)) -> RedirectResponse:
    with session_scope() as session:
        unlink_note_thesis(session, note_id=int(note_id), thesis_id=int(thesis_id))
    return RedirectResponse(url=f"/notes/{note_id}", status_code=303)


@app.post("/notes/{note_id}/link/dataset")
def note_link_dataset(
    note_id: int,
    dataset_id: int = Form(...),
    relation: str = Form("context"),
) -> RedirectResponse:
    with session_scope() as session:
        ensure_dataset_tables(session)
        link_dataset(
            session,
            dataset_id=int(dataset_id),
            object_type="note",
            object_id=int(note_id),
            relation=str(relation or "context"),
        )
    return RedirectResponse(url=f"/notes/{note_id}", status_code=303)


@app.post("/notes/{note_id}/unlink/dataset")
def note_unlink_dataset(note_id: int, dataset_id: int = Form(...)) -> RedirectResponse:
    with session_scope() as session:
        ensure_dataset_tables(session)
        db_row = (
            session.query(DatasetLink)
            .filter_by(dataset_id=int(dataset_id), object_type="note", object_id=int(note_id))
            .first()
        )
        if db_row:
            session.delete(db_row)
    return RedirectResponse(url=f"/notes/{note_id}", status_code=303)


@app.post("/notes/{note_id}/link/supply-chain")
def note_link_supply_chain(
    note_id: int,
    chain_id: int = Form(...),
    relation: str = Form("context"),
) -> RedirectResponse:
    with session_scope() as session:
        _ensure_supply_chain_tables(session)
        link_supply_chain_object(
            session,
            chain_id=int(chain_id),
            object_type="note",
            object_id=int(note_id),
            relation=str(relation or "context"),
        )
    return RedirectResponse(url=f"/notes/{note_id}", status_code=303)


@app.post("/notes/{note_id}/unlink/supply-chain")
def note_unlink_supply_chain(note_id: int, chain_id: int = Form(...)) -> RedirectResponse:
    with session_scope() as session:
        _ensure_supply_chain_tables(session)
        db_row = (
            session.query(SupplyChainLink)
            .filter_by(chain_id=int(chain_id), object_type="note", object_id=int(note_id))
            .first()
        )
        if db_row:
            session.delete(db_row)
    return RedirectResponse(url=f"/notes/{note_id}", status_code=303)


@app.post("/notes/{note_id}/link/stage-tracker")
def note_link_stage_tracker(note_id: int, entry_id: int = Form(...)) -> RedirectResponse:
    with session_scope() as session:
        link_product_stage_entry_object(
            session,
            entry_id=int(entry_id),
            object_type="note",
            object_id=int(note_id),
        )
    return RedirectResponse(url=f"/notes/{note_id}", status_code=303)


@app.post("/notes/{note_id}/unlink/stage-tracker")
def note_unlink_stage_tracker(note_id: int, entry_id: int = Form(...)) -> RedirectResponse:
    with session_scope() as session:
        unlink_product_stage_entry_object(
            session,
            entry_id=int(entry_id),
            object_type="note",
            object_id=int(note_id),
        )
    return RedirectResponse(url=f"/notes/{note_id}", status_code=303)


@app.post("/notes/{note_id}/link/document")
def note_link_document(
    note_id: int, document_id: int = Form(...), snippet: str | None = Form(None)
) -> RedirectResponse:
    with session_scope() as session:
        link_note_document(session, note_id=int(note_id), document_id=int(document_id), snippet=snippet)
    return RedirectResponse(url=f"/notes/{note_id}", status_code=303)


@app.post("/notes/{note_id}/unlink/document")
def note_unlink_document(note_id: int, document_id: int = Form(...)) -> RedirectResponse:
    with session_scope() as session:
        unlink_note_document(session, note_id=int(note_id), document_id=int(document_id))
    return RedirectResponse(url=f"/notes/{note_id}", status_code=303)


@app.post("/notes/{note_id}/link/proposition")
def note_link_prop(
    note_id: int,
    proposition_key: str = Form(...),
    relation: str = Form("references"),
) -> RedirectResponse:
    with session_scope() as session:
        prop = upsert_proposition(session, key=proposition_key)
        link_note_proposition(session, note_id=int(note_id), proposition_id=prop.id, relation=relation)
    return RedirectResponse(url=f"/notes/{note_id}", status_code=303)


@app.post("/notes/{note_id}/unlink/proposition")
def note_unlink_prop(note_id: int, proposition_id: int = Form(...)) -> RedirectResponse:
    with session_scope() as session:
        unlink_note_proposition(session, note_id=int(note_id), proposition_id=int(proposition_id))
    return RedirectResponse(url=f"/notes/{note_id}", status_code=303)


@app.post("/api/notes/{note_id}/autosave")
def note_autosave(note_id: int, title: str = Form(""), body_md: str = Form("")) -> JSONResponse:
    with session_scope() as session:
        note = update_note(session, note_id=int(note_id), title=title, body_md=body_md)
        preview_html = render_markdown_to_html(session, note.body_md or "")
    return JSONResponse({"ok": True, "preview_html": preview_html})


@app.post("/api/notes/{note_id}/attachments")
async def note_attachment_upload(
    note_id: int,
    file: UploadFile | None = File(None),
    alt: str = Form(""),
) -> JSONResponse:
    if not file:
        return JSONResponse({"ok": False, "error": "File required"}, status_code=400)
    with session_scope() as session:
        note = session.query(Note).filter_by(id=int(note_id)).first()
        if not note:
            return JSONResponse({"ok": False, "error": "Note not found"}, status_code=404)
        data = await file.read()
        ext = Path(file.filename or "").suffix or ""
        if not ext:
            ext = guess_extension(content_type=file.content_type, url=None) or ".bin"
        object_path, sha = put_bytes(data, ext=ext)
        extra = note.extra if isinstance(note.extra, dict) else {}
        attachments = list(extra.get("attachments") or [])
        attachment = {
            "id": secrets.token_hex(6),
            "object_path": object_path,
            "filename": file.filename or "",
            "content_type": file.content_type,
            "sha256": sha,
            "alt": alt or "",
            "created_at": utcnow().isoformat(),
        }
        attachments.append(attachment)
        extra["attachments"] = attachments
        note.extra = extra
        note.updated_at = utcnow()
        session.add(note)
    return JSONResponse(
        {
            "ok": True,
            "attachment": attachment,
            "url": f"/objects/{object_path}",
            "markdown": f"![{alt or (file.filename or 'image')}](/objects/{object_path})",
        }
    )


@app.delete("/api/notes/{note_id}/attachments/{attachment_id}")
def note_attachment_delete(note_id: int, attachment_id: str) -> JSONResponse:
    with session_scope() as session:
        note = session.query(Note).filter_by(id=int(note_id)).first()
        if not note:
            return JSONResponse({"ok": False, "error": "Note not found"}, status_code=404)
        extra = note.extra if isinstance(note.extra, dict) else {}
        attachments = list(extra.get("attachments") or [])
        new_attachments = [a for a in attachments if str(a.get("id")) != attachment_id]
        extra["attachments"] = new_attachments
        note.extra = extra
        note.updated_at = utcnow()
        session.add(note)
    return JSONResponse({"ok": True})
