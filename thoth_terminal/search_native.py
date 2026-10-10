from __future__ import annotations



def _load_native():
    try:
        from _thoth_html import rank_search_values

        return rank_search_values
    except (ImportError, OSError):
        return None


_NATIVE_RANK = _load_native()


def native_available() -> bool:
    return _NATIVE_RANK is not None


def rank_native(query: str, values: list[str]) -> int:
    if _NATIVE_RANK is None:
        raise RuntimeError("Thoth native search ranker is not installed; build and install native/thoth_html")
    return int(_NATIVE_RANK(query, values))
