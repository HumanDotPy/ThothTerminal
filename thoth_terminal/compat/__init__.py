from __future__ import annotations

import sys


def ensure_cgi() -> None:
    """
    Python 3.13 removed the stdlib `cgi` module.

    Some legacy dependencies (e.g. httpx 0.13.x, pulled by googletrans) still import `cgi.parse_header`.
    We provide a minimal shim so the rest of the system can keep running.
    """

    try:  # pragma: no cover
        import cgi  # type: ignore # noqa: F401

        return
    except ModuleNotFoundError:  # pragma: no cover
        from thoth_terminal.compat import cgi as shim

        sys.modules["cgi"] = shim

