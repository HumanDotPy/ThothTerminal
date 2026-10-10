"""Public report presentation reads publication snapshots, never private rails."""
from __future__ import annotations

import re
from pathlib import Path

from markdown_it import MarkdownIt

from thoth_terminal.config import settings


_PUBLIC_MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": True})


def published_report_metadata(version) -> dict[str, str]:
    content = version.content_json if isinstance(version.content_json, dict) else {}
    subject = content.get("subject") if isinstance(content.get("subject"), dict) else {}
    heading = re.search(r"^#\s+(.+)$", str(version.content_md or ""), re.MULTILINE)
    topics = content.get("topics") if isinstance(content.get("topics"), list) else []
    return {
        "title": str(content.get("title") or subject.get("display_name") or (heading.group(1).strip() if heading else "Published research report")),
        "report_type": str(content.get("report_type_label") or content.get("report_type") or "Research"),
        "topic": str(content.get("topic") or content.get("sector") or (topics[0] if topics else "General")),
        "summary": str(content.get("summary") or content.get("executive_summary") or content.get("description") or "Published Thoth research report.").strip()[:360],
    }


def render_published_markdown(markdown_text: str) -> str:
    # [[doc:...]] / [[thesis:...]] remain publication text. Resolving them would
    # inject current private record titles into an already-published snapshot.
    return _PUBLIC_MARKDOWN.render(markdown_text or "")


def published_artifact_path(object_path: str) -> Path | None:
    root = settings.objects_dir.resolve()
    try:
        path = (root / object_path).resolve()
        path.relative_to(root)
    except (ValueError, OSError):
        return None
    return path if path.is_file() else None
