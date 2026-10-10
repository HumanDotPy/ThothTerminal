"""Bounded, shared analyst catalog discovery for SQL-backed dataset surfaces."""
from __future__ import annotations

import re
import json
from functools import lru_cache
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, and_, cast, func, literal, not_, or_, select, union_all
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Session, load_only
from sqlalchemy.sql.functions import FunctionElement

from thoth_terminal.models import Dataset

DATASET_SEARCH_FIELDS = ("title", "description", "family", "category", "source_kind", "slug")
DATASET_SEARCH_EXTRA_FIELDS = ("company_name", "company_slug", "ticker", "issuer", "issuer_name", "symbol")


def dataset_search_values(dataset: Dataset) -> list[str]:
    """The discovery fields for downstream object match ranking."""
    extra = dataset.extra if isinstance(dataset.extra, dict) else {}
    return ([str(getattr(dataset, key) or "") for key in DATASET_SEARCH_FIELDS]
            + [str(extra.get(key) or "") for key in DATASET_SEARCH_EXTRA_FIELDS])


class _text_key(FunctionElement):
    type = String()
    inherit_cache = True


class _fixture_match(FunctionElement):
    type = Boolean()
    inherit_cache = True


class _scope_text_match(FunctionElement):
    type = Boolean()
    inherit_cache = True


@compiles(_scope_text_match, "postgresql")
def _pg_scope_text_match(element, compiler, **kw):
    haystack, _tokens, phrases, dataset_id, *fallback = list(element.clauses)
    value = compiler.process(haystack, **kw)
    payload = compiler.process(phrases, **kw)
    identity = compiler.process(dataset_id, **kw)
    # Materialize normalization once. Expand the scope once and hash-join its
    # rare anchor words, rather than rescanning a large JSON object per row.
    # This is statement-local state, so metadata updates are visible immediately.
    matched = ("SELECT scope_candidates.id FROM scope_candidates "
               "CROSS JOIN LATERAL unnest(string_to_array(scope_candidates.value, ' ')) AS scope_word(word) "
               "JOIN scope_phrases ON scope_phrases.anchor = scope_word.word "
               "WHERE strpos(scope_candidates.value, scope_phrases.token)>0")
    if fallback:
        matched += (" UNION ALL SELECT scope_candidates.id FROM scope_candidates WHERE "
                    f"scope_candidates.value LIKE ANY({compiler.process(fallback[0], **kw)})")
    return (f"({identity} IN (WITH scope_candidates AS MATERIALIZED "
            f"(SELECT datasets.id, {value} AS value FROM datasets WHERE datasets.canonical_dataset_id IS NULL), "
            f"scope_phrases AS MATERIALIZED (SELECT * FROM jsonb_to_recordset({payload}) "
            "AS phrase(anchor text, token text)) " + matched + "))")


@compiles(_scope_text_match, "sqlite")
def _sqlite_scope_text_match(element, compiler, **kw):
    haystack, tokens, *_unused = list(element.clauses)
    return f"thoth_dataset_scope_match({compiler.process(haystack, **kw)}, {compiler.process(tokens, **kw)})"


@lru_cache(maxsize=8)
def _scope_tokens(payload):
    return tuple(json.loads(payload))


@compiles(_fixture_match, "postgresql")
def _pg_fixture_match(element, compiler, **kw):
    arg = compiler.process(list(element.clauses)[0], **kw)
    return f"({arg} ~ '\\m(smoke|fixture|demo)\\M|\\mtest dataset\\M|^route test\\M')"


@compiles(_fixture_match, "sqlite")
def _sqlite_fixture_match(element, compiler, **kw):
    arg = compiler.process(list(element.clauses)[0], **kw)
    return f"({arg} REGEXP '\\b(smoke|fixture|demo)\\b|\\btest dataset\\b|^route test\\b')"


@compiles(_text_key, "postgresql")
def _pg_text_key(element, compiler, **kw):
    arg = compiler.process(list(element.clauses)[0], **kw)
    return f"trim(regexp_replace(lower(coalesce({arg}, '')), '[^a-z0-9가-힣]+', ' ', 'g'))"


@compiles(_text_key, "sqlite")
def _sqlite_text_key(element, compiler, **kw):
    return "thoth_dataset_text_key(" + compiler.process(list(element.clauses)[0], **kw) + ")"


def _prepare_sqlite(session: Session) -> None:
    if session.get_bind().dialect.name == "sqlite":
        session.connection().connection.driver_connection.create_function(
            "thoth_dataset_text_key", 1,
            lambda value: re.sub(r"[^a-z0-9가-힣]+", " ", str(value or "").strip().lower()).strip(),
            deterministic=True,
        )
        session.connection().connection.driver_connection.create_function(
            "thoth_dataset_scope_match", 2,
            lambda haystack, payload: any(token in str(haystack or "") for token in _scope_tokens(payload)),
            deterministic=True,
        )


def _extra(key: str, *, indexed: bool = False):
    index = literal(key, type_=JSON.JSONStrIndexType, literal_execute=True) if indexed else key
    empty = literal("", type_=String, literal_execute=True) if indexed else ""
    return func.coalesce(Dataset.extra[index].as_string(), empty)


def _choice(values):
    """Inline constant sets so the compiled predicate can match a partial index."""
    return [literal(value, type_=String, literal_execute=True) for value in values]


def _extra_raw(key: str):
    """Raw JSON accessor (no string coercion) for the explicit-hidden contract."""
    return Dataset.extra[literal(key, type_=JSON.JSONStrIndexType, literal_execute=True)]


def _lower(value):
    return func.lower(func.trim(func.coalesce(value, literal("", type_=String, literal_execute=True))))


def dataset_search_text():
    """Expression shared with the production trigram index, if installed."""
    fields = [getattr(Dataset, key) for key in DATASET_SEARCH_FIELDS]
    # Fixed index keys and separators must stay SQL constants. Parameters here
    # prevent PostgreSQL's generic prepared plans from matching the GIN index.
    fields.extend(_extra(key, indexed=True) for key in DATASET_SEARCH_EXTRA_FIELDS)
    empty = literal("", type_=String, literal_execute=True)
    space = literal(" ", type_=String, literal_execute=True)
    value = func.coalesce(fields[0], empty)
    for item in fields[1:]:
        value = value + space + func.coalesce(item, empty)
    return func.lower(value)


def dataset_scope_text():
    space = literal(" ", type_=String, literal_execute=True)
    return space + _text_key(dataset_search_text()) + space


def dataset_scope_index_expressions():
    return {
        "company_id": func.trim(cast(_extra("company_id", indexed=True), String)),
        "issuer_id": func.trim(cast(_extra("issuer_id", indexed=True), String)),
        "company_slug": _text_key(_extra("company_slug", indexed=True)),
        "ticker": func.upper(func.trim(_extra("ticker", indexed=True))),
    }


def _visible():
    # JSON false/0 are falsy in the existing visibility contract; only their
    # string equivalents opt out. Fixture flags accept JSON booleans as well.
    visibility = _lower(_extra("analyst_visible", indexed=True))
    explicit_hidden = and_(visibility.in_(_choice(("0", "false", "no", "hidden"))),
                           cast(_extra_raw("analyst_visible"), String).not_in(_choice(("false", "0"))))
    flags = [_lower(_extra(key, indexed=True)).in_(_choice(("1", "true", "yes", "y", "fixture", "smoke", "test")))
             for key in ("fixture", "is_fixture", "test_fixture", "is_test", "is_smoke", "smoke")]
    # Normalized word boundaries preserve punctuation/underscore distinction:
    # use SQL's portable regexp_match (SQLite's SQLAlchemy REGEXP callback).
    empty = literal("", type_=String, literal_execute=True)
    fixture_text = (func.coalesce(Dataset.title, empty) + literal(" ", type_=String, literal_execute=True)
                    + func.coalesce(Dataset.slug, empty) + literal(" ", type_=String, literal_execute=True)
                    + func.coalesce(Dataset.description, empty) + literal(" ", type_=String, literal_execute=True)
                    + func.coalesce(Dataset.category, empty))
    fixture = _fixture_match(func.lower(func.trim(fixture_text)))
    return not_(or_(explicit_hidden, *flags,
                    _text_key(Dataset.status).in_(_choice(("test", "fixture", "smoke"))),
                    and_(_text_key(Dataset.source_kind).in_(_choice(("", "internal", "test", "fixture", "smoke"))), fixture)))


def _scope_filter(scope: dict[str, Any]):
    matches = []
    fields = dataset_scope_index_expressions()
    for key, field_name in (("company_ids", "company_id"), ("issuer_ids", "issuer_id")):
        values = scope.get(key) or set()
        if values:
            matches.append(fields[field_name].in_([str(v) for v in values]))
    if scope.get("company_slugs"):
        matches.append(fields["company_slug"].in_(sorted(scope["company_slugs"])))
    if scope.get("tickers"):
        matches.append(fields["ticker"].in_(sorted(scope["tickers"])))
    haystack = dataset_scope_text()
    tokens = list(scope.get("text_tokens") or ())
    if tokens:
        phrases, fallback = [], []
        frequency = Counter(word for token in set(tokens) for word in token.strip().split())
        for token in sorted(set(tokens)):
            if re.fullmatch(r" [a-z0-9가-힣]+(?: [a-z0-9가-힣]+)* ", token):
                anchor = min(token.strip().split(), key=lambda word: (frequency[word], -len(word), word))
                phrases.append({"anchor": anchor, "token": token})
            else:
                fallback.append("%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        # Look up only phrases containing a word in the candidate. A scope
        # with thousands of names must not compare every name with every row.
        clauses = [haystack, literal(json.dumps(tokens)), literal(phrases, type_=JSONB), Dataset.id]
        if fallback:
            clauses.append(literal(fallback, type_=ARRAY(String)))
        matches.append(_scope_text_match(*clauses))
    if not matches:
        return literal(False)
    # Separate branches retain index access for identity matches while the text
    # branch computes its statement-local phrase hash join only once.
    scope_ids = union_all(*(select(Dataset.id).where(Dataset.canonical_dataset_id.is_(None), match).correlate(None)
                           for match in matches))
    return Dataset.id.in_(scope_ids)


def _candidates(*, query: str = "", family: str = "", category: str = "", scope=None, include_hidden=False):
    predicates = [] if include_hidden else [_visible()]
    predicates.append(Dataset.canonical_dataset_id.is_(None))
    if query:
        predicates.append(dataset_search_text().contains(query.strip().lower(), autoescape=True))
    if family:
        predicates.append(_lower(Dataset.family) == family.strip().lower())
    if category:
        predicates.append(_lower(Dataset.category) == category.strip().lower())
    if scope and scope.get("active"):
        predicates.append(_scope_filter(scope))
    return select(Dataset.id, Dataset.family, Dataset.category, Dataset.updated_at).where(*predicates).cte()


@dataclass
class DatasetDiscovery:
    rows: list[Dataset]
    total_count: int = 0
    scoped_count: int = 0
    raw_match_count: int = 0
    match_count: int = 0
    family_options: list[str] = field(default_factory=list)
    category_options: list[str] = field(default_factory=list)
    family_counts: dict[str, int] = field(default_factory=dict)


def discover_datasets(session: Session, *, query: str = "", family: str = "", category: str = "",
                      scope: dict[str, Any] | None = None, limit: int = 50,
                      include_hidden: bool = False, include_facets: bool = False) -> DatasetDiscovery:
    """Literal substring search across canonical datasets, newest first.

    Facets precede selected family/category; rails describe the visible catalog.
    Independent datasets retain their own identities rather than merging titles
    or company names. Managed aliases are excluded after storage consolidation.
    """
    _prepare_sqlite(session)
    limit = max(1, min(int(limit), 240))
    result = DatasetDiscovery(rows=[])
    if include_facets:
        options = _candidates(query=query, scope=scope, include_hidden=include_hidden)
        ranked = select(options)
        if family:
            ranked = ranked.where(_lower(options.c.family) == family.strip().lower())
        if category:
            ranked = ranked.where(_lower(options.c.category) == category.strip().lower())
        ranked = ranked.cte()
        selected = select(ranked).order_by(ranked.c.updated_at.desc(), ranked.c.id.desc()).limit(limit).cte()
        catalog = _candidates(include_hidden=include_hidden)
        no_id, no_text, no_time = literal(None, type_=Integer), literal(None, type_=String), literal(None, type_=DateTime)
        records = session.execute(union_all(
            select(literal("item"),selected.c.id,selected.c.family,selected.c.category,selected.c.updated_at,literal(1)),
            select(literal("facet"),no_id,options.c.family,options.c.category,no_time,func.count()).group_by(options.c.family,options.c.category),
            select(literal("rail"),no_id,catalog.c.family,no_text,no_time,func.count()).group_by(catalog.c.family),
            select(literal("count"),no_id,no_text,no_text,no_time,func.count()).select_from(ranked),
        )).all()
        ids = []
        families, categories = set(), set()
        for kind, row_id, family_name, category_name, _updated, count in records:
            if kind == "item":
                ids.append(int(row_id))
            elif kind == "facet":
                result.scoped_count += int(count)
                if str(family_name or "").strip():
                    families.add(str(family_name).strip())
                if str(category_name or "").strip():
                    categories.add(str(category_name).strip())
            elif kind == "rail":
                result.family_counts[str(family_name or "general").strip() or "general"] = int(count)
                result.total_count += int(count)
            else:
                result.match_count = result.raw_match_count = int(count)
        result.family_options, result.category_options = sorted(families), sorted(categories)
    else:
        ranked = _candidates(query=query, family=family, category=category, scope=scope, include_hidden=include_hidden)
        ids = [int(row.id) for row in session.execute(select(ranked.c.id).order_by(ranked.c.updated_at.desc(), ranked.c.id.desc()).limit(limit))]
    rows = (session.query(Dataset).options(load_only(Dataset.id, Dataset.slug, Dataset.title, Dataset.description,
            Dataset.category, Dataset.family, Dataset.status, Dataset.source_kind, Dataset.created_at, Dataset.updated_at, Dataset.extra))
            .filter(Dataset.id.in_(ids)).order_by(Dataset.updated_at.desc(), Dataset.id.desc()).all()) if ids else []
    result.rows = rows
    return result
