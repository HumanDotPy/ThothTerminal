from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Any


@dataclass(frozen=True)
class ImageRouteFeatures:
    width: int
    height: int
    aspect_ratio: float
    dark_pixel_ratio: float
    colorfulness: float
    edge_density: float
    horizontal_line_ratio: float
    vertical_line_ratio: float
    text_run_density: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ImageTranscriptionRoute:
    lane: str
    image_kind: str
    confidence: float
    reason: str
    features: ImageRouteFeatures
    needs_vlm: bool = False
    source_risk: str = "unknown"

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["features"] = self.features.as_dict()
        return payload


@dataclass(frozen=True)
class OcrMediaAssessment:
    media_kind: str
    ocr_quality: str
    needs_vlm: bool
    is_signal: bool
    confidence: float
    reason: str
    source_risk: str = "normal"
    visual_signal_terms: tuple[str, ...] = ()
    layout_labels: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


HIGH_VALUE_VISUAL_SOURCE_PATTERNS = (
    "semianalysis",
    "semi analysis",
    "substack",
    "fabricated knowledge",
    "asianometry",
    "stratechery",
    "the chip letter",
    "high yield",
)

NON_SIGNAL_OCR_PATTERNS = (
    "stock photo",
    "getty images",
    "shutterstock",
    "illustration only",
    "representative image",
    "logo",
    "profile picture",
)

CHART_TERMS = (
    "axis",
    "x-axis",
    "y-axis",
    "legend",
    "market share",
    "duopoly",
    "eda",
    "revenue by",
    "revenue ($m)",
    "annual revenue",
    "company reports",
    "stacked bar",
    "stacked",
    "bar chart",
    "line chart",
    "series",
    "quarter",
    "source:",
    "$m",
    "$b",
    "%",
    "y/y",
    "qoq",
)

DIAGRAM_TERMS = (
    "flow",
    "architecture",
    "diagram",
    "verification",
    "tape-out",
    "tape out",
    "rtl",
    "synthesis",
    "floor planning",
    "place & route",
    "signoff",
    "arrow",
    "tsv",
    "oxide bond",
    "hybrid bond",
    "logic",
    "dram",
    "hbm",
    "interposer",
)

TABLE_TERMS = ("<table", "|", "thead", "tbody", "</tr>", "markdown table")


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def source_is_high_value_visual(*values: object) -> bool:
    haystack = " ".join(_clean_text(value).lower() for value in values if value is not None)
    return any(pattern in haystack for pattern in HIGH_VALUE_VISUAL_SOURCE_PATTERNS)


def _layout_labels(layout_details: object) -> tuple[str, ...]:
    if isinstance(layout_details, str):
        raw = layout_details.strip()
        if raw[:1] in {"{", "["}:
            try:
                layout_details = json.loads(raw)
            except Exception:
                layout_details = None
    labels: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            label = str(value.get("label") or "").strip().lower()
            if label:
                labels.append(label)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(layout_details)
    return tuple(sorted(set(labels)))


def _matched_terms(text: str, terms: tuple[str, ...]) -> tuple[str, ...]:
    lower = text.lower()
    return tuple(term for term in terms if term in lower)


def _ocr_quality(text: str) -> str:
    cleaned = _clean_text(text)
    if not cleaned:
        return "empty"
    alnum = len(re.findall(r"[A-Za-z0-9]", cleaned))
    garbage_marks = cleaned.count("�") + cleaned.count("□")
    if len(cleaned) < 80 or alnum < 25:
        return "sparse"
    if garbage_marks >= 3 or (len(cleaned) > 0 and alnum / max(1, len(cleaned)) < 0.18):
        return "garbled"
    if len(cleaned) < 220:
        return "partial"
    return "good"


def classify_ocr_media_output(
    text: object,
    *,
    route: dict[str, Any] | ImageTranscriptionRoute | None = None,
    layout_details: object = None,
    source_context: dict[str, Any] | None = None,
) -> OcrMediaAssessment:
    """Classify OCR output as text/table/chart/diagram and decide VLM escalation."""

    raw_text = str(text or "")
    cleaned = _clean_text(raw_text)
    lower = cleaned.lower()
    quality = _ocr_quality(cleaned)
    route_dict = route.as_dict() if hasattr(route, "as_dict") else dict(route or {})
    route_kind = str(route_dict.get("image_kind") or "").strip().lower()
    labels = _layout_labels(layout_details)
    source_context = dict(source_context or {})
    high_value = source_is_high_value_visual(
        source_context.get("source_name"),
        source_context.get("source_url"),
        source_context.get("document_url"),
        source_context.get("document_title"),
        source_context.get("original_url"),
    )

    chart_matches = set(_matched_terms(cleaned, CHART_TERMS))
    diagram_matches = set(_matched_terms(cleaned, DIAGRAM_TERMS))
    table_matches = set(_matched_terms(cleaned, TABLE_TERMS))
    years = re.findall(r"\b20[0-4][0-9]\b|\b1q[0-9]{2}\b|\b[1-4]q[0-9]{2}\b", lower)
    if len(set(years)) >= 3:
        chart_matches.add("time-series labels")
    if any(label in {"figure", "chart"} for label in labels):
        chart_matches.add("layout:figure")
    if any(label in {"table"} for label in labels):
        table_matches.add("layout:table")
    if any(label in {"formula"} for label in labels):
        diagram_matches.add("layout:formula")

    if any(pattern in lower for pattern in NON_SIGNAL_OCR_PATTERNS) and not chart_matches and not diagram_matches:
        return OcrMediaAssessment(
            media_kind="non_signal_image",
            ocr_quality=quality,
            needs_vlm=False,
            is_signal=False,
            confidence=0.82,
            reason="OCR text matches common non-signal/illustrative media patterns.",
            source_risk="high_value_visual" if high_value else "normal",
            visual_signal_terms=(),
            layout_labels=labels,
        )

    if diagram_matches:
        media_kind = "technical_diagram" if {"tsv", "oxide bond", "hybrid bond", "logic", "dram", "hbm", "interposer"} & diagram_matches else "process_diagram"
    elif chart_matches:
        media_kind = "chart"
    elif table_matches:
        media_kind = "table"
    elif "diagram" in route_kind or "visual" in route_kind or "chart" in route_kind:
        media_kind = "visual_slide"
    elif "text" in route_kind:
        media_kind = "text_page"
    else:
        media_kind = "mixed_text_visual"

    visual_terms = tuple(sorted(chart_matches | diagram_matches | table_matches))
    visual_heavy = media_kind in {"chart", "process_diagram", "technical_diagram", "visual_slide"}
    weak_ocr = quality in {"empty", "sparse", "garbled"}
    needs_vlm = bool(
        visual_heavy
        or (high_value and media_kind in {"mixed_text_visual", "chart", "process_diagram", "technical_diagram", "visual_slide"})
        or (weak_ocr and route_kind not in {"text_page_or_screenshot", "table_or_grid"})
    )
    if media_kind == "table" and not weak_ocr and not high_value:
        needs_vlm = False

    reasons: list[str] = []
    if visual_heavy:
        reasons.append(f"{media_kind} requires visual relationship/value extraction")
    if high_value:
        reasons.append("high-value visual source")
    if weak_ocr:
        reasons.append(f"OCR quality={quality}")
    if visual_terms:
        reasons.append("matched " + ", ".join(visual_terms[:8]))
    if not reasons:
        reasons.append(f"OCR quality={quality}; route_kind={route_kind or 'unknown'}")

    confidence = 0.55
    if visual_terms:
        confidence += 0.18
    if labels:
        confidence += 0.08
    if high_value:
        confidence += 0.07
    if quality == "good":
        confidence += 0.08
    if weak_ocr:
        confidence -= 0.05

    return OcrMediaAssessment(
        media_kind=media_kind,
        ocr_quality=quality,
        needs_vlm=needs_vlm,
        is_signal=True,
        confidence=round(max(0.2, min(confidence, 0.95)), 3),
        reason="; ".join(reasons),
        source_risk="high_value_visual" if high_value else "normal",
        visual_signal_terms=visual_terms,
        layout_labels=labels,
    )


def analyze_image_features(path: str | Path, *, max_size: int = 384) -> ImageRouteFeatures:
    try:
        from PIL import Image, ImageOps
    except Exception as exc:  # pragma: no cover - exercised only when optional dep is absent
        raise RuntimeError("Pillow is required for image route classification.") from exc

    img = Image.open(path)
    img = ImageOps.exif_transpose(img).convert("RGB")
    width, height = img.size
    aspect_ratio = round(width / max(1, height), 4)

    work = img.copy()
    work.thumbnail((max_size, max_size))
    gray = work.convert("L")
    pixels = list(gray.getdata())
    total = max(1, len(pixels))
    dark_pixel_ratio = sum(1 for px in pixels if px < 110) / total

    rgb = list(work.getdata())
    colorfulness = sum((max(px) - min(px)) / 255.0 for px in rgb) / max(1, len(rgb))

    w, h = gray.size
    edge_hits = 0
    comparisons = 0
    for y in range(h):
        row = [gray.getpixel((x, y)) for x in range(w)]
        for x in range(1, w):
            comparisons += 1
            if abs(row[x] - row[x - 1]) > 45:
                edge_hits += 1
    for x in range(w):
        prev = gray.getpixel((x, 0))
        for y in range(1, h):
            cur = gray.getpixel((x, y))
            comparisons += 1
            if abs(cur - prev) > 45:
                edge_hits += 1
            prev = cur
    edge_density = edge_hits / max(1, comparisons)

    foreground_is_bright = dark_pixel_ratio > 0.65

    def foreground(pixel: int) -> bool:
        return pixel > 145 if foreground_is_bright else pixel < 125

    dark_rows = 0
    text_runs = 0
    for y in range(h):
        row_dark = []
        for x in range(w):
            row_dark.append(foreground(gray.getpixel((x, y))))
        dark_count = sum(1 for flag in row_dark if flag)
        if dark_count / max(1, w) > 0.55:
            dark_rows += 1
        in_run = False
        for flag in row_dark:
            if flag and not in_run:
                text_runs += 1
                in_run = True
            elif not flag:
                in_run = False

    dark_cols = 0
    for x in range(w):
        dark_count = 0
        for y in range(h):
            if foreground(gray.getpixel((x, y))):
                dark_count += 1
        if dark_count / max(1, h) > 0.55:
            dark_cols += 1

    return ImageRouteFeatures(
        width=width,
        height=height,
        aspect_ratio=aspect_ratio,
        dark_pixel_ratio=round(dark_pixel_ratio, 5),
        colorfulness=round(colorfulness, 5),
        edge_density=round(edge_density, 5),
        horizontal_line_ratio=round(dark_rows / max(1, h), 5),
        vertical_line_ratio=round(dark_cols / max(1, w), 5),
        text_run_density=round(text_runs / max(1, w * h), 5),
    )


def classify_image_transcription_route(path: str | Path) -> ImageTranscriptionRoute:
    features = analyze_image_features(path)
    table_score = features.horizontal_line_ratio + features.vertical_line_ratio
    ink_score = min(features.dark_pixel_ratio, 0.25)
    if features.dark_pixel_ratio > 0.65:
        ink_score = min(1.0 - features.dark_pixel_ratio, 0.25)
    text_score = features.text_run_density * 12.0 + ink_score
    semantic_score = features.colorfulness + features.edge_density * 3.0

    visual_slide_score = features.colorfulness * 1.3 + features.edge_density * 4.0
    wide_visual = features.aspect_ratio >= 1.6 and features.colorfulness >= 0.12 and features.edge_density >= 0.035

    if table_score >= 0.025 and text_score >= 0.05 and not wide_visual:
        return ImageTranscriptionRoute(
            lane="ocr_first_then_vlm_if_low_confidence",
            image_kind="table_or_grid",
            confidence=round(min(0.95, 0.62 + table_score * 3.0), 3),
            reason="Detected strong horizontal/vertical ruling plus text-like dark runs.",
            features=features,
        )

    if text_score >= 0.08 and features.colorfulness < 0.18:
        return ImageTranscriptionRoute(
            lane="ocr_first",
            image_kind="text_page_or_screenshot",
            confidence=round(min(0.95, 0.58 + text_score), 3),
            reason="Detected dense text-like dark runs with low color/diagram signal.",
            features=features,
        )

    if wide_visual or semantic_score >= 0.18 and text_score < 0.14 or visual_slide_score >= 0.26 and text_score < 0.22:
        return ImageTranscriptionRoute(
            lane="vlm_first",
            image_kind="diagram_chart_or_visual_slide",
            confidence=round(min(0.92, 0.55 + max(semantic_score, visual_slide_score)), 3),
            reason="Detected visual/diagram signal with limited plain-text density.",
            features=features,
            needs_vlm=True,
        )

    return ImageTranscriptionRoute(
        lane="ocr_first_then_vlm_if_low_confidence",
        image_kind="mixed_text_visual",
        confidence=0.56,
        reason="Mixed or ambiguous layout; start with cheap OCR and escalate if sparse or structurally weak.",
        features=features,
    )
