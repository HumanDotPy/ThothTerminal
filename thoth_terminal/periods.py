from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import calendar
import re
from typing import Any


_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_QWORD_TO_INT = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
}
_QY_RE = re.compile(r"\bQ(?P<q>[1-4])[\s_-]*(?P<fy>FY)?[\s_-]*(?P<year>[12]\d{3}|\d{2})(?=\D|$)", re.IGNORECASE)
_YQ_RE = re.compile(r"\b(?P<year>[12]\d{3}|\d{2})[\s_-]*(?P<fy>FY)?[\s_-]*Q(?P<q>[1-4])(?=\D|$)", re.IGNORECASE)
_FYQ_RE = re.compile(r"\bFY[\s_-]*(?P<year>[12]\d{3}|\d{2})[\s_-]*Q(?P<q>[1-4])(?=\D|$)", re.IGNORECASE)
_FY_RE = re.compile(r"\bFY[\s_-]*(?P<year>[12]\d{3}|\d{2})(?=\D|$)", re.IGNORECASE)
_FISCAL_YEAR_RE = re.compile(r"\bfiscal\s+year\s+(?P<year>[12]\d{3})\b", re.IGNORECASE)
_QWORD_FY_RE = re.compile(
    r"\b(?P<qword>first|1st|second|2nd|third|3rd|fourth|4th)[\s-]+quarter(?:\s+of)?\s+FY\s*(?P<year>[12]\d{3})\b",
    re.IGNORECASE,
)
_QWORD_Y_RE = re.compile(
    r"\b(?P<qword>first|1st|second|2nd|third|3rd|fourth|4th)[\s-]+quarter(?:\s+of|\s+fiscal|\s+fy)?\s*(?P<year>[12]\d{3}|\d{2})\b",
    re.IGNORECASE,
)
_Y_QWORD_RE = re.compile(
    r"\b(?P<year>[12]\d{3})\s+(?P<qword>first|1st|second|2nd|third|3rd|fourth|4th)[\s-]+quarter\b",
    re.IGNORECASE,
)
_MONTH_DAY_YEAR_RE = (
    r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(?P<year>[12]\d{3})"
)
_PERIOD_ENDED_RE = re.compile(
    r"\b(?P<duration>three|six|nine|twelve)\s+months\s+ended\s+" + _MONTH_DAY_YEAR_RE,
    re.IGNORECASE,
)
_FY_ENDED_RE = re.compile(
    r"\b(?:fiscal\s+year|year)\s+ended\s+" + _MONTH_DAY_YEAR_RE,
    re.IGNORECASE,
)
_FY_ENDING_MONTH_RE = re.compile(
    r"\b(?:fiscal\s+year|year)\s+ending\s+"
    r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\.?(?:\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,)?)?\s+(?P<year>[12]\d{3})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PeriodNormalization:
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    period_start: date | None = None
    period_end: date | None = None
    calendar_quarter: str | None = None
    issuer_fiscal_year_label: str | None = None
    vendor_fiscal_label: str | None = None
    source_label_raw: str | None = None
    basis: str | None = None
    confidence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fiscal_year": self.fiscal_year,
            "fiscal_quarter": self.fiscal_quarter,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "calendar_quarter": self.calendar_quarter,
            "issuer_fiscal_year_label": self.issuer_fiscal_year_label,
            "vendor_fiscal_label": self.vendor_fiscal_label,
            "source_label_raw": self.source_label_raw,
            "basis": self.basis,
            "confidence": self.confidence,
        }


def calendar_quarter_for_date(value: date | None) -> str | None:
    if value is None:
        return None
    return f"{value.year}Q{((value.month - 1) // 3) + 1}"


def _normalize_year(value: str | int | None) -> int | None:
    raw = str(value or "").strip()
    if not raw.isdigit():
        return None
    if len(raw) == 2:
        n = int(raw)
        return 2000 + n if n <= 79 else 1900 + n
    if len(raw) == 4:
        return int(raw)
    return None


def _month(value: str | None) -> int | None:
    return _MONTHS.get(str(value or "").strip(".").lower())


def _last_day(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _date_from_match(match: re.Match[str]) -> date | None:
    month_v = _month(match.group("month"))
    if month_v is None:
        return None
    try:
        return date(int(match.group("year")), month_v, int(match.group("day")))
    except Exception:
        return None


def _is_japan_context(*, issuer: Any = None, company: Any = None, metadata: dict[str, Any] | None = None) -> bool:
    meta = metadata or {}
    values = [
        getattr(issuer, "jurisdiction", None),
        getattr(company, "region", None),
        getattr(company, "exchange", None),
        getattr(company, "ticker", None),
        getattr(company, "name", None),
        meta.get("country"),
        meta.get("exchange"),
        meta.get("ticker"),
        meta.get("sec_code"),
        meta.get("stock_code"),
    ]
    hay = " ".join(str(v or "") for v in values).upper()
    return any(token in hay for token in ("JP", "JAPAN", "TOKYO", "TSE", "JPX", ".T"))


def fiscal_year_end_month_for_context(
    *,
    issuer: Any = None,
    company: Any = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    meta = dict(metadata or {})
    for source in (getattr(issuer, "extra", None), getattr(company, "metadata_json", None), meta):
        if isinstance(source, dict):
            raw = source.get("fiscal_year_end_month") or source.get("fye_month")
            try:
                month_v = int(raw)
            except Exception:
                month_v = 0
            if 1 <= month_v <= 12:
                return month_v
    return 3 if _is_japan_context(issuer=issuer, company=company, metadata=meta) else 12


def fiscal_year_label_convention(
    *,
    issuer: Any = None,
    company: Any = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    meta = dict(metadata or {})
    for source in (getattr(issuer, "extra", None), getattr(company, "metadata_json", None), meta):
        if isinstance(source, dict):
            raw = str(source.get("fiscal_year_label_convention") or "").strip().lower()
            if raw in {"start", "end"}:
                return raw
    return "start" if _is_japan_context(issuer=issuer, company=company, metadata=meta) else "end"


def _fiscal_start_month(fye_month: int) -> int:
    return (int(fye_month) % 12) + 1


def _fiscal_year_from_end(value: date, *, fye_month: int, convention: str) -> int:
    if convention == "start" and int(fye_month) != 12:
        return value.year - 1 if value.month <= int(fye_month) else value.year
    return value.year if value.month <= int(fye_month) else value.year + (1 if int(fye_month) != 12 else 0)


def _fiscal_end_year_from_label(fiscal_year: int, *, fye_month: int, convention: str) -> int:
    if convention == "start" and int(fye_month) != 12:
        return int(fiscal_year) + 1
    return int(fiscal_year)


def _quarter_from_period_end(value: date, *, fye_month: int) -> int:
    start_month = _fiscal_start_month(fye_month)
    return (((value.month - start_month) % 12) // 3) + 1


def _period_end_from_fiscal_quarter(fiscal_year: int, quarter: int, *, fye_month: int, convention: str) -> date:
    fiscal_end_year = _fiscal_end_year_from_label(fiscal_year, fye_month=fye_month, convention=convention)
    start_month = _fiscal_start_month(fye_month)
    month = ((start_month + (int(quarter) * 3) - 2) % 12) + 1
    start_year = fiscal_end_year - 1 if start_month > 1 else fiscal_end_year
    year = start_year if month >= start_month else fiscal_end_year
    return _last_day(year, month)


def _quarter_start_from_end(value: date) -> date:
    month = value.month - 2
    year = value.year
    if month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _fiscal_year_start(fiscal_year: int, *, fye_month: int, convention: str) -> date:
    fiscal_end_year = _fiscal_end_year_from_label(fiscal_year, fye_month=fye_month, convention=convention)
    start_month = _fiscal_start_month(fye_month)
    start_year = fiscal_end_year - 1 if start_month > 1 else fiscal_end_year
    return date(start_year, start_month, 1)


def _raw_label(*parts: object) -> str | None:
    text = re.sub(r"\s+", " ", " ".join(str(p or "") for p in parts if str(p or "").strip())).strip()
    return text or None


def _vendor_label(fiscal_year: int | None, fiscal_quarter: int | None, *, period_end: date | None, fye_month: int, convention: str) -> str | None:
    if fiscal_year is None:
        return None
    if fiscal_quarter is not None:
        year = _fiscal_end_year_from_label(fiscal_year, fye_month=fye_month, convention=convention)
        return f"Q{int(fiscal_quarter)} {year}" if convention == "start" and fye_month != 12 else f"Q{int(fiscal_quarter)} {fiscal_year}"
    if period_end is not None:
        return f"FY ended {period_end.isoformat()}"
    return f"FY{fiscal_year}"


def normalize_period_from_text(
    title: str | None,
    url: str | None = None,
    *,
    published_at: datetime | date | None = None,
    issuer: Any = None,
    company: Any = None,
    metadata: dict[str, Any] | None = None,
    annual_as_q4: bool = True,
) -> PeriodNormalization:
    """Normalize issuer fiscal labels, vendor labels, and calendar quarters.

    Japanese listed companies commonly label an April-March fiscal year by start
    year (FY2025 ends in CYQ1 2026). Vendor feeds often call the same period
    Q4 2026. This helper preserves both labels while making the issuer fiscal
    period the canonical key.
    """

    meta = dict(metadata or {})
    raw = _raw_label(
        title,
        url,
        meta.get("period_label"),
        meta.get("fiscal_period"),
        meta.get("fiscal_year"),
        meta.get("fiscal_quarter"),
    )
    if not raw:
        return PeriodNormalization()

    fye_month = fiscal_year_end_month_for_context(issuer=issuer, company=company, metadata=meta)
    convention = fiscal_year_label_convention(issuer=issuer, company=company, metadata=meta)
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    period_end: date | None = None
    period_start: date | None = None
    basis = "unparsed"
    confidence = "low"
    cumulative = False

    ended = _PERIOD_ENDED_RE.search(raw)
    if ended:
        period_end = _date_from_match(ended)
        duration = str(ended.group("duration") or "").strip().lower()
        cumulative = duration in {"six", "nine", "twelve"}
        if period_end is not None:
            fiscal_quarter = _quarter_from_period_end(period_end, fye_month=fye_month)
            fiscal_year = _fiscal_year_from_end(period_end, fye_month=fye_month, convention=convention)
            basis = "period_end"
            confidence = "high"

    fy_ended = _FY_ENDED_RE.search(raw)
    if fy_ended and period_end is None:
        period_end = _date_from_match(fy_ended)
        if period_end is not None:
            fiscal_year = _fiscal_year_from_end(period_end, fye_month=fye_month, convention=convention)
            fiscal_quarter = 4 if annual_as_q4 else None
            cumulative = True
            basis = "fiscal_year_ended"
            confidence = "high"

    fy_ending = _FY_ENDING_MONTH_RE.search(raw)
    if fy_ending and period_end is None:
        month_v = _month(fy_ending.group("month"))
        year_v = _normalize_year(fy_ending.group("year"))
        if month_v is not None and year_v is not None:
            fiscal_year_end = _last_day(year_v, month_v)
            fiscal_year = _fiscal_year_from_end(fiscal_year_end, fye_month=month_v, convention=convention)
            lower_raw = raw.lower()
            qword_match = re.search(r"\b(first|1st|second|2nd|third|3rd|fourth|4th)[\s-]+quarter\b", lower_raw)
            if qword_match:
                fiscal_quarter = _QWORD_TO_INT.get(qword_match.group(1))
            elif "semi-annual" in lower_raw or "semiannual" in lower_raw or "six months" in lower_raw or "half year" in lower_raw:
                fiscal_quarter = 2
            else:
                fiscal_quarter = 4 if annual_as_q4 else None
            period_end = (
                _period_end_from_fiscal_quarter(fiscal_year, fiscal_quarter, fye_month=month_v, convention=convention)
                if fiscal_quarter is not None
                else fiscal_year_end
            )
            cumulative = True
            basis = "fiscal_year_ending_month"
            confidence = "medium"

    if fiscal_year is None:
        match = _QWORD_FY_RE.search(raw)
        if match:
            q = _QWORD_TO_INT.get(str(match.group("qword") or "").strip().lower())
            fy = _normalize_year(match.group("year"))
            if q is not None and fy is not None:
                fiscal_year = fy
                fiscal_quarter = q
                period_end = _period_end_from_fiscal_quarter(fy, q, fye_month=fye_month, convention=convention)
                basis = "issuer_fy_quarter_label"
                confidence = "high"

    if fiscal_year is None:
        match = _QWORD_Y_RE.search(raw) or _Y_QWORD_RE.search(raw)
        if match:
            q = _QWORD_TO_INT.get(str(match.group("qword") or "").strip().lower())
            year_v = _normalize_year(match.group("year"))
            if q is not None and year_v is not None:
                if convention == "start" and fye_month != 12 and "fy" not in match.group(0).lower() and "fiscal" not in match.group(0).lower():
                    fiscal_year = year_v - 1
                    basis = "vendor_end_year_quarter_label"
                else:
                    fiscal_year = year_v
                    basis = "quarter_word_label"
                fiscal_quarter = q
                period_end = _period_end_from_fiscal_quarter(fiscal_year, q, fye_month=fye_month, convention=convention)
                confidence = "medium"

    if fiscal_year is None:
        match = _FYQ_RE.search(raw)
        if match:
            year_token = str(match.group("year") or "")
            fy = _normalize_year(year_token)
            q = int(match.group("q"))
            if fy is not None:
                if len(year_token) == 2 and convention == "start" and fye_month != 12:
                    fiscal_year = fy - 1
                    basis = "vendor_end_year_compact_fy_quarter_label"
                else:
                    fiscal_year = fy
                    basis = "issuer_fy_quarter_label"
                fiscal_quarter = q
                period_end = _period_end_from_fiscal_quarter(fiscal_year, q, fye_month=fye_month, convention=convention)
                confidence = "high"

    if fiscal_year is None:
        match = _QY_RE.search(raw) or _YQ_RE.search(raw)
        if match:
            q = int(match.group("q"))
            year_v = _normalize_year(match.group("year"))
            has_fy = bool(match.groupdict().get("fy"))
            if year_v is not None:
                if convention == "start" and fye_month != 12 and not has_fy:
                    fiscal_year = year_v - 1
                    basis = "vendor_end_year_quarter_label"
                else:
                    fiscal_year = year_v
                    basis = "issuer_fy_quarter_label" if has_fy else "quarter_label"
                fiscal_quarter = q
                period_end = _period_end_from_fiscal_quarter(fiscal_year, q, fye_month=fye_month, convention=convention)
                confidence = "medium"

    if fiscal_year is None:
        fy_match = _FY_RE.search(raw) or _FISCAL_YEAR_RE.search(raw)
        if fy_match:
            fy = _normalize_year(fy_match.group("year"))
            if fy is not None:
                fiscal_year = fy
                fiscal_quarter = 4 if annual_as_q4 and re.search(r"\b(financial\s+results|earnings|results)\b", raw, re.IGNORECASE) else None
                if fiscal_quarter is not None or convention == "start" or fye_month != 12:
                    period_end = _period_end_from_fiscal_quarter(
                        fiscal_year,
                        fiscal_quarter or 4,
                        fye_month=fye_month,
                        convention=convention,
                    )
                    cumulative = True
                basis = "fiscal_year_label"
                confidence = "medium"

    if fiscal_year is None:
        return PeriodNormalization(source_label_raw=raw, basis=basis, confidence=confidence)

    if period_end is not None:
        if cumulative or fiscal_quarter is None or fiscal_quarter == 4 and "fiscal year" in raw.lower():
            period_start = _fiscal_year_start(fiscal_year, fye_month=fye_month, convention=convention)
        elif fiscal_quarter is not None:
            period_start = _quarter_start_from_end(period_end)
    elif fiscal_quarter is not None:
        period_end = _period_end_from_fiscal_quarter(fiscal_year, fiscal_quarter, fye_month=fye_month, convention=convention)
        period_start = _quarter_start_from_end(period_end)

    issuer_label = f"FY{fiscal_year}" if fiscal_year is not None else None
    if issuer_label and fiscal_quarter is not None:
        issuer_label = f"{issuer_label} Q{int(fiscal_quarter)}"
    return PeriodNormalization(
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        period_start=period_start,
        period_end=period_end,
        calendar_quarter=calendar_quarter_for_date(period_end),
        issuer_fiscal_year_label=issuer_label,
        vendor_fiscal_label=_vendor_label(
            fiscal_year,
            fiscal_quarter,
            period_end=period_end,
            fye_month=fye_month,
            convention=convention,
        ),
        source_label_raw=raw,
        basis=basis,
        confidence=confidence,
    )
