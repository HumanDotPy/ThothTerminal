from __future__ import annotations

from typing import Any, Iterable


SEMI_CORE_REPORTERS = ("US", "TW", "KR", "JP", "EU")

SEMI_TRADE_CODEBOOK: dict[str, dict[str, Any]] = {
    "memory": {
        "label": "Memory",
        "description": "Memory devices and memory-heavy systems.",
        "hs_codes": ["847170", "854232"],
        "keywords": ["dram", "nand", "hbm", "memory", "ddr", "lpddr", "ssd"],
    },
    "wafers": {
        "label": "Wafers",
        "description": "Silicon wafers and semiconductor-grade inputs.",
        "hs_codes": ["381800", "280461"],
        "keywords": ["wafer", "wafers", "silicon wafer", "epi", "epitaxy"],
    },
    "lithography": {
        "label": "Lithography",
        "description": "Lithography tools and optics-adjacent fab equipment.",
        "hs_codes": ["848620", "901320"],
        "keywords": ["lithography", "euv", "scanner", "immersion", "photoresist"],
    },
    "deposition_etch": {
        "label": "Deposition / Etch",
        "description": "Deposition, etch, clean, and process tooling.",
        "hs_codes": ["848620", "848640"],
        "keywords": ["etch", "deposition", "cvd", "ald", "clean", "metrology", "inspection"],
    },
    "substrates": {
        "label": "Substrates",
        "description": "Package substrates and related interconnect inputs.",
        "hs_codes": ["741021", "741022", "853400"],
        "keywords": ["substrate", "abf", "bt resin", "package substrate", "pcb"],
    },
    "accelerators_servers": {
        "label": "Accelerators / Servers",
        "description": "Accelerators, processors, servers, and compute systems.",
        "hs_codes": ["847141", "847150", "847180", "854231"],
        "keywords": ["gpu", "gpus", "accelerator", "accelerators", "server", "servers", "ai server", "cpu"],
    },
    "semi_materials": {
        "label": "Semiconductor Materials",
        "description": "Electronic chemicals, gases, and fab materials.",
        "hs_codes": ["280429", "281122", "381800"],
        "keywords": ["photoresist", "slurry", "precursor", "gas", "chemicals", "silicon", "materials"],
    },
}


def normalize_hs_code(value: object) -> str | None:
    raw = "".join(ch for ch in str(value or "").strip() if ch.isdigit())
    if len(raw) < 4:
        return None
    return raw[:10]


def theme_for_hs_code(hs_code: object) -> str | None:
    code = normalize_hs_code(hs_code)
    if not code:
        return None
    for theme, cfg in SEMI_TRADE_CODEBOOK.items():
        codes = {normalize_hs_code(v) for v in (cfg.get("hs_codes") or [])}
        if code in codes:
            return theme
    return None


def expand_theme_codes(themes: Iterable[object] | None = None, *, extra_codes: Iterable[object] | None = None) -> list[str]:
    codes: list[str] = []
    for theme in themes or []:
        cfg = SEMI_TRADE_CODEBOOK.get(str(theme or "").strip().lower())
        if not cfg:
            continue
        for code in cfg.get("hs_codes") or []:
            norm = normalize_hs_code(code)
            if norm and norm not in codes:
                codes.append(norm)
    for code in extra_codes or []:
        norm = normalize_hs_code(code)
        if norm and norm not in codes:
            codes.append(norm)
    return codes


def infer_trade_themes(
    text_parts: Iterable[object] | None = None,
    *,
    entity_tokens: Iterable[object] | None = None,
    limit: int = 4,
) -> list[str]:
    hay = " ".join(str(part or "").strip().lower() for part in (text_parts or []) if str(part or "").strip())
    entity_hay = " ".join(str(token or "").strip().lower() for token in (entity_tokens or []) if str(token or "").strip())
    matches: list[tuple[str, int]] = []
    for theme, cfg in SEMI_TRADE_CODEBOOK.items():
        score = 0
        for keyword in cfg.get("keywords") or []:
            token = str(keyword or "").strip().lower()
            if token and token in hay:
                score += 2
            if token and token in entity_hay:
                score += 1
        for code in cfg.get("hs_codes") or []:
            token = str(code or "").strip()
            if token and token in hay:
                score += 3
        if score > 0:
            matches.append((theme, score))
    matches.sort(key=lambda item: (item[1], item[0]), reverse=True)
    return [theme for theme, _score in matches[: max(1, int(limit or 4))]]
