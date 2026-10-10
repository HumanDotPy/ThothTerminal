from __future__ import annotations

import csv
import io
import re
import zipfile
from typing import Any

import requests

from thoth_terminal.net import DEFAULT_HEADERS


EDINET_CODE_LIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
EDINET_CODE_LIST_DOWNLOAD_PAGE = "https://disclosure2.edinet-fsa.go.jp/weee0010.aspx"


def _normalize_jp_stock_code(value: object) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    raw = re.sub(r"\.(T|JP)$", "", raw)
    raw = re.sub(r"[^0-9A-Z]+", "", raw)
    if raw.endswith("0"):
        candidate = raw[:-1]
        if re.fullmatch(r"[0-9]{4}[A-Z]?|[0-9]{3}[A-Z]", candidate):
            raw = candidate
    return raw if re.fullmatch(r"[0-9]{4}[A-Z]?|[0-9]{3}[A-Z]", raw) else ""


def _row_value(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def parse_edinet_code_list_zip(content: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        csv_name = next((name for name in archive.namelist() if name.lower().endswith(".csv")), None)
        if not csv_name:
            raise ValueError("EDINET code list zip did not contain a CSV file.")
        raw = archive.read(csv_name)
    text = raw.decode("cp932")
    reader = csv.reader(io.StringIO(text))
    meta = next(reader, [])
    headers = next(reader, [])
    rows: list[dict[str, Any]] = []
    for values in reader:
        if not values or not any(str(value or "").strip() for value in values):
            continue
        row = {str(headers[idx] or "").strip(): str(value or "").strip() for idx, value in enumerate(values[: len(headers)])}
        edinet_code = _row_value(row, "ＥＤＩＮＥＴコード", "EDINETコード")
        security_code = _row_value(row, "証券コード", "証券コード等")
        stock_code = _normalize_jp_stock_code(security_code)
        if not edinet_code:
            continue
        rows.append(
            {
                "edinet_code": edinet_code,
                "stock_code": stock_code or None,
                "security_code": security_code or None,
                "filer_type": _row_value(row, "提出者種別") or None,
                "listed_status": _row_value(row, "上場区分") or None,
                "consolidated": _row_value(row, "連結の有無") or None,
                "capital_stock": _row_value(row, "資本金") or None,
                "fiscal_year_end": _row_value(row, "決算日") or None,
                "filer_name": _row_value(row, "提出者名") or None,
                "filer_name_en": _row_value(row, "提出者名（英字）") or None,
                "filer_name_kana": _row_value(row, "提出者名（ヨミ）") or None,
                "location": _row_value(row, "所在地") or None,
                "industry": _row_value(row, "提出者業種") or None,
                "corporate_number": _row_value(row, "提出者法人番号") or None,
            }
        )
    return {
        "rows": rows,
        "stats": {
            "count": len(rows),
            "csv_name": csv_name,
            "source_kind": "edinet_code_list",
            "download_meta": meta,
        },
    }


def fetch_edinet_code_list(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(config or {})
    url = str(cfg.get("url") or EDINET_CODE_LIST_URL)
    resp = requests.get(
        url,
        headers={**DEFAULT_HEADERS, "Accept": "application/octet-stream,application/zip,*/*"},
        timeout=float(cfg.get("timeout_s") or 30.0),
    )
    resp.raise_for_status()
    result = parse_edinet_code_list_zip(resp.content)
    result["stats"] = {**dict(result.get("stats") or {}), "url": url, "download_page": EDINET_CODE_LIST_DOWNLOAD_PAGE}
    return result
