"""Initialize application vocabulary and local ingestion without user research.

Companies, external sources, institutional identities and supply-chain graphs
belong to the user's workspace and are never synthesized during initialization.
Existing workspace objects and edited source configuration are left intact.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.models import Domain, EventType, Source, SourceState, Tag


DOMAINS: list[dict[str, str]] = [
    {"slug": "compute_platforms", "name": "Compute Platforms"},
    {"slug": "memory", "name": "Memory"},
    {"slug": "advanced_packaging", "name": "Advanced Packaging"},
    {"slug": "foundry_manufacturing", "name": "Foundry & Manufacturing"},
    {"slug": "supply_chain_materials", "name": "Supply Chain & Materials"},
    {"slug": "systems_racks", "name": "Systems & Racks"},
    {"slug": "hyperscalers_customers", "name": "Hyperscalers & Customers"},
    {"slug": "policy_geopolitics", "name": "Policy & Geopolitics"},
    {"slug": "financial_broker_signals", "name": "Financial / Broker Signals"},
    {"slug": "meta_market_structure", "name": "Meta / Market Structure"},
    {"slug": "research_library", "name": "Research Library"},
]

EVENT_TYPES: list[dict[str, str]] = [
    {"slug": "price_move", "name": "Price move"},
    {"slug": "supply_shortage", "name": "Supply shortage"},
    {"slug": "capacity_expansion", "name": "Capacity expansion"},
    {"slug": "design_win", "name": "Design win"},
    {"slug": "adoption_timing", "name": "Adoption timing"},
    {"slug": "technology_transition", "name": "Technology transition"},
    {"slug": "regulatory_change", "name": "Regulatory change"},
    {"slug": "margin_shift", "name": "Margin shift"},
    {"slug": "lead_time_change", "name": "Lead time change"},
    {"slug": "qualification", "name": "Qualification / certification"},
    {"slug": "research_report", "name": "Research report"},
]

TAGS: list[dict[str, str]] = [
    {"slug": "watch", "name": "watch"},
    {"slug": "early_signal", "name": "early signal"},
    {"slug": "needs_confirmation", "name": "needs confirmation"},
    {"slug": "rumor", "name": "rumor"},
    {"slug": "high_conviction", "name": "high conviction"},
    {"slug": "contrarian", "name": "contrarian"},
    {"slug": "supercycle", "name": "supercycle"},
    {"slug": "deep-research", "name": "deep research"},
    {"slug": "research-library", "name": "research library"},
]

def seed_all(engine: Engine) -> None:
    """Idempotently install generic defaults in the supplied database."""
    with Session(engine) as session:
        for model, definitions in ((Domain, DOMAINS), (EventType, EVENT_TYPES), (Tag, TAGS)):
            for definition in definitions:
                if session.query(model).filter_by(slug=definition["slug"]).first() is None:
                    session.add(model(slug=definition["slug"], name=definition["name"]))

        now = utcnow()
        sources = (
            ("internal", "internal", None, {}),
            (
                "local_deep_research_library", "research", 3600,
                {
                    "connector": "local_research_library",
                    "library_dir": "research/inbox",
                    "max_files_per_run": 50,
                    "refresh_existing": True,
                    "enabled": True,
                    "full_refresh_days": 90,
                    "addendum_extension_days": 45,
                    "default_domains": ["research_library"],
                    "default_tags": ["deep-research", "research-library"],
                    "default_event_types": ["research_report"],
                },
            ),
        )
        for name, category, interval, config in sources:
            source = session.query(Source).filter_by(name=name).first()
            if source is None:
                source = Source(
                    name=name, category=category, priority=5,
                    polling_interval_seconds=interval, config=config, created_at=now,
                )
                session.add(source)
                session.flush()
            if session.query(SourceState).filter_by(source_id=source.id).first() is None:
                session.add(SourceState(
                    source_id=source.id,
                    updated_at=now,
                    next_run_at=(now + timedelta(seconds=source.polling_interval_seconds))
                    if source.polling_interval_seconds else None,
                ))
        session.commit()
