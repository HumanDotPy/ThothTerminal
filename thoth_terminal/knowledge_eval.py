from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.models import LocalAgentEvalCase


_QUESTION_RE = re.compile(r"(?m)^Question(?:\s+\d+)?:\s*(.+?)\s*$")
_CHOICE_RE = re.compile(r"(?m)^([A-Z])\.\s+(.+?)\s*$")
_ANSWER_RE = re.compile(r"(?mi)^Correct answers?:\s*([A-Z](?:\s*,\s*[A-Z])*)\s*$")
_EXPLANATION_RE = re.compile(r"(?ms)^Explanation:\s*(.+?)\s*$")


def parse_knowledge_eval_text(text: str) -> list[dict[str, Any]]:
    """Parse the operator's Question/A./Correct answer text format."""
    starts = list(_QUESTION_RE.finditer(str(text or "")))
    cases: list[dict[str, Any]] = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[match.start() : end].strip()
        question = match.group(1).strip()
        answer_match = _ANSWER_RE.search(block)
        choices = {letter: value.strip() for letter, value in _CHOICE_RE.findall(block)}
        if not question or not answer_match or len(choices) < 2:
            continue
        answers = [value.strip().upper() for value in answer_match.group(1).split(",") if value.strip()]
        if not answers or any(answer not in choices for answer in answers):
            continue
        explanation_match = _EXPLANATION_RE.search(block)
        cases.append(
            {
                "question": question,
                "choices": choices,
                "correct_answers": answers,
                "explanation": explanation_match.group(1).strip() if explanation_match else "",
            }
        )
    return cases


def _case_slug(question: str) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:84].strip("-")
    digest = hashlib.sha256(question.encode("utf-8")).hexdigest()[:12]
    return f"knowledge-{words or 'question'}-{digest}"


def deduplicate_knowledge_cases(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Keep the final exact-question occurrence and preserve first-seen ordering."""
    by_slug: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        slug = _case_slug(str(item.get("question") or ""))
        if slug not in by_slug:
            order.append(slug)
        by_slug[slug] = item
    return [by_slug[slug] for slug in order], len(items) - len(order)


def import_knowledge_eval_file(
    session: Session,
    *,
    path: str | Path,
    source_label: str = "operator_knowledge_eval",
) -> dict[str, int]:
    source_path = Path(path).expanduser().resolve(strict=True)
    parsed = parse_knowledge_eval_text(source_path.read_text(encoding="utf-8-sig"))
    unique_items, duplicate_count = deduplicate_knowledge_cases(parsed)
    created = 0
    updated = 0
    now = utcnow()
    slugs = [_case_slug(str(item["question"])) for item in unique_items]
    existing = {
        row.slug: row
        for row in session.query(LocalAgentEvalCase).filter(LocalAgentEvalCase.slug.in_(slugs)).all()
    } if slugs else {}
    for item in unique_items:
        question = str(item["question"])
        slug = _case_slug(question)
        fixture = "\n".join([question, *[f"{key}. {value}" for key, value in item["choices"].items()]])
        expected = {
            "eval_kind": "knowledge_qa",
            "answer_format": "multiple_choice",
            "correct_answers": list(item["correct_answers"]),
            "choices": dict(item["choices"]),
            "explanation": str(item.get("explanation") or ""),
            "source_file": str(source_path),
        }
        row = existing.get(slug)
        if row is None:
            row = LocalAgentEvalCase(
                slug=slug,
                title=question[:255],
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            existing[slug] = row
            created += 1
        else:
            updated += 1
        row.description = "Knowledge question for closed-book and Terminal-assisted local-agent evaluation."
        row.source_label = source_label
        row.fixture_title = question[:512]
        row.fixture_text = fixture
        row.expected_json = expected
        row.tags_json = ["knowledge-eval", "multiple-choice", "paired-terminal-eval"]
        row.active = True
        row.updated_at = now
    session.flush()
    return {
        "parsed": len(parsed),
        "unique": len(unique_items),
        "duplicates": duplicate_count,
        "created": created,
        "updated": updated,
    }


def import_structured_knowledge_eval_file(
    session: Session,
    *,
    path: str | Path,
    source_label: str = "structured_knowledge_eval",
) -> dict[str, int]:
    source_path = Path(path).expanduser().resolve(strict=True)
    payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
    cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(cases, list):
        raise ValueError("structured knowledge eval must be a case array or an object with cases")
    created = 0
    updated = 0
    now = utcnow()
    for index, item in enumerate(cases):
        errors = validate_structured_knowledge_case(item)
        if errors:
            raise ValueError(f"invalid structured knowledge case at index {index}: {'; '.join(errors)}")
        case_key = str(item["case_key"]).strip()
        question = str(item["question"]).strip()
        choices = {str(key).upper(): str(value).strip() for key, value in item["choices"].items()}
        answers = [str(value).upper() for value in item["correct_answers"]]
        slug = f"knowledge-structured-{re.sub(r'[^a-z0-9]+', '-', case_key.lower()).strip('-')}"[:128].rstrip("-")
        row = session.query(LocalAgentEvalCase).filter_by(slug=slug).first()
        if row is None:
            row = LocalAgentEvalCase(slug=slug, title=question[:255], created_at=now, updated_at=now)
            session.add(row)
            created += 1
        else:
            updated += 1
        fixture = "\n".join([question, *[f"{key}. {value}" for key, value in choices.items()]])
        row.title = str(item.get("title") or question)[:255]
        row.description = str(item.get("description") or "Structured source-grounded knowledge evaluation case.")
        row.source_label = source_label
        row.document_id = _single_document_id(item.get("artifacts"))
        row.fixture_title = question[:512]
        row.fixture_text = fixture
        row.expected_json = {
            "eval_kind": "knowledge_qa_multimodal",
            "case_key": case_key,
            "correct_answers": answers,
            "choices": choices,
            "explanation": str(item.get("explanation") or ""),
            "workflow_packet_key": str(item.get("workflow_packet_key") or ""),
            "artifacts": list(item.get("artifacts") or []),
            "evidence_anchors": list(item.get("evidence_anchors") or []),
            "required_modalities": list(item.get("required_modalities") or ["text"]),
            "context_tier": str(item.get("context_tier") or "standard"),
            "difficulty": str(item.get("difficulty") or "hard"),
            "category": str(item.get("category") or "source_grounded"),
            "split": str(item.get("split") or "development"),
            "source_file": str(source_path),
        }
        row.tags_json = sorted({
            "knowledge-eval",
            "structured",
            "source-grounded",
            str(item.get("category") or "source-grounded"),
            str(item.get("difficulty") or "hard"),
            *[f"modality:{value}" for value in item.get("required_modalities") or ["text"]],
        })
        row.active = bool(item.get("active", True))
        row.updated_at = now
    session.flush()
    return {"parsed": len(cases), "created": created, "updated": updated}


def validate_structured_knowledge_case(item: Any) -> list[str]:
    if not isinstance(item, dict):
        return ["case must be an object"]
    errors: list[str] = []
    for key in ("case_key", "question", "workflow_packet_key"):
        if not str(item.get(key) or "").strip():
            errors.append(f"{key} is required")
    choices = item.get("choices")
    if not isinstance(choices, dict) or len(choices) < 2:
        errors.append("choices must contain at least two options")
        choices = {}
    normalized_choices = {str(key).upper() for key in choices}
    answers = item.get("correct_answers")
    if not isinstance(answers, list) or not answers:
        errors.append("correct_answers must be a non-empty array")
    elif any(str(answer).upper() not in normalized_choices for answer in answers):
        errors.append("correct_answers must reference choice keys")
    artifacts = item.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("artifacts must reference at least one packet artifact")
    elif not all(isinstance(value, str) and value.strip() for value in artifacts):
        errors.append("artifact references must be non-empty strings")
    anchors = item.get("evidence_anchors")
    if not isinstance(anchors, list) or not anchors:
        errors.append("evidence_anchors are required")
    modalities = item.get("required_modalities") or ["text"]
    if not isinstance(modalities, list) or not set(modalities).issubset({"text", "image", "table", "chart", "ocr"}):
        errors.append("required_modalities contains an unsupported value")
    if str(item.get("split") or "development") not in {"development", "hidden_validation", "frozen_holdout", "challenge"}:
        errors.append("split is not recognized")
    return errors


def _single_document_id(artifacts: Any) -> int | None:
    values = []
    for value in artifacts if isinstance(artifacts, list) else []:
        match = re.fullmatch(r"document:(\d+)", str(value).strip())
        if match:
            values.append(int(match.group(1)))
    return values[0] if len(set(values)) == 1 else None


def diagnose_paired_knowledge_run(
    *,
    closed_book_correct: bool,
    terminal_correct: bool,
    supporting_evidence_exists: bool | None,
    supporting_evidence_retrieved: bool | None,
    tool_error: bool = False,
) -> str:
    """Classify a paired run without pretending answer accuracy identifies root cause."""
    if terminal_correct:
        return "terminal_uplift" if not closed_book_correct else "both_correct"
    if tool_error:
        return "terminal_tool_failure"
    if supporting_evidence_exists is False:
        return "terminal_coverage_gap"
    if supporting_evidence_exists is True and supporting_evidence_retrieved is False:
        return "terminal_retrieval_failure"
    if supporting_evidence_retrieved is True:
        return "model_reasoning_failure"
    return "unresolved_needs_evidence_audit"
