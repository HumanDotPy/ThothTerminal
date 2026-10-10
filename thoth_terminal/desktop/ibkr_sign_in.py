"""Desktop-owned IBKR vault dialog and finite, single-attempt sign-in lifecycle.

No credentials cross the desktop HTTP bridge. Gateway work stays off the Qt
thread; only public certificates, auth booleans and fixed diagnostics return.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import ssl
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtWebEngineCore import QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from thoth_terminal.config import settings
from thoth_terminal.desktop.ibkr_credentials import (
    IbkrCredentials,
    IbkrCredentialStore,
    normalize_gateway_origin,
)
from thoth_terminal.desktop.ibkr_login_page import IbkrLoginPage
from thoth_terminal.integrations.ibkr.gateway import (
    gateway_login_url,
    gateway_process_ids,
    launch_gateway_if_needed,
)
from thoth_terminal.runtime_processes import run_hidden_process
from thoth_terminal.security import normalized_mode


def credential_status() -> dict[str, object]:
    """Presence only; never decode the vault's secret blob for status."""
    supported = os.name == "nt" and normalized_mode(settings.mode) == "local_desktop"
    saved = False
    if supported:
        try:
            saved = IbkrCredentialStore(gateway_login_url()).exists()
        except (OSError, ValueError, RuntimeError):
            pass
    return {"supported": supported, "credentials_saved": saved}


def _connection(origin: str) -> http.client.HTTPSConnection:
    target = urlsplit(normalize_gateway_origin(origin))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return http.client.HTTPSConnection(target.hostname, target.port or 443,
                                      timeout=4, context=context)


def gateway_certificate(origin: str) -> str:
    """Public certificate only; first trust is a native user decision."""
    connection = _connection(origin)
    try:
        connection.connect()
        return hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
    finally:
        connection.close()


def _gateway_json(origin: str, fingerprint: str, path: str, *, payload=None) -> dict:
    """Pin TLS before sending a request; keep Gateway responses bounded/private."""
    connection = _connection(origin)
    try:
        connection.connect()
        actual = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
        if actual != fingerprint:
            raise ValueError("Gateway certificate changed")
        headers = {
            "User-Agent": "ThothTerminal/IBKR",
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        connection.request("GET" if payload is None else "POST", path,
                           body=None if payload is None else json.dumps(payload), headers=headers)
        response = connection.getresponse()
        body = response.read(8193)
        if response.status != 200 or len(body) > 8192:
            raise ValueError("Gateway response could not be verified")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("Gateway response could not be verified")
        return value
    finally:
        connection.close()


def gateway_authenticated(origin: str, fingerprint: str) -> bool:
    """Read the brokerage session, rather than treating SSO as completion."""
    return _gateway_json(origin, fingerprint, "/v1/api/iserver/auth/status").get("authenticated") is True


def _sso_authenticated(origin: str, fingerprint: str) -> bool:
    # SSO responses can contain sensitive fields. Only this boolean escapes.
    return _gateway_json(origin, fingerprint, "/v1/api/sso/validate").get("RESULT") is True


def _completion_status(origin: str, fingerprint: str, *, initialize: bool,
                       stop: threading.Event) -> dict:
    if stop.is_set():
        return {"authenticated": False}
    status = _gateway_json(origin, fingerprint, "/v1/api/iserver/auth/status")
    result = {"authenticated": status.get("authenticated") is True,
              "competing": status.get("competing") is True}
    if result["authenticated"] or not initialize:
        return result
    if not _sso_authenticated(origin, fingerprint) or stop.is_set():
        return result
    result.update(sso_authenticated=True, initialization_attempted=True)
    try:
        initialized = _gateway_json(origin, fingerprint, "/v1/api/iserver/auth/ssodh/init",
                                    payload={"publish": True, "compete": True})
        result.update(authenticated=initialized.get("authenticated") is True,
                      competing=initialized.get("competing") is True)
    except Exception:
        # A timeout has an unknown outcome. Never repeat the mutation silently.
        result["ok"] = False
    return result


def _preflight(origin: str, stop: threading.Event) -> dict[str, object]:
    if stop.is_set():
        return {"cancelled": True}
    result = launch_gateway_if_needed(stop_event=stop)
    if not result.get("ok") or result.get("ready") is False:
        raise RuntimeError("Gateway is not ready")
    if stop.is_set():
        return {"cancelled": True}
    # An arbitrary listener on the configured port must not receive saved
    # credentials. Compare its PID with the existing Gateway process inventory.
    port = urlsplit(origin).port or 443
    probe = run_hidden_process(
        ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
         f"Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique"],
        check=False, timeout_s=5,
    )
    listeners = {int(value) for value in (probe.stdout or "").splitlines() if value.strip().isdigit()}
    if probe.returncode != 0 or not listeners or not listeners.issubset(gateway_process_ids()):
        raise RuntimeError("Gateway listener is not a recognized local IBKR process")
    fingerprint = gateway_certificate(origin)
    authenticated = gateway_authenticated(origin, fingerprint)
    return {"certificate_sha256": fingerprint, "authenticated": authenticated,
            "sso_authenticated": authenticated or _sso_authenticated(origin, fingerprint)}


class IbkrSignInManager(QObject):
    work_finished = Signal(int, str, object)

    def __init__(self, controller, parent=None):
        super().__init__(controller)
        self.controller = controller
        self.window_parent = parent
        self._state = "idle"
        self._message = "Saved sign-in is ready." if credential_status()["credentials_saved"] else "Save credentials in the desktop dialog to use sign-in."
        self._window = None
        self._credential_dialog = None
        self._threads: set[threading.Thread] = set()
        self._generation = 0
        self._stop = threading.Event()
        self._closing = False
        self._confirmed_at: str | None = None
        self.work_finished.connect(self._on_work_finished)

    def status(self):
        return {**credential_status(), "state": self._state, "message": self._message,
                "signed_in_at_utc": self._confirmed_at}

    def _set_state(self, state, message):
        if state == "signed_in":
            self._confirmed_at = datetime.now(timezone.utc).isoformat()
        self._state, self._message = state, message
        if self._window is not None:
            self._window.message.setText(message)
        if state == "signed_in":
            self._work("notify", self._notify_backend)

    def _notify_backend(self):
        """Existing authenticated status rail propagates a verified session."""
        import requests

        base_url = self.controller._base_url
        headers = {}
        if settings.app_auth_token:
            headers["Authorization"] = "Bearer " + settings.app_auth_token
        response = requests.get(base_url.rstrip("/") + "/api/desktop/status",
                                headers=headers, timeout=4, allow_redirects=False, stream=True)
        try:
            return {"notified": response.status_code == 200}
        finally:
            response.close()

    def _work(self, kind, operation):
        generation = self._generation
        def run():
            try:
                result = {"ok": True, **operation()}
            except Exception:
                # Exception text from third-party pages/transport is never a
                # public desktop diagnostic or credential-bearing log entry.
                result = {"ok": False}
            finally:
                self._threads.discard(threading.current_thread())
            if not self._closing:
                self.work_finished.emit(generation, kind, result)
        thread = threading.Thread(target=run, name="ibkr-sign-in-" + kind, daemon=True)
        self._threads.add(thread)
        thread.start()

    def manage_credentials(self):
        if self._closing:
            return
        if self._window is not None or self._state in {"starting", "awaiting_trust"}:
            if self._window is not None:
                self._window.raise_()
                self._window.activateWindow()
            return
        if self._credential_dialog is not None:
            self._credential_dialog.raise_()
            self._credential_dialog.activateWindow()
            return
        if not credential_status()["supported"]:
            self._set_state("unavailable", "Saved IBKR sign-in requires the Windows desktop app.")
            return
        try:
            store = IbkrCredentialStore(gateway_login_url())
            existing = store.load()
        except (OSError, ValueError, RuntimeError):
            self._set_state("failed", "Windows could not open the IBKR credential vault.")
            return
        dialog = QDialog(self.window_parent)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("IBKR saved sign-in")
        layout = QVBoxLayout(dialog)
        intro = QLabel("Credentials are saved in Windows Credential Manager for your Windows user.\nThoth submits them once; you complete IB Key/2FA in the login window.\nIBKR allows one brokerage session. Signing in here, including on launch if enabled, can disconnect another IBKR trading session.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        username = QLineEdit(existing.username if existing else "")
        username.setObjectName("ibkr-native-username")
        password = QLineEdit()
        password.setObjectName("ibkr-native-password")
        password.setEchoMode(QLineEdit.EchoMode.Password)
        password.setPlaceholderText("Enter a password to save or replace credentials")
        form.addRow("Username", username)
        form.addRow("Password", password)
        layout.addLayout(form)
        auto = QCheckBox("Sign in when Thoth launches (IB Key/2FA still applies)")
        auto.setObjectName("ibkr-native-auto")
        auto.setChecked(self.controller.current_preferences().ibkr_sign_in_on_launch)
        layout.addWidget(auto)
        message = QLabel("")
        layout.addWidget(message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        forget = QPushButton("Forget saved credentials")
        forget.setEnabled(existing is not None)
        layout.addWidget(forget)
        existing_pin = existing.certificate_sha256 if existing else ""
        existing = None
        def save():
            try:
                if not username.text().strip() or not password.text():
                    message.setText("Enter both username and password.")
                    return
                store.save(IbkrCredentials(username.text().strip(), password.text(), existing_pin))
            except (OSError, ValueError, RuntimeError):
                message.setText("Windows could not save the credentials.")
                return
            password.clear()
            try:
                self.controller.update_preferences({"ibkr_sign_in_on_launch": auto.isChecked()})
            except (OSError, ValueError, RuntimeError):
                self._set_state("idle", "Credentials saved. The launch preference could not be updated; change it in Integrations.")
                dialog.accept()
                return
            self._set_state("idle", "Credentials saved. Press Sign in to connect.")
            dialog.accept()
        def remove():
            try:
                store.delete()
            except (OSError, ValueError, RuntimeError):
                message.setText("Windows could not remove the saved credential.")
                return
            password.clear()
            try:
                self.controller.update_preferences({"ibkr_sign_in_on_launch": False})
            except (OSError, ValueError, RuntimeError):
                self._set_state("idle", "Credentials removed. Disable the launch preference in Integrations; no saved sign-in can run.")
                dialog.accept()
                return
            self._set_state("idle", "Saved credentials removed; sign-in on launch disabled.")
            dialog.accept()
        def finished(_result):
            password.clear()
            username.clear()
            self._credential_dialog = None
            if self._state == "managing_credentials":
                self._set_state("idle", "Credential dialog closed.")
        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        forget.clicked.connect(remove)
        dialog.finished.connect(finished)
        self._credential_dialog = dialog
        self._set_state("managing_credentials", "Enter credentials in the native desktop dialog.")
        dialog.show()
        dialog.raise_()

    def sign_in(self, *, automatic=False):
        if self._credential_dialog is not None:
            self._credential_dialog.raise_()
            self._credential_dialog.activateWindow()
            return
        if self._window is not None:
            if self._state in {"timed_out", "failed"}:
                self._window.reject()
            else:
                self._window.raise_()
                self._window.activateWindow()
                return
        if self._state in {"starting", "awaiting_trust"} or self._closing:
            return
        if normalized_mode(settings.mode) != "local_desktop" or not settings.enable_ibkr or settings.ibkr_mock_enabled:
            self._set_state("unavailable", "Enable live IBKR in Integrations before signing in.")
            return
        if not credential_status()["credentials_saved"]:
            self._set_state("credentials_missing", "Save IBKR credentials in the desktop dialog first.")
            if not automatic:
                self.manage_credentials()
            return
        try:
            self._origin = normalize_gateway_origin(gateway_login_url())
        except ValueError:
            self._set_state("unavailable", "Saved sign-in requires an HTTPS localhost Gateway URL.")
            return
        self._generation += 1
        self._stop.clear()
        self._set_state("starting", "Starting the local IBKR Gateway...")
        self._work("preflight", lambda: _preflight(self._origin, self._stop))

    @Slot(int, str, object)
    def _on_work_finished(self, generation, kind, result):
        if self._closing or generation != self._generation:
            return
        if kind == "notify":
            return
        if kind == "status":
            window = self._window
            if window is None:
                return
            window.poll_busy = False
            if result.get("initialization_attempted"):
                window.initialization_attempted = True
            if not result["ok"]:
                window.stop_polling()
                self._set_state("failed", "Gateway status could not be verified. Close this window and try again; credentials will not be resubmitted.")
            elif result["authenticated"]:
                self._set_state("signed_in", "IBKR sign-in confirmed.")
                window.accept()
            elif result.get("competing"):
                window.stop_polling()
                self._set_state("failed", "Another IBKR brokerage session is active. Close it, then press Sign in to connect.")
            elif result.get("sso_authenticated"):
                self._set_state("awaiting_approval", "IBKR login approved. Waiting for the brokerage API session...")
            return
        if not result["ok"] or result.get("cancelled"):
            self._set_state("failed", "The local IBKR Gateway could not be verified. Use Open Gateway or try again.")
            return
        try:
            store = IbkrCredentialStore(self._origin)
            credentials = store.load()
            if credentials is None:
                self._set_state("credentials_missing", "Saved credentials are no longer available.")
                return
            fingerprint = result["certificate_sha256"]
            if fingerprint != credentials.certificate_sha256:
                self._set_state("awaiting_trust", "Review the native local Gateway certificate prompt to continue sign-in.")
                choice = QMessageBox.question(
                    self.window_parent, "Trust this local IBKR Gateway?",
                    f"The recognized local Gateway at {self._origin} uses this certificate:\n\nSHA-256: {fingerprint}\n\nTrust this certificate for saved sign-in? A changed certificate will require confirmation again.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if self._closing:
                    return
                if choice != QMessageBox.StandardButton.Yes:
                    self._set_state("cancelled", "Gateway certificate was not trusted; credentials were not submitted.")
                    return
                credentials = IbkrCredentials(credentials.username, credentials.password, fingerprint)
                store.save(credentials)
            if result["authenticated"]:
                self._set_state("signed_in", "An authenticated IBKR session is already available.")
                return
            window = _LoginWindow(self, self._origin, fingerprint, self.window_parent)
            self._window = window
            if result.get("sso_authenticated"):
                self._set_state("awaiting_approval", "IBKR login is already approved. Connecting the brokerage API session...")
                window.timer.start()
                window._poll()
            else:
                self._set_state("loading", "Loading the IBKR login form...")
                window.page.begin_credentials(credentials.username, credentials.password)
            window.view.load(QUrl(self._origin + "/"))
            window.show()
            window.raise_()
            window.activateWindow()
        except (OSError, ValueError, RuntimeError):
            if self._window is not None:
                self._window.reject()
            self._set_state("failed", "The saved credential or login window could not be opened.")

    def shutdown(self):
        self._closing = True
        self._stop.set()
        self._generation += 1
        if self._window is not None:
            self._window.reject()
        if self._credential_dialog is not None:
            self._credential_dialog.reject()
        # Preflight's existing Gateway launcher can wait 35 seconds. Finish
        # this finite call before the controller stops its managed Gateway.
        deadline = time.monotonic() + 45
        for thread in tuple(self._threads):
            thread.join(timeout=max(0, deadline - time.monotonic()))


class _LoginWindow(QDialog):
    def __init__(self, manager, origin, fingerprint, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("IBKR sign-in — approve IB Key/2FA")
        self.resize(850, 760)
        self.manager, self.origin, self.fingerprint = manager, origin, fingerprint
        self.poll_busy = False
        self.initialization_attempted = False
        self.poll_stop = threading.Event()
        self.deadline = time.monotonic() + 180
        layout = QVBoxLayout(self)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.profile = QWebEngineProfile()  # unnamed: off the record
        self.view = QWebEngineView(self)
        self.page = IbkrLoginPage(self.profile, origin, fingerprint, self.view)
        # The profile must outlive its page, including deferred window deletion.
        # This connection retains it until Chromium has released the page.
        profile = self.profile
        self.page.destroyed.connect(lambda: profile.deleteLater())
        self.view.setPage(self.page)
        layout.addWidget(self.view)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self.page.credentials_submitted.connect(self._submitted)
        self.page.login_problem.connect(self._problem)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self._poll)
        self.finished.connect(self._finished)

    def _submitted(self):
        self.manager._set_state("awaiting_approval", "Credentials submitted once. Complete IB Key/2FA in this window or on your phone.")
        self.timer.start()
        self._poll()

    def _problem(self, message):
        self.stop_polling()
        self.manager._set_state("failed", message)

    def _poll(self):
        if time.monotonic() >= self.deadline:
            self.stop_polling()
            self.manager._set_state("timed_out", "Sign-in was not confirmed within three minutes. Press Sign in for a new attempt.")
            return
        if not self.poll_busy:
            self.poll_busy = True
            initialize = not self.initialization_attempted
            self.manager._work("status", lambda: _completion_status(
                self.origin, self.fingerprint, initialize=initialize, stop=self.poll_stop))

    def stop_polling(self):
        self.poll_stop.set()
        self.timer.stop()
        self.page.cancel()

    def _finished(self, _result):
        self.stop_polling()
        self.manager._generation += 1
        if self.manager._state not in {"signed_in", "failed", "timed_out"}:
            self.manager._set_state("cancelled", "Sign-in window closed; no retry will run.")
        self.manager._window = None
        # Explicit page-before-profile teardown releases Chromium state.
        self.page.deleteLater()
