from __future__ import annotations


def format_src_id(source_id: int) -> str:
    return f"SRC-{int(source_id):06d}"


def format_exidea_id(idea_id: int) -> str:
    return f"EXIDEA-{int(idea_id):06d}"


def format_queue_id(queue_id: int) -> str:
    return f"Q-{int(queue_id):06d}"


def format_pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    pct = value * 100.0
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    cols = len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i in range(cols):
            cell = row[i] if i < len(row) else ""
            widths[i] = max(widths[i], len(cell))

    def _fmt(row: list[str]) -> str:
        cells = []
        for i in range(cols):
            cell = row[i] if i < len(row) else ""
            cells.append(cell.ljust(widths[i]))
        return "  ".join(cells).rstrip()

    lines = [_fmt(headers)]
    lines.append(_fmt(["-" * w for w in widths]))
    for row in rows:
        lines.append(_fmt(row))

    return "```text\n" + "\n".join(lines) + "\n```"

