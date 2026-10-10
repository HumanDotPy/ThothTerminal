from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


@dataclass(frozen=True)
class MoneyMention:
    raw: str
    currency: str
    amount: float
    start: int
    end: int


_SCALE_MAP = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "mn": 1e6,
    "mm": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
    "t": 1e12,
    "tn": 1e12,
    "trillion": 1e12,
}

_SYMBOLS = {
    "US$": "USD",
    "$": "USD",
    "$NT": "TWD",
    "C$": "CAD",
    "CA$": "CAD",
    "HK$": "HKD",
    "NT$": "TWD",
    "€": "EUR",
    "£": "GBP",
    "₩": "KRW",
    "¥": "JPY",
    "￥": "JPY",
    "CN¥": "CNY",
}

_WORD_TO_CODE = {
    "usd": "USD",
    "us dollars": "USD",
    "u.s. dollars": "USD",
    "dollars": "USD",
    "cad": "CAD",
    "canadian dollars": "CAD",
    "hkd": "HKD",
    "hong kong dollars": "HKD",
    "twd": "TWD",
    "ntd": "TWD",
    "taiwan dollars": "TWD",
    "new taiwan dollars": "TWD",
    "rmb": "CNY",
    "cny": "CNY",
    "yuan": "CNY",
    "renminbi": "CNY",
    "jpy": "JPY",
    "yen": "JPY",
    "krw": "KRW",
    "won": "KRW",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "gbp": "GBP",
    "pound": "GBP",
    "pounds": "GBP",
    "pound sterling": "GBP",
}

_CURRENCY_PREFIX = r"(US\$|\$NT|\$|USD|C\$|CA\$|CAD|HK\$|HKD|NT\$|TWD|NTD|RMB|CNY|CN¥|¥|￥|€|£|₩)"
_CURRENCY_SUFFIX = (
    r"(USD|US dollars?|U\.S\. dollars?|dollars?|CAD|C\$|CA\$|Canadian dollars?|HKD|HK\$|Hong Kong dollars?|"
    r"TWD|NTD|NT\$|New Taiwan dollars?|Taiwan dollars?|RMB|CNY|yuan|renminbi|JPY|yen|KRW|won|EUR|euros?|GBP|pounds?|pound sterling)"
)
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_SCALE = r"(?:k|m|mn|mm|bn|b|tn|t|thousand|million|billion|trillion)"

_RE_PREFIX = re.compile(
    rf"(?P<currency>{_CURRENCY_PREFIX})\s*(?P<number>{_NUMBER})\s*(?P<scale>{_SCALE})?\b",
    re.IGNORECASE,
)
_RE_SUFFIX = re.compile(
    rf"(?P<number>{_NUMBER})\s*(?P<scale>{_SCALE})?\s*(?P<currency>{_CURRENCY_SUFFIX})\b",
    re.IGNORECASE,
)


def _parse_number(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except Exception:
        return None


def _scale_value(scale: str | None) -> float:
    if not scale:
        return 1.0
    key = scale.strip().lower()
    return float(_SCALE_MAP.get(key, 1.0))


def _resolve_currency(token: str, *, context: str | None = None) -> str | None:
    if not token:
        return None
    raw = token.strip()
    if raw in _SYMBOLS:
        if raw in {"¥", "￥"} and context:
            ctx = context.lower()
            if any(k in ctx for k in ["rmb", "cny", "yuan", "renminbi", "china"]):
                return "CNY"
        return _SYMBOLS[raw]
    key = raw.lower()
    if key in _WORD_TO_CODE:
        return _WORD_TO_CODE[key]
    if key.replace("$", "") in _WORD_TO_CODE:
        return _WORD_TO_CODE[key.replace("$", "")]
    return None


def _overlaps(span: tuple[int, int], used: list[tuple[int, int]]) -> bool:
    s, e = span
    for u0, u1 in used:
        if s < u1 and e > u0:
            return True
    return False


def extract_money_mentions(text: str) -> list[MoneyMention]:
    if not text:
        return []
    mentions: list[MoneyMention] = []
    used: list[tuple[int, int]] = []

    for match in _RE_PREFIX.finditer(text):
        s, e = match.span()
        if _overlaps((s, e), used):
            continue
        currency_token = match.group("currency") or ""
        number = _parse_number(match.group("number") or "")
        if number is None:
            continue
        scale = _scale_value(match.group("scale"))
        ctx = text[max(0, s - 40) : min(len(text), e + 40)]
        currency = _resolve_currency(currency_token, context=ctx)
        if not currency:
            continue
        amount = number * scale
        raw = text[s:e]
        mentions.append(MoneyMention(raw=raw, currency=currency, amount=amount, start=s, end=e))
        used.append((s, e))

    for match in _RE_SUFFIX.finditer(text):
        s, e = match.span()
        if _overlaps((s, e), used):
            continue
        currency_token = match.group("currency") or ""
        number = _parse_number(match.group("number") or "")
        if number is None:
            continue
        scale = _scale_value(match.group("scale"))
        ctx = text[max(0, s - 40) : min(len(text), e + 40)]
        currency = _resolve_currency(currency_token, context=ctx)
        if not currency:
            continue
        amount = number * scale
        raw = text[s:e]
        mentions.append(MoneyMention(raw=raw, currency=currency, amount=amount, start=s, end=e))
        used.append((s, e))

    return mentions


def summarize_mentions(mentions: Iterable[MoneyMention]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for m in mentions:
        totals[m.currency] = totals.get(m.currency, 0.0) + float(m.amount)
    return totals
