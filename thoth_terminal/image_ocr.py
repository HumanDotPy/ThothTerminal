from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from thoth_terminal.text import clean_whitespace


_WINDOWS_TESSERACT_CANDIDATES = (
    Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
    Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
    Path.home() / "AppData/Local/Programs/Tesseract-OCR/tesseract.exe",
)


@dataclass(frozen=True)
class OCRResult:
    backend: str
    text: str
    detail: str | None = None


def detect_tesseract_cmd() -> str | None:
    explicit = str(os.getenv("THOTH_TESSERACT_CMD") or "").strip()
    if explicit and Path(explicit).exists():
        return explicit
    for candidate in _WINDOWS_TESSERACT_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    return None


def available_ocr_backends() -> list[str]:
    backends: list[str] = []
    if detect_tesseract_cmd():
        backends.append("tesseract")
    return backends


def summarize_ocr_text(text: str, *, max_lines: int = 6, max_chars: int = 600) -> str:
    cleaned = clean_whitespace(text)
    if not cleaned:
        return ""
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if max_lines > 0:
        lines = lines[:max_lines]
    summary = "\n".join(lines).strip()
    if max_chars > 0 and len(summary) > max_chars:
        summary = summary[: max_chars - 1].rstrip() + "…"
    return summary


def extract_text_from_image_path(
    path: str | Path,
    *,
    preferred_backend: str | None = None,
    languages: str = "eng",
) -> OCRResult | None:
    backend = (preferred_backend or "").strip().lower() or None
    if backend not in {None, "tesseract"}:
        return None

    cmd = detect_tesseract_cmd()
    if not cmd:
        return None

    try:
        import pytesseract
    except Exception:
        return None

    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
        if img.mode not in {"L", "RGB"}:
            img = img.convert("RGB")
        text = pytesseract.image_to_string(
            ImageOps.grayscale(img),
            lang=(languages or "eng").strip() or "eng",
            config="--psm 6",
        )
    except Exception:
        return None

    cleaned = clean_whitespace(text)
    if not cleaned:
        return None
    return OCRResult(backend="tesseract", text=cleaned, detail=cmd)


def describe_ocr_status(*, preferred_backend: str | None = None) -> dict[str, Any]:
    available = available_ocr_backends()
    preferred = (preferred_backend or "").strip().lower() or None
    selected = preferred if preferred in available else (available[0] if available else None)
    detail = detect_tesseract_cmd() if selected == "tesseract" else None
    return {
        "available": bool(selected),
        "backend": selected,
        "available_backends": available,
        "detail": detail,
    }
