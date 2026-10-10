from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from typing import Any

from thoth_terminal.google_oauth import DRIVE_READONLY_SCOPE, GoogleIntegrationError, build_service
from thoth_terminal.text import clean_whitespace

GOOGLE_DOCS_MIME = "application/vnd.google-apps.document"
GOOGLE_SHEETS_MIME = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDES_MIME = "application/vnd.google-apps.presentation"
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_EXPORT_MIME_MAP = {
    GOOGLE_DOCS_MIME: ("text/plain", ".txt"),
    GOOGLE_SHEETS_MIME: ("text/csv", ".csv"),
    GOOGLE_SLIDES_MIME: ("text/plain", ".txt"),
}
DEFAULT_MIME_ALLOWLIST = [
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
    "text/html",
    GOOGLE_DOCS_MIME,
    GOOGLE_SHEETS_MIME,
    GOOGLE_SLIDES_MIME,
]


def _coerce_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _coerce_bool(v: Any, default: bool) -> bool:
    if v is None:
        return bool(default)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _ensure_list_str(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [tok.strip() for tok in v.split(",") if tok.strip()]
    return []


def _parse_drive_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    txt = str(value).strip()
    if not txt:
        return None
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except Exception:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _iso_utc_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages:
            txt = page.extract_text() or ""
            if txt:
                parts.append(txt)
        return clean_whitespace("\n\n".join(parts))
    except Exception:
        return ""


def _extract_docx_text(data: bytes) -> str:
    try:
        import docx  # type: ignore
    except Exception:
        return ""
    try:
        doc = docx.Document(io.BytesIO(data))
        return clean_whitespace("\n".join([p.text for p in doc.paragraphs if p.text]))
    except Exception:
        return ""


def _extract_text_for_file(*, mime_type: str, data: bytes) -> str:
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if mime == "application/pdf":
        return _extract_pdf_text(data)
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return _extract_docx_text(data)
    if mime in {"text/plain", "text/markdown", "text/csv", "text/html"}:
        try:
            return clean_whitespace(data.decode("utf-8", errors="ignore"))
        except Exception:
            return ""
    try:
        return clean_whitespace(data.decode("utf-8", errors="ignore"))
    except Exception:
        return ""


def _list_children(service, *, parent_id: str, include_shared_drives: bool) -> list[dict[str, Any]]:
    q = f"'{parent_id}' in parents and trashed = false"
    page_token = None
    out: list[dict[str, Any]] = []
    while True:
        req = service.files().list(
            q=q,
            pageSize=200,
            pageToken=page_token,
            fields=(
                "nextPageToken,files(id,name,mimeType,modifiedTime,createdTime,webViewLink,webContentLink,"
                "size,md5Checksum,parents,owners(displayName,emailAddress),driveId)"
            ),
            supportsAllDrives=bool(include_shared_drives),
            includeItemsFromAllDrives=bool(include_shared_drives),
        )
        resp = req.execute()
        rows = resp.get("files") if isinstance(resp, dict) else []
        for row in rows or []:
            if isinstance(row, dict):
                out.append(row)
        page_token = resp.get("nextPageToken") if isinstance(resp, dict) else None
        if not page_token:
            break
    return out


def _download_bytes(service, *, file_id: str, mime_type: str) -> tuple[bytes | None, str | None, str | None]:
    mime = (mime_type or "").strip()
    if mime in GOOGLE_EXPORT_MIME_MAP:
        export_mime, ext = GOOGLE_EXPORT_MIME_MAP[mime]
        try:
            data = service.files().export_media(fileId=file_id, mimeType=export_mime).execute()
        except Exception:
            return None, None, None
        return (bytes(data or b""), export_mime, ext)
    try:
        data = service.files().get_media(fileId=file_id).execute()
    except Exception:
        return None, None, None
    return (bytes(data or b""), mime or None, None)


def fetch_google_drive_folder(config: dict[str, Any], *, source_cursor: dict | None = None) -> dict[str, Any]:
    cfg = dict(config or {})
    folder_id = str(cfg.get("folder_id") or "").strip()
    if not folder_id:
        raise ValueError("google_drive_folder connector requires config.folder_id")

    try:
        service = build_service("drive", "v3", scopes=[DRIVE_READONLY_SCOPE])
    except GoogleIntegrationError:
        raise
    except Exception as exc:
        raise GoogleIntegrationError(f"Failed to initialize Drive API client: {exc}") from exc

    recursive = _coerce_bool(cfg.get("recursive"), True)
    include_shared_drives = _coerce_bool(cfg.get("include_shared_drives"), True)
    download_files = _coerce_bool(cfg.get("download_files"), True)
    extract_text = _coerce_bool(cfg.get("extract_text"), True)
    store_binary = _coerce_bool(cfg.get("store_binary_attachments"), True)
    max_files = max(1, min(_coerce_int(cfg.get("max_files_per_run"), 50), 500))
    max_file_bytes = max(0, _coerce_int(cfg.get("max_file_bytes"), 25_000_000))
    mime_allow = _ensure_list_str(cfg.get("mime_allowlist")) or list(DEFAULT_MIME_ALLOWLIST)
    mime_deny = set([m.lower() for m in _ensure_list_str(cfg.get("mime_denylist"))])

    cur = dict(source_cursor or {})
    overlap_s = max(0, _coerce_int(cur.get("overlap_seconds"), 6 * 3600))
    watermark_dt = _parse_drive_dt(str(cur.get("last_modified_at") or ""))
    cutoff_dt = (watermark_dt - timedelta(seconds=overlap_s)) if watermark_dt else None

    queue = [folder_id]
    seen_folders: set[str] = set()
    rows: list[dict[str, Any]] = []
    while queue:
        fid = queue.pop(0)
        if fid in seen_folders:
            continue
        seen_folders.add(fid)
        children = _list_children(service, parent_id=fid, include_shared_drives=include_shared_drives)
        for row in children:
            mime = str(row.get("mimeType") or "")
            if mime == GOOGLE_FOLDER_MIME and recursive:
                child_id = str(row.get("id") or "").strip()
                if child_id:
                    queue.append(child_id)
            rows.append(row)

    rows.sort(key=lambda r: str(r.get("modifiedTime") or ""), reverse=True)
    items: list[dict[str, Any]] = []
    inspected = 0
    accepted = 0
    max_modified: datetime | None = watermark_dt
    for row in rows:
        if len(items) >= max_files:
            break
        if not isinstance(row, dict):
            continue
        inspected += 1
        file_id = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip() or None
        mime = str(row.get("mimeType") or "").strip()
        if not file_id or not mime or mime == GOOGLE_FOLDER_MIME:
            continue
        mime_l = mime.lower()
        if mime_deny and mime_l in mime_deny:
            continue
        if mime_allow and mime not in mime_allow:
            continue

        modified_at = _parse_drive_dt(str(row.get("modifiedTime") or ""))
        if cutoff_dt and modified_at and modified_at < cutoff_dt:
            continue
        if modified_at and (max_modified is None or modified_at > max_modified):
            max_modified = modified_at

        data_bytes: bytes | None = None
        stored_mime: str | None = mime
        exported_ext: str | None = None
        raw_html = None
        normalized_text = ""
        if download_files:
            data_bytes, stored_mime, exported_ext = _download_bytes(service, file_id=file_id, mime_type=mime)
            if data_bytes is not None and max_file_bytes > 0 and len(data_bytes) > max_file_bytes:
                data_bytes = None
            if data_bytes and extract_text:
                normalized_text = _extract_text_for_file(mime_type=stored_mime or mime, data=data_bytes)
                if (stored_mime or "").startswith("text/html"):
                    try:
                        raw_html = data_bytes.decode("utf-8", errors="ignore")
                    except Exception:
                        raw_html = None

        owners = row.get("owners") if isinstance(row.get("owners"), list) else []
        owner_names = [str(o.get("displayName") or o.get("emailAddress") or "").strip() for o in owners if isinstance(o, dict)]
        author = ", ".join([x for x in owner_names if x]) or None
        web_view = str(row.get("webViewLink") or "").strip() or None
        ext_id_fingerprint = str(row.get("modifiedTime") or row.get("md5Checksum") or "")
        attachments: list[dict[str, Any]] = []
        if data_bytes and store_binary:
            attachments.append(
                {
                    "kind": "file",
                    "filename": name,
                    "content_type": stored_mime or mime,
                    "data_bytes": data_bytes,
                    "original_url": web_view,
                    "ext": exported_ext,
                }
            )

        items.append(
            {
                "external_id": f"gdrive:{file_id}:{ext_id_fingerprint}",
                "url": web_view,
                "canonical_url": web_view,
                "title": name,
                "author": author,
                "published_at": modified_at or _parse_drive_dt(str(row.get("createdTime") or "")),
                "raw_text": normalized_text or (name or file_id),
                "normalized_text": normalized_text or (name or file_id),
                "raw_html": raw_html,
                "attachments": attachments,
                "extra": {
                    "connector": "google_drive_folder",
                    "google_drive": {
                        "file_id": file_id,
                        "mimeType": mime,
                        "webViewLink": web_view,
                        "webContentLink": row.get("webContentLink"),
                        "parents": row.get("parents") if isinstance(row.get("parents"), list) else [],
                        "owners": owners,
                        "md5Checksum": row.get("md5Checksum"),
                        "size": row.get("size"),
                        "modifiedTime": row.get("modifiedTime"),
                        "createdTime": row.get("createdTime"),
                        "driveId": row.get("driveId"),
                    }
                },
            }
        )
        accepted += 1

    next_cursor = dict(cur)
    next_cursor.setdefault("overlap_seconds", 6 * 3600)
    if max_modified is not None:
        next_cursor["last_modified_at"] = _iso_utc_z(max_modified)

    return {
        "items": items,
        "cursor": next_cursor,
        "stats": {
            "connector": "google_drive_folder",
            "inspected_files": inspected,
            "accepted_files": accepted,
            "recursive": bool(recursive),
            "include_shared_drives": bool(include_shared_drives),
        },
    }
