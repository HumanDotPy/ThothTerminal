from __future__ import annotations

import re


_DUPLICATED_ABSOLUTE_URL_RE = re.compile(r"^(https?://[^/?#]+)(https?:?//.+)$", re.IGNORECASE)


def clean_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    current = raw
    for _ in range(3):
        match = _DUPLICATED_ABSOLUTE_URL_RE.match(current)
        if not match:
            break
        current = match.group(2)
        if current.startswith("http//"):
            current = "http://" + current[len("http//") :]
        elif current.startswith("https//"):
            current = "https://" + current[len("https//") :]
    return current
