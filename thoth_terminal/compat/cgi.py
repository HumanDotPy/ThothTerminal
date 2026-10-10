from __future__ import annotations

from email.message import Message
from typing import Any


def parse_header(value: str) -> tuple[str, dict[str, str]]:
    """
    Minimal replacement for `cgi.parse_header`.

    Enough for legacy deps that need to parse Content-Type style headers:
      "text/html; charset=utf-8"
    """

    msg = Message()
    msg["content-type"] = value
    params = msg.get_params(header="content-type") or []
    if not params:
        return value, {}

    main = params[0][0] or value
    out: dict[str, str] = {}
    for k, v in params[1:]:
        if k and v is not None:
            out[str(k)] = str(v)
    return str(main), out

