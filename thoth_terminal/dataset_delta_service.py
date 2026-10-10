"""Bounded value deltas for research context, using existing dataset versions."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import String, and_, cast, or_
from sqlalchemy.orm import aliased

from thoth_terminal.models import DatasetRow, DatasetVersion


def dataset_value_delta(session, dataset_id: int, *, limit: int = 8) -> dict:
    limit = max(1, min(limit, 20))
    versions = (session.query(DatasetVersion)
                .filter_by(dataset_id=dataset_id, status="published")
                .order_by(DatasetVersion.version.desc(), DatasetVersion.id.desc()).limit(2).all())
    if not versions:
        return {"status": "unpublished", "changes": []}
    current = versions[0]
    previous = versions[1] if len(versions) > 1 else None
    result = {"version_id": current.id, "previous_version_id": previous.id if previous else None,
              "status": "changed" if previous else "initial_snapshot", "changes": [], "truncated": False}
    if previous is None:
        return result
    old = aliased(DatasetRow)
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import JSONB
        unequal = cast(DatasetRow.values, JSONB) != cast(old.values, JSONB)
    else:
        unequal = cast(DatasetRow.values, String) != cast(old.values, String)
    candidates = (session.query(DatasetRow.row_key, DatasetRow.values, old.values, old.id)
                  .outerjoin(old, and_(old.dataset_version_id == previous.id, old.row_key == DatasetRow.row_key))
                  .filter(DatasetRow.dataset_version_id == current.id)
                  .filter(or_(old.id.is_(None), unequal))
                  .order_by(DatasetRow.row_key).yield_per(128))
    changes = []
    for key, after, before, old_id in candidates:
        fields = []
        before = before or {}
        after = after or {}
        for field in sorted(set(before) | set(after)):
            if before.get(field) == after.get(field) and (field in before) == (field in after):
                continue
            def compact(value):
                text = json.dumps(value, ensure_ascii=False, default=str)
                return value if len(text) <= 240 else text[:237] + "..."
            change = {"field": field, "before": compact(before.get(field)), "after": compact(after.get(field))}
            if field not in before or field not in after:
                change.update(before_present=field in before, after_present=field in after)
            fields.append(change)
        if fields or old_id is None:
            changes.append({"row_key": key, "kind": "changed" if old_id is not None else "added",
                            "fields": fields[:12], "fields_truncated": len(fields) > 12})
            if len(changes) > limit:
                break
    result["truncated"] = len(changes) > limit
    changes = changes[:limit]
    remaining = limit - len(changes)
    new = aliased(DatasetRow)
    removed = (session.query(DatasetRow.row_key)
               .outerjoin(new, and_(new.dataset_version_id == current.id, new.row_key == DatasetRow.row_key))
               .filter(DatasetRow.dataset_version_id == previous.id, new.id.is_(None))
               .order_by(DatasetRow.row_key).limit(remaining + 1).all())
    changes.extend({"row_key": key, "kind": "removed", "fields": []} for (key,) in removed[:remaining])
    result["truncated"] = result["truncated"] or len(removed) > remaining
    result["changes"] = changes
    result["digest"] = hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()
    if not changes and not result["truncated"]:
        result["status"] = "no_value_changes"
    return result
