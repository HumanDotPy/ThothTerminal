from __future__ import annotations

from typing import Callable


NativeExtractor = Callable[[str, str | None], str]
NativeDocumentExtractor = Callable[[str, str | None, int], tuple[str, str | None, list[str]]]
NativeTitleExtractor = Callable[[str, str | None], str | None]
NativeImageExtractor = Callable[[str, str | None, int], list[str]]


def _load_native() -> tuple[
    NativeExtractor | None,
    NativeDocumentExtractor | None,
    NativeTitleExtractor | None,
    NativeImageExtractor | None,
]:
    try:
        from _thoth_html import (
            extract_document_from_html,
            extract_image_urls_from_html,
            extract_text_from_html,
            extract_title_from_html,
        )

        return (
            extract_text_from_html,
            extract_document_from_html,
            extract_title_from_html,
            extract_image_urls_from_html,
        )
    except (ImportError, OSError):
        return None, None, None, None


(
    _NATIVE_EXTRACTOR,
    _NATIVE_DOCUMENT_EXTRACTOR,
    _NATIVE_TITLE_EXTRACTOR,
    _NATIVE_IMAGE_EXTRACTOR,
) = _load_native()


def native_available() -> bool:
    return _NATIVE_EXTRACTOR is not None


def extract_native(html: str, *, url: str | None = None) -> str:
    if _NATIVE_EXTRACTOR is None:
        raise RuntimeError("Thoth native HTML extension is not installed")
    return _NATIVE_EXTRACTOR(html, url)


def extract_document_native(
    html: str, *, url: str | None = None, image_limit: int = 3
) -> tuple[str, str | None, list[str]]:
    if _NATIVE_DOCUMENT_EXTRACTOR is None:
        raise RuntimeError("Thoth native combined HTML extension is not installed")
    return _NATIVE_DOCUMENT_EXTRACTOR(html, url, max(0, int(image_limit)))


def extract_title_native(html: str, *, url: str | None = None) -> str | None:
    if _NATIVE_TITLE_EXTRACTOR is None:
        raise RuntimeError("Thoth native HTML title extractor is not installed")
    return _NATIVE_TITLE_EXTRACTOR(html, url)


def extract_images_native(html: str, *, url: str | None = None, limit: int = 3) -> list[str]:
    if _NATIVE_IMAGE_EXTRACTOR is None:
        raise RuntimeError("Thoth native HTML image extractor is not installed")
    return _NATIVE_IMAGE_EXTRACTOR(html, url, max(0, int(limit)))
