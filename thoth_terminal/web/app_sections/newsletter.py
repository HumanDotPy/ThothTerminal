from __future__ import annotations

import hashlib
import hmac
import html
import logging
from threading import Lock
import time

from thoth_terminal.newsletter_service import (
    confirm_subscription,
    request_subscription,
    send_confirmation,
    unsubscribe,
)
from thoth_terminal.web.app_sections._shared import bind_app_context


bind_app_context(globals())
logger = logging.getLogger("thoth.newsletter")
_attempts: dict[str, list[float]] = {}
_attempts_lock = Lock()


def _limited(key: str, *, limit: int = 3, window_s: int = 3600) -> bool:
    with _attempts_lock:
        now = time.monotonic()
        recent = [stamp for stamp in _attempts.get(key, []) if now - stamp < window_s]
        if len(_attempts) > 5000:
            _attempts.clear()
        if len(recent) >= limit:
            _attempts[key] = recent
            return True
        recent.append(now)
        _attempts[key] = recent
        return False


def _subscription_result(*, ok: bool = True) -> HTMLResponse:
    heading = "Check your inbox" if ok else "Unable to subscribe"
    body = (
        "If this address can be subscribed, a confirmation link will arrive shortly."
        if ok
        else "Please return to the Thoth page and enter a valid email address."
    )
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex,nofollow'><title>Thoth heartbeat</title>"
        "<style>body{margin:0;background:#0a0a0a;color:#faf9f5;font:16px/1.5 Arial,sans-serif}"
        "main{max-width:680px;margin:16vh auto;padding:32px}p{color:#aaa}a{color:#d7ff3f}</style>"
        f"</head><body><main><h1>{heading}</h1><p>{body}</p>"
        "<a href='https://www.agenticsystems.group/thoth'>Return to Thoth</a></main></body></html>",
        status_code=200 if ok else 400,
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"},
    )


@app.post("/public/newsletter/subscribe", response_class=HTMLResponse)
def public_newsletter_subscribe(
    email: str = Form(...),
    company: str = Form(""),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    if company.strip():
        return _subscription_result()
    email_key = hmac.new(
        str(getattr(settings, "newsletter_signing_secret", "") or "unconfigured").encode("utf-8"),
        email.strip().lower().encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if _limited(email_key):
        return _subscription_result()
    try:
        subscriber, should_confirm = request_subscription(db, email=email, source="asg_web")
        db.commit()
        if should_confirm:
            try:
                send_confirmation(int(subscriber.id))
            except Exception as exc:
                logger.warning("newsletter confirmation delivery failed: %s", type(exc).__name__)
        return _subscription_result()
    except ValueError:
        return _subscription_result(ok=False)


@app.get("/public/newsletter/confirm", response_class=HTMLResponse)
def public_newsletter_confirm(token: str, db: Session = Depends(get_session)) -> HTMLResponse:
    ok = confirm_subscription(db, token=token)
    if ok:
        db.commit()
    return _subscription_result(ok=ok)


@app.get("/public/newsletter/unsubscribe", response_class=HTMLResponse)
def public_newsletter_unsubscribe_form(token: str) -> HTMLResponse:
    token_safe = html.escape(str(token or ""), quote=True)
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex,nofollow'><title>Unsubscribe</title></head>"
        "<body style='font:16px Arial,sans-serif;max-width:620px;margin:15vh auto;padding:24px'>"
        "<h1>Leave the Thoth heartbeat?</h1><p>You will stop receiving future editions.</p>"
        "<form method='post'><input type='hidden' name='token' value='"
        + token_safe
        + "'><button type='submit'>Unsubscribe</button></form></body></html>",
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"},
    )


@app.post("/public/newsletter/unsubscribe", response_class=HTMLResponse)
def public_newsletter_unsubscribe(
    request: Request,
    token: str | None = Form(None),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    token_query = str(request.query_params.get("token") or "")
    ok = unsubscribe(db, token=token or token_query)
    if ok:
        db.commit()
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex,nofollow'><title>Unsubscribed</title></head>"
        "<body style='font:16px Arial,sans-serif;max-width:620px;margin:15vh auto;padding:24px'>"
        + ("<h1>Unsubscribed</h1><p>You will not receive future editions.</p>" if ok else "<h1>Link expired</h1><p>This unsubscribe link is invalid or expired.</p>")
        + "</body></html>",
        status_code=200 if ok else 400,
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"},
    )
