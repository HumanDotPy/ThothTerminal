from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlencode


def build_traditional_pagination(
    *,
    base_path: str,
    page: int,
    page_size: int,
    total: int,
    params: Mapping[str, object] | None = None,
) -> dict[str, object]:
    total = max(0, int(total))
    page_size = max(1, int(page_size))
    page_count = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(int(page or 1), page_count))
    hidden_params = {
        str(key): str(value)
        for key, value in (params or {}).items()
        if value is not None and str(value) != ""
    }

    def href(target_page: int) -> str:
        query = {**hidden_params, "page": max(1, min(int(target_page), page_count)), "page_size": page_size}
        return f"{base_path}?{urlencode(query)}"

    visible = {1, page_count}
    visible.update(range(max(1, page - 2), min(page_count, page + 2) + 1))
    pages: list[dict[str, object] | None] = []
    previous_number = 0
    for number in sorted(visible):
        if previous_number and number - previous_number > 1:
            pages.append(None)
        pages.append(
            {
                "number": number,
                "current": number == page,
                "href": href(number),
            }
        )
        previous_number = number

    start = 0 if total == 0 else ((page - 1) * page_size) + 1
    end = min(total, page * page_size)
    return {
        "base_path": base_path,
        "page": page,
        "page_size": page_size,
        "page_count": page_count,
        "total": total,
        "start": start,
        "end": end,
        "pages": pages,
        "hidden_params": hidden_params,
        "previous_href": href(page - 1) if page > 1 else None,
        "next_href": href(page + 1) if page < page_count else None,
        "back_ten_href": href(max(1, page - 10)) if page > 1 else None,
        "forward_ten_href": href(min(page_count, page + 10)) if page < page_count else None,
    }
