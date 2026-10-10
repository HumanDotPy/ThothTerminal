from __future__ import annotations

import importlib.metadata
import re
import tomllib
from pathlib import Path


PROJECT_NAME = "thoth-terminal"
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def validate_version(value: str) -> str:
    version = str(value or "").strip()
    if not SEMVER_PATTERN.fullmatch(version):
        raise ValueError(f"Version must be valid SemVer: {value!r}")
    return version


def checkout_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[1]
    return candidate if (candidate / "pyproject.toml").exists() else None


def read_project_version(project_root: Path) -> str:
    pyproject_path = Path(project_root) / "pyproject.toml"
    payload = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    value = (payload.get("project") or {}).get("version")
    if not isinstance(value, str):
        raise RuntimeError(f"Missing [project].version in {pyproject_path}")
    return validate_version(value)


def resolve_version(*, project_root: Path | None = None) -> str:
    root = Path(project_root) if project_root is not None else checkout_root()
    if root is not None:
        return read_project_version(root)
    try:
        installed = importlib.metadata.version(PROJECT_NAME)
        # Python distributions normalize SemVer prereleases: -beta.1 becomes
        # b1. Keep the same display version in installed and source builds.
        prerelease = re.fullmatch(r"(\d+\.\d+\.\d+)(a|b|rc)(\d+)(\+.+)?", installed)
        if prerelease:
            base, stage, number, local = prerelease.groups()
            label = {"a": "alpha", "b": "beta", "rc": "rc"}[stage]
            installed = f"{base}-{label}.{number}{local or ''}"
        return validate_version(installed)
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0+unknown"


def set_project_version(project_root: Path, version: str) -> Path:
    value = validate_version(version)
    pyproject_path = Path(project_root) / "pyproject.toml"
    content = pyproject_path.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'(?m)^(version\s*=\s*)"[^"]+"',
        rf'\g<1>"{value}"',
        content,
        count=1,
    )
    if count != 1:
        raise RuntimeError(f"Unable to update [project].version in {pyproject_path}")
    temp_path = pyproject_path.with_name(f".{pyproject_path.name}.version.tmp")
    temp_path.write_text(updated, encoding="utf-8")
    temp_path.replace(pyproject_path)
    return pyproject_path


__version__ = resolve_version()
