from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any


SECTIONS = (
    "runtime_core",
    "ops_routes",
    "deepseek_harness",
    "external_agent_api",
    "research_desk",
    "codex_usage",
    "feed_document_helpers",
    "entity_report_helpers",
    "supply_chain_dataset_helpers",
    "research_plays",
    "universes",
    "documents",
    "reports_knowledge",
    "notifications",
    "newsletter",
    "source_routes",
    "companies",
    "calendar_canon",
    "consensus",
    "roadmaps",
    "informal_research",
    "product_models",
    "agentx",
    "markets_trade",
    "visualization_charts",
    "robotics_model",
    "memory_model",
    "supply_chain_templates",
    "supply_chain_routes",
    "data_series_routes",
    "instrument_stage",
    "settings_desktop",
    "source_capture",
    "notes",
    "theses",
    "home_feed_search_api",
)
_MODULES: list[ModuleType] = []


def _copyable(namespace: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in namespace.items()
        if not (key.startswith("__") and key.endswith("__"))
        and key not in {"import_module", "ModuleType", "Any", "SECTIONS", "_MODULES"}
    }


def register_app_sections(app_globals: dict[str, Any]) -> None:
    if _MODULES:
        finalize_app_sections(app_globals)
        return
    aggregate: dict[str, Any] = dict(_copyable(app_globals))
    modules: list[ModuleType] = []
    for name in SECTIONS:
        app_globals.update(aggregate)
        module = import_module(f"thoth_terminal.web.app_sections.{name}")
        modules.append(module)
        aggregate.update(_copyable(vars(module)))
        app_globals.update(aggregate)
        for loaded in modules:
            vars(loaded).update(aggregate)
    _MODULES[:] = modules
    finalize_app_sections(app_globals)


def finalize_app_sections(app_globals: dict[str, Any]) -> None:
    aggregate: dict[str, Any] = dict(_copyable(app_globals))
    for module in _MODULES:
        aggregate.update(_copyable(vars(module)))
    app_globals.update(aggregate)
    for module in _MODULES:
        vars(module).update(aggregate)


__all__ = ["SECTIONS", "register_app_sections", "finalize_app_sections"]
