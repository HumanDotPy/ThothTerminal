from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from thoth_terminal.config import ensure_data_dirs, settings


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _path_for_sha256(sha256_hex: str, *, ext: str) -> Path:
    prefix1 = sha256_hex[:2]
    prefix2 = sha256_hex[2:4]
    filename = f"{sha256_hex}{ext}"
    return Path(prefix1) / prefix2 / filename


def guess_extension(*, content_type: str | None, url: str | None = None) -> str:
    if content_type:
        ctype = content_type.split(";", 1)[0].strip()
        ext = mimetypes.guess_extension(ctype) or ""
        if ext:
            return ext
    if url:
        suffix = Path(url.split("?", 1)[0]).suffix
        if suffix and len(suffix) <= 6:
            return suffix
    return ""


def put_bytes(data: bytes, *, ext: str) -> tuple[str, str]:
    ensure_data_dirs()
    sha = _sha256_bytes(data)
    rel = _path_for_sha256(sha, ext=ext)
    abs_path = settings.objects_dir / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    if not abs_path.exists():
        abs_path.write_bytes(data)
    return (str(rel).replace("\\", "/"), sha)


def abs_path(object_path: str) -> Path:
    return settings.objects_dir / object_path

