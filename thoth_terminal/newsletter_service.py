from __future__ import annotations

import base64
import hashlib
import hmac
import html
import re
import smtplib
import time
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any
from urllib.parse import quote

from sqlalchemy.orm import Session

from thoth_terminal.config import settings
from thoth_terminal.db import session_scope, utcnow
from thoth_terminal.models import (
    NewsletterDelivery,
    NewsletterEdition,
    NewsletterSubscriber,
)


NEWSLETTER_DELIVERY_JOB = "newsletter_heartbeat_delivery"
_EMAIL_RE = re.compile(r"^[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+\.[^@\s\x00-\x1f\x7f]+$")


def normalize_email(value: object) -> tuple[str, str]:
    email = str(value or "").strip()
    if len(email) > 320 or "\r" in email or "\n" in email or not _EMAIL_RE.fullmatch(email):
        raise ValueError("invalid email address")
    local, domain = email.rsplit("@", 1)
    try:
        domain_ascii = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("invalid email domain") from exc
    normalized = f"{local.lower()}@{domain_ascii}"
    return email, normalized


def _signing_secret() -> bytes:
    secret = str(getattr(settings, "newsletter_signing_secret", "") or "")
    if len(secret) < 32:
        raise RuntimeError("newsletter signing secret is not configured")
    return secret.encode("utf-8")


def _token(*, subscriber: NewsletterSubscriber, purpose: str, expires_at: int) -> str:
    email_digest = hashlib.sha256(subscriber.email_normalized.encode("utf-8")).hexdigest()
    payload = f"{int(subscriber.id)}:{email_digest}:{purpose}:{int(expires_at)}"
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(_signing_secret(), encoded.encode("ascii"), hashlib.sha256).digest()
    sig_encoded = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{encoded}.{sig_encoded}"


def _verify_token(session: Session, *, token: str, purpose: str) -> NewsletterSubscriber | None:
    try:
        encoded, supplied_sig = str(token or "").split(".", 1)
        expected_sig = base64.urlsafe_b64encode(
            hmac.new(_signing_secret(), encoded.encode("ascii"), hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
        if not hmac.compare_digest(supplied_sig, expected_sig):
            return None
        padding = "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
        subscriber_id, email_digest, token_purpose, expires_at = raw.split(":", 3)
        if token_purpose != purpose or int(expires_at) < int(time.time()):
            return None
        subscriber = session.query(NewsletterSubscriber).filter_by(id=int(subscriber_id)).first()
        expected_email_digest = (
            hashlib.sha256(subscriber.email_normalized.encode("utf-8")).hexdigest()
            if subscriber is not None
            else ""
        )
        if subscriber is None or not hmac.compare_digest(expected_email_digest, email_digest):
            return None
        return subscriber
    except (TypeError, ValueError, UnicodeError):
        return None


def _public_base_url() -> str:
    return str(
        getattr(settings, "newsletter_public_base_url", None)
        or getattr(settings, "public_base_url", None)
        or "http://127.0.0.1:8750"
    ).strip().rstrip("/")


def _smtp_send(message: EmailMessage) -> None:
    host = str(getattr(settings, "newsletter_smtp_host", "") or "").strip()
    if not host:
        raise RuntimeError("newsletter SMTP host is not configured")
    port = int(getattr(settings, "newsletter_smtp_port", 587) or 587)
    timeout = max(2.0, min(float(getattr(settings, "newsletter_smtp_timeout_s", 10.0) or 10.0), 30.0))
    username = str(getattr(settings, "newsletter_smtp_username", "") or "").strip()
    password = str(getattr(settings, "newsletter_smtp_password", "") or "")
    with smtplib.SMTP(host=host, port=port, timeout=timeout) as client:
        client.ehlo()
        if bool(getattr(settings, "newsletter_smtp_starttls", True)):
            client.starttls()
            client.ehlo()
        if username:
            client.login(username, password)
        client.send_message(message)


def _base_message(*, recipient: str, subject: str) -> EmailMessage:
    if any(char in recipient + subject for char in ("\r", "\n")):
        raise ValueError("email header injection refused")
    sender = str(getattr(settings, "newsletter_from_email", "") or "")
    if "\r" in sender or "\n" in sender:
        raise ValueError("email header injection refused")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject[:240]
    reply_to = str(getattr(settings, "newsletter_reply_to", "") or "").strip()
    if reply_to:
        if "\r" in reply_to or "\n" in reply_to:
            raise ValueError("email header injection refused")
        message["Reply-To"] = reply_to
    message["Message-ID"] = make_msgid(domain=recipient.rsplit("@", 1)[-1])
    return message


def request_subscription(
    session: Session,
    *,
    email: object,
    source: str = "public_site",
) -> tuple[NewsletterSubscriber, bool]:
    display_email, normalized = normalize_email(email)
    now = utcnow()
    subscriber = (
        session.query(NewsletterSubscriber)
        .filter(NewsletterSubscriber.email_normalized == normalized)
        .first()
    )
    should_confirm = False
    if subscriber is None:
        subscriber = NewsletterSubscriber(
            email=display_email,
            email_normalized=normalized,
            status="pending",
            source=str(source or "public_site")[:64],
            consent_version="2026-08-03",
            consented_at=now,
            confirmed_at=None,
            unsubscribed_at=None,
            last_confirmation_sent_at=None,
            last_sent_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(subscriber)
        session.flush()
        should_confirm = True
    elif subscriber.status != "active":
        subscriber.email = display_email
        subscriber.status = "pending"
        subscriber.source = str(source or "public_site")[:64]
        subscriber.consented_at = now
        subscriber.unsubscribed_at = None
        subscriber.updated_at = now
        should_confirm = (
            subscriber.last_confirmation_sent_at is None
            or (now - subscriber.last_confirmation_sent_at).total_seconds() >= 300
        )
    return subscriber, should_confirm


def send_confirmation(subscriber_id: int) -> None:
    with session_scope() as session:
        subscriber = session.query(NewsletterSubscriber).filter_by(id=int(subscriber_id)).first()
        if subscriber is None or subscriber.status == "active":
            return
        token = _token(
            subscriber=subscriber,
            purpose="confirm",
            expires_at=int(time.time()) + 7 * 24 * 60 * 60,
        )
        confirm_url = f"{_public_base_url()}/public/newsletter/confirm?token={quote(token)}"
        message = _base_message(
            recipient=subscriber.email,
            subject="Confirm your Thoth heartbeat subscription",
        )
        text = (
            "Confirm your subscription to the Thoth heartbeat:\n\n"
            f"{confirm_url}\n\n"
            "If you did not request this, you can ignore this message."
        )
        message.set_content(text)
        message.add_alternative(
            "<h1>Confirm the Thoth heartbeat</h1>"
            "<p>One concise research dispatch, with the evidence that changed our view.</p>"
            f'<p><a href="{html.escape(confirm_url, quote=True)}">Confirm subscription</a></p>'
            "<p>If you did not request this, you can ignore this message.</p>",
            subtype="html",
        )
        _smtp_send(message)
        subscriber.last_confirmation_sent_at = utcnow()
        subscriber.updated_at = utcnow()


def confirm_subscription(session: Session, *, token: str) -> bool:
    subscriber = _verify_token(session, token=token, purpose="confirm")
    if subscriber is None:
        return False
    now = utcnow()
    subscriber.status = "active"
    subscriber.confirmed_at = subscriber.confirmed_at or now
    subscriber.unsubscribed_at = None
    subscriber.updated_at = now
    return True


def unsubscribe(session: Session, *, token: str) -> bool:
    subscriber = _verify_token(session, token=token, purpose="unsubscribe")
    if subscriber is None:
        return False
    now = utcnow()
    subscriber.status = "unsubscribed"
    subscriber.unsubscribed_at = now
    subscriber.updated_at = now
    return True


def _clean_dispatch_text(value: object, *, limit: int) -> str:
    text = str(value or "").replace("\x00", "").strip()
    return text[:limit]


def enqueue_heartbeat_edition(
    session: Session,
    *,
    run_id: int,
    final: dict[str, Any],
) -> NewsletterEdition | None:
    if not bool(getattr(settings, "newsletter_enabled", False)):
        return None
    existing = session.query(NewsletterEdition).filter_by(source_run_id=int(run_id)).first()
    if existing is not None:
        return existing
    title = _clean_dispatch_text(final.get("title"), limit=200) or "Thoth Research Heartbeat"
    summary = _clean_dispatch_text(final.get("summary"), limit=1600)
    dispatch = _clean_dispatch_text(final.get("dispatch_md"), limit=30000)
    key_points = [
        _clean_dispatch_text(item, limit=600)
        for item in (final.get("key_points") or [])
        if _clean_dispatch_text(item, limit=600)
    ][:8]
    sections = [title]
    if dispatch:
        sections.extend(["", dispatch])
    elif key_points:
        sections.extend(["", *[f"• {point}" for point in key_points]])
    if summary and summary not in dispatch:
        sections.extend(["", "Summary", summary])
    content_text = "\n".join(sections).strip()
    now = utcnow()
    edition = NewsletterEdition(
        source_run_id=int(run_id),
        subject=title[:240],
        preview_text=(summary or dispatch or title)[:320],
        content_text=content_text,
        status="queued",
        queued_at=now,
        sent_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(edition)
    session.flush()
    subscriber_ids = [
        int(row[0])
        for row in session.query(NewsletterSubscriber.id)
        .filter(NewsletterSubscriber.status == "active")
        .all()
    ]
    for subscriber_id in subscriber_ids:
        session.add(
            NewsletterDelivery(
                edition_id=int(edition.id),
                subscriber_id=subscriber_id,
                status="pending",
                attempts=0,
                message_id=None,
                last_error=None,
                sent_at=None,
                created_at=now,
                updated_at=now,
            )
        )
    from thoth_terminal.jobs import enqueue_job

    enqueue_job(
        session,
        kind=NEWSLETTER_DELIVERY_JOB,
        payload={"edition_id": int(edition.id)},
        dedupe_key=f"newsletter_heartbeat_delivery:{int(edition.id)}",
        priority=5,
        max_attempts=5,
    )
    return edition


def _edition_html(*, edition: NewsletterEdition, unsubscribe_url: str) -> str:
    paragraphs = []
    for block in edition.content_text.split("\n\n"):
        escaped = html.escape(block).replace("\n", "<br />")
        paragraphs.append(f"<p>{escaped}</p>")
    return (
        '<div style="max-width:680px;margin:0 auto;font:16px/1.55 Arial,sans-serif;color:#111">'
        '<p style="font:700 12px/1 Arial,sans-serif;letter-spacing:.14em;text-transform:uppercase">'
        "Thoth Research Heartbeat</p>"
        + "".join(paragraphs)
        + '<hr style="border:0;border-top:1px solid #ccc;margin:32px 0" />'
        + f'<p style="font-size:12px;color:#666"><a href="{html.escape(unsubscribe_url, quote=True)}">'
        "Manage or unsubscribe</a></p></div>"
    )


def handle_newsletter_delivery_job(payload: dict[str, Any]) -> None:
    edition_id = int(payload.get("edition_id") or 0)
    if edition_id <= 0:
        return
    with session_scope() as session:
        edition = session.query(NewsletterEdition).filter_by(id=edition_id).first()
        if edition is None:
            return
        delivery_ids = [
            int(row[0])
            for row in session.query(NewsletterDelivery.id)
            .filter(
                NewsletterDelivery.edition_id == edition_id,
                NewsletterDelivery.status.in_(["pending", "error"]),
                NewsletterDelivery.attempts < 5,
            )
            .order_by(NewsletterDelivery.id.asc())
            .all()
        ]
        edition.status = "sending" if delivery_ids else "sent"
        edition.updated_at = utcnow()

    errors: list[str] = []
    for delivery_id in delivery_ids:
        try:
            with session_scope() as session:
                delivery = session.query(NewsletterDelivery).filter_by(id=delivery_id).first()
                if delivery is None or delivery.status == "sent":
                    continue
                edition = session.query(NewsletterEdition).filter_by(id=delivery.edition_id).first()
                subscriber = session.query(NewsletterSubscriber).filter_by(id=delivery.subscriber_id).first()
                if edition is None or subscriber is None or subscriber.status != "active":
                    delivery.status = "skipped"
                    delivery.updated_at = utcnow()
                    continue
                unsubscribe_token = _token(
                    subscriber=subscriber,
                    purpose="unsubscribe",
                    expires_at=int(time.time()) + 10 * 365 * 24 * 60 * 60,
                )
                unsubscribe_url = (
                    f"{_public_base_url()}/public/newsletter/unsubscribe"
                    f"?token={quote(unsubscribe_token)}"
                )
                message = _base_message(recipient=subscriber.email, subject=edition.subject)
                message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
                message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
                message.set_content(f"{edition.content_text}\n\nManage or unsubscribe: {unsubscribe_url}")
                message.add_alternative(
                    _edition_html(edition=edition, unsubscribe_url=unsubscribe_url),
                    subtype="html",
                )
                _smtp_send(message)
                now = utcnow()
                delivery.status = "sent"
                delivery.attempts = int(delivery.attempts or 0) + 1
                delivery.message_id = str(message["Message-ID"] or "")[:255] or None
                delivery.last_error = None
                delivery.sent_at = now
                delivery.updated_at = now
                subscriber.last_sent_at = now
                subscriber.updated_at = now
        except Exception as exc:
            errors.append(f"{delivery_id}:{type(exc).__name__}")
            with session_scope() as session:
                delivery = session.query(NewsletterDelivery).filter_by(id=delivery_id).first()
                if delivery is not None:
                    delivery.status = "error"
                    delivery.attempts = int(delivery.attempts or 0) + 1
                    delivery.last_error = f"{type(exc).__name__}: {exc}"[:1000]
                    delivery.updated_at = utcnow()

    with session_scope() as session:
        edition = session.query(NewsletterEdition).filter_by(id=edition_id).first()
        if edition is not None:
            pending = (
                session.query(NewsletterDelivery.id)
                .filter(
                    NewsletterDelivery.edition_id == edition_id,
                    NewsletterDelivery.status.in_(["pending", "error"]),
                    NewsletterDelivery.attempts < 5,
                )
                .first()
            )
            edition.status = "error" if errors else "sent"
            if pending is None and not errors:
                edition.sent_at = utcnow()
            edition.updated_at = utcnow()
    if errors:
        raise RuntimeError(f"newsletter delivery failed for {len(errors)} recipient(s)")
