from __future__ import annotations

from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())


_ops_health_cache: dict[str, object] = {"ts": 0.0, "db_path": None, "payload": None}
_OPS_HEALTH_TTL_S = 5.0
_FEED_EXCLUDED_DOCUMENT_KINDS = ("product_snapshot",)


def _triage_counts(session: Session) -> dict[str, int]:
    return _TRIAGE_COUNTS_CACHE.get_or_set(
        f"feed:{_cache_scope()}:counts",
        lambda: _triage_counts_uncached(session),
        ttl_s=10.0,
    )


def _triage_counts_uncached(session: Session) -> dict[str, int]:
    base_filter = ~Document.kind.in_(_FEED_EXCLUDED_DOCUMENT_KINDS)
    saved_filter = and_(
        Document.saved.is_(True),
        Document.triage_state != TRIAGE_IGNORED,
        Document.triage_state != TRIAGE_ARCHIVED,
    )

    def count_if(condition: object) -> object:
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    def aggregate(*filters: object) -> tuple[int, int, int, int, int, int, int]:
        row = (
            session.query(
                func.count(Document.id),
                count_if(Document.triage_state == TRIAGE_NEW),
                count_if(Document.triage_state == TRIAGE_OPENED),
                count_if(Document.triage_state == TRIAGE_ACTIONED),
                count_if(Document.triage_state == TRIAGE_IGNORED),
                count_if(Document.triage_state == TRIAGE_ARCHIVED),
                count_if(saved_filter),
            )
            .filter(*filters)
            .one()
        )
        values = tuple(int(value or 0) for value in row)
        return (values[0], values[1], values[2], values[3], values[4], values[5], values[6])

    row = aggregate(base_filter, human_feed_visible_filter(session))

    return {
        "all": int(row[0] or 0),
        "new": int(row[1] or 0),
        "opened": int(row[2] or 0),
        "actioned": int(row[3] or 0),
        "ignored": int(row[4] or 0),
        "archived": int(row[5] or 0),
        "saved": int(row[6] or 0),
    }


def _json_config_value(session: Session, column: Any, key: str) -> Any:
    try:
        dialect = str(session.get_bind().dialect.name or "").lower()
    except Exception:
        dialect = ""
    if dialect.startswith("postgres"):
        return column.op("->>")(key)
    return func.json_extract(column, f"$.{key}")


def _config_deleted_flag_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    return str(value or "").strip().lower() in {"true", "1", "yes"}


def _feed_source_options(session: Session) -> list[SimpleNamespace]:
    deleted_flag = _json_config_value(session, Source.config, "deleted").label("deleted_flag")
    rows = (
        session.query(Source.id, Source.name, deleted_flag)
        .order_by(Source.name.asc())
        .all()
    )
    return [
        SimpleNamespace(id=int(row.id), name=row.name)
        for row in rows
        if not _config_deleted_flag_truthy(row.deleted_flag)
        and not is_human_feed_hidden_source_name(row.name)
    ]


def _annotate_feed_document_excerpts(session: Session, docs: list[Document], *, limit: int = 1201) -> None:
    ids = [int(doc.id) for doc in docs if doc.id is not None]
    if not ids:
        return
    text_expr = func.coalesce(func.nullif(Document.translation_en, ""), Document.normalized_text, "")
    rows = (
        session.query(
            Document.id,
            func.substr(text_expr, 1, max(1, int(limit))),
            func.length(text_expr),
            Document.translation_en.isnot(None),
            Document.translation_en != "",
        )
        .filter(Document.id.in_(ids))
        .all()
    )
    excerpt_map = {
        int(doc_id): {
            "excerpt": str(excerpt or ""),
            "truncated": int(text_len or 0) > int(limit) - 1,
            "has_translation": bool(has_translation),
            "translation_nonempty": bool(translation_nonempty),
        }
        for doc_id, excerpt, text_len, has_translation, translation_nonempty in rows
    }
    for doc in docs:
        payload = excerpt_map.get(int(doc.id), {})
        excerpt = str(payload.get("excerpt") or "")
        truncated = bool(payload.get("truncated"))
        filing_excerpt = filing_review_excerpt(doc, max_chars=max(240, int(limit) - 1))
        if filing_excerpt:
            excerpt = filing_excerpt
            truncated = len(filing_excerpt) >= int(limit) - 1
        setattr(doc, "feed_excerpt", excerpt)
        setattr(doc, "feed_excerpt_truncated", truncated)
        setattr(doc, "feed_has_translation", bool(payload.get("has_translation")))
        setattr(doc, "feed_translation_nonempty", bool(payload.get("translation_nonempty")))


def _session_read() -> Session:
    SessionLocal = session_factory()
    return SessionLocal()


def _truncate_text(text: str | None, *, limit: int = 400) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _is_url_char(ch: str) -> bool:
    if ch.isalnum():
        return True
    return ch in "-._~:/?#[]@!$&'()*+,;=%"


def _extract_urls_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    cleaned = (
        text.replace("\u00ad", "")
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\ufeff", "")
    )
    urls: list[str] = []
    n = len(cleaned)
    i = 0
    while i < n:
        if cleaned.startswith("http://", i) or cleaned.startswith("https://", i):
            scheme = "https://" if cleaned.startswith("https://", i) else "http://"
            j = i + len(scheme)
            buf = scheme
            while j < n:
                ch = cleaned[j]
                if _is_url_char(ch):
                    buf += ch
                    j += 1
                    continue
                if ch.isspace():
                    k = j
                    while k < n and cleaned[k].isspace():
                        k += 1
                    if k < n and _is_url_char(cleaned[k]):
                        j = k
                        continue
                    break
                break
            buf = buf.rstrip(").,;\"'…")
            if buf and buf not in urls:
                urls.append(buf)
            i = j
            continue
        if cleaned.startswith("www.", i) and (i == 0 or not cleaned[i - 1].isalnum()):
            j = i + 4
            buf = "https://www."
            while j < n:
                ch = cleaned[j]
                if _is_url_char(ch):
                    buf += ch
                    j += 1
                    continue
                if ch.isspace():
                    k = j
                    while k < n and cleaned[k].isspace():
                        k += 1
                    if k < n and _is_url_char(cleaned[k]):
                        j = k
                        continue
                    break
                break
            buf = buf.rstrip(").,;\"'…")
            if buf and buf not in urls:
                urls.append(buf)
            i = j
            continue
        i += 1
    return urls


def _slugify_token(value: str) -> str:
    out: list[str] = []
    for ch in value.strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_", "/"):
            if not out or out[-1] != "_":
                out.append("_")
        elif ch == ".":
            continue
    slug = "".join(out).strip("_")
    return slug or "series"


def _build_metric_key(subject: str, topic: str, metric: str) -> str:
    parts = [subject, topic, metric]
    slugs = [_slugify_token(p) for p in parts if p and p.strip()]
    return ":".join(slugs)


def _metric_value_type(unit: str | None, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if unit and "%" in unit:
        return "percent"
    return "number"


def _resolve_primary_prop(db: Session, thesis: Thesis) -> Proposition | None:
    extra = thesis.extra or {}
    primary_id = extra.get("primary_proposition_id")
    if primary_id:
        prop = db.query(Proposition).filter_by(id=int(primary_id)).first()
        if prop:
            return prop
    key_link = (
        db.query(ThesisProposition)
        .filter_by(thesis_id=thesis.id, role="key")
        .first()
    )
    if key_link:
        return db.query(Proposition).filter_by(id=key_link.proposition_id).first()
    any_link = (
        db.query(ThesisProposition)
        .filter_by(thesis_id=thesis.id)
        .order_by(ThesisProposition.created_at.desc())
        .first()
    )
    if any_link:
        return db.query(Proposition).filter_by(id=any_link.proposition_id).first()
    return None


def _set_primary_prop(session: Session, thesis: Thesis, prop: Proposition, meta: dict[str, object]) -> None:
    session.query(ThesisProposition).filter_by(thesis_id=thesis.id, role="key").update(
        {"role": "supporting"}, synchronize_session=False
    )
    link_thesis_proposition(session, thesis=thesis, proposition=prop, role="key")
    extra = dict(thesis.extra or {})
    extra.update(meta)
    extra["primary_proposition_id"] = prop.id
    thesis.extra = extra
    thesis.updated_at = utcnow()


def _parse_table_rows(raw: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    if not lines:
        return ([], [])
    # Prefer tab-separated, else split on 2+ spaces, then fallback to any whitespace.
    parsed: list[list[str]] = []
    for line in lines:
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
        else:
            parts = [p for p in re.split(r"\s{2,}", line.strip()) if p]
            if len(parts) <= 1:
                parts = [p for p in re.split(r"\s+", line.strip()) if p]
        parsed.append(parts)
    if not parsed:
        return ([], [])

    header = None
    if len(parsed[0]) >= 2:
        header_candidate = parsed[0]
        header_label = (header_candidate[0] or "").strip().lower()
        header_by_label = header_label in {"period", "quarter", "qtr", "date", "time", "month", "year", "week"}
        has_alpha = any(any(ch.isalpha() for ch in cell) for cell in header_candidate[1:])
        if has_alpha or header_by_label:
            header = header_candidate
            parsed = parsed[1:]

    if not parsed:
        return ([], [])

    series_names: list[str]
    if header:
        series_names = [h.strip() or f"series_{i+1}" for i, h in enumerate(header[1:])]
    else:
        series_names = [f"series_{i+1}" for i in range(max(0, len(parsed[0]) - 1))]

    rows: list[tuple[str, list[str]]] = []
    for row in parsed:
        if not row:
            continue
        period = row[0].strip()
        values = [c.strip() for c in row[1:]]
        if len(values) < len(series_names):
            values.extend([""] * (len(series_names) - len(values)))
        rows.append((period, values))
    return (series_names, rows)


def _find_similar_theses(
    session: Session,
    *,
    title: str,
    statement: str,
    entity_tokens: list[str],
    limit: int = 6,
) -> list[dict[str, object]]:
    hits: dict[int, Thesis] = {}
    text = (title or "").strip() or (statement or "").strip()
    if text:
        needle = text[:120]
        like = f"%{needle}%"
        rows = (
            session.query(Thesis)
            .options(joinedload(Thesis.entities))
            .filter(or_(Thesis.title.ilike(like), Thesis.statement.ilike(like)))
            .order_by(Thesis.updated_at.desc())
            .limit(limit)
            .all()
        )
        for th in rows:
            hits[th.id] = th

    tokens = [t.strip() for t in entity_tokens if t.strip()]
    if tokens:
        ent_filters = [Entity.canonical_name.ilike(tok) for tok in tokens]
        ents = session.query(Entity).filter(or_(*ent_filters)).all() if ent_filters else []
        ent_ids = [e.id for e in ents]
        if ent_ids:
            rows = (
                session.query(Thesis)
                .options(joinedload(Thesis.entities))
                .join(Thesis.entities)
                .filter(Entity.id.in_(ent_ids))
                .order_by(Thesis.updated_at.desc())
                .limit(limit)
                .all()
            )
            for th in rows:
                hits.setdefault(th.id, th)

    out: list[dict[str, object]] = []
    for th in list(hits.values())[:limit]:
        out.append(
            {
                "id": th.id,
                "title": th.title,
                "status": th.status,
                "updated_at": th.updated_at,
                "entities": [e.canonical_name for e in (th.entities or [])],
            }
        )
    return out


def _attachment_is_image(att: Attachment) -> bool:
    ext = str(att.object_path or "").lower()
    ctype = str(att.content_type or "").lower()
    return (
        att.kind == "image"
        or ctype.startswith("image/")
        or ext.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
    )


def _build_doc_media_entries(doc: Document) -> list[dict[str, object]]:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    manifest = extra.get("media_manifest") if isinstance(extra.get("media_manifest"), list) else []
    notes = extract_image_ocr_notes(extra)
    attachments = list(doc.attachments or [])
    by_original_url: dict[str, list[Attachment]] = {}
    local_only: list[Attachment] = []
    for att in attachments:
        if not _attachment_is_image(att):
            continue
        if att.original_url:
            by_original_url.setdefault(str(att.original_url), []).append(att)
        else:
            local_only.append(att)

    entries: list[dict[str, object]] = []
    seen_urls: set[str] = set()
    seen_object_paths: set[str] = set()

    def _note_for(*keys: str | None) -> dict[str, object] | None:
        for key in keys:
            probe = str(key or "").strip()
            if probe and probe in notes:
                return notes[probe]
        return None

    def _append_entry(
        *,
        url: str | None,
        row: dict[str, object] | None = None,
        attachment: Attachment | None = None,
        label: str | None = None,
    ) -> None:
        src = None
        if attachment is not None:
            src = f"/objects/{attachment.object_path}"
        else:
            src = str(url or "").strip() or None
        if not src:
            return
        original_url = str(url or attachment.original_url or "").strip() or None
        object_path = str(attachment.object_path or "").strip() if attachment is not None else None
        if original_url and original_url in seen_urls:
            return
        if object_path and object_path in seen_object_paths:
            return
        note = _note_for(original_url, object_path)
        entries.append(
            {
                "src": src,
                "open_url": original_url or src,
                "original_url": original_url,
                "object_path": object_path,
                "label": label or str((row or {}).get("label") or "Media").strip() or "Media",
                "section": str((row or {}).get("section") or "media").strip() or "media",
                "relation": str((row or {}).get("relation") or "").strip() or None,
                "tweet_url": str((row or {}).get("tweet_url") or "").strip() or None,
                "article_url": str((row or {}).get("article_url") or "").strip() or None,
                "stored": bool(attachment is not None),
                "ocr_summary": str((note or {}).get("summary") or "").strip() or None,
                "ocr_text": str((note or {}).get("text") or "").strip() or None,
            }
        )
        if original_url:
            seen_urls.add(original_url)
        if object_path:
            seen_object_paths.add(object_path)

    for row in manifest:
        if not isinstance(row, dict):
            continue
        if str(row.get("kind") or "image").strip().lower() != "image":
            continue
        media_url = str(row.get("url") or "").strip() or None
        attachment = None
        if media_url and by_original_url.get(media_url):
            attachment = by_original_url[media_url].pop(0)
        _append_entry(url=media_url, row=row, attachment=attachment)

    for media_url in extract_candidate_image_urls(extra):
        attachment = None
        if by_original_url.get(media_url):
            attachment = by_original_url[media_url].pop(0)
        _append_entry(url=media_url, attachment=attachment)

    for rows in by_original_url.values():
        for attachment in rows:
            _append_entry(
                url=str(attachment.original_url or "").strip() or None,
                attachment=attachment,
                label="Stored media",
            )

    for attachment in local_only:
        _append_entry(url=None, attachment=attachment, label="Stored media")

    return entries


def _agent_settings_defaults() -> dict[str, object]:
    return {
        "context_default_lookback_hours": int(getattr(settings, "agent_context_default_lookback_hours", 24) or 24),
        "context_max_items_per_section": int(getattr(settings, "agent_context_max_items_per_section", 25) or 25),
        "context_max_prompt_chars": int(getattr(settings, "agent_context_max_prompt_chars", 24000) or 24000),
        "daily_master_enabled": bool(getattr(settings, "agent_daily_master_enabled", True)),
        "daily_master_hhmm": str(getattr(settings, "agent_daily_master_hhmm", "18:10") or "18:10"),
        "daily_master_tz": str(getattr(settings, "agent_daily_master_tz", "America/New_York") or "America/New_York"),
        "default_include_sections": [],
        "default_post_to_outbox": False,
    }


def _normalize_agent_settings_patch(patch: dict[str, object]) -> dict[str, object]:
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")

    out: dict[str, object] = {}

    def _boolish(value: object) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    def _intish(value: object, *, min_v: int, max_v: int) -> int:
        try:
            val = int(value)  # type: ignore[arg-type]
        except Exception as exc:
            raise ValueError(f"Invalid integer value: {value!r}") from exc
        return max(min_v, min(max_v, val))

    if "daily_master_enabled" in patch:
        out["daily_master_enabled"] = _boolish(patch.get("daily_master_enabled"))
    if "default_post_to_outbox" in patch:
        out["default_post_to_outbox"] = _boolish(patch.get("default_post_to_outbox"))

    if "daily_master_hhmm" in patch:
        hhmm = str(patch.get("daily_master_hhmm") or "").strip()
        if hhmm and re.match(r"^\d{1,2}:\d{2}$", hhmm):
            out["daily_master_hhmm"] = hhmm
        elif hhmm:
            raise ValueError("daily_master_hhmm must look like HH:MM")

    if "daily_master_tz" in patch:
        tz_name = str(patch.get("daily_master_tz") or "").strip()
        if tz_name:
            out["daily_master_tz"] = tz_name

    if "context_default_lookback_hours" in patch:
        out["context_default_lookback_hours"] = _intish(
            patch.get("context_default_lookback_hours"), min_v=1, max_v=24 * 30
        )
    if "context_max_items_per_section" in patch:
        out["context_max_items_per_section"] = _intish(
            patch.get("context_max_items_per_section"), min_v=1, max_v=200
        )
    if "context_max_prompt_chars" in patch:
        out["context_max_prompt_chars"] = _intish(
            patch.get("context_max_prompt_chars"), min_v=2000, max_v=200000
        )

    if "default_include_sections" in patch:
        raw = patch.get("default_include_sections")
        if isinstance(raw, str):
            rows = [tok.strip() for tok in raw.split(",") if tok.strip()]
        elif isinstance(raw, list):
            rows = [str(x).strip() for x in raw if str(x).strip()]
        elif raw is None:
            rows = []
        else:
            raise ValueError("default_include_sections must be an array or comma-separated string")
        out["default_include_sections"] = rows

    return out


def _agent_settings_for_guild(guild_id: int | None) -> tuple[dict[str, object], dict[str, object]]:
    defaults = _agent_settings_defaults()
    if guild_id is None or int(guild_id) <= 0:
        return {}, defaults
    cache_key = f"settings:agent:{_cache_scope()}:{int(guild_id)}"
    cached = _AGENT_SETTINGS_CACHE.get(cache_key)
    if cached is not None:
        raw_cached = cached.get("raw") if isinstance(cached.get("raw"), dict) else {}
        effective_cached = cached.get("effective") if isinstance(cached.get("effective"), dict) else defaults
        return dict(raw_cached), dict(effective_cached)
    from thoth_terminal.discord.desk import get_agent_settings

    raw = get_agent_settings(guild_id=int(guild_id))
    effective = dict(defaults)
    if isinstance(raw, dict):
        try:
            normalized = _normalize_agent_settings_patch(raw)
        except Exception:
            normalized = {}
        effective.update(normalized)
    raw_payload = dict(raw) if isinstance(raw, dict) else {}
    payload = {"raw": raw_payload, "effective": dict(effective)}
    _AGENT_SETTINGS_CACHE.set(cache_key, payload)
    return dict(raw_payload), dict(effective)


def _invalidate_agent_settings_cache(guild_id: int | None = None) -> None:
    prefix = f"settings:agent:{_cache_scope()}:"
    if guild_id is None:
        _AGENT_SETTINGS_CACHE.invalidate_prefix(prefix)
        return
    _AGENT_SETTINGS_CACHE.invalidate(f"{prefix}{int(guild_id)}")
