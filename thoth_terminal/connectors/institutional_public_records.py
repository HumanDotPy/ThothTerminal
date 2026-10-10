from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import requests

from thoth_terminal.connectors.official_common import keyword_allowed, safe_json_dumps, stable_external_id
from thoth_terminal.net import DEFAULT_HEADERS


_CONNECTOR_META: dict[str, dict[str, Any]] = {
    "eia_860m": {
        "source_system": "EIA 860M",
        "record_kind": "power",
        "default_url": "https://www.eia.gov/electricity/data/eia860m/",
        "title_fields": ["plant_name", "entity_name", "generator_id"],
        "text_fields": ["plant_name", "entity_name", "status", "county", "state"],
        "date_fields": ["report_date", "report_month", "reporting_month"],
        "id_fields": ["plant_id", "generator_id"],
        "source_family": "power",
        "cadence_label": "monthly",
    },
    "annual_eia_860": {
        "source_system": "EIA 860 Annual",
        "record_kind": "power",
        "default_url": "https://www.eia.gov/electricity/data/eia860/",
        "title_fields": ["plant_name", "entity_name", "utility_name", "generator_id"],
        "text_fields": ["plant_name", "entity_name", "utility_name", "status", "county", "state", "technology"],
        "date_fields": ["report_year", "year", "published_at"],
        "id_fields": ["plant_id", "plant_code", "generator_id"],
        "source_family": "power",
        "cadence_label": "annual",
    },
    "eia_861": {
        "source_system": "EIA 861",
        "record_kind": "power",
        "default_url": "https://www.eia.gov/electricity/data/eia861/",
        "title_fields": ["utility_name", "respondent_name", "balancing_authority_code"],
        "text_fields": ["utility_name", "respondent_name", "state", "service_type", "sector", "balancing_authority_code"],
        "date_fields": ["report_year", "year", "published_at"],
        "id_fields": ["utility_id", "respondent_id", "eia_utility_id"],
        "source_family": "power",
        "cadence_label": "annual",
    },
    "eia_923": {
        "source_system": "EIA 923",
        "record_kind": "power",
        "default_url": "https://www.eia.gov/electricity/data/eia923/",
        "title_fields": ["plant_name", "operator_name", "plant_id"],
        "text_fields": ["plant_name", "operator_name", "state", "fuel_type", "generation_mwh", "net_generation_mwh"],
        "date_fields": ["report_month", "report_year", "year", "published_at"],
        "id_fields": ["plant_id", "operator_id", "generator_id"],
        "source_family": "power",
        "cadence_label": "monthly",
    },
    "ferc_elibrary": {
        "source_system": "FERC eLibrary",
        "record_kind": "regulatory_docket",
        "default_url": "https://elibrary.ferc.gov/eLibrary/search",
        "title_fields": ["description", "title", "docket_number", "accession_number"],
        "text_fields": ["description", "title", "docket_number", "sub_docket_number", "applicant", "company_name"],
        "date_fields": ["filed_date", "issued_date", "published_at"],
        "id_fields": ["accession_number", "document_id", "docket_number"],
        "url_fields": ["url", "document_url", "download_url"],
        "source_family": "regulatory",
        "cadence_label": "daily",
    },
    "ferc_form1": {
        "source_system": "FERC Form 1",
        "record_kind": "utility_financial",
        "default_url": "https://www.ferc.gov/industries-data/electric/general-information/electric-industry-forms/form-1-electric-utility-annual-report",
        "title_fields": ["respondent_name", "company_name", "utility_name", "report_year"],
        "text_fields": ["respondent_name", "company_name", "utility_name", "state", "report_year", "schedule_name"],
        "date_fields": ["report_year", "year", "published_at"],
        "id_fields": ["respondent_id", "respondent_name", "company_name"],
        "source_family": "power",
        "cadence_label": "annual",
    },
    "pjm_planning_center": {
        "source_system": "PJM Planning Center",
        "record_kind": "power",
        "default_url": "https://www.pjm.com/planning",
        "title_fields": ["project_name", "queue_id", "substation"],
        "text_fields": ["project_name", "queue_id", "substation", "county", "state", "status"],
        "date_fields": ["submitted_date", "status_date", "updated_at"],
        "id_fields": ["queue_id", "project_id"],
        "source_family": "power",
        "cadence_label": "daily",
    },
    "caiso_interconnection_queue": {
        "source_system": "CAISO Queue",
        "record_kind": "power",
        "default_url": "https://www.caiso.com/planning/Pages/GeneratorInterconnection/Default.aspx",
        "title_fields": ["project_name", "queue_number", "substation"],
        "text_fields": ["project_name", "queue_number", "county", "state", "status"],
        "date_fields": ["queue_date", "updated_at", "status_date"],
        "id_fields": ["queue_number", "project_id"],
        "source_family": "power",
        "cadence_label": "daily",
    },
    "miso_interconnection_queue": {
        "source_system": "MISO Queue",
        "record_kind": "power",
        "default_url": "https://www.misoenergy.org/planning/generator-interconnection/GI_Queue/",
        "title_fields": ["project_name", "queue_number", "queue_id", "poi_name"],
        "text_fields": ["project_name", "queue_number", "poi_name", "county", "state", "status", "fuel_type"],
        "date_fields": ["queue_date", "submitted_date", "updated_at", "status_date"],
        "id_fields": ["queue_number", "queue_id", "project_id"],
        "url_fields": ["url", "project_url"],
        "source_family": "power",
        "cadence_label": "daily",
    },
    "spp_interconnection_queue": {
        "source_system": "SPP Queue",
        "record_kind": "power",
        "default_url": "https://www.spp.org/engineering/generator-interconnection/",
        "title_fields": ["project_name", "queue_number", "queue_id", "interconnection_request"],
        "text_fields": ["project_name", "queue_number", "county", "state", "status", "fuel_type", "transmission_owner"],
        "date_fields": ["queue_date", "submitted_date", "updated_at", "status_date"],
        "id_fields": ["queue_number", "queue_id", "project_id", "interconnection_request"],
        "url_fields": ["url", "project_url"],
        "source_family": "power",
        "cadence_label": "daily",
    },
    "nyiso_interconnection_queue": {
        "source_system": "NYISO Queue",
        "record_kind": "power",
        "default_url": "https://www.nyiso.com/interconnections",
        "title_fields": ["project_name", "queue_number", "queue_id", "developer"],
        "text_fields": ["project_name", "queue_number", "developer", "county", "zone", "state", "status"],
        "date_fields": ["queue_date", "submitted_date", "updated_at", "status_date"],
        "id_fields": ["queue_number", "queue_id", "project_id"],
        "url_fields": ["url", "project_url"],
        "source_family": "power",
        "cadence_label": "daily",
    },
    "iso_ne_interconnection_queue": {
        "source_system": "ISO-NE Queue",
        "record_kind": "power",
        "default_url": "https://www.iso-ne.com/system-planning/interconnection-service-requests/",
        "title_fields": ["project_name", "queue_number", "queue_id", "interconnection_request"],
        "text_fields": ["project_name", "queue_number", "state", "status", "fuel_type", "in_service_date"],
        "date_fields": ["queue_date", "submitted_date", "updated_at", "status_date"],
        "id_fields": ["queue_number", "queue_id", "project_id", "interconnection_request"],
        "url_fields": ["url", "project_url"],
        "source_family": "power",
        "cadence_label": "daily",
    },
    "ercot_large_load": {
        "source_system": "ERCOT Large Loads",
        "record_kind": "power",
        "default_url": "https://www.ercot.com/gridinfo/load/load_forecasts",
        "title_fields": ["load_name", "project_name", "substation"],
        "text_fields": ["load_name", "project_name", "substation", "county", "status"],
        "date_fields": ["submitted_date", "energization_date", "updated_at"],
        "id_fields": ["load_id", "project_id"],
        "source_family": "power",
        "cadence_label": "daily",
    },
    "ercot_llwg": {
        "source_system": "ERCOT LLWG",
        "record_kind": "power",
        "default_url": "https://www.ercot.com/committees/other/llwg",
        "title_fields": ["meeting_title", "agenda_item", "project_name"],
        "text_fields": ["meeting_title", "agenda_item", "project_name", "summary"],
        "date_fields": ["meeting_date", "updated_at", "published_at"],
        "id_fields": ["meeting_id", "agenda_id", "project_id"],
        "source_family": "power",
        "cadence_label": "daily",
    },
    "puc_dockets": {
        "source_system": "PUC Dockets",
        "record_kind": "regulatory_docket",
        "default_url": "",
        "title_fields": ["title", "case_title", "docket_title", "docket_number"],
        "text_fields": ["title", "case_title", "docket_number", "applicant", "utility_name", "summary", "status"],
        "date_fields": ["filed_date", "opened_date", "issued_date", "published_at", "updated_at"],
        "id_fields": ["docket_number", "case_number", "document_id"],
        "url_fields": ["url", "document_url", "docket_url"],
        "source_family": "regulatory",
        "cadence_label": "daily",
        "jurisdiction_template_kind": "puc_dockets",
    },
    "utility_irp": {
        "source_system": "Utility IRP",
        "record_kind": "utility_planning",
        "default_url": "",
        "title_fields": ["title", "plan_title", "utility_name", "docket_number"],
        "text_fields": ["title", "plan_title", "utility_name", "docket_number", "resource_need", "capacity_mw", "summary"],
        "date_fields": ["filed_date", "published_at", "plan_year", "updated_at"],
        "id_fields": ["document_id", "docket_number", "plan_id"],
        "url_fields": ["url", "document_url", "docket_url"],
        "source_family": "regulatory",
        "cadence_label": "quarterly",
        "jurisdiction_template_kind": "utility_irp",
    },
    "utility_load_forecast": {
        "source_system": "Utility Load Forecast",
        "record_kind": "utility_planning",
        "default_url": "",
        "title_fields": ["title", "forecast_title", "utility_name", "service_area"],
        "text_fields": ["title", "forecast_title", "utility_name", "service_area", "peak_load_mw", "load_growth", "scenario"],
        "date_fields": ["forecast_date", "filed_date", "published_at", "updated_at"],
        "id_fields": ["forecast_id", "document_id", "docket_number"],
        "url_fields": ["url", "document_url", "docket_url"],
        "source_family": "power",
        "cadence_label": "quarterly",
        "jurisdiction_template_kind": "utility_load_forecast",
    },
    "county_permits": {
        "source_system": "County Permits",
        "record_kind": "permit",
        "default_url": "",
        "title_fields": ["project_name", "permit_name", "address", "permit_number"],
        "text_fields": ["project_name", "permit_name", "address", "applicant", "description", "status", "permit_type"],
        "date_fields": ["issued_date", "submitted_date", "filed_date", "updated_at"],
        "id_fields": ["permit_number", "permit_id", "application_number"],
        "url_fields": ["url", "permit_url", "document_url"],
        "source_family": "permits",
        "cadence_label": "daily",
        "jurisdiction_template_kind": "county_permits",
    },
    "municipal_permits": {
        "source_system": "Municipal Permits",
        "record_kind": "permit",
        "default_url": "",
        "title_fields": ["project_name", "permit_name", "address", "permit_number"],
        "text_fields": ["project_name", "permit_name", "address", "applicant", "description", "status", "permit_type"],
        "date_fields": ["issued_date", "submitted_date", "filed_date", "updated_at"],
        "id_fields": ["permit_number", "permit_id", "application_number"],
        "url_fields": ["url", "permit_url", "document_url"],
        "source_family": "permits",
        "cadence_label": "daily",
        "jurisdiction_template_kind": "municipal_permits",
    },
    "water_permits": {
        "source_system": "Water Permits",
        "record_kind": "water_permit",
        "default_url": "",
        "title_fields": ["facility_name", "project_name", "permit_number", "applicant"],
        "text_fields": ["facility_name", "project_name", "permit_number", "applicant", "water_source", "county", "state", "status"],
        "date_fields": ["issued_date", "submitted_date", "effective_date", "expiration_date", "updated_at"],
        "id_fields": ["permit_number", "permit_id", "application_number"],
        "url_fields": ["url", "permit_url", "document_url"],
        "source_family": "permits",
        "cadence_label": "daily",
        "jurisdiction_template_kind": "water_permits",
    },
    "epa_air_permits": {
        "source_system": "EPA Air Permits",
        "record_kind": "air_permit",
        "default_url": "https://www.epa.gov/caa-permitting",
        "title_fields": ["facility_name", "project_name", "permit_number", "applicant"],
        "text_fields": ["facility_name", "project_name", "permit_number", "applicant", "permit_type", "county", "state", "status"],
        "date_fields": ["issued_date", "submitted_date", "effective_date", "expiration_date", "updated_at"],
        "id_fields": ["permit_number", "permit_id", "application_number"],
        "url_fields": ["url", "permit_url", "document_url"],
        "source_family": "permits",
        "cadence_label": "daily",
        "jurisdiction_template_kind": "epa_air_permits",
    },
    "state_air_permits": {
        "source_system": "State Air Permits",
        "record_kind": "air_permit",
        "default_url": "",
        "title_fields": ["facility_name", "project_name", "permit_number", "applicant"],
        "text_fields": ["facility_name", "project_name", "permit_number", "applicant", "permit_type", "county", "state", "status"],
        "date_fields": ["issued_date", "submitted_date", "effective_date", "expiration_date", "updated_at"],
        "id_fields": ["permit_number", "permit_id", "application_number"],
        "url_fields": ["url", "permit_url", "document_url"],
        "source_family": "permits",
        "cadence_label": "daily",
        "jurisdiction_template_kind": "state_air_permits",
    },
    "copernicus_browser": {
        "source_system": "Copernicus Browser",
        "record_kind": "imagery",
        "default_url": "https://browser.dataspace.copernicus.eu/",
        "title_fields": ["name", "scene_id", "tile_id"],
        "text_fields": ["name", "scene_id", "tile_id", "platform", "cloud_cover"],
        "date_fields": ["acquired_at", "datetime", "published_at"],
        "id_fields": ["scene_id", "tile_id"],
        "url_fields": ["preview_url", "asset_url"],
        "source_family": "imagery",
        "cadence_label": "daily",
    },
    "usgs_earthexplorer": {
        "source_system": "USGS EarthExplorer",
        "record_kind": "imagery",
        "default_url": "https://earthexplorer.usgs.gov/",
        "title_fields": ["display_id", "entity_id", "dataset_name"],
        "text_fields": ["display_id", "entity_id", "dataset_name", "acquisition_date"],
        "date_fields": ["acquisition_date", "published_at"],
        "id_fields": ["entity_id", "display_id"],
        "url_fields": ["browse_url", "download_url"],
        "source_family": "imagery",
        "cadence_label": "daily",
    },
    "usgs_naip": {
        "source_system": "USGS NAIP",
        "record_kind": "imagery",
        "default_url": "https://www.usgs.gov/centers/eros/science/usgs-eros-archive-aerial-photography-national-agriculture-imagery-program-naip",
        "title_fields": ["scene_name", "scene_id", "county"],
        "text_fields": ["scene_name", "scene_id", "county", "state"],
        "date_fields": ["acquisition_date", "published_at"],
        "id_fields": ["scene_id"],
        "url_fields": ["download_url", "browse_url"],
        "source_family": "imagery",
        "cadence_label": "daily",
    },
    "cbp_cross": {
        "source_system": "CBP CROSS",
        "record_kind": "classification",
        "default_url": "https://rulings.cbp.gov/",
        "title_fields": ["title", "ruling_number", "hts_number"],
        "text_fields": ["title", "ruling_number", "hts_number", "summary"],
        "date_fields": ["issue_date", "published_at"],
        "id_fields": ["ruling_number"],
        "url_fields": ["url"],
        "source_family": "technical",
        "cadence_label": "daily",
    },
    "usitc_hts": {
        "source_system": "USITC HTS",
        "record_kind": "classification",
        "default_url": "https://hts.usitc.gov/",
        "title_fields": ["htsno", "description", "heading"],
        "text_fields": ["htsno", "description", "heading"],
        "date_fields": ["effective_date", "published_at"],
        "id_fields": ["htsno"],
        "url_fields": ["url"],
        "source_family": "technical",
        "cadence_label": "daily",
    },
    "eu_taric": {
        "source_system": "EU TARIC",
        "record_kind": "classification",
        "default_url": "https://taxation-customs.ec.europa.eu/customs/calculation-customs-duties/customs-tariff_en",
        "title_fields": ["commodity_code", "description"],
        "text_fields": ["commodity_code", "description", "measure_type"],
        "date_fields": ["valid_from", "published_at"],
        "id_fields": ["commodity_code", "measure_sid"],
        "url_fields": ["url"],
        "source_family": "technical",
        "cadence_label": "daily",
    },
    "fcc_oet_kdb": {
        "source_system": "FCC OET KDB",
        "record_kind": "certification",
        "default_url": "https://apps.fcc.gov/oetcf/kdb/",
        "title_fields": ["title", "publication_number", "kdb_number"],
        "text_fields": ["title", "publication_number", "kdb_number", "summary"],
        "date_fields": ["publish_date", "published_at"],
        "id_fields": ["publication_number", "kdb_number"],
        "url_fields": ["url"],
        "source_family": "technical",
        "cadence_label": "daily",
    },
    "chips_funding": {
        "source_system": "CHIPS Funding",
        "record_kind": "procurement",
        "default_url": "https://www.nist.gov/chips",
        "title_fields": ["title", "awardee", "program_name"],
        "text_fields": ["title", "awardee", "program_name", "summary"],
        "date_fields": ["announcement_date", "published_at"],
        "id_fields": ["award_id", "notice_id", "program_id"],
        "url_fields": ["url"],
        "source_family": "procurement",
        "cadence_label": "daily",
    },
}


def _canonical_field_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _normalise_row(row: dict[str, Any]) -> dict[str, Any]:
    nested = row
    for key in ("attributes", "properties"):
        if isinstance(row.get(key), dict):
            nested = {**dict(row), **dict(row[key])}
            break
    normalised: dict[str, Any] = {}
    for key, value in nested.items():
        normalised[str(key)] = value
        canonical = _canonical_field_name(key)
        if canonical and canonical not in normalised:
            normalised[canonical] = value
    return normalised


def _pick_first(row: dict[str, Any], fields: list[str]) -> str | None:
    for field in fields:
        value = str(row.get(field) or row.get(_canonical_field_name(field)) or "").strip()
        if value:
            return value
    return None


def _parse_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        pass
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d",
        "%Y/%m/%d %H:%M",
        "%m/%d/%Y",
        "%m/%d/%Y %H:%M",
        "%Y%m%d",
        "%Y",
    ):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            continue
    return None


def _extract_rows(payload: object, json_items_path: str | None) -> list[dict[str, Any]]:
    value = payload
    if json_items_path:
        for token in [part.strip() for part in json_items_path.split(".") if part.strip()]:
            if isinstance(value, dict):
                value = value.get(token)
            elif isinstance(value, list) and token.isdigit():
                index = int(token)
                value = value[index] if 0 <= index < len(value) else None
            else:
                value = None
            if value is None:
                break
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for key in ("items", "results", "data", "records", "value", "features"):
            rows = value.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        for path in (
            ("response", "data"),
            ("response", "items"),
            ("response", "records"),
            ("result", "data"),
            ("result", "records"),
            ("d", "results"),
        ):
            nested = value
            for token in path:
                nested = nested.get(token) if isinstance(nested, dict) else None
                if nested is None:
                    break
            if isinstance(nested, list):
                return [row for row in nested if isinstance(row, dict)]
    return []


def _response_format(resp: requests.Response, url: str, config: dict[str, Any]) -> str:
    configured = str(config.get("response_format") or "").strip().lower()
    if configured in {"json", "csv", "tsv"}:
        return configured
    content_type = str(resp.headers.get("content-type") or "").lower()
    url_lower = url.lower()
    if "json" in content_type or url_lower.endswith(".json"):
        return "json"
    if "tab-separated" in content_type or url_lower.endswith(".tsv") or url_lower.endswith(".tab"):
        return "tsv"
    if "csv" in content_type or url_lower.endswith(".csv"):
        return "csv"
    return "json"


def _rows_from_response(resp: requests.Response, url: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    fmt = _response_format(resp, url, config)
    if fmt == "json":
        payload = resp.json()
        return [_normalise_row(row) for row in _extract_rows(payload, str(config.get("json_items_path") or "").strip() or None)]
    delimiter = "\t" if fmt == "tsv" else str(config.get("delimiter") or ",")
    reader = csv.DictReader(io.StringIO(resp.text), delimiter=delimiter)
    return [_normalise_row(dict(row)) for row in reader]


def _default_title(row: dict[str, Any], meta: dict[str, Any]) -> str:
    title = _pick_first(row, list(meta.get("title_fields") or []))
    return title or _pick_first(row, ["title", "name", "description"]) or f"{meta.get('source_system')} record"


def _default_text(row: dict[str, Any], meta: dict[str, Any]) -> str:
    fields = [field for field in list(meta.get("text_fields") or []) if str(row.get(field) or "").strip()]
    if fields:
        return "\n".join(str(row.get(field) or "").strip() for field in fields)
    return safe_json_dumps(row)


def _default_url(row: dict[str, Any], meta: dict[str, Any], base_url: str) -> str:
    value = _pick_first(row, list(meta.get("url_fields") or [])) or base_url
    return urljoin(base_url, value)


def _request(config: dict[str, Any], url: str) -> requests.Response:
    method = str(config.get("method") or "GET").strip().upper()
    timeout_s = float(config.get("timeout_s") or 30.0)
    headers = {**DEFAULT_HEADERS, **dict(config.get("headers") or {})}
    query_params = dict(config.get("query_params") or {})
    if method == "POST":
        resp = requests.post(
            url,
            params=query_params,
            json=(config.get("request_body") if isinstance(config.get("request_body"), dict) else None),
            data=(config.get("request_data") if isinstance(config.get("request_data"), dict) else None),
            headers=headers,
            timeout=timeout_s,
        )
    else:
        resp = requests.get(url, params=query_params, headers=headers, timeout=timeout_s)
    resp.raise_for_status()
    return resp


def public_record_connector_names() -> list[str]:
    return sorted(_CONNECTOR_META)


def public_record_connector_profile(connector: str) -> dict[str, Any]:
    meta = dict(_CONNECTOR_META[str(connector or "").strip()])
    return {
        "connector": connector,
        "source_system": meta.get("source_system"),
        "record_kind": meta.get("record_kind"),
        "source_family": meta.get("source_family") or "general",
        "rights_class": "public",
        "acquisition_mode": "manual_template" if meta.get("jurisdiction_template_kind") else "connector",
        "cadence_label": meta.get("cadence_label") or "event_driven",
        "review_required": bool(meta.get("jurisdiction_template_kind")),
        "subject_binding_kind": "subject",
        "site_binding_kind": "site" if meta.get("source_family") in {"power", "permits"} else None,
        "jurisdiction_template_kind": meta.get("jurisdiction_template_kind"),
    }


def _fetch_public_records(config: dict[str, Any], *, connector: str) -> dict[str, Any]:
    cfg = dict(config or {})
    meta = _CONNECTOR_META[connector]
    url = str(cfg.get("data_url") or cfg.get("api_url") or cfg.get("list_url") or meta.get("default_url") or "").strip()
    if not url:
        raise ValueError(f"{connector} requires data_url, api_url, or list_url in source config.")
    rows = _rows_from_response(_request(cfg, url), url, cfg)
    limit = max(1, min(int(cfg.get("max_items_per_run") or 50), 500))
    items: list[dict[str, Any]] = []
    for row in rows:
        title = _default_title(row, meta)
        text = _default_text(row, meta)
        external_id = _pick_first(row, list(meta.get("id_fields") or []))
        if not keyword_allowed(cfg, title, text, external_id):
            continue
        published_at = _parse_datetime(_pick_first(row, list(meta.get("date_fields") or [])))
        item_url = _default_url(row, meta, url)
        items.append(
            {
                "kind": "institutional_record",
                "external_id": external_id or stable_external_id(connector, title, text),
                "url": item_url,
                "canonical_url": item_url,
                "title": title,
                "author": str(meta.get("source_system") or connector),
                "published_at": published_at,
                "raw_text": text,
                "normalized_text": text,
                "extra": {
                    "connector": connector,
                    "source_system": meta.get("source_system"),
                    "record_kind": meta.get("record_kind"),
                    "source_family": meta.get("source_family"),
                    "jurisdiction_template_kind": meta.get("jurisdiction_template_kind"),
                    "row": dict(row),
                },
            }
        )
        if len(items) >= limit:
            break
    return {
        "items": items,
        "cursor": None,
        "stats": {
            "count": len(items),
            "source_kind": connector,
            "record_kind": meta.get("record_kind"),
            "source_family": meta.get("source_family"),
        },
    }


def fetch_eia_860m(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="eia_860m")


def fetch_annual_eia_860(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="annual_eia_860")


def fetch_eia_861(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="eia_861")


def fetch_eia_923(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="eia_923")


def fetch_ferc_elibrary(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="ferc_elibrary")


def fetch_ferc_form1(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="ferc_form1")


def fetch_pjm_planning_center(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="pjm_planning_center")


def fetch_caiso_interconnection_queue(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="caiso_interconnection_queue")


def fetch_miso_interconnection_queue(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="miso_interconnection_queue")


def fetch_spp_interconnection_queue(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="spp_interconnection_queue")


def fetch_nyiso_interconnection_queue(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="nyiso_interconnection_queue")


def fetch_iso_ne_interconnection_queue(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="iso_ne_interconnection_queue")


def fetch_ercot_large_load(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="ercot_large_load")


def fetch_ercot_llwg(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="ercot_llwg")


def fetch_puc_dockets(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="puc_dockets")


def fetch_utility_irp(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="utility_irp")


def fetch_utility_load_forecast(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="utility_load_forecast")


def fetch_county_permits(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="county_permits")


def fetch_municipal_permits(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="municipal_permits")


def fetch_water_permits(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="water_permits")


def fetch_epa_air_permits(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="epa_air_permits")


def fetch_state_air_permits(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="state_air_permits")


def fetch_copernicus_browser(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="copernicus_browser")


def fetch_usgs_earthexplorer(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="usgs_earthexplorer")


def fetch_usgs_naip(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="usgs_naip")


def fetch_cbp_cross(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="cbp_cross")


def fetch_usitc_hts(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="usitc_hts")


def fetch_eu_taric(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="eu_taric")


def fetch_fcc_oet_kdb(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="fcc_oet_kdb")


def fetch_chips_funding(config: dict[str, Any]) -> dict[str, Any]:
    return _fetch_public_records(config, connector="chips_funding")
