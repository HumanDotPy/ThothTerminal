from __future__ import annotations

from typing import Any


TAIWAN_COMPANY_EN = {
    "2330": "TSMC",
    "2303": "UMC",
    "2454": "MediaTek",
    "3711": "ASE Technology",
    "3037": "Unimicron",
    "3035": "Faraday Technology",
    "3034": "Novatek",
    "2379": "Realtek",
    "3661": "Alchip",
    "3443": "Global Unichip",
    "6488": "GlobalWafers",
    "5347": "Vanguard International Semiconductor",
    "2408": "Nanya Technology",
    "2337": "Macronix",
    "2344": "Winbond",
    "2449": "King Yuan Electronics",
    "3413": "Foxsemicon",
    "6531": "AP Memory",
    "5274": "ASPEED",
}

TAIWAN_COMPANY_ALIASES = {
    "5274": [
        "ASPEED Technology Inc.",
        "ASPEED Technology",
        "信驊",
        "信驊科技",
        "信驊科技股份有限公司",
        "Xinhua",
        "5274.TW",
        "5274.TWO",
    ],
}

TAIWAN_INDUSTRY_EN = {
    "半導體業": "Semiconductors",
    "電子零組件業": "Electronic Components",
    "電腦及週邊設備業": "Computers and Peripherals",
    "通信網路業": "Communications and Networking",
    "光電業": "Optoelectronics",
    "電子通路業": "Electronic Distribution",
    "資訊服務業": "Information Services",
    "其他電子業": "Other Electronics",
}


def _clean(value: object) -> str:
    return str(value or "").strip()


def _dedupe(values: list[object]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean(value)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _listish(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [_clean(item) for item in value if _clean(item)]
    return [_clean(value)] if _clean(value) else []


def _translation_entry(config: dict[str, Any] | None, keys: list[str]) -> tuple[str | None, list[str], str | None]:
    cfg = dict(config or {})
    for field in (
        "company_name_translations",
        "company_name_translation_map",
        "company_names_en",
        "company_display_names",
    ):
        mapping = cfg.get(field)
        if not isinstance(mapping, dict):
            continue
        for key in keys:
            if not key or key not in mapping:
                continue
            value = mapping.get(key)
            if isinstance(value, dict):
                name = (
                    _clean(value.get("company_name_en"))
                    or _clean(value.get("english_name"))
                    or _clean(value.get("display_name"))
                    or _clean(value.get("canonical_name"))
                    or _clean(value.get("name"))
                )
                aliases = _listish(value.get("aliases") or value.get("alias"))
                return (name or None), aliases, field
            name = _clean(value)
            if name:
                return name, [], field
    return None, [], None


def resolve_taiwan_company_name(
    *,
    company_code: object,
    local_name: object,
    config: dict[str, Any] | None = None,
) -> dict[str, object]:
    code = _clean(company_code)
    original = _clean(local_name)
    override, override_aliases, source = _translation_entry(config, [code, original])
    builtin = TAIWAN_COMPANY_EN.get(code)
    english = override or builtin
    display = english or original or code
    builtin_aliases = TAIWAN_COMPANY_ALIASES.get(code, [])
    aliases = _dedupe([display, english, original, code, *builtin_aliases, *override_aliases])
    return {
        "company_code": code,
        "company_name": display,
        "company_name_en": english,
        "company_name_original": original or None,
        "company_aliases": aliases,
        "company_name_source": source or ("taiwan_builtin" if builtin else "source"),
    }


def resolve_taiwan_industry_name(industry: object, config: dict[str, Any] | None = None) -> dict[str, object]:
    original = _clean(industry)
    cfg = dict(config or {})
    override = None
    mapping = cfg.get("industry_name_translations")
    if isinstance(mapping, dict):
        value = mapping.get(original)
        override = (
            _clean(value.get("industry_en") or value.get("english_name") or value.get("name"))
            if isinstance(value, dict)
            else _clean(value)
        )
    english = override or TAIWAN_INDUSTRY_EN.get(original)
    display = english or original
    return {
        "industry": display,
        "industry_en": english,
        "industry_original": original or None,
        "industry_name_source": "config" if override else ("taiwan_builtin" if english else "source"),
    }
