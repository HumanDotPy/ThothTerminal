"""Desktop-only IBKR secrets in the current user's Windows Credential Manager.

No file or network fallback is provided. Native secret buffers are wiped before
release; Python strings and JSON temporaries cannot reliably be zeroized.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import re
import sys
from ctypes import wintypes
from dataclasses import dataclass, field

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
# https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentialw
_MAX_BLOB_SIZE = 5 * 512
_ORIGIN = re.compile(r"https://(localhost|127\.0\.0\.1|\[::1\])(?::([0-9]{1,5}))?/?", re.I)


class IbkrCredentialError(RuntimeError):
    """Safe native failure containing only an OS error code."""

    def __init__(self, code: int):
        self.code = int(code)
        super().__init__(f"Windows Credential Manager error {self.code}")


class IbkrCredentialUnsupportedError(RuntimeError):
    """The secure credential backend is unavailable on this platform."""


class IbkrCredentialDataError(ValueError):
    """Credential data is invalid, oversized, or has an unsupported schema."""


def normalize_gateway_origin(url: str) -> str:
    """Accept only an exact HTTPS loopback origin, optionally ending in '/'."""
    match = _ORIGIN.fullmatch(url) if isinstance(url, str) else None
    if match is None:
        raise ValueError("Invalid IBKR HTTPS loopback origin")
    host, raw_port = match.groups()
    port = int(raw_port) if raw_port is not None else 443
    if not 1 <= port <= 65535:
        raise ValueError("Invalid IBKR HTTPS loopback origin")
    suffix = f":{port}" if port != 443 else ""
    return f"https://{host.lower()}{suffix}"


@dataclass(frozen=True)
class IbkrCredentials:
    username: str = field(repr=False)
    password: str = field(repr=False)
    certificate_sha256: str = field(default="", repr=False)


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _validate_credentials(credentials: IbkrCredentials) -> None:
    if (
        not isinstance(credentials, IbkrCredentials)
        or not isinstance(credentials.username, str)
        or not credentials.username
        or not isinstance(credentials.password, str)
        or not credentials.password
        or not isinstance(credentials.certificate_sha256, str)
        or (
            credentials.certificate_sha256
            and re.fullmatch(r"[0-9a-fA-F]{64}", credentials.certificate_sha256) is None
        )
    ):
        raise IbkrCredentialDataError("Invalid credential data")


class IbkrCredentialStore:
    """User-scoped, machine-persistent generic Windows credentials."""

    def __init__(self, origin: str, target_prefix: str = "ThothTerminal/IBKR"):
        self.origin = normalize_gateway_origin(origin)
        if (
            not isinstance(target_prefix, str)
            or not target_prefix
            or len(target_prefix) > 512
            or any(ord(char) < 32 for char in target_prefix)
        ):
            raise ValueError("Invalid credential target prefix")
        self._target = f"{target_prefix}/{hashlib.sha256(self.origin.encode('ascii')).hexdigest()}"
        if sys.platform != "win32":
            raise IbkrCredentialUnsupportedError("Windows Credential Manager is required")
        try:
            self._api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        except OSError as exc:
            raise IbkrCredentialError(exc.winerror or 0) from None
        pointer = ctypes.POINTER(_Credential)
        self._api.CredReadW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(pointer)
        ]
        self._api.CredReadW.restype = wintypes.BOOL
        self._api.CredWriteW.argtypes = [pointer, wintypes.DWORD]
        self._api.CredWriteW.restype = wintypes.BOOL
        self._api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._api.CredDeleteW.restype = wintypes.BOOL
        self._api.CredFree.argtypes = [ctypes.c_void_p]
        self._api.CredFree.restype = None

    def _read(self):
        pointer = ctypes.POINTER(_Credential)()
        if not self._api.CredReadW(self._target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            code = ctypes.get_last_error()
            if code == _ERROR_NOT_FOUND:
                return None
            raise IbkrCredentialError(code)
        return pointer

    def _release(self, pointer) -> None:
        try:
            credential = pointer.contents
            if credential.CredentialBlob and credential.CredentialBlobSize:
                ctypes.memset(credential.CredentialBlob, 0, credential.CredentialBlobSize)
        finally:
            self._api.CredFree(pointer)

    def exists(self) -> bool:
        """Check native presence without decoding or copying the secret blob."""
        pointer = self._read()
        if pointer is None:
            return False
        self._release(pointer)
        return True

    def load(self) -> IbkrCredentials | None:
        pointer = self._read()
        if pointer is None:
            return None
        try:
            credential = pointer.contents
            if not 0 < credential.CredentialBlobSize <= _MAX_BLOB_SIZE:
                raise IbkrCredentialDataError("Invalid credential data")
            data = json.loads(ctypes.string_at(
                credential.CredentialBlob, credential.CredentialBlobSize
            ).decode("utf-8"))
            if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 1:
                raise IbkrCredentialDataError("Invalid credential data")
            credentials = IbkrCredentials(
                username=data["username"], password=data["password"],
                certificate_sha256=data["certificate_sha256"],
            )
            _validate_credentials(credentials)
            return credentials
        except (UnicodeError, ValueError, KeyError, TypeError):
            raise IbkrCredentialDataError("Invalid credential data") from None
        finally:
            self._release(pointer)

    def save(self, credentials: IbkrCredentials) -> None:
        _validate_credentials(credentials)
        try:
            blob = bytearray(json.dumps({
                "schema": 1,
                "username": credentials.username,
                "password": credentials.password,
                "certificate_sha256": credentials.certificate_sha256,
            }, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except UnicodeError:
            raise IbkrCredentialDataError("Invalid credential data") from None
        try:
            if len(blob) > _MAX_BLOB_SIZE:
                raise IbkrCredentialDataError("Credential data exceeds Windows limit")
            native_blob = (ctypes.c_ubyte * len(blob)).from_buffer(blob)
            credential = _Credential()
            credential.Type = _CRED_TYPE_GENERIC
            credential.TargetName = self._target
            credential.CredentialBlobSize = len(blob)
            credential.CredentialBlob = native_blob
            credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
            # Keep username in the secret blob, out of readable credential metadata.
            credential.UserName = ""
            if not self._api.CredWriteW(ctypes.byref(credential), 0):
                raise IbkrCredentialError(ctypes.get_last_error())
        finally:
            if blob:
                ctypes.memset((ctypes.c_ubyte * len(blob)).from_buffer(blob), 0, len(blob))

    def delete(self) -> None:
        if not self._api.CredDeleteW(self._target, _CRED_TYPE_GENERIC, 0):
            code = ctypes.get_last_error()
            if code != _ERROR_NOT_FOUND:
                raise IbkrCredentialError(code)
