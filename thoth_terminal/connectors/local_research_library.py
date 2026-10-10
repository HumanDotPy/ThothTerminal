from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from thoth_terminal.config import settings
from thoth_terminal.runtime_env import normalize_path, repo_root
from thoth_terminal.text import clean_whitespace, extract_text_from_html, extract_text_from_pdf


SUPPORTED_RESEARCH_EXTENSIONS = {".pdf", ".md", ".markdown", ".txt", ".html", ".htm"}
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SOURCE_TIER_PRIMARY = "primary/company/regulatory/customer data"
_SOURCE_TIER_TECHNICAL = "technical paper/conference material"
_SOURCE_TIER_INSTITUTIONAL = "institutional analyst synthesis"
_SOURCE_TIER_VETTED_RELAY = "vetted specialist analyst relay"
_SOURCE_TIER_REPORT_MEMORY = "thoth report memory"
_SOURCE_TIER_GAP = "gap/speculation"


def _safe_title_from_filename(path: Path) -> str:
    return path.stem.replace("_", " ").replace("-", " ").strip() or path.name


def _title_from_text(text: str, *, fallback: str) -> str:
    for line in text.splitlines():
        cleaned = clean_whitespace(line).lstrip("#").strip()
        if len(cleaned) >= 8:
            return cleaned[:240]
    return fallback[:240]


def _file_url(path: Path) -> str:
    return path.resolve(strict=False).as_uri()


def _file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_research_file(path: Path) -> tuple[str, list[dict[str, Any]]]:
    data = path.read_bytes()
    ext = path.suffix.lower()
    attachments: list[dict[str, Any]] = []
    if ext == ".pdf":
        text = extract_text_from_pdf(data)
        attachments.append(
            {
                "kind": "pdf",
                "original_url": _file_url(path),
                "content_type": "application/pdf",
                "filename": path.name,
                "ext": ".pdf",
                "data_bytes": data,
            }
        )
        return text, attachments
    raw = data.decode("utf-8", errors="ignore")
    if ext in {".html", ".htm"}:
        return extract_text_from_html(raw, url=_file_url(path)), attachments
    return clean_whitespace(raw), attachments


def _published_at(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except Exception:
        return None


def _absolute_http_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return raw


def _manifest_publication_date(metadata: dict[str, Any]) -> datetime | None:
    raw = str(metadata.get("published_at") or metadata.get("publication_date") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _manifest_source_tier(metadata: dict[str, Any]) -> tuple[str | None, str | None]:
    """Classify a hash-verified manifest source from explicit or source semantics.

    The caller must only pass trusted manifest metadata. This keeps a declared
    tier, publisher name, or rights label from influencing provenance when the
    local bytes or publisher URL do not validate.
    """

    declared = clean_whitespace(str(metadata.get("source_tier") or ""))
    if declared:
        lowered = declared.lower()
        canonical = {
            _SOURCE_TIER_PRIMARY,
            _SOURCE_TIER_TECHNICAL,
            _SOURCE_TIER_INSTITUTIONAL,
            _SOURCE_TIER_VETTED_RELAY,
            _SOURCE_TIER_REPORT_MEMORY,
            _SOURCE_TIER_GAP,
        }
        if lowered in canonical:
            return lowered, "manifest_declared"
        if any(token in lowered for token in ("official", "issuer", "vendor", "regulatory", "sec filing", "company")):
            return _SOURCE_TIER_PRIMARY, "manifest_declared"
        if any(token in lowered for token in ("conference", "proceeding", "academic", "peer-reviewed", "technical paper")):
            return _SOURCE_TIER_TECHNICAL, "manifest_declared"
        if "institutional" in lowered:
            return _SOURCE_TIER_INSTITUTIONAL, "manifest_declared"
        if any(token in lowered for token in ("relay", "specialist", "analyst")):
            return _SOURCE_TIER_VETTED_RELAY, "manifest_declared"

    surface = " ".join(
        clean_whitespace(str(metadata.get(key) or "")).lower()
        for key in ("rights_access", "title", "publisher", "url")
    )
    official_markers = (
        "issuer-hosted",
        "issuer hosted",
        "issuer release",
        "official source",
        "joint technical release",
        "official technology overview",
        "official technical overview",
        "sec filing",
        "regulatory filing",
        "annual report",
        "annual-report",
        "form 10-k",
        "form 10-q",
        "form 20-f",
        "vendor white paper",
        "vendor technical article",
        "vendor technical note",
        "vendor brochure",
        "vendor document",
        "vendor product",
        "company white paper",
        "company technical article",
        "company technical note",
        "company technology overview",
        "company product overview",
        "press release",
        "news release",
    )
    if any(marker in surface for marker in official_markers):
        return _SOURCE_TIER_PRIMARY, "manifest_inferred_official"

    technical_markers = (
        "conference paper",
        "conference proceeding",
        "proceedings paper",
        "peer-reviewed paper",
        "academic paper",
        "author-hosted conference",
        "author hosted conference",
    )
    if any(marker in surface for marker in technical_markers):
        return _SOURCE_TIER_TECHNICAL, "manifest_inferred_technical"
    return None, None


def _research_lifecycle(config: dict[str, Any]) -> dict[str, Any]:
    full_days = int(config.get("full_refresh_days") or getattr(settings, "research_report_full_refresh_days", 90) or 90)
    extension_days = int(config.get("addendum_extension_days") or getattr(settings, "research_report_addendum_extension_days", 45) or 45)
    return {
        "full_refresh_days": max(1, full_days),
        "addendum_extension_days": max(0, extension_days),
        "update_policy": "quarterly_full_refresh_with_addenda_extensions",
        "memory_role": "compiled_research",
    }


def _source_manifest(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Load optional acquisition metadata without making a manifest mandatory.

    Hard-document packs use ``manifest.json`` to preserve publisher URLs,
    access/rights posture, hashes, and retrieval outcomes. The stable local
    file URI remains the document identity; a hash-verified publisher URL is
    retained separately as the preferred citation route.
    """

    path = root / "manifest.json"
    if not path.is_file():
        return {}, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, {}
    if not isinstance(payload, dict):
        return {}, {}
    by_path: dict[str, dict[str, Any]] = {}
    for row in payload.get("documents") or []:
        if not isinstance(row, dict):
            continue
        local_path = str(row.get("local_path") or "").strip()
        if not local_path or Path(local_path).name != local_path:
            continue
        by_path[local_path.casefold()] = dict(row)
    pack = {
        key: payload.get(key)
        for key in ("pack", "access_date", "scope")
        if payload.get(key) not in (None, "")
    }
    return by_path, pack


def _subject_hints(path: Path, title: str, config: dict[str, Any]) -> list[str]:
    explicit = config.get("subject_hints")
    if isinstance(explicit, list):
        hints = [str(item).strip() for item in explicit if str(item).strip()]
        if hints:
            return hints
    hay = f"{path.name} {title}".lower()
    hints: list[str] = []
    if any(token in hay for token in ("memory", "hbm", "dram", "nand", "storage")):
        hints.extend(["memory", "HBM", "DRAM", "NAND", "AI memory hierarchy"])
    if any(token in hay for token in ("inference", "serving", "interactivity", "latency")):
        hints.extend(["premium inference", "interactive inference", "AI serving architecture"])
    if any(token in hay for token in ("packaging", "cowos", "substrate", "cpo")):
        hints.extend(["advanced packaging", "AI supply chain"])
    return sorted(set(hints), key=str.lower)


def _iter_files(root: Path, *, recursive: bool, patterns: list[str]) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        iterator = root.rglob(pattern) if recursive else root.glob(pattern)
        for path in iterator:
            if path.is_file() and path.suffix.lower() in SUPPORTED_RESEARCH_EXTENSIONS:
                files.append(path)
    return sorted(set(files), key=lambda p: (p.stat().st_mtime if p.exists() else 0, str(p).lower()), reverse=True)


def fetch_local_research_library(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    raw_dir = cfg.get("library_dir") or cfg.get("inbox_dir") or getattr(settings, "research_library_inbox_dir", None)
    root = normalize_path(raw_dir or "research/inbox", base_dir=repo_root())
    root.mkdir(parents=True, exist_ok=True)
    patterns = cfg.get("patterns") if isinstance(cfg.get("patterns"), list) else ["*.pdf", "*.md", "*.markdown", "*.txt", "*.html", "*.htm"]
    recursive = bool(cfg.get("recursive", False))
    max_files = max(1, int(cfg.get("max_files_per_run") or getattr(settings, "research_library_max_files_per_run", 50) or 50))
    lifecycle = _research_lifecycle(cfg)
    default_author = str(cfg.get("default_author") or "Deep Research / Human").strip() or None
    manifest_by_path, manifest_pack = _source_manifest(root)
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for path in _iter_files(root, recursive=recursive, patterns=[str(pattern) for pattern in patterns])[:max_files]:
        try:
            data = path.read_bytes()
            digest = _file_sha256(data)
            text, attachments = _read_research_file(path)
            normalized = clean_whitespace(text)
            fallback_title = _safe_title_from_filename(path)
            title = _title_from_text(normalized, fallback=fallback_title)
            source_path = str(path.resolve(strict=False))
            source_file_url = _file_url(path)
            manifest_entry = manifest_by_path.get(path.name.casefold(), {})
            declared_sha256 = str(manifest_entry.get("sha256") or "").strip()
            manifest_hash_matches = bool(_SHA256_RE.fullmatch(declared_sha256)) and declared_sha256.casefold() == digest
            publisher_url = _absolute_http_url(manifest_entry.get("url")) if manifest_hash_matches else None
            manifest_trusted = bool(manifest_entry) and manifest_hash_matches and publisher_url is not None
            source_metadata = manifest_entry if manifest_trusted else {}
            manifest_source_tier, manifest_source_tier_basis = (
                _manifest_source_tier(source_metadata) if manifest_trusted else (None, None)
            )
            # Use the local route as stable identity across byte changes and
            # manifest trust transitions. A verified publisher URL remains the
            # preferred citation URL, not the dedupe key.
            canonical_url = source_file_url
            citation_url = publisher_url or source_file_url
            # A manifest denotes an acquired source. Its access/download time is
            # not evidence of publication time, so preserve an explicit unknown.
            published = _manifest_publication_date(source_metadata) if manifest_trusted else _published_at(path)
            if not manifest_entry:
                manifest_validation = "not_present"
            elif not _SHA256_RE.fullmatch(declared_sha256):
                manifest_validation = "invalid_or_missing_sha256"
            elif not manifest_hash_matches:
                manifest_validation = "sha256_mismatch"
            elif publisher_url is None:
                manifest_validation = "invalid_publisher_url"
            else:
                manifest_validation = "sha256_match_http_url"
            item_extra = {
                "research_library": True,
                "content_kind": "research_report",
                "source_path": source_path,
                "file_name": path.name,
                "file_extension": path.suffix.lower(),
                "file_sha256": digest,
                "local_source_url": source_file_url,
                "subject_hints": _subject_hints(path, title, cfg),
                "report_lifecycle": dict(lifecycle),
                "report_update_status": "indexed",
                # These keys are connector-owned and intentionally emitted on
                # every refresh so stale trusted provenance cannot survive a
                # later mismatch, invalid URL, or removed manifest.
                "manifest_provenance_trusted": manifest_trusted,
                "source_manifest_validation": manifest_validation,
                "source_url": publisher_url if manifest_trusted else None,
                "source_publisher": source_metadata.get("publisher") if manifest_trusted else None,
                "source_tier": manifest_source_tier,
                "source_tier_basis": manifest_source_tier_basis,
                "rights_access": source_metadata.get("rights_access") if manifest_trusted else None,
                "retrieval_result": source_metadata.get("retrieval_result") if manifest_trusted else None,
                "source_pack": manifest_pack if manifest_trusted else {},
                "source_manifest": {},
                "accessed_at": (
                    manifest_entry.get("access_date") or manifest_pack.get("access_date")
                    if manifest_trusted
                    else None
                ),
                "publication_date_known": (published is not None) if manifest_trusted else None,
            }
            if manifest_trusted:
                item_extra.update(
                    {
                        "source_manifest": {
                            key: source_metadata.get(key)
                            for key in (
                                "title",
                                "publisher",
                                "rights_access",
                                "url",
                                "retrieval_result",
                                "sha256",
                                "size_bytes",
                                "pdf_magic_valid",
                                "published_at",
                                "publication_date",
                                "source_tier",
                            )
                            if source_metadata.get(key) is not None
                        },
                    }
                )
                title = str(source_metadata.get("title") or title).strip()[:240] or title
            items.append(
                {
                    "kind": "research_report",
                    # File path is the durable identity; content hashes are
                    # provenance/change detectors, not cross-path identities.
                    "external_id": f"local-research-path:{hashlib.sha256(canonical_url.encode('utf-8')).hexdigest()}",
                    "canonical_url": canonical_url,
                    "url": citation_url,
                    "title": title,
                    "author": default_author,
                    "published_at": published,
                    "raw_text": normalized,
                    "normalized_text": normalized,
                    "attachments": attachments,
                    "extra": item_extra,
                }
            )
        except Exception as exc:
            skipped.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})

    return {
        "items": items,
        "cursor": None,
        "stats": {
            "source_kind": "local_research_library",
            "library_dir": str(root),
            "file_count": len(items),
            "skipped": skipped,
            "supported_extensions": sorted(SUPPORTED_RESEARCH_EXTENSIONS),
            "manifest_entries": len(manifest_by_path),
            "manifest_pack": manifest_pack,
        },
    }
