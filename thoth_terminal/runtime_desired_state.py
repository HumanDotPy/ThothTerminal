from __future__ import annotations

import json
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from thoth_terminal.runtime_env import repo_root


REGISTRY_RELATIVE_PATH = Path("config/runtime_desired_state.json")
ALLOWED_DESIRED = {"active", "inactive"}
ALLOWED_OWNERS = {"watchdog", "desktop-supervisor", "desktop"}


@dataclass(frozen=True)
class DesiredComponent:
    name: str
    owner: str
    kind: str
    desired: str
    enabled_when: str
    requires: tuple[str, ...]
    probe: dict[str, Any]
    repair: dict[str, Any]
    critical: bool = False
    stop_in_gaming_mode: bool = False

    @property
    def interval_seconds(self) -> float:
        return max(30.0, float(self.probe.get("interval_seconds") or self.repair.get("backoff_seconds") or 300))


@dataclass(frozen=True)
class DesiredStateRegistry:
    schema_version: int
    registry_version: str
    components: tuple[DesiredComponent, ...]

    def by_name(self) -> dict[str, DesiredComponent]:
        return {row.name: row for row in self.components}

    def component(self, name: str) -> DesiredComponent:
        row = self.by_name().get(str(name))
        if row is None:
            raise KeyError(f"Unknown desired-state component: {name}")
        return row

    def owned_by(self, owner: str) -> list[DesiredComponent]:
        return [row for row in self.components if row.owner == str(owner)]

    def topological_names(self) -> list[str]:
        by_name = self.by_name()
        visiting: set[str] = set()
        visited: set[str] = set()
        ordered: list[str] = []

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise ValueError(f"Desired-state dependency cycle includes {name}")
            visiting.add(name)
            for dependency in by_name[name].requires:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)
            ordered.append(name)

        for name in sorted(by_name):
            visit(name)
        return ordered


def _component(raw: dict[str, Any]) -> DesiredComponent:
    return DesiredComponent(
        name=str(raw.get("name") or "").strip(),
        owner=str(raw.get("owner") or "").strip(),
        kind=str(raw.get("kind") or "").strip(),
        desired=str(raw.get("desired") or "").strip(),
        enabled_when=str(raw.get("enabled_when") or "always").strip(),
        requires=tuple(str(value).strip() for value in raw.get("requires") or [] if str(value).strip()),
        probe=dict(raw.get("probe") or {}),
        repair=dict(raw.get("repair") or {}),
        critical=bool(raw.get("critical", False)),
        stop_in_gaming_mode=bool(raw.get("stop_in_gaming_mode", False)),
    )


def validate_desired_state_registry(registry: DesiredStateRegistry) -> DesiredStateRegistry:
    if registry.schema_version != 1:
        raise ValueError(f"Unsupported desired-state schema_version: {registry.schema_version}")
    if not registry.registry_version:
        raise ValueError("registry_version is required")
    names = [row.name for row in registry.components]
    if any(not name for name in names):
        raise ValueError("Every desired-state component requires a name")
    if len(names) != len(set(names)):
        raise ValueError("Desired-state component names must be unique")
    known = set(names)
    for row in registry.components:
        if row.owner not in ALLOWED_OWNERS:
            raise ValueError(f"Invalid owner for {row.name}: {row.owner}")
        if row.desired not in ALLOWED_DESIRED:
            raise ValueError(f"Invalid desired state for {row.name}: {row.desired}")
        missing = sorted(set(row.requires) - known)
        if missing:
            raise ValueError(f"{row.name} requires unknown components: {', '.join(missing)}")
        if row.name in row.requires:
            raise ValueError(f"{row.name} cannot depend on itself")
        if not str(row.probe.get("kind") or "").strip():
            raise ValueError(f"{row.name} requires a probe kind")
        if not str(row.repair.get("action") or "").strip():
            raise ValueError(f"{row.name} requires a repair action")
    registry.topological_names()
    return registry


def load_desired_state_registry(path: Path | None = None) -> DesiredStateRegistry:
    registry_path = path or (repo_root() / REGISTRY_RELATIVE_PATH)
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Desired-state registry must be a JSON object")
    rows = raw.get("components")
    if not isinstance(rows, list):
        raise ValueError("Desired-state registry components must be a list")
    registry = DesiredStateRegistry(
        schema_version=int(raw.get("schema_version") or 0),
        registry_version=str(raw.get("registry_version") or "").strip(),
        components=tuple(_component(row) for row in rows if isinstance(row, dict)),
    )
    return validate_desired_state_registry(registry)


@lru_cache(maxsize=1)
def desired_state_registry() -> DesiredStateRegistry:
    return load_desired_state_registry()


def blocked_dependencies(component: DesiredComponent, states: dict[str, str]) -> list[str]:
    return [name for name in component.requires if states.get(name) not in {"healthy", "active"}]


def component_projection(
    component: DesiredComponent,
    *,
    enabled: bool,
    observed_state: str,
    dependency_states: dict[str, str] | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dependencies = dict(dependency_states or {})
    blocked = blocked_dependencies(component, dependencies) if enabled else []
    effective = "inactive" if not enabled or component.desired == "inactive" else ("blocked" if blocked else observed_state)
    return {
        "name": component.name,
        "owner": component.owner,
        "kind": component.kind,
        "desired": component.desired,
        "enabled": bool(enabled),
        "enabled_when": component.enabled_when,
        "observed": effective,
        "requires": list(component.requires),
        "blocked_by": blocked,
        "probe": dict(component.probe),
        "repair": dict(component.repair),
        "critical": bool(component.critical),
        "detail": dict(detail or {}),
    }


def read_desired_state_projection(*, state_dir: Path | None = None) -> dict[str, Any] | None:
    from thoth_terminal.runtime_info import runtime_state_dir

    path = (state_dir or runtime_state_dir()) / "desired-state.json"
    for attempt in range(3):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            if attempt < 2 and path.exists():
                time.sleep(0.05)
                continue
            return None
        return payload if isinstance(payload, dict) else None
    return None
