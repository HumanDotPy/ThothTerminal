from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from thoth_terminal.config import ensure_data_dirs, settings
from thoth_terminal.runtime_env import repo_root

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DEFAULT_SCOPES = [GMAIL_READONLY_SCOPE, DRIVE_READONLY_SCOPE]
STATE_PATH_DEFAULT = Path("data/integrations/google/oauth_state.json")


class GoogleIntegrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class GoogleIntegrationStatus:
    connected: bool
    sdk_available: bool
    oauthlib_available: bool
    client_secrets_exists: bool
    token_path: str
    scopes: list[str]
    expiry: str | None
    has_refresh_token: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "connected": bool(self.connected),
            "sdk_available": bool(self.sdk_available),
            "oauthlib_available": bool(self.oauthlib_available),
            "client_secrets_exists": bool(self.client_secrets_exists),
            "token_path": self.token_path,
            "scopes": list(self.scopes),
            "expiry": self.expiry,
            "has_refresh_token": bool(self.has_refresh_token),
        }


def _state_path() -> Path:
    return (repo_root() / STATE_PATH_DEFAULT).resolve()


def _token_path() -> Path:
    ensure_data_dirs()
    return Path(settings.google_oauth_token_path)


def _client_secrets_path() -> Path:
    return Path(settings.google_oauth_client_secrets_path)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sdk_flags() -> tuple[bool, bool]:
    sdk_available = True
    oauthlib_available = True
    try:
        import google.oauth2.credentials  # noqa: F401
        import google.auth.transport.requests  # noqa: F401
        import googleapiclient.discovery  # noqa: F401
    except Exception:
        sdk_available = False
    try:
        import google_auth_oauthlib.flow  # noqa: F401
    except Exception:
        oauthlib_available = False
    return sdk_available, oauthlib_available


def _require_google_sdk(*, require_oauthlib: bool = False) -> None:
    sdk_available, oauthlib_available = _sdk_flags()
    if not sdk_available:
        raise GoogleIntegrationError(
            "Google API SDK not installed. Install: pip install google-api-python-client google-auth google-auth-oauthlib"
        )
    if require_oauthlib and not oauthlib_available:
        raise GoogleIntegrationError(
            "google-auth-oauthlib not installed. Install: pip install google-auth-oauthlib"
        )


def _sorted_scopes(scopes: list[str] | None = None) -> list[str]:
    raw = list(scopes or DEFAULT_SCOPES)
    out = [str(s).strip() for s in raw if str(s).strip()]
    return sorted(set(out))


def status() -> GoogleIntegrationStatus:
    token_path = _token_path()
    client_secrets = _client_secrets_path()
    sdk_available, oauthlib_available = _sdk_flags()
    token_json = _load_json(token_path) if token_path.exists() else None
    scopes = []
    expiry = None
    has_refresh = False
    connected = False
    if token_json:
        scopes_val = token_json.get("scopes")
        if isinstance(scopes_val, list):
            scopes = [str(s) for s in scopes_val if str(s).strip()]
        exp_val = token_json.get("expiry")
        expiry = str(exp_val) if exp_val else None
        has_refresh = bool(token_json.get("refresh_token"))
        connected = bool(token_json.get("token") or token_json.get("refresh_token"))
    return GoogleIntegrationStatus(
        connected=connected,
        sdk_available=sdk_available,
        oauthlib_available=oauthlib_available,
        client_secrets_exists=client_secrets.exists(),
        token_path=str(token_path),
        scopes=scopes,
        expiry=expiry,
        has_refresh_token=has_refresh,
    )


def disconnect() -> bool:
    token_path = _token_path()
    removed = False
    if token_path.exists():
        try:
            token_path.unlink()
            removed = True
        except Exception as exc:
            raise GoogleIntegrationError(f"Failed to remove token file: {exc}") from exc
    state_path = _state_path()
    if state_path.exists():
        try:
            state_path.unlink()
        except Exception:
            pass
    return removed


def _load_credentials(*, scopes: list[str] | None = None, refresh: bool = True):
    _require_google_sdk()
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2.credentials import Credentials

    token_path = _token_path()
    if not token_path.exists():
        raise GoogleIntegrationError(f"Google OAuth token file not found: {token_path}")

    creds = Credentials.from_authorized_user_file(str(token_path), scopes=_sorted_scopes(scopes))
    if refresh and getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
        creds.refresh(GoogleRequest())
        _save_token_credentials(creds)
    return creds


def _save_token_credentials(creds) -> None:
    _save_json(_token_path(), json.loads(creds.to_json()))


def build_service(service_name: str, version: str, *, scopes: list[str] | None = None):
    _require_google_sdk()
    from googleapiclient.discovery import build

    creds = _load_credentials(scopes=scopes, refresh=True)
    return build(service_name, version, credentials=creds, cache_discovery=False)


def gmail_test() -> dict[str, Any]:
    service = build_service("gmail", "v1", scopes=[GMAIL_READONLY_SCOPE])
    profile = service.users().getProfile(userId="me").execute()
    labels = service.users().labels().list(userId="me").execute()
    label_rows = labels.get("labels") if isinstance(labels, dict) else []
    return {
        "service": "gmail",
        "emailAddress": profile.get("emailAddress") if isinstance(profile, dict) else None,
        "messagesTotal": profile.get("messagesTotal") if isinstance(profile, dict) else None,
        "threadsTotal": profile.get("threadsTotal") if isinstance(profile, dict) else None,
        "labels_count": len(label_rows) if isinstance(label_rows, list) else 0,
    }


def drive_test() -> dict[str, Any]:
    service = build_service("drive", "v3", scopes=[DRIVE_READONLY_SCOPE])
    about = service.about().get(fields="user(displayName,emailAddress)").execute()
    files = service.files().list(pageSize=5, fields="files(id,name,mimeType,modifiedTime)").execute()
    rows = files.get("files") if isinstance(files, dict) else []
    user = about.get("user") if isinstance(about, dict) else {}
    return {
        "service": "drive",
        "user": user if isinstance(user, dict) else {},
        "files_preview": rows if isinstance(rows, list) else [],
    }


def _redirect_uri(base_url: str | None = None) -> str:
    chosen = (base_url or getattr(settings, "google_oauth_redirect_base_url", None) or "").strip()
    if not chosen:
        host = str(getattr(settings, "host", "127.0.0.1"))
        port = int(getattr(settings, "port", 8750) or 8750)
        chosen = f"http://{host}:{port}"
    lower = chosen.lower().rstrip("/")
    if lower.endswith("/settings/google/callback"):
        return chosen.rstrip("/")
    if lower.endswith("/settings"):
        chosen = chosen[: -len("/settings")]
    return chosen.rstrip("/") + "/settings/google/callback"


def start_web_connect(*, base_url: str | None = None, scopes: list[str] | None = None) -> str:
    _require_google_sdk(require_oauthlib=True)
    from google_auth_oauthlib.flow import Flow

    client_path = _client_secrets_path()
    if not client_path.exists():
        raise GoogleIntegrationError(f"Client secrets file not found: {client_path}")

    requested_scopes = _sorted_scopes(scopes)
    redirect_uri = _redirect_uri(base_url)
    flow = Flow.from_client_secrets_file(
        str(client_path),
        scopes=requested_scopes,
        redirect_uri=redirect_uri,
    )
    authorization_url, google_state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    _save_json(
        _state_path(),
        {
            "google_state": google_state,
            "scopes": requested_scopes,
            "redirect_uri": redirect_uri,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return authorization_url


def finish_web_connect(*, state: str, code: str) -> GoogleIntegrationStatus:
    _require_google_sdk(require_oauthlib=True)
    from google_auth_oauthlib.flow import Flow

    saved = _load_json(_state_path())
    if not saved:
        raise GoogleIntegrationError("Missing pending OAuth state. Start connect again.")
    state_raw = str(state or "").strip()
    if not state_raw:
        raise GoogleIntegrationError("Invalid OAuth state.")
    google_state = state_raw
    if google_state != str(saved.get("google_state") or ""):
        raise GoogleIntegrationError("OAuth provider state mismatch.")

    client_path = _client_secrets_path()
    if not client_path.exists():
        raise GoogleIntegrationError(f"Client secrets file not found: {client_path}")
    scopes_val = saved.get("scopes") if isinstance(saved.get("scopes"), list) else DEFAULT_SCOPES
    redirect_uri = str(saved.get("redirect_uri") or _redirect_uri())

    flow = Flow.from_client_secrets_file(
        str(client_path),
        scopes=_sorted_scopes(scopes_val),
        state=google_state,
        redirect_uri=redirect_uri,
    )
    flow.fetch_token(code=code)
    _save_token_credentials(flow.credentials)
    try:
        _state_path().unlink(missing_ok=True)
    except Exception:
        pass
    return status()


def connect_cli(*, scopes: list[str] | None = None, open_browser: bool = True) -> GoogleIntegrationStatus:
    _require_google_sdk(require_oauthlib=True)
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_path = _client_secrets_path()
    if not client_path.exists():
        raise GoogleIntegrationError(f"Client secrets file not found: {client_path}")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), scopes=_sorted_scopes(scopes))
    creds = flow.run_local_server(open_browser=bool(open_browser), access_type="offline", prompt="consent")
    _save_token_credentials(creds)
    return status()
