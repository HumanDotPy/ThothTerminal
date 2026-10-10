"""One-shot credential entry into the pinned, local IBKR Gateway login page."""

from __future__ import annotations

import json
import re

from PySide6.QtCore import QCryptographicHash, QObject, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import (
    QWebEngineCertificateError,
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineSettings,
    QWebEngineUrlRequestInfo,
    QWebEngineUrlRequestInterceptor,
)

from .ibkr_credentials import normalize_gateway_origin


def _same_origin(url: QUrl, origin: QUrl) -> bool:
    return (
        url.isValid()
        and url.scheme() == "https"
        and not url.userInfo()
        and url.host().lower() == origin.host().lower()
        and url.port(443) == origin.port(443)
    )


class _GatewayRequests(QWebEngineUrlRequestInterceptor):
    """Remote static assets may load; credential-bearing requests stay local."""

    def __init__(self, origin: QUrl, parent: QObject):
        super().__init__(parent)
        self._origin = origin
        self.allow_remote_assets = True

    def interceptRequest(self, info: QWebEngineUrlRequestInfo) -> None:
        url = info.requestUrl()
        if _same_origin(url, self._origin):
            return
        asset_types = {
            QWebEngineUrlRequestInfo.ResourceType.ResourceTypeStylesheet,
            QWebEngineUrlRequestInfo.ResourceType.ResourceTypeScript,
            QWebEngineUrlRequestInfo.ResourceType.ResourceTypeImage,
            QWebEngineUrlRequestInfo.ResourceType.ResourceTypeFontResource,
            QWebEngineUrlRequestInfo.ResourceType.ResourceTypeFavicon,
        }
        if not (
            self.allow_remote_assets
            and url.scheme() == "https"
            and not url.userInfo()
            and bytes(info.requestMethod()) == b"GET"
            and info.resourceType() in asset_types
        ):
            info.block(True)


# IBKR's XYZ login template uses this exact shape. A changed/ambiguous form must
# require manual entry rather than guessing which input should receive a secret.
_CHECK_FORM = r"""
const trusted = () => window === window.top && location.origin === ORIGIN &&
    (location.pathname === '/' || location.pathname === '/sso/Login');
if (!trusted()) return 'untrusted';
const visible = el => !!el && el.isConnected && !el.disabled &&
    el.getClientRects().length > 0 && getComputedStyle(el).visibility === 'visible';
const passwords = [...document.querySelectorAll('input[type="password"]')].filter(visible);
if (passwords.length === 0) return 'waiting';
if (passwords.length !== 1) return 'changed';
const password = passwords[0];
const form = password.form;
if (!form || !form.matches('form.xyzform-username') ||
    !password.matches('#xyz-field-password[name="password"]')) return 'changed';
const users = [...form.querySelectorAll('#xyz-field-username[name="username"][type="text"]')]
    .filter(visible);
const buttons = [...form.querySelectorAll('button.xyz-button-login[type="submit"]')]
    .filter(visible);
if (users.length !== 1 || buttons.length !== 1 || users[0].form !== form ||
    buttons[0].form !== form) return 'changed';
const username = users[0], button = buttons[0];
const safeTarget = () => {
    try {
        const action = new URL(button.getAttribute('formaction') ??
            form.getAttribute('action') ?? '', document.baseURI);
        const target = button.getAttribute('formtarget') ?? form.getAttribute('target') ?? '';
        const method = button.getAttribute('formmethod') ?? form.getAttribute('method') ?? 'get';
        return trusted() && action.origin === ORIGIN && !action.username && !action.password &&
            (!target || target === '_self') && method.toLowerCase() === 'post' &&
            form.isConnected && password.form === form && username.form === form &&
            button.form === form;
    } catch (_) { return false; }
};
if (!safeTarget()) return 'untrusted';
"""


class IbkrLoginPage(QWebEnginePage):
    """Submit saved credentials once, leaving IB Key and all 2FA to the user.

    The caller owns an off-the-record profile and closes the page after an
    independently authenticated Gateway status. Call ``cancel`` before closing.
    Neither script results nor console messages are copied into diagnostics.
    """

    credentials_submitted = Signal()
    login_problem = Signal(str)

    def __init__(
        self, profile: QWebEngineProfile, origin: str, certificate_sha256: str,
        parent: QObject | None = None,
    ) -> None:
        normalized = normalize_gateway_origin(origin)
        if not profile.isOffTheRecord():
            raise ValueError("IBKR login requires a private browser profile")
        if re.fullmatch(r"[0-9a-fA-F]{64}", certificate_sha256 or "") is None:
            raise ValueError("IBKR login requires a trusted certificate fingerprint")
        super().__init__(profile, parent)
        self._origin = QUrl(normalized)
        self._origin_text = normalized
        self._fingerprint = certificate_sha256.lower()
        self._credentials: tuple[str, str] | None = None
        self._begun = False
        self._finished = False
        self._injection_started = False
        self._probe_pending = False
        self._requests = _GatewayRequests(self._origin, self)
        self.setUrlRequestInterceptor(self._requests)
        self.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        self.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False)
        self._retry = QTimer(self)
        self._retry.setInterval(250)
        self._retry.timeout.connect(self._probe)
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.setInterval(30_000)
        self._deadline.timeout.connect(self._timeout)
        self.certificateError.connect(self._certificate_error)
        self.loadFinished.connect(self._loaded)
        self.urlChanged.connect(self._url_changed)
        self.renderProcessTerminated.connect(
            lambda *_: self._fail("The IBKR login browser stopped. Open a new login window.")
        )

    def begin_credentials(self, username: str, password: str) -> None:
        """Arm one bounded attempt; additional calls cannot cause a second click."""
        if self._begun or self._finished:
            return
        self._begun = True
        if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
            self._fail("Saved IBKR credentials are unavailable. Save them again.")
            return
        self._credentials = (username, password)
        self._deadline.start()
        self._retry.start()
        if not self.url().isEmpty():
            self._probe()

    def cancel(self) -> None:
        """Drop pending secrets and disable further automated entry."""
        self._finished = True
        self._credentials = None
        self._retry.stop()
        self._deadline.stop()

    def _fail(self, message: str) -> None:
        if self._finished:
            return
        self.cancel()
        self.login_problem.emit(message)

    def _timeout(self) -> None:
        self._fail("The IBKR login form was not found. Continue manually or open a new login window.")

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        if not _same_origin(url, self._origin):
            self._fail("IBKR login left the trusted local Gateway. Open a new login window.")
            return False
        if is_main_frame and self._begun and not self._injection_started and url.path() not in {"/", "/sso/Login"}:
            self._fail("The IBKR login page changed. Continue manually or open a new login window.")
        return True

    def createWindow(self, window_type):
        return None

    def javaScriptConsoleMessage(self, level, message, line_number, source_id) -> None:
        # Page scripts can log input contents or include the injected script in
        # an exception. Never forward those messages to Python logging/stdout.
        pass

    def _certificate_error(self, error: QWebEngineCertificateError) -> None:
        chain = error.certificateChain()
        pinned = bool(chain) and bytes(chain[0].digest(QCryptographicHash.Algorithm.Sha256)).hex() == self._fingerprint
        if (
            not self._finished
            and _same_origin(error.url(), self._origin)
            and error.isOverridable()
            and error.type() == QWebEngineCertificateError.Type.CertificateAuthorityInvalid
            and pinned
        ):
            error.acceptCertificate()
            return
        error.rejectCertificate()
        self._fail("The IBKR Gateway certificate could not be verified. Open a new login window.")

    def _url_changed(self, url: QUrl) -> None:
        if not _same_origin(url, self._origin):
            self._fail("IBKR login left the trusted local Gateway. Open a new login window.")
        elif self._begun and not self._injection_started and url.path() not in {"/", "/sso/Login"}:
            self._fail("The IBKR login page changed. Continue manually or open a new login window.")

    def _loaded(self, ok: bool) -> None:
        if not ok and not self._injection_started:
            self._fail("The IBKR Gateway login page could not load. Open a new login window.")
        elif ok:
            self._probe()

    def _script(self, body: str) -> str:
        return "(() => { const ORIGIN = " + json.dumps(self._origin_text) + ";" + _CHECK_FORM + body + "})()"

    def _probe(self) -> None:
        if self._finished or self._injection_started or self._credentials is None or self._probe_pending:
            return
        if self.url().isEmpty():
            return
        if not _same_origin(self.url(), self._origin) or self.url().path() not in {"/", "/sso/Login"}:
            self._fail("IBKR login left the trusted local Gateway. Open a new login window.")
            return
        self._probe_pending = True
        self.runJavaScript(self._script("return 'ready';"), self._probe_result)

    def _probe_result(self, result) -> None:
        self._probe_pending = False
        if self._finished or self._injection_started or self._credentials is None:
            return
        if result == "waiting" or result is None:
            return
        if result != "ready":
            self._fail("The IBKR login form changed or is unsafe. Continue manually.")
            return
        self._injection_started = True
        self._requests.allow_remote_assets = False
        self._retry.stop()
        self._deadline.stop()
        username, password = self._credentials
        self._credentials = None
        body = """
let values = VALUES;
try {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(username, values[0]); setter.call(password, values[1]);
    values = null;
    for (const input of [username, password]) {
        input.dispatchEvent(new Event('input', {bubbles:true}));
        input.dispatchEvent(new Event('change', {bubbles:true}));
    }
    if (!safeTarget() || !visible(username) || !visible(password) || !visible(button)) {
        setter.call(username, ''); setter.call(password, ''); return 'untrusted';
    }
    button.click();
    return 'submitted';
} catch (_) {
    username.value = ''; password.value = ''; return 'changed';
} finally { values = null; }
""".replace("VALUES", json.dumps([username, password], ensure_ascii=True))
        username = password = ""
        self.runJavaScript(self._script(body), self._submission_result)
        body = ""

    def _submission_result(self, result) -> None:
        if self._finished:
            return
        if result == "submitted":
            self.credentials_submitted.emit()
        else:
            self._fail("The IBKR login form changed or is unsafe. Continue manually.")
