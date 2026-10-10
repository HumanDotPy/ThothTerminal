from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import requests

from thoth_terminal.config import settings
from thoth_terminal.connectors.official_common import keyword_allowed, recent_date_window, stable_external_id
from thoth_terminal.net import DEFAULT_HEADERS

EDINET_BASE = "https://api.edinet-fsa.go.jp/api/v2"


def fetch_edinet_disclosures(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config or {})
    subscription_key = str(cfg.get("subscription_key") or getattr(settings, "edinet_subscription_key", None) or "").strip()
    if not subscription_key:
        raise ValueError("EDINET requires THOTH_EDINET_SUBSCRIPTION_KEY or source config subscription_key.")
    if subscription_key.startswith("edb_"):
        raise ValueError("EDINET official API requires an FSA subscription key; EDINET DB keys must use edinetdb_financials.")
    start_dt, end_dt = recent_date_window(cfg, default_days=7)
    if cfg.get("date"):
        start_dt = end_dt = datetime.strptime(str(cfg.get("date")).strip(), "%Y-%m-%d")
    headers = {
        **DEFAULT_HEADERS,
        "Accept": "application/json",
        "Ocp-Apim-Subscription-Key": subscription_key,
    }
    ordinance_allow = {str(v).strip() for v in (cfg.get("ordinance_codes") or []) if str(v).strip()}
    form_allow = {str(v).strip() for v in (cfg.get("form_codes") or []) if str(v).strip()}
    doc_type_allow = {str(v).strip() for v in (cfg.get("doc_type_codes") or []) if str(v).strip()}

    items: list[dict[str, Any]] = []
    limit = max(1, min(int(cfg.get("max_items_per_run") or 100), 500))
    fetched_rows = 0
    day = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end_day and len(items) < limit:
        target_date = day.strftime("%Y-%m-%d")
        params = {
            "date": target_date,
            "type": str(cfg.get("document_list_type") or "2"),
        }
        resp = requests.get(
            f"{EDINET_BASE}/documents.json",
            params=params,
            headers=headers,
            timeout=float(cfg.get("timeout_s") or 30.0),
        )
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("statusCode"):
            raise ValueError(f"EDINET API error {payload.get('statusCode')}: {payload.get('message')}")
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            rows = []
        fetched_rows += len(rows)
        for row in rows:
            if len(items) >= limit:
                break
            if not isinstance(row, dict):
                continue
            if ordinance_allow and str(row.get("ordinanceCode") or "").strip() not in ordinance_allow:
                continue
            if form_allow and str(row.get("formCode") or "").strip() not in form_allow:
                continue
            if doc_type_allow and str(row.get("docTypeCode") or "").strip() not in doc_type_allow:
                continue
            filer_name = str(row.get("filerName") or "").strip()
            doc_description = str(row.get("docDescription") or row.get("formCode") or "").strip()
            if not keyword_allowed(cfg, filer_name, doc_description, row.get("secCode"), row.get("edinetCode")):
                continue
            published_at = None
            try:
                published_at = datetime.fromisoformat(str(row.get("submitDateTime") or "").replace("Z", "+00:00"))
            except Exception:
                published_at = None
            doc_id = str(row.get("docID") or "").strip()
            url = f"{EDINET_BASE}/documents/{doc_id}?type=1" if doc_id else f"{EDINET_BASE}/documents.json?date={target_date}&type=2"
            normalized = "\n".join(
                part
                for part in [
                    f"{filer_name} - {doc_description}" if filer_name and doc_description else (doc_description or filer_name),
                    f"Filer: {filer_name}" if filer_name else "",
                    f"Document: {doc_description}" if doc_description else "",
                    f"EDINET code: {row.get('edinetCode')}" if row.get("edinetCode") else "",
                    f"Security code: {row.get('secCode')}" if row.get("secCode") else "",
                    f"Period end: {row.get('periodEnd')}" if row.get("periodEnd") else "",
                    f"Submit time: {row.get('submitDateTime')}" if row.get("submitDateTime") else "",
                ]
                if part
            ).strip()
            items.append(
                {
                    "kind": "securities_filing",
                    "external_id": doc_id or stable_external_id("edinet_disclosures", filer_name, doc_description, row.get("secCode")),
                    "url": url,
                    "canonical_url": url,
                    "title": f"{filer_name} - {doc_description}" if filer_name and doc_description else (doc_description or filer_name or "EDINET filing"),
                    "author": filer_name or "EDINET",
                    "published_at": published_at,
                    "raw_text": normalized,
                    "normalized_text": normalized,
                    "extra": {
                        "connector": "edinet_disclosures",
                        "regulator": "EDINET",
                        "country": "JP",
                        "company_name": filer_name or None,
                        "doc_id": doc_id or None,
                        "edinet_code": str(row.get("edinetCode") or "").strip() or None,
                        "sec_code": str(row.get("secCode") or "").strip() or None,
                        "jcn": str(row.get("JCN") or "").strip() or None,
                        "ordinance_code": str(row.get("ordinanceCode") or "").strip() or None,
                        "form_code": str(row.get("formCode") or "").strip() or None,
                        "doc_type_code": str(row.get("docTypeCode") or "").strip() or None,
                        "xbrl_flag": row.get("xbrlFlag"),
                        "pdf_flag": row.get("pdfFlag"),
                        "english_doc_flag": row.get("englishDocFlag"),
                        "api_file_date": target_date,
                        "document_api_url": url,
                        "source_identifiers": [
                            {
                                "id_type": "EDINET",
                                "id_value": str(row.get("edinetCode") or "").strip(),
                                "source_key": "edinet_code",
                                "source": "edinet_disclosures",
                                "market": "JP",
                                "is_primary": True,
                            },
                            {
                                "id_type": "JP_STOCK",
                                "id_value": str(row.get("secCode") or "").strip(),
                                "exchange": "JPX",
                                "source_key": "sec_code",
                                "source": "edinet_disclosures",
                                "market": "JPX",
                            },
                            {
                                "id_type": "JCN",
                                "id_value": str(row.get("JCN") or "").strip(),
                                "source_key": "jcn",
                                "source": "edinet_disclosures",
                                "market": "JP",
                            },
                        ],
                    },
                }
            )
        day += timedelta(days=1)
    return {
        "items": items,
        "cursor": None,
        "stats": {"count": len(items), "fetched_rows": fetched_rows, "source_kind": "edinet_disclosures"},
    }
