from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any


PARTS = (
    "core",
    "dashboard_charts",
    "dashboard_payloads",
    "dashboard_view_builder",
    "serialization",
    "mutations",
    "views",
    "draft_rows",
    "io",
    "links",
    "materializer_values",
    "materializer_alias_catalog",
    "materializer_defaults",
    "materializer_rows",
    "inferencex",
    "provider_pricing",
    "artificial_analysis",
    "ai_serving_economics",
    "trade",
)
_MODULES: list[ModuleType] = []


def _copyable(namespace: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in namespace.items()
        if not (key.startswith("__") and key.endswith("__"))
        and key not in {"import_module", "ModuleType", "Any", "PARTS", "_MODULES"}
    }


def install_dataset_parts(target_globals: dict[str, Any]) -> None:
    aggregate: dict[str, Any] = {}
    modules: list[ModuleType] = []
    for name in PARTS:
        module = import_module(f"thoth_terminal.dataset_parts.{name}")
        modules.append(module)
        aggregate.update(_copyable(vars(module)))
        for loaded in modules:
            vars(loaded).update(aggregate)
    target_globals.update(aggregate)
    for module in modules:
        vars(module).update(aggregate)
    _MODULES[:] = modules


__all__ = ["PARTS", "install_dataset_parts"]
