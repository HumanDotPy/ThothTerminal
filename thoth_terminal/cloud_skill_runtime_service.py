from __future__ import annotations

import hashlib
from typing import Any, Iterable

from sqlalchemy.orm import Session

from thoth_terminal.models import ResearchSkill
from thoth_terminal.research_state_tokens import POLICIES, count_tokens, token_page


STATIC_CLOUD_SKILL_PROTOCOL_VERSION = "thoth-static-skill-protocol-v2"
STATIC_CLOUD_SKILL_PROTOCOL_END_MARKER = "</thoth_static_skill_protocol>"


def _text(value: object) -> str:
    return str(value or "").strip()


def _instruction_text(skill: ResearchSkill) -> str:
    extra = dict(skill.extra or {})
    for key in ("instructions_md", "system_prompt", "prompt"):
        value = _text(extra.get(key))
        if value:
            return value
    return ""


def cloud_skill_metadata(skill: ResearchSkill) -> dict[str, Any]:
    trigger = dict(skill.trigger_json or {})
    instructions = _instruction_text(skill)
    return {
        "id": int(skill.id),
        "slug": skill.slug,
        "name": skill.name,
        "description": skill.description or "",
        "commands": [str(value) for value in trigger.get("commands") or [] if _text(value)],
        "keywords": [str(value) for value in trigger.get("keywords") or [] if _text(value)],
        "modes": [str(value) for value in trigger.get("modes") or [] if _text(value)],
        "implicit": bool(trigger.get("implicit", False)),
        "has_instructions": bool(instructions),
        "updated_at": skill.updated_at.isoformat() if skill.updated_at else None,
    }


def list_cloud_skills(session: Session, *, instructions_only: bool = True, limit: int = 200) -> list[ResearchSkill]:
    rows = (
        session.query(ResearchSkill)
        .filter(ResearchSkill.status == "active")
        .order_by(ResearchSkill.name.asc(), ResearchSkill.id.asc())
        .limit(max(1, min(int(limit or 200), 500)))
        .all()
    )
    return [row for row in rows if _instruction_text(row)] if instructions_only else rows


def render_cloud_skill_catalog(session: Session) -> str:
    rows = [cloud_skill_metadata(row) for row in list_cloud_skills(session)]
    if not rows:
        return "No legacy Thoth static skills are registered. Harness-native skills use the separate Harness skill catalog."
    lines = ["# Available Thoth Skills", ""]
    deferred = 0
    for row in rows:
        triggers = row["commands"] + row["keywords"] + row["modes"]
        suffix = f" Triggers: {', '.join(triggers)}." if triggers else ""
        entry = (f"- `{row['slug']}` — Source: get_state_compaction_source(kind=research_skill, object_id={row['id']}). "
                 f"{row['description']}{suffix}")
        page = token_page(entry, limit_tokens=POLICIES["research_skill"].ceiling_tokens)
        entry = page["text"]
        if page["truncated"]:
            entry += " [Metadata excerpt; retrieve the original skill source.]"
        if count_tokens("\n".join(lines + [entry])) > POLICIES["skill_catalog"].ceiling_tokens - 150:
            deferred += 1
            continue
        lines.append(entry)
    if deferred:
        lines.append(f"[{deferred} skill entries deferred by the catalog token budget; page list_state_compaction_candidates(kinds=['research_skill']) to find omitted skills.]")
    return "\n".join(lines)


def resolve_cloud_skills(
    session: Session,
    *,
    prompt: str = "",
    explicit_slugs: Iterable[str] | None = None,
    mode: str | None = None,
) -> list[ResearchSkill]:
    explicit = {_text(value).lower() for value in explicit_slugs or [] if _text(value)}
    prompt_lower = _text(prompt).lower()
    mode_lower = _text(mode).lower()
    selected: list[ResearchSkill] = []
    for skill in list_cloud_skills(session):
        trigger = dict(skill.trigger_json or {})
        commands = [_text(value).lower() for value in trigger.get("commands") or [] if _text(value)]
        keywords = [_text(value).lower() for value in trigger.get("keywords") or [] if _text(value)]
        modes = [_text(value).lower() for value in trigger.get("modes") or [] if _text(value)]
        command_match = any(prompt_lower == command or prompt_lower.startswith(f"{command} ") for command in commands)
        keyword_match = bool(trigger.get("implicit", False)) and any(value in prompt_lower for value in keywords)
        mode_match = bool(mode_lower) and mode_lower in modes
        if skill.slug.lower() in explicit or command_match or keyword_match or mode_match:
            selected.append(skill)
    missing = explicit - {row.slug.lower() for row in selected}
    if missing:
        raise ValueError(f"Unknown or instruction-less Thoth skill(s): {', '.join(sorted(missing))}")
    return selected


def cloud_skill_snapshot(skills: Iterable[ResearchSkill]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for skill in skills:
        instructions = _instruction_text(skill)
        payload.append(
            {
                **cloud_skill_metadata(skill),
                "instruction_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
            }
        )
    return payload


def _active_skill_views(session: Session | None, skills: list[ResearchSkill]) -> dict[int, dict[str, Any]]:
    if session is not None:
        from thoth_terminal.research_state_compaction_service import render_compacted_states
        return render_compacted_states(session, "research_skill", skills,
            source_contents={int(skill.id): _instruction_text(skill) for skill in skills})
    views = {}
    for skill in skills:
        page = token_page(_instruction_text(skill), limit_tokens=POLICIES["research_skill"].ceiling_tokens)
        views[int(skill.id)] = {"content_md": page["text"], "truncated": page["truncated"]}
    return views


def render_cloud_skill_instructions(skills: Iterable[ResearchSkill], *, session: Session | None = None) -> str:
    rows = [skill for skill in skills if _instruction_text(skill)]
    views = _active_skill_views(session, rows)
    parts: list[str] = []
    deferred = 0
    for skill in rows:
        view = views[int(skill.id)]
        entry = [f"## Skill: {skill.name} (`{skill.slug}`)", "", view["content_md"], "",
                 f"Source: get_state_compaction_source(kind=research_skill, object_id={skill.id}).", ""]
        if view.get("truncated"):
            entry.append("Incomplete procedure view; retrieve remaining source pages before relying on omitted details.")
        if count_tokens("\n".join(parts + entry)) > POLICIES["skill_catalog"].ceiling_tokens - 150:
            deferred += 1
            continue
        parts.extend(entry)
    if deferred:
        parts.append(f"[{deferred} selected skill bodies deferred by the catalog token budget; retrieve exact skill sources before applying omitted methods.]")
    return "\n".join(parts).strip() or "No Thoth skill body was activated for this run."


def compile_static_cloud_skill_protocol(session: Session) -> tuple[str, dict[str, Any]]:
    """Render token-bounded active skill views; complete sources remain retrievable."""
    skills = sorted(
        list_cloud_skills(session, limit=500),
        key=lambda row: (_text(row.slug).casefold(), int(row.id)),
    )
    parts = [
        f'<thoth_static_skill_protocol version="{STATIC_CLOUD_SKILL_PROTOCOL_VERSION}">',
        "# Static Thoth Skill Protocol",
        "",
        (
            "This is a token-bounded active view of registered research skills. "
            "Triggers are routing metadata only: apply the relevant methods, and keep the governing "
            "system response contract authoritative."
        ),
        "",
    ]
    if not skills:
        parts.extend(["No legacy Thoth static skills are registered. Harness-native skills use the separate Harness skill catalog.", ""])
    deferred = []
    catalog_limit = POLICIES["skill_catalog"].ceiling_tokens
    views = _active_skill_views(session, skills)
    for skill in skills:
        view = views[int(skill.id)]
        body = view["content_md"]
        entry = [f"## Skill: {skill.name} (`{skill.slug}`)", "",
                 f"Source: get_state_compaction_source(kind=research_skill, object_id={skill.id}).",
                 f"Description: {_text(skill.description) or 'No description provided.'}", "", body, ""]
        if view and view.get("truncated"):
            entry.append("Incomplete source view: retrieve remaining token pages before relying on omitted procedure details.")
        if count_tokens("\n".join(parts + entry)) > catalog_limit - 100:
            deferred.append(int(skill.id))
            continue
        parts.extend(entry)
    if deferred:
        parts.extend([f"[{len(deferred)} skill bodies deferred by the catalog token ceiling. "
                      "Page list_state_compaction_candidates(kinds=['research_skill']) and load relevant exact sources.]", ""])
    parts.append(STATIC_CLOUD_SKILL_PROTOCOL_END_MARKER)
    protocol_md = "\n".join(parts)
    snapshots = cloud_skill_snapshot(skills)
    manifest: dict[str, Any] = {
        "version": STATIC_CLOUD_SKILL_PROTOCOL_VERSION,
        "sha256": hashlib.sha256(protocol_md.encode("utf-8")).hexdigest(),
        "chars": len(protocol_md),
        "reference_tokens": count_tokens(protocol_md),
        "ceiling_tokens": catalog_limit,
        "deferred_skill_ids": deferred,
        "skill_count": len(skills),
        "skill_slugs": [skill.slug for skill in skills],
        "skills": snapshots,
    }
    return protocol_md, manifest


def cloud_skill_runtime_payload(session: Session, *, active: Iterable[ResearchSkill] = ()) -> dict[str, Any]:
    return {
        "catalog": [cloud_skill_metadata(row) for row in list_cloud_skills(session)],
        "active": cloud_skill_snapshot(active),
        "active_slugs": [row.slug for row in active],
    }
