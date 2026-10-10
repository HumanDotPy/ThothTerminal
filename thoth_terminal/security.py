from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Iterable, Protocol


class StartupSecurityError(RuntimeError):
    pass


class SettingsLike(Protocol):
    mode: str
    host: str
    require_auth: bool
    app_auth_token: str | None
    public_build: bool
    public_allow_unauthenticated: bool
    enable_ibkr: bool
    enable_trading: bool
    enable_omori_local_assets: bool


@dataclass(frozen=True)
class SecurityPosture:
    mode: str
    bind_host: str
    local_desktop: bool
    public: bool
    public_build: bool
    require_auth: bool
    broker_enabled: bool
    omori_local_assets_enabled: bool


_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
_TRADING_ROUTE_TOKENS = (
    "order",
    "orders",
    "cancel",
    "exercise",
)


def normalized_mode(value: object) -> str:
    mode = str(value or "local_desktop").strip().lower().replace("-", "_")
    if mode in {"local", "desktop", "private"}:
        return "local_desktop"
    if mode in {"prod", "production", "public_build"}:
        return "public"
    if mode not in {"local_desktop", "public"}:
        raise StartupSecurityError(
            f"Unsupported THOTH_MODE={value!r}; expected local_desktop or public."
        )
    return mode


def is_loopback_host(host: object) -> bool:
    value = str(host or "").strip().lower().strip("[]")
    if value in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def build_security_posture(settings: SettingsLike, *, bind_host: str | None = None) -> SecurityPosture:
    mode = normalized_mode(getattr(settings, "mode", "local_desktop"))
    host = str(bind_host if bind_host is not None else getattr(settings, "host", "127.0.0.1")).strip()
    return SecurityPosture(
        mode=mode,
        bind_host=host or "127.0.0.1",
        local_desktop=mode == "local_desktop",
        public=mode == "public",
        public_build=bool(getattr(settings, "public_build", False)),
        require_auth=bool(getattr(settings, "require_auth", True)),
        broker_enabled=bool(getattr(settings, "enable_ibkr", False)),
        omori_local_assets_enabled=bool(getattr(settings, "enable_omori_local_assets", False)),
    )


def validate_security_posture(
    settings: SettingsLike,
    *,
    bind_host: str | None = None,
    local_asset_refs: Iterable[object] | None = None,
) -> SecurityPosture:
    posture = build_security_posture(settings, bind_host=bind_host)

    if bool(getattr(settings, "enable_trading", False)):
        raise StartupSecurityError("ENABLE_TRADING=true is refused; trading is intentionally disabled.")

    if posture.public and posture.broker_enabled:
        raise StartupSecurityError("Public mode cannot enable IBKR broker integration.")

    if posture.public and posture.omori_local_assets_enabled:
        raise StartupSecurityError("Public mode cannot enable local Omori prototype assets.")

    if posture.public_build:
        if posture.omori_local_assets_enabled:
            raise StartupSecurityError("PUBLIC_BUILD=true cannot use local Omori prototype assets.")
        for ref in local_asset_refs or ():
            text = str(ref or "").replace("\\", "/").lower()
            if "local_assets/" in text or "omori-media-files" in text or "public/omori-local" in text:
                raise StartupSecurityError(
                    f"PUBLIC_BUILD=true cannot reference local-only asset path: {ref}"
                )

    non_loopback = not is_loopback_host(posture.bind_host)
    if posture.local_desktop and non_loopback:
        raise StartupSecurityError(
            f"local_desktop mode must bind to localhost/127.0.0.1, not {posture.bind_host!r}."
        )

    if non_loopback and not posture.require_auth:
        raise StartupSecurityError(
            f"Binding to {posture.bind_host!r} without REQUIRE_AUTH=true is refused."
        )

    public_unauth_ok = bool(getattr(settings, "public_allow_unauthenticated", False))
    if non_loopback and posture.require_auth and not getattr(settings, "app_auth_token", None):
        if not (posture.public and posture.public_build and public_unauth_ok):
            raise StartupSecurityError(
                "Non-localhost deployments with REQUIRE_AUTH=true need THOTH_APP_AUTH_TOKEN, "
                "unless this is an explicitly unauthenticated public landing build."
            )

    if posture.public and (not posture.require_auth) and (not public_unauth_ok):
        raise StartupSecurityError(
            "Public mode without auth requires THOTH_PUBLIC_ALLOW_UNAUTHENTICATED=true."
        )

    if bool(getattr(settings, "newsletter_enabled", False)):
        signing_secret = str(getattr(settings, "newsletter_signing_secret", "") or "")
        if len(signing_secret) < 32:
            raise StartupSecurityError(
                "Newsletter delivery requires THOTH_NEWSLETTER_SIGNING_SECRET with at least 32 characters."
            )
        if not str(getattr(settings, "newsletter_smtp_host", "") or "").strip():
            raise StartupSecurityError(
                "Newsletter delivery requires THOTH_NEWSLETTER_SMTP_HOST."
            )
        sender = str(getattr(settings, "newsletter_from_email", "") or "")
        if "\r" in sender or "\n" in sender or "@" not in sender:
            raise StartupSecurityError("THOTH_NEWSLETTER_FROM_EMAIL is invalid.")

    return posture


def route_path_is_trading_mutation(path: str) -> bool:
    normalized = "/" + str(path or "").strip().lower().strip("/")
    parts = {part for part in normalized.split("/") if part}
    return any(token in parts for token in _TRADING_ROUTE_TOKENS)


def assert_no_trading_routes(routes: Iterable[object]) -> None:
    offenders: list[str] = []
    for route in routes:
        path = str(getattr(route, "path", "") or "")
        if route_path_is_trading_mutation(path):
            offenders.append(path)
    if offenders:
        joined = ", ".join(sorted(set(offenders)))
        raise StartupSecurityError(f"Trading/order route paths are refused: {joined}")


def public_safe_path(path: str, method: str = "GET") -> bool:
    normalized = str(path or "/")
    # Match the decoded ASGI route path without repairing ambiguous paths.
    # Encoded separators are decoded upstream; residual escapes, traversal,
    # backslashes and repeated separators must not become an allowlist bypass.
    if not normalized.startswith("/") or any(token in normalized for token in ("\\", "%", "\x00", "//")):
        return False
    if any(part in {".", ".."} for part in normalized.split("/")):
        return False
    method_norm = str(method or "GET").upper()
    newsletter_paths = {
        "/public/newsletter/subscribe",
        "/public/newsletter/confirm",
        "/public/newsletter/unsubscribe",
    }
    if normalized in newsletter_paths:
        return method_norm in {"GET", "HEAD", "OPTIONS", "POST"}
    if normalized == "/public/api/agents/models" or re.fullmatch(r"/public/api/agents/models/[a-z0-9][a-z0-9:_-]{2,254}", normalized):
        return method_norm in {"GET", "HEAD"}
    if normalized == "/public/api/agents/evidence/pdf" or normalized == "/public/api/agents/proposals":
        return method_norm == "POST"
    if re.fullmatch(r"/public/api/agents/evidence/[1-9][0-9]*(?:/pages/[1-9][0-9]*(?:/image)?)?", normalized):
        return method_norm in {"GET", "HEAD"}
    if re.fullmatch(r"/public/api/agents/proposals/[1-9][0-9]*", normalized):
        return method_norm in {"GET", "HEAD"}
    if re.fullmatch(r"/public/api/agents/requests/[1-9][0-9]*", normalized):
        return method_norm in {"GET", "HEAD"}
    if method_norm not in {"GET", "HEAD", "OPTIONS"}:
        return False
    if normalized in {"/public", "/public/"} or re.fullmatch(
        r"/public/reports/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/(?:reader|artifacts/[1-9][0-9]*/raw)", normalized
    ):
        return True
    if normalized in {
        "/",
        "/favicon.ico",
        "/apple-touch-icon.png",
        "/apple-touch-icon-precomposed.png",
        "/static/public-site.css",
        "/static/public-site.js",
        "/static/icons/thoth-terminal.ico",
        "/static/icons/thoth-terminal-1024.png",
        "/static/icons/thoth-terminal-chatgpt-128.png",
    }:
        return True
    return False
