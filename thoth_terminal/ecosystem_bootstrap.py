"""Explicit integration setup requests; research graphs are user-owned."""
from __future__ import annotations

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from thoth_terminal.config import settings
from thoth_terminal.instrument_service import build_instrument_request_key, create_instrument_request


def bootstrap_missing_integration_requests(session: Session) -> dict[str, int]:
    missing_keys: list[str] = []
    if not settings.edinet_subscription_key:
        missing_keys.append("THOTH_EDINET_SUBSCRIPTION_KEY")
    if not settings.usitc_dataweb_api_key:
        missing_keys.append("THOTH_USITC_DATAWEB_API_KEY")
    if not settings.un_comtrade_api_key:
        missing_keys.append("THOTH_UN_COMTRADE_API_KEY")
    if not settings.sam_gov_api_key:
        missing_keys.append("THOTH_SAM_GOV_API_KEY")
    if not missing_keys:
        return {"created": 0, "updated": 0}

    request_key = build_instrument_request_key(
        intent="ops_config",
        title="Populate official rail API keys",
        target_themes=["ops", "official rails"],
        desired_outputs=missing_keys,
    )
    prior = session.execute(
        sql_text("SELECT 1 FROM instrument_requests WHERE request_key = :request_key LIMIT 1"),
        {"request_key": request_key},
    ).first()
    create_instrument_request(
        session,
        title="Populate official rail API keys (EDINET / USITC / SAM.gov)",
        intent="ops_config",
        summary=(
            "Enable the missing official rails by populating the EDINET, USITC DataWeb, UN Comtrade, and SAM.gov "
            "credentials in the runtime environment and then re-running the source bootstrap."
        ),
        target_entities=["EDINET", "USITC DataWeb", "UN Comtrade", "SAM.gov"],
        target_themes=["official rails", "ops configuration", "source enablement"],
        desired_outputs=[
            "Populate THOTH_EDINET_SUBSCRIPTION_KEY",
            "Populate THOTH_USITC_DATAWEB_API_KEY",
            "Populate THOTH_UN_COMTRADE_API_KEY",
            "Populate THOTH_SAM_GOV_API_KEY",
        ],
        provenance_summary="Seed bootstrap detected disabled official-rail integrations because the required credentials are not configured.",
        evidence_tier="system",
        status="draft",
        priority=9,
        automation_source="seed_all",
        source_kind="ops_gap",
        draft_payload={
            "missing_env_keys": missing_keys,
            "affected_connectors": ["edinet_disclosures", "trade_usitc_dataweb", "trade_un_comtrade", "sam_gov_contracts"],
            "next_step": "Populate the missing keys in .env and rerun `thoth seed`.",
        },
        request_key=request_key,
    )
    return {"created": 0 if prior else 1, "updated": 1 if prior else 0}
