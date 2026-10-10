from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any


PARTS = (
    "tool_definitions",
    "core_tools",
    "sources_trade_documents",
    "theses_agent",
    "discord_agent",
    "companies_reports",
    "canon_campaigns",
    "knowledge_datasets",
    "instruments_product_supply",
    "notes_research_plays",
)
_MODULES: list[ModuleType] = []


def _copyable(namespace: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in namespace.items()
        if not (key.startswith("__") and key.endswith("__"))
        and key not in {"import_module", "ModuleType", "Any", "PARTS", "_MODULES"}
    }


def install_mcp_tool_modules(target_globals: dict[str, Any]) -> None:
    if _MODULES:
        finalize_mcp_tool_modules(target_globals)
        return
    aggregate: dict[str, Any] = dict(_copyable(target_globals))
    modules: list[ModuleType] = []
    for name in PARTS:
        target_globals.update(aggregate)
        module = import_module(f"thoth_terminal.web.mcp_parts.{name}")
        modules.append(module)
        aggregate.update(_copyable(vars(module)))
        target_globals.update(aggregate)
        for loaded in modules:
            vars(loaded).update(aggregate)
    _MODULES[:] = modules


def finalize_mcp_tool_modules(target_globals: dict[str, Any]) -> None:
    aggregate: dict[str, Any] = dict(_copyable(target_globals))
    for module in _MODULES:
        aggregate.update(_copyable(vars(module)))
    target_globals.update(aggregate)
    for module in _MODULES:
        vars(module).update(aggregate)


__all__ = ["PARTS", "install_mcp_tool_modules", "finalize_mcp_tool_modules"]
