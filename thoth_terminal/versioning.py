from __future__ import annotations

import argparse

from thoth_terminal._version import (
    __version__,
    checkout_root,
    read_project_version,
    resolve_version,
    set_project_version,
    validate_version,
)

__all__ = [
    "__version__",
    "checkout_root",
    "read_project_version",
    "resolve_version",
    "set_project_version",
    "validate_version",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or update the Thoth Terminal package version.")
    parser.add_argument("--set", dest="new_version", help="Set [project].version after SemVer validation.")
    parser.add_argument("--check", action="store_true", help="Validate and print the configured version.")
    args = parser.parse_args(argv)
    root = checkout_root()
    if args.new_version:
        if root is None:
            parser.error("--set requires a source checkout containing pyproject.toml")
        set_project_version(root, args.new_version)
    version = resolve_version(project_root=root)
    if args.check:
        validate_version(version)
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
