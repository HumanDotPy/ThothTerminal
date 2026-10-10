from __future__ import annotations

import hashlib
import json
import random
import re
from typing import Any, Iterable

from thoth_terminal.text import clean_whitespace


QA_SCHEMA_VERSION = 1
QA_TASK_MCQ = "mcq"
QA_TASK_MULTI_SELECT = "multi_select"
QA_FORMAT_ANSWER_ONLY = "answer_only"
QA_FORMAT_ANSWER_WITH_EXPLANATION = "answer_with_explanation"
QA_FORMAT_JSON = "json"
QA_FORMAT_EVAL = "eval"
QA_FORMATS = (
    QA_FORMAT_ANSWER_ONLY,
    QA_FORMAT_ANSWER_WITH_EXPLANATION,
    QA_FORMAT_JSON,
    QA_FORMAT_EVAL,
)
QA_LABEL_PAD_TOKEN_ID = -100

_CHOICE_LABELS = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_QUESTION_RE = re.compile(r"(?ms)^Question:\s*(.*?)(?=^Question:\s*|\Z)")
_CHOICE_RE = re.compile(r"(?ms)^([A-Z])\.\s+(.*?)(?=^\s*[A-Z]\.\s+|^Correct answers?:|\Z)")
_CORRECT_RE = re.compile(r"(?mi)^Correct answers?:\s*([A-Z](?:\s*,\s*[A-Z])*)\s*$")
_EXPLANATION_RE = re.compile(r"(?ms)^Explanation:\s*(.*?)\s*$")


def _stable_seed(value: object) -> int:
    digest = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _choice_id(index: int) -> str:
    return f"choice_{index}"


def _answer_ids(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _choice_text(choice: object) -> str:
    if isinstance(choice, dict):
        return clean_whitespace(choice.get("text") or choice.get("answer") or choice.get("value") or "")
    return clean_whitespace(choice)


def normalize_qa_item(row: dict[str, Any], *, item_id: str | None = None) -> dict[str, Any]:
    """Convert a loose QA row into the canonical, unlabelled posttraining shape."""

    question = clean_whitespace(row.get("question") or row.get("prompt") or "")
    raw_choices = row.get("answer_choices")
    if raw_choices is None:
        raw_choices = row.get("choices")
    if not isinstance(raw_choices, list):
        raise ValueError("QA row must include answer_choices or choices as a list.")
    if not question:
        raise ValueError("QA row must include a non-empty question.")

    choices: list[dict[str, Any]] = []
    source_label_to_choice_id: dict[str, str] = {}
    text_to_choice_id: dict[str, str] = {}
    for raw_choice in raw_choices:
        text = _choice_text(raw_choice)
        if not text:
            continue
        choice_id = _choice_id(len(choices))
        source_label = None
        if isinstance(raw_choice, dict):
            source_label = raw_choice.get("source_label") or raw_choice.get("original_label") or raw_choice.get("id")
        source_label_v = clean_whitespace(source_label).upper() if source_label is not None else None
        choice = {"choice_id": choice_id, "text": text}
        if source_label_v:
            choice["source_label"] = source_label_v
            source_label_to_choice_id[source_label_v] = choice_id
        choices.append(choice)
        text_to_choice_id.setdefault(text.lower(), choice_id)
    if len(choices) < 2:
        raise ValueError("QA row must include at least two non-empty choices.")

    raw_answer = row.get("answer")
    if raw_answer is None:
        raw_answer = row.get("correct_choice_ids") or row.get("correct_choice_id")
    valid_choice_ids = {str(choice["choice_id"]) for choice in choices}
    answer_ids: list[str] = []
    for raw in _answer_ids(raw_answer):
        normalized = raw.upper()
        if raw in valid_choice_ids:
            answer_ids.append(raw)
        elif normalized in source_label_to_choice_id:
            answer_ids.append(source_label_to_choice_id[normalized])
        else:
            text_match = text_to_choice_id.get(clean_whitespace(raw).lower())
            if text_match:
                answer_ids.append(text_match)
    answer_ids = [value for idx, value in enumerate(answer_ids) if value in valid_choice_ids and value not in answer_ids[:idx]]
    if not answer_ids:
        raise ValueError("QA row must include at least one answer matching a choice id, source label, or choice text.")

    task_type = clean_whitespace(row.get("task_type") or row.get("task") or "")
    if not task_type:
        task_type = QA_TASK_MULTI_SELECT if len(answer_ids) > 1 else QA_TASK_MCQ
    elif task_type in {"research_mcq", "mcq", "single"}:
        task_type = QA_TASK_MCQ
    elif task_type in {"research_multi_select", "multi_select", "multiple", "multi"}:
        task_type = QA_TASK_MULTI_SELECT
    if task_type == QA_TASK_MCQ and len(answer_ids) != 1:
        raise ValueError("MCQ rows must include exactly one answer.")
    if task_type not in {QA_TASK_MCQ, QA_TASK_MULTI_SELECT}:
        raise ValueError(f"Unsupported QA task_type {task_type!r}.")

    stable_source = {"question": question, "choices": choices, "answer": answer_ids}
    canonical = {
        "schema_version": QA_SCHEMA_VERSION,
        "id": item_id or str(row.get("id") or f"qa:{hashlib.sha256(json.dumps(stable_source, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:16]}"),
        "task_type": task_type,
        "question": question,
        "answer_choices": choices,
        "answer": answer_ids[0] if task_type == QA_TASK_MCQ else answer_ids,
        "explanation": clean_whitespace(row.get("explanation") or ""),
    }
    metadata = row.get("metadata")
    if isinstance(metadata, dict):
        canonical["metadata"] = metadata
    return canonical


def parse_lettered_qa_text(text: str, *, source_id: str | None = None) -> list[dict[str, Any]]:
    """Parse draft Notepad-style MCQ text into canonical rows without trusting labels as durable ids."""

    rows: list[dict[str, Any]] = []
    for match in _QUESTION_RE.finditer(text or ""):
        block = match.group(1).strip()
        correct_match = _CORRECT_RE.search(block)
        if not correct_match:
            continue
        before_correct = block[: correct_match.start()]
        question_text = before_correct
        first_choice = re.search(r"(?m)^[A-Z]\.\s+", before_correct)
        if first_choice:
            question_text = before_correct[: first_choice.start()]
        choices: list[dict[str, str]] = []
        for choice_match in _CHOICE_RE.finditer(block):
            label = choice_match.group(1).strip().upper()
            choice_text = clean_whitespace(choice_match.group(2))
            if choice_text:
                choices.append({"source_label": label, "text": choice_text})
        explanation = ""
        explanation_match = _EXPLANATION_RE.search(block[correct_match.end() :])
        if explanation_match:
            explanation = clean_whitespace(explanation_match.group(1))
        answer_labels = [part.strip().upper() for part in correct_match.group(1).split(",") if part.strip()]
        row = normalize_qa_item(
            {
                "question": clean_whitespace(question_text),
                "answer_choices": choices,
                "answer": answer_labels,
                "explanation": explanation,
                "metadata": {"source_id": source_id} if source_id else {},
            }
        )
        rows.append(row)
    return rows


def shuffle_qa_items(items: Iterable[dict[str, Any]], *, seed: int | str | None = None) -> list[dict[str, Any]]:
    rows = list(items)
    rng = random.Random(_stable_seed(seed) if seed is not None else None)
    rng.shuffle(rows)
    return rows


def materialize_qa_view(
    item: dict[str, Any],
    *,
    seed: int | str | None = None,
    shuffle_choices: bool = True,
) -> dict[str, Any]:
    canonical = normalize_qa_item(item)
    choices = list(canonical.get("answer_choices") or [])
    answer_ids = set(_answer_ids(canonical.get("answer")))
    rng = random.Random(_stable_seed({"seed": seed, "id": canonical.get("id")}))
    order = list(range(len(choices)))
    if shuffle_choices:
        rng.shuffle(order)

    view_choices: list[dict[str, Any]] = []
    label_map: dict[str, str] = {}
    for new_idx, old_idx in enumerate(order):
        source = choices[old_idx]
        label = _CHOICE_LABELS[new_idx]
        canonical_id = str(source["choice_id"])
        label_map[canonical_id] = label
        row = {
            "id": label,
            "text": str(source.get("text") or ""),
            "canonical_choice_id": canonical_id,
        }
        if source.get("source_label"):
            row["source_label"] = source["source_label"]
        view_choices.append(row)

    correct_labels = [label_map[choice_id] for choice_id in [str(choice.get("choice_id")) for choice in choices] if choice_id in answer_ids]
    view = {
        "schema_version": QA_SCHEMA_VERSION,
        "id": canonical.get("id"),
        "task_type": canonical.get("task_type") or (QA_TASK_MULTI_SELECT if len(correct_labels) > 1 else QA_TASK_MCQ),
        "question": canonical.get("question"),
        "choices": view_choices,
        "answer": correct_labels[0] if len(correct_labels) == 1 else correct_labels,
        "correct_choice_id": correct_labels[0] if len(correct_labels) == 1 else None,
        "correct_choice_ids": correct_labels,
        "explanation": canonical.get("explanation") or "",
        "choice_permutation": order,
        "label_map": label_map,
    }
    if canonical.get("metadata"):
        view["metadata"] = canonical["metadata"]
    return view


def format_qa_prompt(view: dict[str, Any]) -> str:
    lines = [clean_whitespace(view.get("question") or ""), ""]
    for choice in view.get("choices") or []:
        lines.append(f"{choice.get('id')}. {clean_whitespace(choice.get('text') or '')}")
    if len(view.get("correct_choice_ids") or []) > 1:
        lines.append("")
        lines.append("Select all correct answers.")
    return "\n".join(lines).strip()


def format_qa_training_example(view: dict[str, Any], *, mode: str = QA_FORMAT_ANSWER_ONLY) -> dict[str, Any]:
    if mode not in QA_FORMATS:
        raise ValueError(f"Unsupported QA format {mode!r}; expected one of {', '.join(QA_FORMATS)}.")
    labels = [str(value) for value in (view.get("correct_choice_ids") or _answer_ids(view.get("answer")))]
    answer_value: str | list[str] = labels[0] if len(labels) == 1 else labels
    explanation = clean_whitespace(view.get("explanation") or "")
    prompt = format_qa_prompt(view)

    if mode == QA_FORMAT_EVAL:
        completion = ""
    elif mode == QA_FORMAT_JSON:
        payload: dict[str, Any] = {"answer": answer_value}
        if explanation:
            payload["explanation"] = explanation
        completion = json.dumps(payload, ensure_ascii=False)
    elif mode == QA_FORMAT_ANSWER_WITH_EXPLANATION:
        completion = f"Answer: {', '.join(labels)}"
        if explanation:
            completion += f"\n\nExplanation: {explanation}"
    else:
        completion = ", ".join(labels)

    return {
        "id": view.get("id"),
        "task_type": view.get("task_type"),
        "mode": mode,
        "prompt": prompt,
        "completion": completion,
        "messages": [{"role": "user", "content": prompt}],
        "answer": answer_value,
        "explanation": explanation,
        "metadata": {
            "choice_permutation": view.get("choice_permutation"),
            "label_map": view.get("label_map"),
            "choices": view.get("choices") or [],
        },
    }


def _token_ids(tokenizer: Any, text: str, *, add_special_tokens: bool = False) -> list[int]:
    encoded = tokenizer(text, add_special_tokens=add_special_tokens)
    if isinstance(encoded, dict):
        values = encoded.get("input_ids")
    else:
        values = getattr(encoded, "input_ids", None)
    if values is None:
        raise ValueError("Tokenizer must return input_ids.")
    if values and isinstance(values[0], list):
        values = values[0]
    return [int(value) for value in values]


def _tokenizer_eos_ids(tokenizer: Any) -> list[int]:
    eos_id = getattr(tokenizer, "eos_token_id", None)
    if eos_id is None:
        return []
    if isinstance(eos_id, (list, tuple)):
        return [int(value) for value in eos_id]
    return [int(eos_id)]


def _tokenizer_pad_id(tokenizer: Any) -> int:
    pad_id = getattr(tokenizer, "pad_token_id", None)
    if pad_id is not None:
        return int(pad_id)
    eos_ids = _tokenizer_eos_ids(tokenizer)
    if eos_ids:
        return eos_ids[0]
    return 0


def _truncate_prompt_completion(
    prompt_ids: list[int],
    completion_ids: list[int],
    eos_ids: list[int],
    *,
    max_length: int | None,
) -> tuple[list[int], list[int], list[int]]:
    if max_length is None or max_length <= 0:
        return prompt_ids, completion_ids, eos_ids
    if len(prompt_ids) + len(completion_ids) + len(eos_ids) <= max_length:
        return prompt_ids, completion_ids, eos_ids
    completion_budget = max(0, max_length - len(eos_ids))
    if len(completion_ids) > completion_budget:
        return [], completion_ids[:completion_budget], eos_ids[: max(0, max_length - completion_budget)]
    prompt_budget = max(0, max_length - len(completion_ids) - len(eos_ids))
    return prompt_ids[-prompt_budget:] if prompt_budget else [], completion_ids, eos_ids


def _pad_to_multiple(length: int, multiple: int | None) -> int:
    if not multiple or multiple <= 1:
        return length
    remainder = length % multiple
    return length if remainder == 0 else length + multiple - remainder


class QaSftCollator:
    """Backend-light SFT collator for formatted QA rows.

    The collator expects rows from ``format_qa_training_example``. It masks prompt
    tokens with ``-100`` so causal LM loss is computed only on the completion.
    It returns Python lists by default and imports torch only when
    ``return_tensors="pt"`` is requested.
    """

    def __init__(
        self,
        tokenizer: Any,
        *,
        max_length: int | None = None,
        pad_to_multiple_of: int | None = None,
        label_pad_token_id: int = QA_LABEL_PAD_TOKEN_ID,
        add_eos_token: bool = True,
        return_tensors: str | None = None,
    ) -> None:
        self.tokenizer = tokenizer
        self.max_length = max_length
        self.pad_to_multiple_of = pad_to_multiple_of
        self.label_pad_token_id = int(label_pad_token_id)
        self.add_eos_token = bool(add_eos_token)
        self.return_tensors = return_tensors

    def __call__(self, features: list[dict[str, Any]]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        eos_ids = _tokenizer_eos_ids(self.tokenizer) if self.add_eos_token else []
        pad_id = _tokenizer_pad_id(self.tokenizer)
        max_len = 0
        for feature in features:
            prompt = str(feature.get("prompt") or "")
            completion = str(feature.get("completion") or "")
            prompt_ids = _token_ids(self.tokenizer, prompt, add_special_tokens=False)
            completion_ids = _token_ids(self.tokenizer, completion, add_special_tokens=False)
            prompt_ids, completion_ids, row_eos_ids = _truncate_prompt_completion(
                prompt_ids,
                completion_ids,
                eos_ids,
                max_length=self.max_length,
            )
            input_ids = [*prompt_ids, *completion_ids, *row_eos_ids]
            labels = [self.label_pad_token_id] * len(prompt_ids) + [*completion_ids, *row_eos_ids]
            attention_mask = [1] * len(input_ids)
            row = {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "labels": labels,
            }
            rows.append(row)
            max_len = max(max_len, len(input_ids))

        target_len = _pad_to_multiple(max_len, self.pad_to_multiple_of)
        batch = {
            "input_ids": [],
            "attention_mask": [],
            "labels": [],
            "metadata": [feature.get("metadata") for feature in features],
            "ids": [feature.get("id") for feature in features],
        }
        for row in rows:
            pad_len = target_len - len(row["input_ids"])
            batch["input_ids"].append([*row["input_ids"], *([pad_id] * pad_len)])
            batch["attention_mask"].append([*row["attention_mask"], *([0] * pad_len)])
            batch["labels"].append([*row["labels"], *([self.label_pad_token_id] * pad_len)])

        if self.return_tensors is None:
            return batch
        if self.return_tensors != "pt":
            raise ValueError("QaSftCollator only supports return_tensors=None or 'pt'.")
        try:
            import torch
        except Exception as exc:
            raise RuntimeError("return_tensors='pt' requires torch to be installed.") from exc
        tensor_batch = {
            "input_ids": torch.tensor(batch["input_ids"], dtype=torch.long),
            "attention_mask": torch.tensor(batch["attention_mask"], dtype=torch.long),
            "labels": torch.tensor(batch["labels"], dtype=torch.long),
            "metadata": batch["metadata"],
            "ids": batch["ids"],
        }
        return tensor_batch
