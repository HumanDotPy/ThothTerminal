from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context
from thoth_terminal.web.public_reports import published_artifact_path, render_published_markdown


bind_app_context(globals())


@app.get("/exports", response_class=HTMLResponse)
def exports(request: Request, path: str | None = None, kind: str | None = None) -> HTMLResponse:
    exports_dir = settings.data_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    raw_dirs = sorted([p for p in exports_dir.iterdir() if p.is_dir()], reverse=True)

    def _read_index(p: Path):
        idx = p / "index.json"
        if not idx.exists():
            return None
        try:
            return json.loads(idx.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            return None

    def _bundle_kind(name: str, payload) -> str:
        if isinstance(payload, dict) and payload.get("kind"):
            return str(payload.get("kind"))
        if name.startswith("bundle_"):
            return "doc_bundle"
        if name.startswith("thesis_"):
            return "thesis_dossier"
        if name.startswith("note_"):
            return "note_bundle"
        return "bundle"

    bundles: list[dict[str, object]] = []
    for p in raw_dirs:
        payload = _read_index(p)
        b_kind = _bundle_kind(p.name, payload)
        if kind and kind != b_kind:
            continue
        count = None
        if isinstance(payload, list):
            count = len(payload)
        elif isinstance(payload, dict):
            if payload.get("evidence"):
                count = len(payload.get("evidence") or [])
            elif payload.get("linked_documents"):
                count = len(payload.get("linked_documents") or [])
        bundles.append(
            {
                "name": p.name,
                "path": p,
                "kind": b_kind,
                "count": count,
                "modified": datetime.fromtimestamp(p.stat().st_mtime, tz=ZoneInfo("UTC")).replace(tzinfo=None),
            }
        )

    selected = exports_dir / path if path else None
    bundle_md = None
    bundle_index = None
    if selected and selected.exists():
        if (selected / "bundle.md").exists():
            bundle_md = (selected / "bundle.md").read_text(encoding="utf-8", errors="ignore")
        bundle_index = _read_index(selected)
    return _template_response(
        "exports.html",
        {
            "request": request,
            "bundles": bundles,
            "selected": selected,
            "bundle_md": bundle_md,
            "bundle_index": bundle_index,
            "kind_filter": kind or "",
        },
    )


@app.get("/reports", response_class=HTMLResponse)
def research_reports_index(
    request: Request,
    q: str | None = None,
    report_type: str | None = None,
    status: str | None = None,
    freshness: str | None = None,
    topic: str | None = None,
    company: str | None = None,
    supply_chain: str | None = None,
    material: str | None = None,
    technology: str | None = None,
    limit: int | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    has_filters = any([q, report_type, status, freshness, topic, company, supply_chain, material, technology])
    limit_val = max(25, min(int(limit or (200 if has_filters else 100)), 200))
    rows = list_research_reports(
        db,
        query=q,
        report_type=report_type,
        status=status,
        freshness=freshness,
        topic=topic,
        company=company,
        supply_chain=supply_chain,
        material=material,
        technology=technology,
        limit=limit_val,
    )
    return _template_response(
        "reports.html",
        {
            "request": request,
            "rows": rows,
            "query": str(q or "").strip(),
            "filters": {
                "q": str(q or "").strip(),
                "report_type": normalize_report_type(report_type) if report_type else "",
                "status": str(status or "").strip(),
                "freshness": str(freshness or "").strip(),
                "topic": str(topic or "").strip(),
                "company": str(company or "").strip(),
                "supply_chain": str(supply_chain or "").strip(),
                "material": str(material or "").strip(),
                "technology": str(technology or "").strip(),
                "limit": str(limit_val),
            },
            "limit_options": [50, 100, 200],
            "report_types": [{"value": value, "label": REPORT_TYPE_LABELS[value]} for value in REPORT_TYPES],
        },
    )


@app.post("/reports/import")
async def research_report_import_action(
    file: UploadFile | None = File(None),
    title: str | None = Form(None),
    report_type: str = Form("imported_report"),
    summary: str | None = Form(None),
    tags: str | None = Form(None),
    topics: str | None = Form(None),
    companies: str | None = Form(None),
    supply_chains: str | None = Form(None),
    materials: str | None = Form(None),
    technologies: str | None = Form(None),
    source_label: str | None = Form(None),
    source_url: str | None = Form(None),
    report_id: int | None = Form(None),
) -> Response:
    def _form_text(value: object | None) -> str | None:
        return value if isinstance(value, str) else None

    if file is None or not str(file.filename or "").strip():
        return HTMLResponse("File required", status_code=400)
    data = await file.read()
    try:
        with session_scope() as session:
            result = import_research_report_bytes(
                session,
                data=data,
                filename=file.filename,
                content_type=file.content_type,
                title=_form_text(title),
                report_type=_form_text(report_type) or "imported_report",
                summary=_form_text(summary),
                tags=_split_csv(_form_text(tags)),
                topics=_split_csv(_form_text(topics)),
                linked_objects={
                    "companies": _split_csv(_form_text(companies)),
                    "supply_chains": _split_csv(_form_text(supply_chains)),
                    "materials": _split_csv(_form_text(materials)),
                    "technologies": _split_csv(_form_text(technologies)),
                },
                source_metadata={
                    "source_label": str(_form_text(source_label) or "").strip() or None,
                    "source_url": str(_form_text(source_url) or "").strip() or None,
                    "import_surface": "reports_web_upload",
                },
                report_id=int(report_id) if str(report_id or "").isdigit() else None,
                actor="browser",
            )
            report = dict(result.get("report") or {})
            version = dict(result.get("version") or {})
    except Exception as exc:
        return HTMLResponse(f"Import failed: {type(exc).__name__}: {exc}", status_code=400)
    _invalidate_terminal_caches("search:")
    target = f"/reports/{report.get('slug') or ''}"
    if version.get("id"):
        target += f"?version_id={int(version['id'])}"
    return RedirectResponse(url=target, status_code=303)


@app.post("/reports/{subject}/build")
def research_report_build_action(
    subject: str,
    refresh: bool = Form(False),
    depth_days: int | None = Form(None),
) -> RedirectResponse:
    with session_scope() as session:
        build_research_report(
            session,
            subject=subject,
            trigger_kind="browser",
            trigger_label="refresh" if bool(refresh) else "build",
            force=bool(refresh),
            window_days=int(depth_days) if depth_days is not None else None,
        )
    return RedirectResponse(url=f"/reports/{subject}", status_code=303)


@app.post("/reports/{subject}/manual-addendum")
def research_report_manual_addendum_action(
    subject: str,
    version_id: int | None = Form(None),
    title: str | None = Form(None),
    summary: str | None = Form(None),
    body_md: str = Form(...),
    tags: str | None = Form(None),
) -> Response:
    try:
        with session_scope() as session:
            result = append_research_report_addendum(
                session,
                subject=subject,
                version_id=int(version_id) if version_id is not None else None,
                title=title,
                summary=summary,
                body_md=body_md,
                tags=_split_csv(tags),
                actor="browser",
            )
            report = dict(result.get("report") or {})
            version = dict(result.get("version") or {})
    except Exception as exc:
        return HTMLResponse(f"Addendum failed: {type(exc).__name__}: {exc}", status_code=400)
    _invalidate_terminal_caches("search:")
    target = f"/reports/{report.get('slug') or subject}"
    if version.get("id"):
        target += f"?version_id={int(version['id'])}"
    return RedirectResponse(url=target, status_code=303)


@app.post("/reports/{subject}/ai-clean")
def research_report_ai_clean_action(
    subject: str,
    version_id: int | None = Form(None),
    instructions: str | None = Form(None),
) -> RedirectResponse:
    with session_scope() as session:
        create_ai_clean_derivative(
            session,
            subject=subject,
            report_version_id=int(version_id) if version_id is not None else None,
            instructions=instructions,
        )
    target = f"/reports/{subject}"
    if version_id is not None:
        target += f"?version_id={int(version_id)}"
    return RedirectResponse(url=target, status_code=303)


@app.post("/reports/{subject}/memory-review/request")
def research_report_memory_review_request_action(
    subject: str,
    version_id: int | None = Form(None),
) -> RedirectResponse:
    with session_scope() as session:
        row = create_report_memory_review_request(
            session,
            subject=subject,
            version_id=int(version_id) if version_id is not None else None,
        )
        request_id = int(row.id)
    return RedirectResponse(url=f"/instruments/requests/{request_id}", status_code=303)


@app.post("/reports/{subject}/memory-review/apply")
def research_report_memory_review_apply_action(
    subject: str,
    decision: str = Form(...),
    version_id: int | None = Form(None),
    reason: str | None = Form(None),
    body_md: str | None = Form(None),
    evidence_text: str | None = Form(None),
) -> RedirectResponse:
    evidence = [line.strip() for line in str(evidence_text or "").splitlines() if line.strip()]
    with session_scope() as session:
        result = apply_report_memory_decision(
            session,
            subject=subject,
            version_id=int(version_id) if version_id is not None else None,
            decision=decision,
            reason=reason,
            body_md=body_md,
            evidence=evidence,
            actor="browser",
        )
        result_version = result.get("version") or {}
        result_decision = str(result.get("decision") or decision or "").strip().lower()
        result_version_id = int(result_version.get("id")) if str(result_version.get("id")).isdigit() else None
    target = f"/reports/{subject}"
    if result_decision != "quarantine" and result_version_id is not None:
        target += f"?version_id={result_version_id}"
    return RedirectResponse(url=target, status_code=303)


def _reader_artifact_for_version(
    version: ResearchReportVersion,
    *,
    artifact_id: int | None = None,
    format_hint: str | None = None,
) -> ResearchReportArtifact | None:
    artifacts = list(version.artifacts or [])

    def _media_type(row: ResearchReportArtifact) -> str:
        return str(row.content_type or "").split(";", 1)[0].strip().lower()

    def _is_pdf(row: ResearchReportArtifact) -> bool:
        return _media_type(row) == "application/pdf" or row.artifact_type == "pdf"

    def _is_markdown(row: ResearchReportArtifact) -> bool:
        return _media_type(row) in {"text/markdown", "text/plain", "text/x-markdown"} or row.artifact_type in {
            "markdown",
            "ai_clean_markdown",
        }

    if artifact_id is not None:
        for artifact in artifacts:
            if int(artifact.id) == int(artifact_id):
                return artifact
        return None
    payload = dict(version.content_json or {}) if isinstance(version.content_json, dict) else {}
    reader = payload.get("reader") if isinstance(payload.get("reader"), dict) else {}
    hint = str(format_hint or reader.get("format") or "").strip().lower()
    if hint == "pdf":
        preferred = [row for row in artifacts if _is_pdf(row)]
    elif hint in {"markdown", "md"}:
        preferred = [row for row in artifacts if _is_markdown(row)]
    else:
        raw_reader_id = reader.get("artifact_id")
        if raw_reader_id is not None and str(raw_reader_id).isdigit():
            for artifact in artifacts:
                if int(artifact.id) == int(raw_reader_id):
                    return artifact
        # Generated reports persist both PDF and Markdown artifacts. The desktop
        # browser does not reliably render embedded PDFs, so make Markdown the
        # default reader when no explicit PDF artifact was requested.
        preferred = [row for row in artifacts if _is_markdown(row)]
        if not preferred:
            preferred = [row for row in artifacts if _is_pdf(row)]
    return preferred[0] if preferred else None


def _artifact_entries_for_template(artifact: ResearchReportArtifact) -> dict[str, object]:
    return {
        "id": int(artifact.id),
        "artifact_type": artifact.artifact_type,
        "variant": artifact.variant,
        "label": artifact.label,
        "object_path": artifact.object_path,
        "content_type": artifact.content_type,
        "payload": dict(artifact.payload or {}),
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
    }


def _manual_addenda_markdown(addenda: list[dict[str, object]] | tuple[dict[str, object], ...] | None) -> str:
    rows = [row for row in (addenda or []) if isinstance(row, dict)]
    if not rows:
        return ""
    lines: list[str] = ["## Manual Addenda", ""]
    for row in rows:
        title = str(row.get("title") or "Manual addendum").strip()
        lines.extend([f"### {title}", ""])
        if row.get("summary"):
            lines.extend([str(row.get("summary") or "").strip(), ""])
        body_md = str(row.get("body_md") or row.get("body_preview") or "").strip()
        if body_md:
            lines.extend([body_md, ""])
        meta: list[str] = []
        if row.get("actor"):
            meta.append(f"actor: {row.get('actor')}")
        if row.get("created_at"):
            meta.append(f"created_at: {row.get('created_at')}")
        if meta:
            lines.extend([f"_Manual layer metadata: {'; '.join(meta)}._", ""])
    return "\n".join(lines).strip() + "\n"


def _public_report_version(
    db: Session,
    *,
    slug: str,
    version_id: int | None = None,
) -> tuple[ResearchReport | None, ResearchReportVersion | None]:
    slug_v = str(slug or "").strip()
    if not slug_v:
        return None, None
    report = (
        db.query(ResearchReport)
        .filter(ResearchReport.status == "active")
        .filter(ResearchReport.slug == slug_v)
        .first()
    )
    if report is None or report.id is None:
        return None, None
    q = db.query(ResearchReportVersion).filter(
        ResearchReportVersion.report_id == int(report.id),
        ResearchReportVersion.status == "published",
    )
    if version_id is not None:
        version = q.filter(ResearchReportVersion.id == int(version_id)).first()
    elif report.latest_published_version_id is not None:
        version = q.filter(ResearchReportVersion.id == int(report.latest_published_version_id)).first()
        if version is None:
            version = q.order_by(ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc()).first()
    else:
        version = q.order_by(ResearchReportVersion.version.desc(), ResearchReportVersion.id.desc()).first()
    return report, version


@app.get("/public/reports/{slug}/artifacts/{artifact_id}/raw")
def public_research_report_artifact_raw(
    slug: str,
    artifact_id: int,
    version_id: int | None = None,
    db: Session = Depends(get_session),
) -> Response:
    report, version = _public_report_version(db, slug=slug, version_id=version_id)
    if report is None or version is None:
        return HTMLResponse("Report not found", status_code=404)
    artifact = db.query(ResearchReportArtifact).filter_by(id=int(artifact_id), report_version_id=int(version.id)).first()
    if artifact is None:
        return HTMLResponse("Artifact not found", status_code=404)
    path = published_artifact_path(str(artifact.object_path or ""))
    if path is None:
        return HTMLResponse("Artifact bytes not found", status_code=404)
    return FileResponse(path=str(path), media_type=artifact.content_type or "application/octet-stream")


@app.get("/public/reports/{slug}/reader", response_class=HTMLResponse)
def public_research_report_reader(
    request: Request,
    slug: str,
    version_id: int | None = None,
    artifact_id: int | None = None,
    format: str | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    report, version = _public_report_version(db, slug=slug, version_id=version_id)
    if report is None or version is None:
        return _template_response(
            "public_report_reader.html",
            {
                "request": request,
                "title": "Report not found",
                "message": "This public report is not available.",
                "report": {"slug": slug, "title": slug, "display_name": slug},
                "version": {},
                "artifact": {},
                "reader_kind": "unavailable",
                "markdown_html": "",
                "raw_url": "",
            },
            status_code=404,
        )
    artifact = _reader_artifact_for_version(version, artifact_id=artifact_id, format_hint=format)
    metadata = published_report_metadata(version)
    summary = {"title": metadata["title"], "display_name": metadata["title"], "slug": report.slug}
    version_payload = {"version": int(version.version), "published_at": version.published_at, "as_of_at": version.as_of_at}
    markdown_text = version.content_md or ""
    raw_url = ""
    artifact_payload: dict[str, object] = {}
    reader_kind = "markdown"
    if artifact is not None:
        artifact_media = str(artifact.content_type or "").split(";", 1)[0].strip().lower()
        reader_kind = "pdf" if artifact_media == "application/pdf" or artifact.artifact_type == "pdf" else "markdown"
        raw_url = f"/public/reports/{report.slug}/artifacts/{int(artifact.id)}/raw?version_id={int(version.id)}"
        artifact_payload = {"id": int(artifact.id), "content_type": artifact.content_type}
        if reader_kind == "markdown":
            path = published_artifact_path(str(artifact.object_path or ""))
            if path is not None:
                markdown_text = path.read_text(encoding="utf-8", errors="ignore")
            if not markdown_text:
                markdown_text = version.content_md or ""
    return _template_response(
        "public_report_reader.html",
        {
            "request": request,
            "title": summary.get("display_name") or summary.get("title") or report.title,
            "message": "",
            "report": summary,
            "version": version_payload,
            "artifact": artifact_payload,
            "reader_kind": reader_kind,
            "markdown_html": render_published_markdown(markdown_text) if reader_kind == "markdown" else "",
            "raw_url": raw_url,
        },
    )


@app.get("/reports/{subject}/artifacts/{artifact_id}/raw")
def research_report_artifact_raw(
    subject: str,
    artifact_id: int,
    version_id: int | None = None,
    db: Session = Depends(get_session),
) -> Response:
    report, version_payload, _summary, _resolved_subject = _resolve_report_record(db, subject=subject, version_id=version_id)
    if report.id is None:
        return HTMLResponse("Report not found", status_code=404)
    version_ids = [int(row.id) for row in db.query(ResearchReportVersion.id).filter_by(report_id=int(report.id)).all()]
    artifact = db.query(ResearchReportArtifact).filter_by(id=int(artifact_id)).first()
    if artifact is None or int(artifact.report_version_id) not in version_ids:
        return HTMLResponse("Artifact not found", status_code=404)
    path = object_abs_path(str(artifact.object_path or ""))
    if not path.exists() or not path.is_file():
        return HTMLResponse("Artifact bytes not found", status_code=404)
    media_type = artifact.content_type or "application/octet-stream"
    return FileResponse(path=str(path), media_type=media_type)


@app.get("/reports/{subject}/reader", response_class=HTMLResponse)
def research_report_reader(
    request: Request,
    subject: str,
    version_id: int | None = None,
    artifact_id: int | None = None,
    format: str | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    report, version_payload, summary, _resolved_subject = _resolve_report_record(db, subject=subject, version_id=version_id)
    if report.id is None:
        return _template_response(
            "report_reader_unavailable.html",
            {
                "request": request,
                "title": "Report not found",
                "message": "The requested report is not in the report registry.",
                "report": {"slug": subject, "display_name": subject, "title": subject},
                "version": {},
            },
            status_code=404,
        )
    if version_payload is None:
        return _template_response(
            "report_reader_unavailable.html",
            {
                "request": request,
                "title": "Report version not found",
                "message": "The selected report version is not available.",
                "report": summary or {"slug": subject, "display_name": subject, "title": subject},
                "version": {},
            },
            status_code=404,
        )
    version = db.query(ResearchReportVersion).filter_by(id=int(version_payload["id"]), report_id=int(report.id)).first()
    if version is None:
        return _template_response(
            "report_reader_unavailable.html",
            {
                "request": request,
                "title": "Report version not found",
                "message": "The selected report version is not available.",
                "report": summary,
                "version": version_payload,
            },
            status_code=404,
        )
    artifact = _reader_artifact_for_version(version, artifact_id=artifact_id, format_hint=format)
    if artifact is None:
        return _template_response(
            "report_reader_unavailable.html",
            {
                "request": request,
                "title": "Readable report artifact not found",
                "message": "This report version does not have a Markdown or PDF reader artifact attached. Use the detail page to inspect metadata, addenda, or raw artifacts.",
                "report": summary,
                "version": version_payload,
            },
            status_code=404,
        )
    artifact_media = str(artifact.content_type or "").split(";", 1)[0].strip().lower()
    reader_kind = "pdf" if artifact_media == "application/pdf" or artifact.artifact_type == "pdf" else "markdown"
    raw_url = f"/reports/{report.slug}/artifacts/{int(artifact.id)}/raw?version_id={int(version.id)}"
    provenance = dict((artifact.payload or {}).get("source_metadata") or {})
    provenance.update(
        {
            "file_name": (artifact.payload or {}).get("file_name"),
            "file_sha256": (artifact.payload or {}).get("file_sha256"),
            "artifact_type": artifact.artifact_type,
            "content_type": artifact.content_type,
            "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
        }
    )
    if reader_kind == "pdf":
        return _template_response(
            "report_pdf_reader.html",
            {
                "request": request,
                "report": summary,
                "version": version_payload,
                "artifact": _artifact_entries_for_template(artifact),
                "raw_url": raw_url,
                "provenance": provenance,
            },
        )
    markdown_text = ""
    path = object_abs_path(str(artifact.object_path or ""))
    if path.exists() and path.is_file():
        markdown_text = path.read_text(encoding="utf-8", errors="ignore")
    if not markdown_text:
        markdown_text = version.content_md or ""
    manual_addenda = summary.get("manual_addenda") if isinstance(summary.get("manual_addenda"), list) else []
    if artifact.artifact_type != "manual_addendum":
        manual_markdown = _manual_addenda_markdown(manual_addenda)
        if manual_markdown:
            markdown_text = f"{markdown_text.rstrip()}\n\n{manual_markdown}".strip() + "\n"
    return _template_response(
        "report_markdown_reader.html",
        {
            "request": request,
            "report": summary,
            "version": version_payload,
            "artifact": _artifact_entries_for_template(artifact),
            "markdown_html": render_markdown_to_html(db, markdown_text),
            "markdown_text": markdown_text,
            "raw_url": raw_url,
            "provenance": provenance,
            "manual_addenda": manual_addenda,
        },
    )


@app.get("/reports/{subject}", response_class=HTMLResponse)
def research_report_detail(
    request: Request,
    subject: str,
    version_id: int | None = None,
    depth_days: int | None = None,
    live: bool = False,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    report, version_payload, summary, resolved_subject = _resolve_report_record(db, subject=subject, version_id=version_id)
    content = None
    preview_payload = None
    if version_payload is not None:
        version_row = db.query(ResearchReportVersion).filter_by(id=int(version_payload["id"])).first()
        content = dict(version_row.content_json or {}) if version_row is not None else None
        if _is_imported_report_payload(content):
            content = _normalize_report_payload(content, resolved_subject)
        elif _report_detail_needs_assembled_payload(content=content):
            if content:
                content = _overlay_report_live_context(
                    db,
                    content,
                    resolved_subject,
                    report,
                    window_days=int(depth_days) if depth_days is not None else None,
                )
            else:
                content, _meta = assemble_report_payload(
                    db,
                    subject=resolved_subject,
                    report=report,
                    window_days=int(depth_days) if depth_days is not None else None,
                )
        elif bool(live) or depth_days is not None or _report_detail_missing_live_context_keys(content):
            content = _overlay_report_live_context(
                db,
                content,
                resolved_subject,
                report,
                window_days=int(depth_days) if depth_days is not None else None,
            )
        if not _is_imported_report_payload(content):
            content = _normalize_report_payload(content, resolved_subject)
    else:
        preview_payload, _meta = assemble_report_payload(
            db,
            subject=resolved_subject,
            report=report,
            window_days=int(depth_days) if depth_days is not None else None,
        )
        preview_payload = _normalize_report_payload(preview_payload, resolved_subject)
    versions = _report_version_history(db, report, limit=25)
    return _template_response(
        "report_detail.html",
        {
            "request": request,
            "report_summary": summary,
            "report_version": version_payload,
            "report_content": content,
            "preview_payload": preview_payload,
            "versions": versions,
            "section_order": list(RESEARCH_REPORT_SECTION_ORDER),
            "subject_slug": subject,
            "depth_days": int(depth_days) if depth_days is not None else None,
            "live_context": bool(live),
        },
    )


@app.get("/reports/{subject}/print", response_class=HTMLResponse)
def research_report_print(
    subject: str,
    version_id: int | None = None,
    depth_days: int | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    _report, version_payload, _summary, resolved_subject = _resolve_report_record(db, subject=subject, version_id=version_id)
    if version_payload is None:
        preview_payload, _meta = assemble_report_payload(
            db,
            subject=resolved_subject,
            report=None,
            window_days=int(depth_days) if depth_days is not None else None,
        )
        preview_payload = _normalize_report_payload(preview_payload, resolved_subject)
        return HTMLResponse(_render_print_html(preview_payload))
    version_row = db.query(ResearchReportVersion).filter_by(id=int(version_payload["id"])).first()
    if version_row is None:
        raise HTTPException(status_code=404, detail="Report version not found.")
    content = dict(version_row.content_json or {})
    if _is_imported_report_payload(content):
        content = _normalize_report_payload(content, resolved_subject)
    elif not _report_payload_has_detail_sections(content):
        content, _meta = assemble_report_payload(
            db,
            subject=resolved_subject,
            report=_report,
            window_days=int(depth_days) if depth_days is not None else None,
        )
    else:
        content = _overlay_report_live_context(
            db,
            content,
            resolved_subject,
            _report,
            window_days=int(depth_days) if depth_days is not None else None,
        )
    if not _is_imported_report_payload(content):
        content = _normalize_report_payload(content, resolved_subject)
    return HTMLResponse(_render_print_html(content or {}))


@app.get("/knowledge", response_class=HTMLResponse)
def knowledge_index(
    request: Request,
    q: str | None = None,
    subject: str | None = None,
    kind: str | None = None,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    rows = list_knowledge_assets(
        db,
        query=q,
        subject=subject,
        kind=kind,
        published_only=False,
        include_version_content=False,
        limit=200,
    )
    return _template_response(
        "knowledge.html",
        {
            "request": request,
            "rows": rows,
            "query": str(q or "").strip(),
            "subject_query": str(subject or "").strip(),
            "kind_filter": str(kind or "").strip(),
        },
    )


@app.post("/knowledge/draft")
def knowledge_save_draft_action(
    subject: str = Form(...),
    kind: str = Form(...),
    title: str | None = Form(None),
    summary: str | None = Form(None),
    content_md: str | None = Form(None),
    stale_after_days: int | None = Form(None),
) -> RedirectResponse:
    with session_scope() as session:
        asset = save_knowledge_asset_draft(
            session,
            subject=subject,
            kind=kind,
            title=title,
            summary=summary,
            content_md=content_md,
            stale_after_days=int(stale_after_days) if stale_after_days is not None else None,
        )["href"]
    return RedirectResponse(url=str(asset), status_code=303)


@app.post("/knowledge/generate")
def knowledge_generate_action(
    subject: str = Form(...),
    kind: str = Form(...),
    instructions: str | None = Form(None),
) -> RedirectResponse:
    with session_scope() as session:
        asset = generate_knowledge_asset_draft(
            session,
            subject=subject,
            kind=kind,
            instructions=instructions,
        )["href"]
    return RedirectResponse(url=str(asset), status_code=303)


@app.post("/knowledge/{slug}/publish")
def knowledge_publish_action(
    slug: str,
) -> RedirectResponse:
    with session_scope() as session:
        payload = get_knowledge_asset(session, slug=slug)
        publish_knowledge_asset(
            session,
            subject=str(payload.get("subject_slug") or payload.get("subject_display_name") or ""),
            kind=str(payload.get("kind") or ""),
        )
    return RedirectResponse(url=f"/knowledge/{slug}", status_code=303)


@app.post("/knowledge/{slug}/review")
def knowledge_review_action(
    slug: str,
) -> RedirectResponse:
    with session_scope() as session:
        asset = get_knowledge_asset(session, slug=slug)
        run_freshness_review(
            session,
            object_type="knowledge_asset",
            object_id=int(asset["id"]),
            trigger_kind="browser",
            allow_live_web=True,
        )
    return RedirectResponse(url=f"/knowledge/{slug}", status_code=303)


@app.get("/knowledge/{slug}", response_class=HTMLResponse)
def knowledge_detail(
    request: Request,
    slug: str,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    asset = get_knowledge_asset(db, slug=slug)
    reviews = list_freshness_reviews(db, object_type="knowledge_asset", object_id=int(asset["id"]), limit=20)
    return _template_response(
        "knowledge_detail.html",
        {
            "request": request,
            "asset": asset,
            "reviews": reviews,
        },
    )


@app.get("/freshness", response_class=HTMLResponse)
def freshness_index(
    request: Request,
    db: Session = Depends(get_session),
) -> HTMLResponse:
    rows = list_freshness_reviews(db, limit=200)
    return _template_response(
        "freshness.html",
        {
            "request": request,
            "rows": rows,
        },
    )


@app.post("/freshness/run")
def freshness_run_action(
    object_type: str = Form(...),
    object_id: int = Form(...),
    redirect_to: str | None = Form(None),
) -> RedirectResponse:
    with session_scope() as session:
        run_freshness_review(
            session,
            object_type=object_type,
            object_id=int(object_id),
            trigger_kind="browser",
            allow_live_web=True,
        )
    return RedirectResponse(url=str(redirect_to or "/freshness"), status_code=303)
