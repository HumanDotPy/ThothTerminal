from __future__ import annotations

from typing import Any


def _copyable(namespace: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in namespace.items()
        if not (key.startswith("__") and key.endswith("__"))
    }


def bind_app_context(module_globals: dict[str, Any]) -> None:
    from thoth_terminal.web import app as app_module

    module_globals.update(_copyable(vars(app_module)))
