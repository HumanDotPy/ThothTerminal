from __future__ import annotations

from dataclasses import dataclass
import time
import re

import requests
from sqlalchemy.orm import Session

from thoth_terminal.compat import ensure_cgi
from thoth_terminal.db import utcnow
from thoth_terminal.models import Document


@dataclass(frozen=True)
class TranslationResult:
    backend: str
    text_en: str


GOOGLETRANS_MAX_CHARS = 15_000
GOOGLETRANS_SERVICE_URLS = [
    # Rotate domains to reduce per-host rate limiting.
    "translate.google.com",
    "translate.google.co.kr",
    "translate.google.co.jp",
    "translate.google.com.tw",
]
GOOGLETRANS_FALLBACK_SPLIT_AT = 4_000
GOOGLE_WEB_FALLBACK_MAX_CHARS = 3_500
TRANSLATION_SPLIT_MARK = "<<<THOTH_SPLIT_MARK>>>"

# Heuristic post-processing to fix known brand mistranslations.
_POSTPROCESS_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bhuida\b", re.IGNORECASE), "NVIDIA"),
    (re.compile(r"\bhui\s*da\b", re.IGNORECASE), "NVIDIA"),
)


def _postprocess_translation(text: str | None) -> str | None:
    if not text:
        return text
    out = text
    for pat, repl in _POSTPROCESS_RULES:
        out = pat.sub(repl, out)
    return out


def _chunk_text_by_paragraphs(text: str, *, max_chars: int) -> tuple[list[str], str]:
    if len(text) <= max_chars:
        return ([text], "\n\n")

    sep = "\n\n" if "\n\n" in text else "\n"
    units = [u.strip() for u in text.split(sep) if u.strip()]
    if not units:
        return ([text[:max_chars]], sep)

    chunks: list[str] = []
    current = ""
    for unit in units:
        if len(unit) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(unit), max_chars):
                chunks.append(unit[i : i + max_chars])
            continue

        candidate = unit if not current else f"{current}{sep}{unit}"
        if len(candidate) > max_chars:
            chunks.append(current)
            current = unit
        else:
            current = candidate

    if current:
        chunks.append(current)
    return (chunks, sep)


def _split_for_googletrans(text: str) -> tuple[str, str]:
    mid = len(text) // 2
    # Prefer splitting near the middle on natural boundaries.
    for boundary in ("\n\n", "\n", ". ", "。", " "):
        idx = text.rfind(boundary, 0, mid)
        if idx != -1 and idx >= int(mid * 0.55):
            cut = idx + len(boundary)
            return (text[:cut].strip(), text[cut:].strip())
    return (text[:mid].strip(), text[mid:].strip())


def _googletrans_translate_one(translator, text: str) -> str:
    """
    googletrans may fail on large inputs well below its stated 15k limit (varies by content/lang).

    Strategy:
    - try direct translate with small retry/backoff
    - if we still get the common JSON(None) failure and the input is large, split and recurse
    """

    text = text.strip()
    if not text:
        return ""

    for attempt in range(2):
        try:
            res = translator.translate(text, dest="en")
            return str(res.text)
        except TypeError:
            if attempt == 0:
                time.sleep(0.8)
            else:
                break

    if len(text) <= GOOGLETRANS_FALLBACK_SPLIT_AT:
        # Likely rate-limited/banned or a transient response glitch.
        # One last retry with a longer pause.
        time.sleep(2.0)
        res = translator.translate(text, dest="en")
        return str(res.text)

    left, right = _split_for_googletrans(text)
    left_en = _googletrans_translate_one(translator, left)
    time.sleep(0.25)
    right_en = _googletrans_translate_one(translator, right)
    # Preserve a readable boundary between recursively translated halves.
    return f"{left_en}\n{right_en}".strip()


def _split_combined_translation(text: str) -> tuple[str | None, str | None]:
    if not text:
        return (None, None)
    if TRANSLATION_SPLIT_MARK not in text:
        return (None, None)
    left, right = text.split(TRANSLATION_SPLIT_MARK, 1)
    return (left.strip() or None, right.strip() or None)


def _translate_googletrans(*, title: str | None, body: str) -> tuple[str | None, str]:
    ensure_cgi()
    from googletrans import Translator  # type: ignore

    translator = Translator(service_urls=GOOGLETRANS_SERVICE_URLS)

    if title and title.strip():
        combined = f"{title.strip()}\n\n{TRANSLATION_SPLIT_MARK}\n\n{body.strip()}".strip()
        chunks, sep = _chunk_text_by_paragraphs(combined, max_chars=GOOGLETRANS_MAX_CHARS)
        translated_chunks: list[str] = []
        for idx, chunk in enumerate(chunks):
            translated_chunks.append(_googletrans_translate_one(translator, chunk))
            if idx < len(chunks) - 1:
                time.sleep(0.25)
        translated = sep.join(translated_chunks)
        title_en, body_en = _split_combined_translation(translated)
        if title_en and body_en:
            return (title_en, body_en)

    # Fallback: translate separately.
    title_en = None
    if title and title.strip():
        title_en = _googletrans_translate_one(translator, title.strip())

    chunks, sep = _chunk_text_by_paragraphs(body, max_chars=GOOGLETRANS_MAX_CHARS)
    translated_chunks: list[str] = []
    for idx, chunk in enumerate(chunks):
        translated_chunks.append(_googletrans_translate_one(translator, chunk))
        if idx < len(chunks) - 1:
            time.sleep(0.25)
    return (title_en, sep.join(translated_chunks))


def _translate_title_only_googletrans(title: str) -> str | None:
    if not title or not title.strip():
        return None
    ensure_cgi()
    from googletrans import Translator  # type: ignore

    translator = Translator(service_urls=GOOGLETRANS_SERVICE_URLS)
    try:
        return _googletrans_translate_one(translator, title.strip())
    except Exception:
        return None


def _translate_google_web_one(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    resp = requests.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": text},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    rows = data[0] if isinstance(data, list) and data and isinstance(data[0], list) else []
    translated = "".join(str(row[0] or "") for row in rows if isinstance(row, list) and row)
    if not translated.strip():
        raise RuntimeError("Google web translation returned empty text")
    return translated


def _translate_google_web(*, title: str | None, body: str) -> tuple[str | None, str]:
    title_en = _translate_google_web_one(title) if title and title.strip() else None
    chunks, sep = _chunk_text_by_paragraphs(body, max_chars=GOOGLE_WEB_FALLBACK_MAX_CHARS)
    translated_chunks: list[str] = []
    for idx, chunk in enumerate(chunks):
        translated_chunks.append(_translate_google_web_one(chunk))
        if idx < len(chunks) - 1:
            time.sleep(0.15)
    return (title_en, sep.join(translated_chunks))


def _translate_hunyuan(text: str) -> str:
    # Optional backend (heavy). Install with: `pip install -e .[hunyuan]`
    try:
        from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore
        import torch  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Hunyuan backend not installed. Install extras: pip install -e .[hunyuan]"
        ) from exc

    model_id = "tencent/Hunyuan-MT-7B"

    tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        trust_remote_code=True,
        device_map="auto",
        torch_dtype=torch.float16,
        load_in_4bit=True,
    )

    prompt = text
    inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=2048)
    decoded = tokenizer.decode(out[0], skip_special_tokens=True)
    return decoded


def translate_text_to_english(
    text: str,
    *,
    title: str | None = None,
    backend: str = "googletrans",
) -> TranslationResult:
    """Translate a standalone text fragment into English without mutating a Document."""
    body = str(text or "").strip()
    if not body:
        return TranslationResult(backend=backend, text_en="")
    if backend == "googletrans":
        resolved_backend = "googletrans"
        try:
            _title_en, text_en = _translate_googletrans(title=title, body=body)
        except Exception as first_exc:
            try:
                _title_en, text_en = _translate_google_web(title=title, body=body)
                resolved_backend = "google-web-fallback"
            except Exception as fallback_exc:
                raise RuntimeError(
                    f"googletrans failed: {first_exc}; google web fallback failed: {fallback_exc}"
                ) from fallback_exc
        return TranslationResult(backend=resolved_backend, text_en=_postprocess_translation(text_en) or text_en)
    if backend == "hunyuan":
        text_en = _translate_hunyuan(body)
        return TranslationResult(backend="hunyuan", text_en=_postprocess_translation(text_en) or text_en)
    raise ValueError(f"Unknown translation backend: {backend}")


def translate_document(
    session: Session,
    *,
    document_id: int,
    backend: str = "googletrans",
    commit: bool = True,
) -> TranslationResult:
    doc = session.query(Document).filter_by(id=document_id).first()
    if not doc:
        raise ValueError(f"Document not found: {document_id}")

    if doc.translation_en:
        if doc.title:
            extra = dict(doc.extra or {})
            if not extra.get("translation_title_en"):
                title_en = None
                lang = (doc.language or "").strip().lower()
                if lang.startswith("en"):
                    title_en = doc.title
                elif backend == "googletrans":
                    title_en = _translate_title_only_googletrans(doc.title)
                if title_en:
                    extra["translation_title_en"] = title_en
                    doc.extra = extra
                    if not doc.translation_backend:
                        doc.translation_backend = backend
                    if not doc.translated_at:
                        doc.translated_at = utcnow()
                    if commit:
                        session.commit()
        return TranslationResult(backend=doc.translation_backend or backend, text_en=doc.translation_en)

    if (doc.language or "").lower().startswith("en"):
        doc.translation_en = _postprocess_translation(doc.normalized_text) or doc.normalized_text
        doc.translation_backend = "identity"
        doc.translation_model = None
        doc.translated_at = utcnow()
        if doc.title:
            extra = dict(doc.extra or {})
            extra["translation_title_en"] = _postprocess_translation(doc.title) or doc.title
            doc.extra = extra
        if commit:
            session.commit()
        return TranslationResult(backend="identity", text_en=doc.translation_en)

    if backend == "googletrans":
        resolved_backend = "googletrans"
        try:
            title_en, text_en = _translate_googletrans(title=doc.title, body=doc.normalized_text)
        except Exception as first_exc:
            try:
                title_en, text_en = _translate_google_web(title=doc.title, body=doc.normalized_text)
                resolved_backend = "google-web-fallback"
            except Exception as fallback_exc:
                raise RuntimeError(
                    f"googletrans failed: {first_exc}; google web fallback failed: {fallback_exc}"
                ) from fallback_exc
        title_en = _postprocess_translation(title_en)
        text_en = _postprocess_translation(text_en)
        doc.translation_en = text_en
        doc.translation_backend = resolved_backend
        doc.translation_model = None
        doc.translated_at = utcnow()
        if title_en:
            extra = dict(doc.extra or {})
            extra["translation_title_en"] = title_en
            doc.extra = extra
        if commit:
            session.commit()
        return TranslationResult(backend=resolved_backend, text_en=text_en)

    if backend == "hunyuan":
        text_en = _translate_hunyuan(doc.normalized_text)
        text_en = _postprocess_translation(text_en)
        doc.translation_en = text_en
        doc.translation_backend = "hunyuan"
        doc.translation_model = "tencent/Hunyuan-MT-7B"
        doc.translated_at = utcnow()
        if doc.title:
            extra = dict(doc.extra or {})
            # Hunyuan currently leaves title as-is; still run postprocess.
            extra["translation_title_en"] = _postprocess_translation(doc.title)
            doc.extra = extra
        if commit:
            session.commit()
        return TranslationResult(backend="hunyuan", text_en=text_en)

    raise ValueError(f"Unknown translation backend: {backend}")
