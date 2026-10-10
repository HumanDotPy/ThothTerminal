"""Offline BPE reference accounting for active research representations.

Counts use OpenAI's published cl100k_base vocabulary, not exact Muse or other
provider/model tokens. Policies limit active representations, not total stored
history, underlying documents, or scheduled-run output. No runtime download or
character-count approximation is permitted.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from importlib.resources import files
import json
from types import MappingProxyType
from typing import Any, Literal

import tiktoken


ENCODING = "cl100k_base"
# Official data and construction parameters:
# https://github.com/openai/tiktoken/blob/main/tiktoken_ext/openai_public.py
# https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken
_VOCABULARY_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
_PATTERN = (
    r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+|"
    r" ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"
)
_MODE_DESCRIPTIONS = {
    "compact": "Compact active representations at or above the ceiling toward the target.",
    "page": "Page source content; retain the full underlying source without compaction.",
    "protected": "Preserve authoritative instructions; never automatically compact them.",
}


@dataclass(frozen=True)
class TokenPolicy:
    ceiling_tokens: int
    mode: Literal["compact", "page", "protected"] = "compact"

    @property
    def target_tokens(self) -> int:
        return self.ceiling_tokens * 3 // 5


POLICIES = MappingProxyType(
    {
        "belief": TokenPolicy(3000),
        "belief_history": TokenPolicy(3000, "page"),
        "world": TokenPolicy(12000),
        "research_skill": TokenPolicy(4000),
        "extension": TokenPolicy(4000),
        "journal": TokenPolicy(8000),
        "research_report": TokenPolicy(12000),
        "knowledge_asset": TokenPolicy(8000),
        "product_model": TokenPolicy(8000),
        "product_configuration": TokenPolicy(6000),
        "agentx_configuration": TokenPolicy(8000),
        "dataset": TokenPolicy(4000),
        "supply_chain": TokenPolicy(8000),
        "roadmap": TokenPolicy(4000),
        "operator_message": TokenPolicy(2000),
        "stable_state": TokenPolicy(32000),
        "volatile_state": TokenPolicy(24000),
        "memory_catalog": TokenPolicy(24000),
        "world_catalog": TokenPolicy(16000),
        "skill_catalog": TokenPolicy(16000),
        "system_instructions": TokenPolicy(16000, "protected"),
        "tool_schema": TokenPolicy(12000, "protected"),
        "harness_native_skill": TokenPolicy(4000, "protected"),
        "research_brief": TokenPolicy(6000, "page"),
        "source_packet": TokenPolicy(12000, "page"),
        "state_delta": TokenPolicy(16000, "page"),
        "source_document": TokenPolicy(12000, "page"),
        "executable_source": TokenPolicy(12000, "page"),
        "numerical_data": TokenPolicy(12000, "page"),
    }
)


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    asset = files("thoth_terminal").joinpath("tokenizer_assets", "cl100k_base.tiktoken")
    data = asset.read_bytes()
    if hashlib.sha256(data).hexdigest() != _VOCABULARY_SHA256:
        raise RuntimeError("Bundled cl100k_base vocabulary failed its SHA256 integrity check")
    ranks = {
        base64.b64decode(token, validate=True): int(rank)
        for token, rank in (line.split() for line in data.splitlines())
    }
    return tiktoken.Encoding(
        name=ENCODING,
        pat_str=_PATTERN,
        mergeable_ranks=ranks,
        special_tokens={
            "<|endoftext|>": 100257,
            "<|fim_prefix|>": 100258,
            "<|fim_middle|>": 100259,
            "<|fim_suffix|>": 100260,
            "<|endofprompt|>": 100276,
        },
    )


def count_tokens(value: Any) -> int:
    """Count literal text or deterministic compact JSON using the reference BPE.

    JSON objects use sorted keys, unescaped Unicode, and no optional whitespace.
    Unsupported JSON values and nonfinite numbers fail explicitly.
    """
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    # Special-token spellings in stored research are ordinary text, not control tokens.
    return len(_encoding().encode_ordinary(text))


def budget_status(kind: str, value: Any) -> dict[str, Any]:
    """Report representation pressure; the target is not the trigger threshold."""
    try:
        policy = POLICIES[kind]
    except KeyError:
        raise ValueError(f"Unknown research token policy: {kind!r}") from None
    tokens = count_tokens(value)
    return {
        "kind": kind,
        "tokens": tokens,
        "ceiling_tokens": policy.ceiling_tokens,
        "target_tokens": policy.target_tokens,
        "compaction_due": policy.mode == "compact" and tokens >= policy.ceiling_tokens,
        "over_ceiling": tokens > policy.ceiling_tokens,
        "mode": policy.mode,
        "mode_description": _MODE_DESCRIPTIONS[policy.mode],
        "encoding": ENCODING,
        "model_tokens_exact": False,
    }


def token_page(
    text: str,
    offset_tokens: int = 0,
    limit_tokens: int = 6000,
    max_limit: int = 12000,
) -> dict[str, Any]:
    """Page text losslessly using a reconstructible integer token continuation.

    Returned offsets refer to the full input's token stream. An arbitrary offset
    inside a UTF-8 codepoint is aligned backward and reported as ``offset_tokens``.
    End boundaries normally shrink to fit the limit. If a tiny requested limit
    cannot fit one codepoint, at most three extra tokens complete that codepoint;
    ``tokens`` reports actual consumption. A hard ``max_limit`` still applies.
    Following ``next_offset_tokens`` reconstructs the original text exactly.
    """
    if not isinstance(text, str):
        raise TypeError("token_page text must be a string")
    for name, value, minimum in (
        ("offset_tokens", offset_tokens, 0),
        ("limit_tokens", limit_tokens, 1),
        ("max_limit", max_limit, 1),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    # Reject invalid Unicode rather than silently replacing source bytes in tiktoken.
    text.encode("utf-8", errors="strict")
    encoding = _encoding()
    tokens = encoding.encode_ordinary(text)
    total = len(tokens)
    start = min(offset_tokens, total)
    limit = min(limit_tokens, max_limit)

    def is_boundary(index: int) -> bool:
        if index == 0 or index == total:
            return True
        first_byte = encoding.decode_single_token_bytes(tokens[index])[0]
        return not 0x80 <= first_byte < 0xC0

    while not is_boundary(start):
        start -= 1
    end = min(start + limit, total)
    while not is_boundary(end):
        end -= 1
    if start < total and end == start:
        end = start + 1
        while not is_boundary(end):
            end += 1
        if end - start > max_limit:
            raise ValueError("max_limit cannot fit the next complete UTF-8 codepoint")
    page = encoding.decode_bytes(tokens[start:end]).decode("utf-8", errors="strict")
    return {
        "text": page,
        "offset_tokens": start,
        "requested_offset_tokens": offset_tokens,
        "limit_tokens": limit,
        "tokens": end - start,
        "total_tokens": total,
        "next_offset_tokens": end if end < total else None,
        "truncated": end < total,
        "encoding": ENCODING,
        "model_tokens_exact": False,
    }
