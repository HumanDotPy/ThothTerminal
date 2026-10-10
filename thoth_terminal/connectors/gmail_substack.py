from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

from thoth_terminal.google_oauth import GMAIL_READONLY_SCOPE, GoogleIntegrationError, build_service
from thoth_terminal.text import clean_whitespace, extract_text_from_html

_URL_RE = re.compile(r"https?://[^\s<>()\]\[\"']+")
_SUBSTACK_HOST_RE = re.compile(r"(?:^|\.)substack\.com$", re.IGNORECASE)


@dataclass(frozen=True)
class SubstackHeaderScore:
    score: int
    reasons: list[str]


def _header_map(headers: list[dict[str, Any]] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in headers or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip().lower()
        value = str(row.get("value") or "").strip()
        if not name or not value:
            continue
        if name not in out:
            out[name] = value
    return out


def _domain_in_header(value: str) -> bool:
    vals = [value or ""]
    vals += re.findall(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+)", value or "")
    text = (value or "").lower()
    if "substack.com" in text:
        return True
    for v in vals[1:]:
        if _SUBSTACK_HOST_RE.search(v.lower()):
            return True
    return False


def score_substack_headers(headers: dict[str, str]) -> SubstackHeaderScore:
    score = 0
    reasons: list[str] = []
    from_v = headers.get("from", "")
    reply_to_v = headers.get("reply-to", "")
    list_id_v = headers.get("list-id", "") or headers.get("mailing-list", "")
    auth_v = headers.get("authentication-results", "")
    sender_v = headers.get("sender", "")
    return_path_v = headers.get("return-path", "")
    dkim_v = headers.get("dkim-signature", "")

    if _domain_in_header(from_v):
        score += 2
        reasons.append("from")
    if _domain_in_header(reply_to_v):
        score += 2
        reasons.append("reply-to")
    if _domain_in_header(list_id_v) or "substack" in list_id_v.lower():
        score += 2
        reasons.append("list-id")
    if _domain_in_header(sender_v):
        score += 1
        reasons.append("sender")
    if _domain_in_header(return_path_v):
        score += 1
        reasons.append("return-path")
    if "substack" in auth_v.lower() or "substack" in dkim_v.lower():
        score += 1
        reasons.append("auth/dkim")

    return SubstackHeaderScore(score=score, reasons=reasons)


def is_substack_message(headers: dict[str, str], *, min_score: int = 3) -> bool:
    return score_substack_headers(headers).score >= int(min_score)


def _decode_part_data(data_b64url: str | None) -> bytes:
    raw = (data_b64url or "").strip()
    if not raw:
        return b""
    pad = "=" * ((4 - len(raw) % 4) % 4)
    try:
        return base64.urlsafe_b64decode((raw + pad).encode("ascii"))
    except Exception:
        return b""


def _flatten_parts(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    out: list[dict[str, Any]] = [payload]
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            out.extend(_flatten_parts(part))
    return out


def _find_body_html_text(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    html_body = None
    text_body = None
    for part in _flatten_parts(payload):
        mime = str(part.get("mimeType") or "").lower()
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        data = _decode_part_data(body.get("data"))
        if not data:
            continue
        if mime == "text/html" and html_body is None:
            try:
                html_body = data.decode("utf-8", errors="ignore")
            except Exception:
                html_body = None
        elif mime == "text/plain" and text_body is None:
            try:
                text_body = data.decode("utf-8", errors="ignore")
            except Exception:
                text_body = None
    return html_body, text_body


def _extract_urls(text: str | None) -> list[str]:
    if not text:
        return []
    return [m.group(0).strip() for m in _URL_RE.finditer(text)]


def _pick_canonical_url(*, html_body: str | None, text_body: str | None) -> str | None:
    candidates = []
    candidates.extend(_extract_urls(html_body))
    candidates.extend(_extract_urls(text_body))
    seen: set[str] = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        low = url.lower()
        if any(tok in low for tok in ["unsubscribe", "manage-subscription", "/api/v1/track", "cdn.substack.com"]):
            continue
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        if host.endswith("substack.com"):
            return url
    for url in candidates:
        low = url.lower()
        if "unsubscribe" in low:
            continue
        return url
    return None


def _parse_date_to_naive_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _coerce_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _message_internal_date_ms(msg: dict[str, Any]) -> int | None:
    raw = msg.get("internalDate")
    try:
        return int(raw) if raw is not None else None
    except Exception:
        return None


def _publication_name(headers: dict[str, str]) -> str | None:
    from_v = headers.get("from") or ""
    m = re.match(r"\s*([^<]+?)\s*<", from_v)
    if m:
        name = m.group(1).strip().strip('"')
        return name or None
    return None


def _fetch_attachment_bytes(service, *, message_id: str, attachment_id: str) -> bytes:
    if not attachment_id:
        return b""
    res = service.users().messages().attachments().get(userId="me", messageId=message_id, id=attachment_id).execute()
    if not isinstance(res, dict):
        return b""
    return _decode_part_data(res.get("data"))


def _binary_attachments(service, *, message_id: str, payload: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    if not bool(config.get("store_binary_attachments", True)):
        return []
    max_bytes = _coerce_int(config.get("max_attachment_bytes"), 10_000_000)
    store_inline = bool(config.get("store_inline_images", False))
    out: list[dict[str, Any]] = []
    for part in _flatten_parts(payload):
        filename = str(part.get("filename") or "").strip()
        if not filename:
            continue
        mime = str(part.get("mimeType") or "application/octet-stream")
        if (not store_inline) and mime.startswith("image/"):
            continue
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        attach_id = str(body.get("attachmentId") or "").strip()
        data = b""
        if attach_id:
            data = _fetch_attachment_bytes(service, message_id=message_id, attachment_id=attach_id)
        else:
            data = _decode_part_data(body.get("data"))
        if not data:
            continue
        if max_bytes > 0 and len(data) > max_bytes:
            continue
        out.append(
            {
                "kind": "file",
                "filename": filename,
                "content_type": mime,
                "data_bytes": data,
                "original_url": None,
            }
        )
    return out


def _passes_publication_filters(publication: str | None, *, allow: list[str], deny: list[str]) -> bool:
    name = (publication or "").strip().lower()
    if allow:
        if not any(tok.strip().lower() in name for tok in allow if str(tok).strip()):
            return False
    if deny:
        if any(tok.strip().lower() in name for tok in deny if str(tok).strip()):
            return False
    return True


def _label_id_for_name(service, *, label_name: str) -> str | None:
    if not label_name:
        return None
    resp = service.users().labels().list(userId="me").execute()
    labels = resp.get("labels") if isinstance(resp, dict) else []
    wanted = label_name.strip().casefold()
    for row in labels or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("name") or "").strip().casefold() == wanted:
            label_id = str(row.get("id") or "").strip()
            return label_id or None
    return None


def fetch_gmail_substack(config: dict[str, Any], *, source_cursor: dict | None = None) -> dict[str, Any]:
    try:
        service = build_service("gmail", "v1", scopes=[GMAIL_READONLY_SCOPE])
    except GoogleIntegrationError:
        raise
    except Exception as exc:
        raise GoogleIntegrationError(f"Failed to initialize Gmail API client: {exc}") from exc

    cfg = dict(config or {})
    cur = dict(source_cursor or {})
    max_messages = max(1, min(_coerce_int(cfg.get("max_messages_per_run"), 50), 200))
    require_label = bool(cfg.get("require_label", True))
    label_name = str(cfg.get("gmail_label_name") or "Thoth/Substack").strip()
    label_id = str(cfg.get("gmail_label_id") or cur.get("gmail_label_id") or "").strip() or None
    if label_name and not label_id:
        label_id = _label_id_for_name(service, label_name=label_name)
    if require_label and not label_id:
        raise GoogleIntegrationError(
            f"Gmail label not found: {label_name!r}. Create the label and route Substack mail into it."
        )

    user_query = str(cfg.get("gmail_query") or "").strip()
    query_parts: list[str] = []
    if user_query:
        query_parts.append(user_query)
    last_ms = _coerce_int(cur.get("last_internal_date_ms"), 0)
    overlap_s = max(0, _coerce_int(cur.get("overlap_seconds"), 6 * 3600))
    if last_ms > 0:
        after_s = max(0, int(last_ms // 1000) - overlap_s)
        query_parts.append(f"after:{after_s}")
    q = " ".join([p for p in query_parts if p]).strip() or None

    list_kwargs: dict[str, Any] = {"userId": "me", "maxResults": max_messages}
    if label_id:
        list_kwargs["labelIds"] = [label_id]
    if q:
        list_kwargs["q"] = q
    resp = service.users().messages().list(**list_kwargs).execute()
    msg_rows = resp.get("messages") if isinstance(resp, dict) else []

    allow_publications = [str(x) for x in (cfg.get("allow_publications") or []) if str(x).strip()]
    deny_publications = [str(x) for x in (cfg.get("deny_publications") or []) if str(x).strip()]
    min_score = max(1, min(_coerce_int(cfg.get("substack_min_score"), 3), 10))

    items: list[dict[str, Any]] = []
    max_seen_ms = last_ms
    inspected = 0
    accepted = 0
    for row in (msg_rows or []):
        if not isinstance(row, dict):
            continue
        msg_id = str(row.get("id") or "").strip()
        if not msg_id:
            continue
        full = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        if not isinstance(full, dict):
            continue
        inspected += 1
        payload = full.get("payload") if isinstance(full.get("payload"), dict) else {}
        headers = _header_map(payload.get("headers") if isinstance(payload, dict) else None)
        score = score_substack_headers(headers)
        if score.score < min_score:
            continue
        publication = _publication_name(headers)
        if not _passes_publication_filters(publication, allow=allow_publications, deny=deny_publications):
            continue

        html_body, text_body = _find_body_html_text(payload)
        normalized = ""
        raw_text = text_body or ""
        if html_body:
            try:
                normalized = extract_text_from_html(html_body)
            except Exception:
                normalized = clean_whitespace(text_body or "")
        if not normalized:
            normalized = clean_whitespace(text_body or "")
        if not normalized:
            normalized = clean_whitespace(str(full.get("snippet") or ""))
        subject = headers.get("subject") or None
        canonical_url = _pick_canonical_url(html_body=html_body, text_body=text_body)
        published_at = _parse_date_to_naive_utc(headers.get("date"))
        internal_ms = _message_internal_date_ms(full)
        if internal_ms and internal_ms > max_seen_ms:
            max_seen_ms = internal_ms

        attachments: list[dict[str, Any]] = []
        if html_body and bool(cfg.get("store_email_html_attachment", True)):
            attachments.append(
                {
                    "kind": "html",
                    "filename": f"gmail-{msg_id}.html",
                    "content_type": "text/html",
                    "data_bytes": html_body.encode("utf-8", errors="ignore"),
                    "original_url": canonical_url,
                }
            )
        attachments.extend(_binary_attachments(service, message_id=msg_id, payload=payload, config=cfg))

        items.append(
            {
                "external_id": f"gmail:{msg_id}",
                "url": canonical_url,
                "canonical_url": canonical_url,
                "title": subject,
                "author": publication,
                "published_at": published_at,
                "raw_text": raw_text or normalized,
                "normalized_text": normalized or (subject or msg_id),
                "raw_html": html_body,
                "attachments": attachments,
                "extra": {
                    "connector": "gmail_substack",
                    "gmail_message_id": msg_id,
                    "gmail_thread_id": full.get("threadId"),
                    "gmail_label_id": label_id,
                    "gmail_label_name": label_name or None,
                    "snippet": full.get("snippet"),
                    "substack": {
                        "publication": publication,
                        "score": score.score,
                        "score_reasons": score.reasons,
                        "from": headers.get("from"),
                        "reply_to": headers.get("reply-to"),
                        "list_id": headers.get("list-id") or headers.get("mailing-list"),
                        "sender": headers.get("sender"),
                        "return_path": headers.get("return-path"),
                    },
                },
            }
        )
        accepted += 1

    next_cursor = dict(cur)
    if label_id:
        next_cursor["gmail_label_id"] = label_id
    if max_seen_ms > 0:
        next_cursor["last_internal_date_ms"] = int(max_seen_ms)
    if "overlap_seconds" not in next_cursor:
        next_cursor["overlap_seconds"] = 6 * 3600

    return {
        "items": items,
        "cursor": next_cursor,
        "stats": {
            "connector": "gmail_substack",
            "inspected_messages": inspected,
            "accepted_messages": accepted,
            "label_id": label_id,
            "query": q,
        },
    }
