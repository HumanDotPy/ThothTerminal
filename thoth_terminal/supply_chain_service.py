from __future__ import annotations

import json
import re
from collections import defaultdict
from threading import RLock
from typing import Any

from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.models import (
    Company,
    Issuer,
    IssuerIdentifier,
    Note,
    SupplyChain,
    SupplyChainAlertRule,
    SupplyChainEdge,
    SupplyChainLink,
    SupplyChainMetricBinding,
    SupplyChainNode,
    SupplyChainRelation,
    SupplyChainScenario,
    SupplyChainSnapshot,
    SupplyChainVersion,
    Thesis,
)


SUPPLY_CHAIN_OBJECT_TYPES = {"note", "thesis"}
SUPPLY_CHAIN_ROLE_LABELS = {
    "primary": "Primary",
    "supplier_option": "Supplier Options",
    "ecosystem": "Ecosystem",
    "downstream_demand": "Downstream Demand",
}
_SUPPLY_CHAIN_TABLE_ENSURE_LOCK = RLock()
_SUPPLY_CHAIN_TABLE_ENSURED_BIND_KEYS: set[str] = set()
STAGE_FAMILY_ORDER = {
    "input": 0,
    "capability": 1,
    "production": 2,
    "component": 3,
    "system": 4,
    "market": 5,
}
STAGE_FAMILY_META = {
    "input": {
        "label": "Inputs & Materials",
        "description": "Upstream raw inputs, chemicals, substrates, and other feedstock dependencies.",
        "shape_key": "diamond_plaque",
    },
    "capability": {
        "label": "Capability",
        "description": "Equipment, process IP, and enablement nodes that gate throughput or yield.",
        "shape_key": "hex_plate",
    },
    "production": {
        "label": "Production",
        "description": "Manufacturing, packaging, assembly, and capacity nodes that scale output.",
        "shape_key": "block_rect",
    },
    "component": {
        "label": "Component",
        "description": "Subsystem, memory, and component nodes that feed into downstream systems.",
        "shape_key": "capsule",
    },
    "system": {
        "label": "System",
        "description": "Integrated products, trays, racks, and end-system assemblies.",
        "shape_key": "chevron_card",
    },
    "market": {
        "label": "Market",
        "description": "Demand-side or end-customer nodes that pull the rest of the chain.",
        "shape_key": "tag_badge",
    },
}
_LAYER_FAMILY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("market", ("customer", "customers", "market", "markets", "demand", "oem", "channel")),
    ("system", ("system", "systems", "integration", "rack", "trays", "tray", "platform", "product")),
    ("component", ("memory", "component", "components", "subsystem", "subsystems", "module", "modules")),
    ("production", ("fab", "fabs", "foundry", "manufacturing", "capacity", "packaging", "assembly", "test", "osat")),
    ("capability", ("equipment", "process", "ip", "know-how", "tool", "tooling", "recipe")),
    ("input", ("input", "inputs", "material", "materials", "substrate", "substrates", "chemical", "chemicals", "gas", "gases", "wafer", "wafers")),
)
_NODE_TYPE_FAMILY_HINTS = {
    "materials": "input",
    "material": "input",
    "substrate": "input",
    "chemical": "input",
    "gas": "input",
    "equipment": "capability",
    "tooling": "capability",
    "tool": "capability",
    "process": "capability",
    "ip": "capability",
    "fab": "production",
    "foundry": "production",
    "packaging": "production",
    "assembly": "production",
    "osat": "production",
    "test": "production",
    "memory": "component",
    "component": "component",
    "sensor": "component",
    "customer": "market",
    "market": "market",
    "system": "system",
}
_LINK_GAP_TYPES = {"system", "module", "rack", "tray", "platform", "chiplet"}
_ENTITY_SUFFIX_TOKENS = {
    "ag",
    "co",
    "company",
    "corp",
    "corporation",
    "group",
    "holding",
    "holdings",
    "inc",
    "incorporated",
    "limited",
    "llc",
    "lp",
    "ltd",
    "nv",
    "plc",
    "sa",
    "spa",
    "the",
}
_US_LISTING_EXCHANGES = {"NASDAQ", "NYSE", "NYSEAMERICAN", "NYSEARCA", "OTC", "OTCQB", "OTCQX", "OTCMKTS"}
_EXCHANGE_RANK = {
    "NASDAQ": 1,
    "NYSE": 2,
    "NYSEAMERICAN": 3,
    "TSE": 4,
    "JPX": 5,
    "TWSE": 6,
    "KRX": 7,
    "HKEX": 8,
    "LSE": 9,
    "SSE": 10,
    "SZSE": 11,
    "EURONEXT": 12,
}


def _ticker_display_mode(value: object) -> str:
    raw = str(value or "").strip().lower()
    return "adr" if raw == "adr" else "local"


def supply_chain_stage_family_legend() -> list[dict[str, Any]]:
    return [
        {
            "id": family,
            "label": meta["label"],
            "description": meta["description"],
            "shape_key": meta["shape_key"],
            "order": STAGE_FAMILY_ORDER[family],
        }
        for family, meta in STAGE_FAMILY_META.items()
    ]


def _clean_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _text_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    if "\n" in text:
        parts = [part.strip(" -\t") for part in text.splitlines()]
    else:
        parts = [part.strip() for part in text.split(",")]
    return [part for part in parts if part]


def _split_entity_values(value: object) -> list[str]:
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_split_entity_values(item))
        seen: set[str] = set()
        deduped: list[str] = []
        for item in out:
            key = _normalize_entity_exact(item)
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped
    text = _clean_text(value)
    if not text:
        return []
    if any(sep in text for sep in (" / ", ";", "|")):
        parts = re.split(r"\s*(?:/|;|\|)\s*", text)
    else:
        parts = [text]
    return [part.strip() for part in parts if part and part.strip()]


def _normalize_entity_exact(value: object) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    raw = raw.replace("&", " and ")
    raw = re.sub(r"[^a-z0-9]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _normalize_entity_alias(value: object) -> str:
    tokens = [token for token in _normalize_entity_exact(value).split() if token not in _ENTITY_SUFFIX_TOKENS]
    return " ".join(tokens).strip()


def _first_token_alias(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    token = re.split(r"[\s/|;,]+", raw, maxsplit=1)[0].strip(" .,()[]{}")
    if not token or not token.isascii():
        return None
    normalized = _normalize_entity_exact(token)
    if 2 <= len(normalized) <= 5 and normalized.isalnum():
        return normalized
    return None


def _initialism_alias(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    tokens = [token for token in re.split(r"[^A-Za-z0-9]+", raw) if token]
    filtered = [token for token in tokens if _normalize_entity_exact(token) not in _ENTITY_SUFFIX_TOKENS]
    if len(filtered) < 3:
        return None
    initials = "".join(token[0] for token in filtered if token[0].isalnum()).lower()
    if 2 <= len(initials) <= 6:
        return initials
    return None


def _looks_like_ticker(value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw or " " in raw:
        return None
    if not re.fullmatch(r"[A-Z0-9.\-]{1,12}", raw):
        return None
    return raw


def _security_listing_kind(exchange: object, jurisdiction: object) -> str:
    exch = str(exchange or "").strip().upper()
    juris = str(jurisdiction or "").strip().upper()
    if exch in _US_LISTING_EXCHANGES and juris and juris != "US":
        return "adr"
    return "local"


def _security_sort_key(row: dict[str, Any], *, ticker_display: str) -> tuple[int, int, int, str, str]:
    listing_kind = str(row.get("listing_kind") or "local")
    preferred_kind_rank = 0
    if ticker_display == "adr":
        preferred_kind_rank = 0 if listing_kind == "adr" else 1
    else:
        preferred_kind_rank = 0 if listing_kind != "adr" else 1
    exchange = str(row.get("exchange") or "").strip().upper()
    return (
        preferred_kind_rank,
        0 if bool(row.get("is_primary")) else 1,
        _EXCHANGE_RANK.get(exchange, 999),
        str(row.get("ticker") or ""),
        exchange,
    )


def _dedupe_security_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        exchange = str(row.get("exchange") or "").strip().upper()
        if not ticker:
            continue
        key = (ticker, exchange)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "ticker": ticker,
                "exchange": exchange or None,
                "is_primary": bool(row.get("is_primary")),
                "listing_kind": str(row.get("listing_kind") or "local"),
                "display": f"{ticker}" + (f" · {exchange}" if exchange else ""),
            }
        )
    return out


def _preferred_securities(rows: list[dict[str, Any]], *, ticker_display: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    deduped = _dedupe_security_rows(rows)
    if not deduped:
        return None, []
    ordered = sorted(deduped, key=lambda row: _security_sort_key(row, ticker_display=ticker_display))
    return ordered[0], ordered[1:]


def _register_exposure_alias(index: dict[str, Any], key: str, candidate_key: str, *, bucket: str) -> None:
    norm = str(key or "").strip()
    if not norm:
        return
    bucket_map = index[bucket]
    bucket_map.setdefault(norm, set()).add(candidate_key)


def _candidate_aliases(*values: object) -> tuple[set[str], set[str]]:
    exact: set[str] = set()
    alias: set[str] = set()
    for value in values:
        raw = _clean_text(value)
        if not raw:
            continue
        exact_key = _normalize_entity_exact(raw)
        alias_key = _normalize_entity_alias(raw)
        if exact_key:
            exact.add(exact_key)
        if alias_key:
            alias.add(alias_key)
        first_token = _first_token_alias(raw)
        if first_token:
            alias.add(first_token)
        initialism = _initialism_alias(raw)
        if initialism:
            alias.add(initialism)
    return exact, alias


def _build_exposure_index(session: Session) -> dict[str, Any]:
    companies = session.query(Company).order_by(Company.id.asc()).all()
    issuers = {int(row.id): row for row in session.query(Issuer).order_by(Issuer.id.asc()).all()}
    identifiers = (
        session.query(IssuerIdentifier)
        .filter(IssuerIdentifier.id_type == "TICKER")
        .order_by(IssuerIdentifier.issuer_id.asc(), IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
        .all()
    )
    identifiers_by_issuer: dict[int, list[IssuerIdentifier]] = defaultdict(list)
    company_ids_by_issuer: dict[int, list[int]] = defaultdict(list)
    for row in identifiers:
        identifiers_by_issuer[int(row.issuer_id)].append(row)
    for company in companies:
        if company.issuer_id is not None:
            company_ids_by_issuer[int(company.issuer_id)].append(int(company.id))

    index: dict[str, Any] = {"candidates": {}, "exact": {}, "alias": {}, "ticker": {}}

    def add_candidate(candidate_key: str, candidate: dict[str, Any], *, exact_values: set[str], alias_values: set[str]) -> None:
        index["candidates"][candidate_key] = candidate
        for value in exact_values:
            _register_exposure_alias(index, value, candidate_key, bucket="exact")
        for value in alias_values:
            _register_exposure_alias(index, value, candidate_key, bucket="alias")
        for security in candidate.get("securities") or []:
            ticker = str(security.get("ticker") or "").strip().upper()
            if ticker:
                _register_exposure_alias(index, ticker, candidate_key, bucket="ticker")

    for company in companies:
        issuer = issuers.get(int(company.issuer_id)) if company.issuer_id is not None else None
        raw_securities = [
            {
                "ticker": ident.id_value,
                "exchange": ident.exchange,
                "is_primary": bool(ident.is_primary),
                "listing_kind": _security_listing_kind(ident.exchange, issuer.jurisdiction if issuer else None),
            }
            for ident in identifiers_by_issuer.get(int(issuer.id), [])
        ] if issuer else []
        if company.ticker:
            raw_securities.append(
                {
                    "ticker": company.ticker,
                    "exchange": company.exchange,
                    "is_primary": not raw_securities,
                    "listing_kind": _security_listing_kind(company.exchange, issuer.jurisdiction if issuer else None),
                }
            )
        securities = _dedupe_security_rows(raw_securities)
        if not securities:
            continue
        exact_values, alias_values = _candidate_aliases(
            company.name,
            company.slug,
            issuer.display_name if issuer else None,
            issuer.legal_name if issuer else None,
        )
        add_candidate(
            f"company:{int(company.id)}",
            {
                "candidate_key": f"company:{int(company.id)}",
                "display_name": company.name,
                "company_slug": company.slug,
                "issuer_id": int(company.issuer_id) if company.issuer_id is not None else None,
                "jurisdiction": issuer.jurisdiction if issuer else None,
                "securities": securities,
            },
            exact_values=exact_values,
            alias_values=alias_values,
        )

    for issuer_id, issuer in issuers.items():
        if company_ids_by_issuer.get(issuer_id):
            continue
        securities = _dedupe_security_rows(
            [
                {
                    "ticker": ident.id_value,
                    "exchange": ident.exchange,
                    "is_primary": bool(ident.is_primary),
                    "listing_kind": _security_listing_kind(ident.exchange, issuer.jurisdiction),
                }
                for ident in identifiers_by_issuer.get(issuer_id, [])
            ]
        )
        if not securities:
            continue
        exact_values, alias_values = _candidate_aliases(issuer.display_name, issuer.legal_name)
        add_candidate(
            f"issuer:{issuer_id}",
            {
                "candidate_key": f"issuer:{issuer_id}",
                "display_name": issuer.display_name,
                "company_slug": None,
                "issuer_id": issuer_id,
                "jurisdiction": issuer.jurisdiction,
                "securities": securities,
            },
            exact_values=exact_values,
            alias_values=alias_values,
        )
    return index


def _candidate_matches(index: dict[str, Any], keys: set[str] | None) -> list[dict[str, Any]]:
    if not keys:
        return []
    return [dict(index["candidates"][key]) for key in sorted(keys) if key in index["candidates"]]


def _resolved_exposure_entry(
    candidate: dict[str, Any],
    *,
    role: str,
    raw_name: str,
    ticker_display: str,
    match_confidence: str,
    match_source: str,
) -> dict[str, Any]:
    primary_security, alternate_securities = _preferred_securities(
        [dict(row) for row in (candidate.get("securities") or []) if isinstance(row, dict)],
        ticker_display=ticker_display,
    )
    return {
        "role": role,
        "role_label": SUPPLY_CHAIN_ROLE_LABELS.get(role, role.replace("_", " ").title()),
        "display_name": str(candidate.get("display_name") or raw_name),
        "source_name": raw_name,
        "company_slug": candidate.get("company_slug"),
        "company_href": f"/companies/{candidate['company_slug']}" if candidate.get("company_slug") else None,
        "is_public": True,
        "primary_security": primary_security,
        "alternate_securities": alternate_securities,
        "match_confidence": match_confidence,
        "match_source": match_source,
        "match_state": "resolved",
        "ambiguous_matches": [],
    }


def _ambiguous_exposure_entry(raw_name: str, *, role: str, candidates: list[dict[str, Any]], ticker_display: str, match_source: str) -> dict[str, Any]:
    previews = []
    for candidate in candidates[:3]:
        primary_security, _ = _preferred_securities(
            [dict(row) for row in (candidate.get("securities") or []) if isinstance(row, dict)],
            ticker_display=ticker_display,
        )
        previews.append(
            {
                "display_name": str(candidate.get("display_name") or raw_name),
                "company_slug": candidate.get("company_slug"),
                "primary_security": primary_security,
            }
        )
    return {
        "role": role,
        "role_label": SUPPLY_CHAIN_ROLE_LABELS.get(role, role.replace("_", " ").title()),
        "display_name": raw_name,
        "source_name": raw_name,
        "company_slug": None,
        "company_href": None,
        "is_public": False,
        "primary_security": None,
        "alternate_securities": [],
        "match_confidence": "ambiguous",
        "match_source": match_source,
        "match_state": "ambiguous",
        "ambiguous_matches": previews,
    }


def _unresolved_exposure_entry(raw_name: str, *, role: str) -> dict[str, Any]:
    return {
        "role": role,
        "role_label": SUPPLY_CHAIN_ROLE_LABELS.get(role, role.replace("_", " ").title()),
        "display_name": raw_name,
        "source_name": raw_name,
        "company_slug": None,
        "company_href": None,
        "is_public": False,
        "primary_security": None,
        "alternate_securities": [],
        "match_confidence": None,
        "match_source": None,
        "match_state": "private_or_unmapped",
        "ambiguous_matches": [],
    }


def _resolve_exposure_entry(index: dict[str, Any], raw_name: str, *, role: str, ticker_display: str) -> dict[str, Any]:
    name = _clean_text(raw_name)
    if not name:
        return _unresolved_exposure_entry(str(raw_name or ""), role=role)
    ticker = _looks_like_ticker(name)
    if ticker:
        matches = _candidate_matches(index, index["ticker"].get(ticker))
        if len(matches) == 1:
            return _resolved_exposure_entry(
                matches[0],
                role=role,
                raw_name=name,
                ticker_display=ticker_display,
                match_confidence="high",
                match_source="explicit_ticker",
            )
        if len(matches) > 1:
            return _ambiguous_exposure_entry(name, role=role, candidates=matches, ticker_display=ticker_display, match_source="explicit_ticker")
    exact_key = _normalize_entity_exact(name)
    if exact_key:
        matches = _candidate_matches(index, index["exact"].get(exact_key))
        if len(matches) == 1:
            return _resolved_exposure_entry(
                matches[0],
                role=role,
                raw_name=name,
                ticker_display=ticker_display,
                match_confidence="high",
                match_source="name_exact",
            )
        if len(matches) > 1:
            return _ambiguous_exposure_entry(name, role=role, candidates=matches, ticker_display=ticker_display, match_source="name_exact")
    alias_key = _normalize_entity_alias(name)
    if alias_key:
        matches = _candidate_matches(index, index["alias"].get(alias_key))
        if len(matches) == 1:
            return _resolved_exposure_entry(
                matches[0],
                role=role,
                raw_name=name,
                ticker_display=ticker_display,
                match_confidence="medium",
                match_source="name_alias",
            )
        if len(matches) > 1:
            return _ambiguous_exposure_entry(name, role=role, candidates=matches, ticker_display=ticker_display, match_source="name_alias")
    return _unresolved_exposure_entry(name, role=role)


def _shape_key_for_family(family: str | None) -> str:
    meta = STAGE_FAMILY_META.get(str(family or "").strip().lower())
    if meta:
        return str(meta["shape_key"])
    return str(STAGE_FAMILY_META["component"]["shape_key"])


def _family_label(family: str | None) -> str:
    meta = STAGE_FAMILY_META.get(str(family or "").strip().lower())
    if meta:
        return str(meta["label"])
    return str(STAGE_FAMILY_META["component"]["label"])


def derive_supply_chain_layer_family(layer: dict[str, Any] | None) -> str:
    if not isinstance(layer, dict):
        return "component"
    explicit = str(layer.get("family") or "").strip().lower()
    if explicit in STAGE_FAMILY_ORDER:
        return explicit
    blob = " ".join(
        [
            str(layer.get("id") or ""),
            str(layer.get("name") or ""),
            str(layer.get("description") or ""),
        ]
    ).lower()
    for family, hints in _LAYER_FAMILY_HINTS:
        if any(hint in blob for hint in hints):
            return family
    return "component"


def _node_type_family(node_type: object) -> str | None:
    raw = str(node_type or "").strip().lower()
    if not raw:
        return None
    return _NODE_TYPE_FAMILY_HINTS.get(raw)


def _resolve_node_stage_family(node: dict[str, Any], layer: dict[str, Any] | None) -> str:
    override = str(node.get("display_family_override") or "").strip().lower()
    if override in STAGE_FAMILY_ORDER:
        return override
    typed = _node_type_family(node.get("type"))
    if typed:
        return typed
    return derive_supply_chain_layer_family(layer)


def _default_chain_about(chain: SupplyChain, *, layers: list[dict[str, Any]], nodes: list[dict[str, Any]], lints: list[dict[str, Any]]) -> dict[str, Any]:
    family_names = [_family_label(layer.get("family")) for layer in layers[:3]]
    family_clause = ", ".join(family_names) if family_names else "the modeled operating stages"
    key_players: list[str] = []
    seen_players: set[str] = set()
    ranked_nodes = sorted(
        nodes,
        key=lambda row: (
            float(row.get("bottleneck") or 0),
            int(row.get("lead_time_months") or 0),
            float(row.get("pricing_power") or 0),
        ),
        reverse=True,
    )
    for node in ranked_nodes:
        players = _text_list(node.get("who_matters"))
        manufacturer = _clean_text(node.get("manufacturer"))
        if not players and manufacturer:
            players = [part.strip() for part in manufacturer.replace(" / ", "/").split("/") if part.strip()]
        if not players:
            players = [str(node.get("name") or "").strip()]
        for player in players:
            item = player.strip()
            if not item or item.lower() in seen_players:
                continue
            seen_players.add(item.lower())
            key_players.append(item)
            if len(key_players) >= 6:
                break
        if len(key_players) >= 6:
            break

    watchpoints = []
    for node in ranked_nodes[:4]:
        pressure = float(node.get("bottleneck") or 0)
        lead = int(node.get("lead_time_months") or 0)
        pricing = float(node.get("pricing_power") or 0)
        if pressure >= 0.45 or lead >= 9 or pricing >= 0.55:
            watchpoints.append(
                f"{node.get('name')}: bottleneck {round(pressure * 100)}%, lead {lead or 'n/a'} mo, pricing {round(pricing * 100)}%."
            )
    if not watchpoints:
        watchpoints = [str(lint.get("message") or "").strip() for lint in lints[:4] if str(lint.get("message") or "").strip()]

    summary = (
        _clean_text(chain.description)
        or f"{chain.title} maps the path through {family_clause} for {chain.product or chain.title}."
    )
    scope = f"{len(nodes)} modeled nodes across {len(layers)} layers for {chain.product or chain.title}."
    why_it_matters = (
        f"This chain concentrates on the gating steps, suppliers, and downstream assemblies that matter most for {chain.product or chain.title}."
    )
    return {
        "summary": summary,
        "scope": scope,
        "why_it_matters": why_it_matters,
        "key_players": key_players[:6],
        "watchpoints": watchpoints[:6],
    }


def _merge_chain_about(chain: SupplyChain, raw_about: dict[str, Any], *, layers: list[dict[str, Any]], nodes: list[dict[str, Any]], lints: list[dict[str, Any]]) -> dict[str, Any]:
    base = _default_chain_about(chain, layers=layers, nodes=nodes, lints=lints)
    raw = dict(raw_about or {})
    return {
        "summary": _clean_text(raw.get("summary")) or base["summary"],
        "scope": _clean_text(raw.get("scope")) or base["scope"],
        "why_it_matters": _clean_text(raw.get("why_it_matters")) or base["why_it_matters"],
        "key_players": _text_list(raw.get("key_players")) or list(base["key_players"]),
        "watchpoints": _text_list(raw.get("watchpoints")) or list(base["watchpoints"]),
    }


def _display_layers(raw_layers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for layer in sorted(raw_layers, key=lambda row: int(row.get("order") or 0)):
        row = dict(layer)
        family = derive_supply_chain_layer_family(row)
        row["family"] = family
        row["family_label"] = _family_label(family)
        row["shape_key"] = _shape_key_for_family(family)
        out.append(row)
    return out


def _edge_key(edge: dict[str, Any]) -> str:
    return f"{str(edge.get('source') or '')}:{str(edge.get('target') or '')}:{str(edge.get('type') or 'materials')}"


def _display_graph(raw_graph: dict[str, Any]) -> dict[str, Any]:
    raw_layers = [dict(layer) for layer in (raw_graph.get("layers") or []) if isinstance(layer, dict)]
    raw_nodes = [dict(node) for node in (raw_graph.get("nodes") or []) if isinstance(node, dict)]
    raw_edges = [dict(edge) for edge in (raw_graph.get("edges") or []) if isinstance(edge, dict)]
    layers = _display_layers(raw_layers)
    layer_map = {str(layer.get("id") or ""): layer for layer in layers}
    nodes: list[dict[str, Any]] = []
    for node in raw_nodes:
        row = dict(node)
        layer = layer_map.get(str(row.get("layer") or ""))
        family = _resolve_node_stage_family(row, layer)
        row["stage_family"] = family
        row["shape_key"] = _shape_key_for_family(family)
        row["layer_family"] = derive_supply_chain_layer_family(layer)
        row["layer_name"] = str((layer or {}).get("name") or row.get("layer") or "Unassigned")
        row["has_child_chain"] = bool(row.get("chain_ref_id"))
        row["display_family_override"] = str(row.get("display_family_override") or "").strip().lower() or None
        row["who_matters"] = _text_list(row.get("who_matters"))
        row["watchpoints"] = _text_list(row.get("watchpoints"))
        row["definition"] = _clean_text(row.get("definition"))
        row["why_it_matters"] = _clean_text(row.get("why_it_matters"))
        row["glossary"] = _clean_text(row.get("glossary"))
        nodes.append(row)
    node_map = {str(node.get("id") or ""): node for node in nodes if node.get("id")}
    edges: list[dict[str, Any]] = []
    for edge in raw_edges:
        row = dict(edge)
        row["key"] = _edge_key(row)
        source = node_map.get(str(row.get("source") or ""))
        target = node_map.get(str(row.get("target") or ""))
        row["source_name"] = str((source or {}).get("name") or row.get("source") or "")
        row["target_name"] = str((target or {}).get("name") or row.get("target") or "")
        row["source_family"] = str((source or {}).get("stage_family") or "")
        row["target_family"] = str((target or {}).get("stage_family") or "")
        edges.append(row)
    return {
        "about": dict(raw_graph.get("about") or {}) if isinstance(raw_graph.get("about"), dict) else {},
        "layers": layers,
        "nodes": nodes,
        "edges": edges,
        "bindings": [dict(binding) for binding in (raw_graph.get("bindings") or []) if isinstance(binding, dict)],
        "alerts": [dict(alert) for alert in (raw_graph.get("alerts") or []) if isinstance(alert, dict)],
    }


def _compute_supply_chain_lints(graph: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    nodes = [dict(node) for node in (graph.get("nodes") or []) if isinstance(node, dict)]
    edges = [dict(edge) for edge in (graph.get("edges") or []) if isinstance(edge, dict)]
    node_map = {str(node.get("id") or ""): node for node in nodes if node.get("id")}
    inbound: dict[str, list[dict[str, Any]]] = defaultdict(list)
    outbound: dict[str, list[dict[str, Any]]] = defaultdict(list)
    lints: list[dict[str, Any]] = []
    node_lints: dict[str, list[dict[str, Any]]] = defaultdict(list)
    edge_lints: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def add_lint(entry: dict[str, Any]) -> None:
        lint = dict(entry)
        lint.setdefault("severity", "warning")
        lints.append(lint)
        node_id = str(lint.get("node_id") or "").strip()
        if node_id:
            node_lints[node_id].append(lint)
        edge_key = str(lint.get("edge_key") or "").strip()
        if edge_key:
            edge_lints[edge_key].append(lint)

    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source:
            outbound[source].append(edge)
        if target:
            inbound[target].append(edge)
        source_node = node_map.get(source)
        target_node = node_map.get(target)
        if not source_node or not target_node:
            continue
        source_family = str(source_node.get("stage_family") or "")
        target_family = str(target_node.get("stage_family") or "")
        if source_family in STAGE_FAMILY_ORDER and target_family in STAGE_FAMILY_ORDER:
            if STAGE_FAMILY_ORDER[source_family] > STAGE_FAMILY_ORDER[target_family]:
                add_lint(
                    {
                        "code": "edge_direction_backward",
                        "severity": "warning",
                        "node_id": source,
                        "edge_key": _edge_key(edge),
                        "message": f"{source_node.get('name')} feeds backward into {target_node.get('name')} relative to the stage-family flow.",
                    }
                )

    for node in nodes:
        node_id = str(node.get("id") or "")
        node_type = str(node.get("type") or "").strip().lower()
        stage_family = str(node.get("stage_family") or "")
        expected_family = _node_type_family(node_type)
        if expected_family and expected_family != stage_family:
            add_lint(
                {
                    "code": "layer_family_mismatch",
                    "severity": "warning",
                    "node_id": node_id,
                    "message": f"{node.get('name')} looks like {expected_family} but is displayed in {stage_family or 'unknown'}.",
                }
            )
        if stage_family in {"system", "component"} and node_type in _LINK_GAP_TYPES and not node.get("chain_ref_id"):
            add_lint(
                {
                    "code": "linked_chain_gap",
                    "severity": "info",
                    "node_id": node_id,
                    "message": f"{node.get('name')} is a nested downstream module without a linked child supply chain yet.",
                }
            )
        has_explanatory_copy = bool(
            _clean_text(node.get("definition"))
            or _clean_text(node.get("why_it_matters"))
            or _clean_text(node.get("notes"))
        )
        if stage_family in {"system", "market"} and (len(inbound.get(node_id, [])) + len(outbound.get(node_id, []))) > 0 and not has_explanatory_copy:
            add_lint(
                {
                    "code": "downstream_readability",
                    "severity": "info",
                    "node_id": node_id,
                    "message": f"{node.get('name')} needs an explicit definition or linked drilldown to stay readable downstream.",
                }
            )

    return lints, node_lints, edge_lints


def _build_linked_context(session: Session, chain_id: int) -> dict[str, list[dict[str, Any]]]:
    link_rows = session.query(SupplyChainLink).filter_by(chain_id=int(chain_id)).all()
    note_ids = [int(row.object_id) for row in link_rows if row.object_type == "note"]
    thesis_ids = [int(row.object_id) for row in link_rows if row.object_type == "thesis"]
    notes = (
        session.query(Note).filter(Note.id.in_(note_ids)).order_by(Note.updated_at.desc()).all()
        if note_ids
        else []
    )
    theses = (
        session.query(Thesis).filter(Thesis.id.in_(thesis_ids)).order_by(Thesis.updated_at.desc()).all()
        if thesis_ids
        else []
    )
    return {
        "notes": [{"id": int(note.id), "title": note.title} for note in notes],
        "theses": [{"id": int(thesis.id), "title": thesis.title} for thesis in theses],
    }


def _node_watchpoints(node: dict[str, Any], lints: list[dict[str, Any]]) -> list[str]:
    watchpoints = _text_list(node.get("watchpoints"))
    if watchpoints:
        return watchpoints[:6]
    out: list[str] = []
    bottleneck = float(node.get("bottleneck") or 0)
    if bottleneck >= 0.5:
        out.append(f"Bottleneck pressure is {round(bottleneck * 100)}%.")
    lead = int(node.get("lead_time_months") or 0)
    if lead >= 9:
        out.append(f"Lead time is {lead} months.")
    if float(node.get("pricing_power") or 0) >= 0.55:
        out.append(f"Pricing power sits near {round(float(node.get('pricing_power') or 0) * 100)}%.")
    if node.get("has_child_chain"):
        out.append("This node has a linked child chain for deeper drilldown.")
    for lint in lints:
        msg = _clean_text(lint.get("message"))
        if msg and msg not in out:
            out.append(msg)
    if not out and _clean_text(node.get("notes")):
        out.append(str(node.get("notes")))
    return out[:6]


def _node_definition(node: dict[str, Any]) -> str:
    explicit = _clean_text(node.get("definition"))
    if explicit:
        return explicit
    gloss = _clean_text(node.get("glossary"))
    if gloss:
        return gloss
    notes = _clean_text(node.get("notes"))
    if notes:
        return notes
    manufacturer = _clean_text(node.get("manufacturer"))
    lead = int(node.get("lead_time_months") or 0)
    tail = f" Typical lead time is {lead} months." if lead else ""
    company = f" Primary participant: {manufacturer}." if manufacturer else ""
    return f"{node.get('name')} is a {_family_label(node.get('stage_family')).lower()} node in {node.get('layer_name')}.{company}{tail}".strip()


def _node_why_it_matters(node: dict[str, Any]) -> str:
    explicit = _clean_text(node.get("why_it_matters"))
    if explicit:
        return explicit
    notes = _clean_text(node.get("notes"))
    if notes:
        return notes
    clauses: list[str] = []
    bottleneck = float(node.get("bottleneck") or 0)
    if bottleneck >= 0.5:
        clauses.append(f"bottleneck pressure is {round(bottleneck * 100)}%")
    lead = int(node.get("lead_time_months") or 0)
    if lead >= 9:
        clauses.append(f"lead times run around {lead} months")
    pricing = float(node.get("pricing_power") or 0)
    if pricing >= 0.5:
        clauses.append(f"pricing power is {round(pricing * 100)}%")
    if node.get("has_child_chain"):
        clauses.append("it anchors a deeper linked subchain")
    if not clauses:
        clauses.append("it helps explain how value and bottlenecks propagate through the chain")
    return f"{node.get('name')} matters because " + ", ".join(clauses) + "."


def _node_participants(node: dict[str, Any]) -> list[str]:
    explicit = _text_list(node.get("who_matters"))
    if explicit:
        return explicit[:8]
    out: list[str] = []
    manufacturer = _clean_text(node.get("manufacturer"))
    if manufacturer:
        out.extend([part.strip() for part in manufacturer.replace(" / ", "/").split("/") if part.strip()])
    for option in node.get("options") or []:
        if not isinstance(option, dict):
            continue
        name = _clean_text(option.get("company")) or _clean_text(option.get("name"))
        if name and name not in out:
            out.append(name)
    return out[:8]


def _node_neighbors(node_map: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for edge in rows:
        node = node_map.get(str(edge.get("source") or "")) or node_map.get(str(edge.get("target") or ""))
        if not node:
            continue
        out.append(
            {
                "id": str(node.get("id") or ""),
                "name": str(node.get("name") or node.get("id") or ""),
                "stage_family": str(node.get("stage_family") or ""),
                "shape_key": str(node.get("shape_key") or ""),
                "layer_name": str(node.get("layer_name") or node.get("layer") or ""),
            }
        )
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in out:
        node_id = str(row.get("id") or "")
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        deduped.append(row)
    return deduped


def _node_downstream_demand(node_map: dict[str, dict[str, Any]], outbound: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for edge in outbound:
        target = node_map.get(str(edge.get("target") or ""))
        if not target:
            continue
        stage_family = str(target.get("stage_family") or "")
        node_type = str(target.get("type") or "").strip().lower()
        if stage_family != "market" and node_type not in {"customer", "market"}:
            continue
        names = _split_entity_values(target.get("manufacturer")) or _text_list(target.get("who_matters"))
        if not names:
            fallback = _clean_text(target.get("name"))
            if fallback:
                names = [fallback]
        for name in names:
            key = _normalize_entity_exact(name)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(name)
    return out[:8]


def _node_exposure_payload(
    node: dict[str, Any],
    *,
    node_map: dict[str, dict[str, Any]],
    outbound: list[dict[str, Any]],
    exposure_index: dict[str, Any],
    ticker_display: str,
) -> dict[str, Any]:
    primary_names = _split_entity_values(node.get("manufacturer"))
    supplier_option_names = [
        name
        for name in (
            _clean_text(option.get("company")) or _clean_text(option.get("name"))
            for option in (node.get("options") or [])
            if isinstance(option, dict)
        )
        if name
    ]
    seen_source_names = {
        _normalize_entity_exact(name)
        for name in [*primary_names, *supplier_option_names]
        if _normalize_entity_exact(name)
    }
    ecosystem_names = []
    for name in _text_list(node.get("who_matters")):
        norm = _normalize_entity_exact(name)
        if not norm or norm in seen_source_names:
            continue
        seen_source_names.add(norm)
        ecosystem_names.append(name)
    downstream_names = _node_downstream_demand(node_map, outbound)
    role_sources = {
        "primary": primary_names,
        "supplier_option": supplier_option_names,
        "ecosystem": ecosystem_names,
        "downstream_demand": downstream_names,
    }
    groups: dict[str, dict[str, Any]] = {}
    preview: list[dict[str, Any]] = []
    public_count = 0
    private_count = 0
    ambiguous_count = 0
    for role, names in role_sources.items():
        items: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for name in names:
            norm = _normalize_entity_exact(name)
            if not norm or norm in seen_names:
                continue
            seen_names.add(norm)
            item = _resolve_exposure_entry(exposure_index, name, role=role, ticker_display=ticker_display)
            items.append(item)
            if item.get("match_state") == "ambiguous":
                ambiguous_count += 1
            elif item.get("is_public") and item.get("primary_security"):
                public_count += 1
                if item.get("match_confidence") in {"high", "medium"} and len(preview) < 3:
                    preview.append(
                        {
                            "role": role,
                            "role_label": SUPPLY_CHAIN_ROLE_LABELS.get(role, role.replace("_", " ").title()),
                            "display_name": str(item.get("display_name") or name),
                            "company_slug": item.get("company_slug"),
                            "primary_security": dict(item.get("primary_security") or {}),
                            "match_confidence": item.get("match_confidence"),
                        }
                    )
            else:
                private_count += 1
        groups[role] = {
            "role": role,
            "label": SUPPLY_CHAIN_ROLE_LABELS.get(role, role.replace("_", " ").title()),
            "items": items,
        }
    return {
        "groups": groups,
        "preview": preview,
        "counts": {
            "public": public_count,
            "private_or_unmapped": private_count,
            "ambiguous": ambiguous_count,
        },
    }


def _annotate_graph_exposure(session: Session, graph: dict[str, Any], *, ticker_display: str) -> dict[str, Any]:
    nodes = [node for node in (graph.get("nodes") or []) if isinstance(node, dict)]
    edges = [dict(edge) for edge in (graph.get("edges") or []) if isinstance(edge, dict)]
    node_map = {str(node.get("id") or ""): node for node in nodes if node.get("id")}
    outbound: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        source = str(edge.get("source") or "")
        if source:
            outbound[source].append(edge)
    exposure_index = _build_exposure_index(session)
    for node in nodes:
        exposure = _node_exposure_payload(
            node,
            node_map=node_map,
            outbound=outbound.get(str(node.get("id") or ""), []),
            exposure_index=exposure_index,
            ticker_display=ticker_display,
        )
        node["exposure"] = exposure
        node["exposure_preview"] = list(exposure.get("preview") or [])
    return graph


def _build_exposure_summary(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    by_company: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_id = str(node.get("id") or "")
        node_name = str(node.get("name") or node_id)
        bottleneck = float(node.get("bottleneck") or 0)
        pricing_power = float(node.get("pricing_power") or 0)
        exposure = node.get("exposure") if isinstance(node.get("exposure"), dict) else {}
        groups = exposure.get("groups") if isinstance(exposure.get("groups"), dict) else {}
        for role in SUPPLY_CHAIN_ROLE_LABELS:
            group = groups.get(role) if isinstance(groups, dict) else None
            items = group.get("items") if isinstance(group, dict) else []
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                if not item.get("is_public") or not item.get("primary_security") or item.get("match_state") != "resolved":
                    continue
                company_key = str(item.get("company_slug") or item.get("display_name") or "").strip().lower()
                if not company_key:
                    continue
                bucket = by_company.setdefault(
                    company_key,
                    {
                        "display_name": str(item.get("display_name") or ""),
                        "company_slug": item.get("company_slug"),
                        "company_href": item.get("company_href"),
                        "primary_security": dict(item.get("primary_security") or {}),
                        "alternate_securities": [dict(row) for row in (item.get("alternate_securities") or []) if isinstance(row, dict)],
                        "linked_node_ids": set(),
                        "top_nodes": [],
                        "role_counts": {key: 0 for key in SUPPLY_CHAIN_ROLE_LABELS},
                        "max_bottleneck": 0.0,
                        "max_pricing_power": 0.0,
                    },
                )
                bucket["linked_node_ids"].add(node_id)
                bucket["role_counts"][role] = int(bucket["role_counts"].get(role) or 0) + 1
                bucket["max_bottleneck"] = max(float(bucket["max_bottleneck"] or 0), bottleneck)
                bucket["max_pricing_power"] = max(float(bucket["max_pricing_power"] or 0), pricing_power)
                bucket["top_nodes"].append(
                    {
                        "id": node_id,
                        "name": node_name,
                        "bottleneck": bottleneck,
                        "pricing_power": pricing_power,
                    }
                )
    rows: list[dict[str, Any]] = []
    for bucket in by_company.values():
        top_nodes = sorted(
            bucket["top_nodes"],
            key=lambda row: (float(row.get("bottleneck") or 0), float(row.get("pricing_power") or 0), str(row.get("name") or "")),
            reverse=True,
        )[:3]
        rows.append(
            {
                "display_name": bucket["display_name"],
                "company_slug": bucket["company_slug"],
                "company_href": bucket["company_href"],
                "primary_security": bucket["primary_security"],
                "alternate_securities": bucket["alternate_securities"],
                "linked_node_count": len(bucket["linked_node_ids"]),
                "role_counts": bucket["role_counts"],
                "max_bottleneck": bucket["max_bottleneck"],
                "max_pricing_power": bucket["max_pricing_power"],
                "top_nodes": top_nodes,
            }
        )
    rows.sort(
        key=lambda row: (
            -int(row.get("linked_node_count") or 0),
            -float(row.get("max_bottleneck") or 0),
            str(row.get("display_name") or "").lower(),
        )
    )
    return {"companies": rows, "count": len(rows)}


def _build_node_dossier(session: Session, chain: SupplyChain, graph: dict[str, Any], node_key: str, *, ticker_display: str = "local") -> dict[str, Any] | None:
    nodes = [dict(node) for node in (graph.get("nodes") or []) if isinstance(node, dict)]
    edges = [dict(edge) for edge in (graph.get("edges") or []) if isinstance(edge, dict)]
    alerts = [dict(alert) for alert in (graph.get("alerts") or []) if isinstance(alert, dict)]
    bindings = [dict(binding) for binding in (graph.get("bindings") or []) if isinstance(binding, dict)]
    node_map = {str(node.get("id") or ""): node for node in nodes if node.get("id")}
    node = node_map.get(str(node_key or ""))
    if not node:
        return None
    inbound = [edge for edge in edges if str(edge.get("target") or "") == str(node_key)]
    outbound = [edge for edge in edges if str(edge.get("source") or "") == str(node_key)]
    node_lints = [dict(item) for item in (node.get("lint") or []) if isinstance(item, dict)]
    alert_rows = [
        alert
        for alert in alerts
        if str(alert.get("scope_type") or "chain") == "chain"
        or (
            str(alert.get("scope_type") or "").lower() == "node"
            and str(alert.get("scope_key") or "") == str(node_key)
        )
    ]
    binding_rows = [
        binding
        for binding in bindings
        if str(binding.get("scope_type") or "chain") == "chain"
        or (
            str(binding.get("scope_type") or "").lower() == "node"
            and str(binding.get("scope_key") or "") == str(node_key)
        )
    ]
    linked_context = _build_linked_context(session, int(chain.id))
    subchain = None
    if str(node.get("chain_ref_id") or "").isdigit():
        child = session.query(SupplyChain).filter_by(id=int(node["chain_ref_id"])).first()
        if child:
            child_payload = serialize_supply_chain(session, child, include_graph=True, ticker_display=ticker_display)
            child_nodes = [dict(row) for row in ((child_payload.get("data") or {}).get("nodes") or []) if isinstance(row, dict)]
            subchain = {
                "id": int(child.id),
                "title": child.title,
                "category": child.category,
                "product": child.product,
                "counts": dict(child_payload.get("counts") or {}),
                "summary": str((child_payload.get("about") or {}).get("summary") or child.description or "").strip(),
                "top_nodes": [
                    {
                        "id": str(row.get("id") or ""),
                        "name": str(row.get("name") or row.get("id") or ""),
                        "stage_family": str(row.get("stage_family") or ""),
                        "shape_key": str(row.get("shape_key") or ""),
                        "bottleneck": float(row.get("bottleneck") or 0),
                    }
                    for row in sorted(
                        child_nodes,
                        key=lambda item: (
                            float(item.get("bottleneck") or 0),
                            int(item.get("lead_time_months") or 0),
                        ),
                        reverse=True,
                    )[:4]
                ],
            }
    return {
        "node_id": str(node.get("id") or ""),
        "overview": {
            "definition": _node_definition(node),
            "why_it_matters": _node_why_it_matters(node),
            "glossary": _clean_text(node.get("glossary")),
            "family": str(node.get("stage_family") or ""),
            "family_label": _family_label(node.get("stage_family")),
            "shape_key": str(node.get("shape_key") or ""),
            "layer": str(node.get("layer_name") or node.get("layer") or ""),
            "manufacturer": _clean_text(node.get("manufacturer")),
            "location": _clean_text(node.get("location")),
            "metrics": {
                "bottleneck": float(node.get("bottleneck") or 0),
                "capacity": float(node.get("capacity") or 0),
                "pricing_power": float(node.get("pricing_power") or 0),
                "confidence": float(node.get("confidence") or 0),
                "lead_time_months": int(node.get("lead_time_months") or 0),
            },
        },
        "participants": {
            "manufacturer": _clean_text(node.get("manufacturer")),
            "who_matters": _node_participants(node),
            "supplier_options": [
                {
                    "name": _clean_text(option.get("name")),
                    "company": _clean_text(option.get("company")),
                    "mix_pct": option.get("mix_pct"),
                    "chain_ref_id": int(option["chain_ref_id"]) if str(option.get("chain_ref_id") or "").isdigit() else None,
                    "chain_ref_title": _clean_text(option.get("chain_ref_title")),
                }
                for option in (node.get("options") or [])
                if isinstance(option, dict)
            ],
            "upstream": _node_neighbors(node_map, inbound),
            "downstream": _node_neighbors(node_map, outbound),
        },
        "exposure": dict(node.get("exposure") or {}),
        "watchpoints": _node_watchpoints(node, node_lints),
        "subchain": subchain,
        "context": {
            "bindings": binding_rows,
            "alerts": alert_rows,
            "notes": linked_context["notes"],
            "theses": linked_context["theses"],
            "lints": node_lints,
        },
    }


def _slugify(value: str, *, default: str = "supply-chain") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug[:120] or default


def _graph_template() -> dict[str, Any]:
    return {"about": {}, "layers": [], "nodes": [], "edges": [], "bindings": [], "alerts": []}


def ensure_supply_chain_snapshot_tables(session: Session) -> None:
    bind = session.get_bind()
    url = getattr(bind, "url", None)
    if url is not None:
        try:
            bind_key = str(url.render_as_string(hide_password=False))
        except Exception:
            bind_key = str(url)
    else:
        bind_key = str(id(bind))
    if bind_key in _SUPPLY_CHAIN_TABLE_ENSURED_BIND_KEYS:
        return
    with _SUPPLY_CHAIN_TABLE_ENSURE_LOCK:
        if bind_key in _SUPPLY_CHAIN_TABLE_ENSURED_BIND_KEYS:
            return
        failed = False
        for model in (
            SupplyChain,
            SupplyChainRelation,
            SupplyChainVersion,
            SupplyChainSnapshot,
            SupplyChainNode,
            SupplyChainEdge,
            SupplyChainScenario,
            SupplyChainMetricBinding,
            SupplyChainAlertRule,
            SupplyChainLink,
        ):
            try:
                model.__table__.create(bind=bind, checkfirst=True)
            except Exception:
                failed = True
        if not failed:
            _SUPPLY_CHAIN_TABLE_ENSURED_BIND_KEYS.add(bind_key)


def version_payload(snapshot: SupplyChainSnapshot | None) -> dict[str, Any] | None:
    if not snapshot:
        return None
    return {
        "id": int(snapshot.id),
        "version": int(snapshot.version),
        "status": snapshot.status,
        "label": snapshot.label,
        "based_on_snapshot_id": int(snapshot.based_on_snapshot_id) if snapshot.based_on_snapshot_id is not None else None,
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
        "updated_at": snapshot.updated_at.isoformat() if snapshot.updated_at else None,
    }


def _chain_unique_slug(session: Session, title: str, *, slug: str | None = None) -> str:
    base = _slugify(slug or title)
    existing = {row[0] for row in session.query(SupplyChain.slug).filter(SupplyChain.slug.like(f"{base}%")).all()}
    if base not in existing:
        return base
    idx = 2
    while f"{base}-{idx}" in existing:
        idx += 1
    return f"{base}-{idx}"


def latest_chain_snapshot(session: Session, chain_id: int, *, status: str | None = None) -> SupplyChainSnapshot | None:
    q = session.query(SupplyChainSnapshot).filter_by(chain_id=int(chain_id))
    if status:
        q = q.filter_by(status=status)
    return q.order_by(SupplyChainSnapshot.version.desc(), SupplyChainSnapshot.updated_at.desc()).first()


def latest_chain_snapshot_map(session: Session, *, status: str | None = None) -> dict[int, SupplyChainSnapshot]:
    """Latest snapshot per chain in two queries instead of one query per chain."""
    q = session.query(
        SupplyChainSnapshot.chain_id,
        func.max(SupplyChainSnapshot.version),
    ).group_by(SupplyChainSnapshot.chain_id)
    if status:
        q = q.filter_by(status=status)
    latest_versions = {int(chain_id): int(max_version) for chain_id, max_version in q.all()}
    if not latest_versions:
        return {}
    rows_q = session.query(SupplyChainSnapshot).filter(
        tuple_(SupplyChainSnapshot.chain_id, SupplyChainSnapshot.version).in_(
            [(chain_id, version) for chain_id, version in latest_versions.items()]
        )
    )
    if status:
        rows_q = rows_q.filter_by(status=status)
    return {int(row.chain_id): row for row in rows_q}


def _next_snapshot_version_num(session: Session, chain_id: int) -> int:
    max_ver = session.query(func.max(SupplyChainSnapshot.version)).filter_by(chain_id=int(chain_id), status="published").scalar()
    return int(max_ver or 0) + 1


def _scenario_by_slug(session: Session, chain_id: int, slug: str = "base") -> SupplyChainScenario | None:
    return session.query(SupplyChainScenario).filter_by(chain_id=int(chain_id), slug=str(slug or "base")).first()


def _node_payload(node: SupplyChainNode) -> dict[str, Any]:
    extra = dict(node.extra or {})
    payload = {
        "id": node.node_key,
        "name": node.name,
        "layer": node.layer_key,
        "type": node.node_type,
        "manufacturer": node.manufacturer,
        "location": node.location,
        "capacity": node.capacity_value,
        "capacity_unit": node.capacity_unit,
        "lead_time_months": node.lead_time_months,
        "pricing_power": node.pricing_power,
        "bottleneck": node.bottleneck,
        "confidence": node.confidence,
        "notes": node.notes,
    }
    payload.update(extra)
    return payload


def _edge_payload(edge: SupplyChainEdge) -> dict[str, Any]:
    payload = {
        "source": edge.source_node.node_key if edge.source_node else None,
        "target": edge.target_node.node_key if edge.target_node else None,
        "type": edge.edge_type,
        "criticality": edge.criticality,
        "notes": edge.notes,
    }
    payload.update(dict(edge.extra or {}))
    return payload


def _binding_payload(binding: SupplyChainMetricBinding) -> dict[str, Any]:
    dataset = binding.dataset
    dataset_view = binding.dataset_view
    return {
        "id": int(binding.id),
        "scope_type": binding.scope_type,
        "scope_key": binding.scope_key,
        "metric_kind": binding.metric_kind,
        "dataset_id": int(binding.dataset_id) if binding.dataset_id is not None else None,
        "dataset_view_id": int(binding.dataset_view_id) if binding.dataset_view_id is not None else None,
        "dataset": {
            "id": int(dataset.id),
            "slug": dataset.slug,
            "title": dataset.title,
            "category": dataset.category,
            "family": dataset.family,
            "source_kind": dataset.source_kind,
        }
        if dataset is not None
        else None,
        "dataset_view": {
            "id": int(dataset_view.id),
            "slug": dataset_view.slug,
            "name": dataset_view.name,
        }
        if dataset_view is not None
        else None,
        "column_key": binding.column_key,
        "aggregation": binding.aggregation,
        "extra": dict(binding.extra or {}),
    }


def _alert_payload(rule: SupplyChainAlertRule) -> dict[str, Any]:
    return {
        "id": int(rule.id),
        "scope_type": rule.scope_type,
        "scope_key": rule.scope_key,
        "name": rule.name,
        "metric_kind": rule.metric_kind,
        "operator": rule.operator,
        "threshold_value": rule.threshold_value,
        "window_days": rule.window_days,
        "status": rule.status,
        "last_triggered_at": rule.last_triggered_at.isoformat() if rule.last_triggered_at else None,
        "extra": dict(rule.extra or {}),
    }


def graph_from_snapshot(session: Session, snapshot: SupplyChainSnapshot | None, *, chain_id: int | None = None) -> dict[str, Any]:
    if snapshot is None:
        return _graph_template()
    data = dict(snapshot.data or {})
    layers = list(data.get("layers") or [])
    nodes = [_node_payload(node) for node in session.query(SupplyChainNode).filter_by(snapshot_id=int(snapshot.id)).order_by(SupplyChainNode.id.asc()).all()]
    edges = [
        _edge_payload(edge)
        for edge in session.query(SupplyChainEdge).filter_by(snapshot_id=int(snapshot.id)).order_by(SupplyChainEdge.id.asc()).all()
    ]
    bindings = [
        _binding_payload(binding)
        for binding in session.query(SupplyChainMetricBinding).filter_by(snapshot_id=int(snapshot.id)).order_by(SupplyChainMetricBinding.id.asc()).all()
    ]
    alerts: list[dict[str, Any]] = []
    if chain_id is not None:
        alerts = [
            _alert_payload(rule)
            for rule in session.query(SupplyChainAlertRule).filter_by(chain_id=int(chain_id)).order_by(SupplyChainAlertRule.id.asc()).all()
        ]
    return {
        "about": dict(data.get("about") or {}) if isinstance(data.get("about"), dict) else {},
        "layers": layers,
        "nodes": nodes,
        "edges": edges,
        "bindings": bindings,
        "alerts": alerts,
    }


def supply_chain_counts_by_chain_id(session: Session, chains: list[SupplyChain]) -> dict[int, dict[str, int]]:
    chain_ids = [int(chain.id) for chain in chains if chain.id is not None]
    if not chain_ids:
        return {}

    snapshots_by_chain_status: dict[tuple[int, str], SupplyChainSnapshot] = {}
    for snapshot in (
        session.query(SupplyChainSnapshot)
        .filter(SupplyChainSnapshot.chain_id.in_(chain_ids), SupplyChainSnapshot.status.in_(["draft", "published"]))
        .order_by(
            SupplyChainSnapshot.chain_id.asc(),
            SupplyChainSnapshot.status.asc(),
            SupplyChainSnapshot.version.desc(),
            SupplyChainSnapshot.updated_at.desc(),
            SupplyChainSnapshot.id.desc(),
        )
        .all()
    ):
        snapshots_by_chain_status.setdefault((int(snapshot.chain_id), str(snapshot.status or "")), snapshot)

    snapshot_by_chain: dict[int, SupplyChainSnapshot] = {}
    for chain_id in chain_ids:
        draft = snapshots_by_chain_status.get((chain_id, "draft"))
        published = snapshots_by_chain_status.get((chain_id, "published"))
        if draft is not None:
            snapshot_by_chain[chain_id] = draft
        elif published is not None:
            snapshot_by_chain[chain_id] = published

    snapshot_ids = [int(snapshot.id) for snapshot in snapshot_by_chain.values()]

    def _count_by_snapshot(model: Any) -> dict[int, int]:
        if not snapshot_ids:
            return {}
        return {
            int(snapshot_id): int(count or 0)
            for snapshot_id, count in (
                session.query(model.snapshot_id, func.count(model.id))
                .filter(model.snapshot_id.in_(snapshot_ids))
                .group_by(model.snapshot_id)
                .all()
            )
        }

    node_counts = _count_by_snapshot(SupplyChainNode)
    edge_counts = _count_by_snapshot(SupplyChainEdge)
    binding_counts = _count_by_snapshot(SupplyChainMetricBinding)
    alert_counts = {
        int(chain_id): int(count or 0)
        for chain_id, count in (
            session.query(SupplyChainAlertRule.chain_id, func.count(SupplyChainAlertRule.id))
            .filter(SupplyChainAlertRule.chain_id.in_(chain_ids))
            .group_by(SupplyChainAlertRule.chain_id)
            .all()
        )
    }

    out: dict[int, dict[str, int]] = {}
    for chain in chains:
        if chain.id is None:
            continue
        chain_id = int(chain.id)
        snapshot = snapshot_by_chain.get(chain_id)
        if snapshot is not None:
            snapshot_id = int(snapshot.id)
            layers = list((snapshot.data or {}).get("layers") or []) if isinstance(snapshot.data, dict) else []
            out[chain_id] = {
                "layers": len(layers),
                "nodes": node_counts.get(snapshot_id, 0),
                "edges": edge_counts.get(snapshot_id, 0),
                "bindings": binding_counts.get(snapshot_id, 0),
                "alerts": alert_counts.get(chain_id, 0),
            }
            continue
        data = dict(chain.data or {}) if isinstance(chain.data, dict) else {}
        out[chain_id] = {
            "layers": len(data.get("layers") or []),
            "nodes": len(data.get("nodes") or []),
            "edges": len(data.get("edges") or []),
            "bindings": len(data.get("bindings") or []),
            "alerts": max(len(data.get("alerts") or []), alert_counts.get(chain_id, 0)),
        }
    return out


def _replace_snapshot_graph(session: Session, snapshot: SupplyChainSnapshot, data: dict[str, Any]) -> None:
    normalized = dict(data or {})
    about = dict(normalized.get("about") or {}) if isinstance(normalized.get("about"), dict) else {}
    for key in ("visibility", "private"):
        if key in normalized and key not in about:
            about[key] = normalized.get(key)
    layers = [dict(layer) for layer in (normalized.get("layers") or []) if isinstance(layer, dict)]
    nodes = [dict(node) for node in (normalized.get("nodes") or []) if isinstance(node, dict)]
    edges = [dict(edge) for edge in (normalized.get("edges") or []) if isinstance(edge, dict)]
    bindings = [dict(binding) for binding in (normalized.get("bindings") or []) if isinstance(binding, dict)]

    session.query(SupplyChainEdge).filter_by(snapshot_id=int(snapshot.id)).delete(synchronize_session=False)
    session.query(SupplyChainNode).filter_by(snapshot_id=int(snapshot.id)).delete(synchronize_session=False)
    session.query(SupplyChainMetricBinding).filter_by(snapshot_id=int(snapshot.id)).delete(synchronize_session=False)

    snapshot.data = {
        "about": about,
        "layers": layers,
        "nodes": nodes,
        "edges": edges,
        "bindings": bindings,
    }
    snapshot.updated_at = utcnow()
    session.add(snapshot)
    session.flush()

    node_map: dict[str, SupplyChainNode] = {}
    for node in nodes:
        node_key = str(node.get("id") or "").strip()
        if not node_key:
            continue
        row = SupplyChainNode(
            snapshot_id=int(snapshot.id),
            node_key=node_key,
            name=str(node.get("name") or node_key),
            layer_key=str(node.get("layer") or "default"),
            node_type=str(node.get("type") or "") or None,
            manufacturer=str(node.get("manufacturer") or "") or None,
            location=str(node.get("location") or "") or None,
            capacity_value=float(node.get("capacity")) if node.get("capacity") not in (None, "") else None,
            capacity_unit=str(node.get("capacity_unit") or "") or None,
            lead_time_months=int(node.get("lead_time_months")) if str(node.get("lead_time_months") or "").isdigit() else None,
            pricing_power=float(node.get("pricing_power")) if node.get("pricing_power") not in (None, "") else None,
            bottleneck=float(node.get("bottleneck")) if node.get("bottleneck") not in (None, "") else None,
            confidence=float(node.get("confidence")) if node.get("confidence") not in (None, "") else None,
            notes=str(node.get("notes") or "") or None,
            extra={
                key: value
                for key, value in node.items()
                if key
                not in {
                    "id",
                    "name",
                    "layer",
                    "type",
                    "manufacturer",
                    "location",
                    "capacity",
                    "capacity_unit",
                    "lead_time_months",
                    "pricing_power",
                    "bottleneck",
                    "confidence",
                    "notes",
                }
            },
        )
        session.add(row)
        session.flush()
        node_map[node_key] = row

    for edge in edges:
        source = node_map.get(str(edge.get("source") or ""))
        target = node_map.get(str(edge.get("target") or ""))
        if not source or not target:
            continue
        session.add(
            SupplyChainEdge(
                snapshot_id=int(snapshot.id),
                source_node_id=int(source.id),
                target_node_id=int(target.id),
                edge_type=str(edge.get("type") or "materials"),
                criticality=float(edge.get("criticality")) if edge.get("criticality") not in (None, "") else None,
                notes=str(edge.get("notes") or "") or None,
                extra={
                    key: value
                    for key, value in edge.items()
                    if key not in {"source", "target", "type", "criticality", "notes"}
                },
            )
        )

    now = utcnow()
    for binding in bindings:
        session.add(
            SupplyChainMetricBinding(
                snapshot_id=int(snapshot.id),
                dataset_id=int(binding["dataset_id"]) if str(binding.get("dataset_id") or "").isdigit() else None,
                dataset_view_id=int(binding["dataset_view_id"]) if str(binding.get("dataset_view_id") or "").isdigit() else None,
                scope_type=str(binding.get("scope_type") or "chain"),
                scope_key=str(binding.get("scope_key") or "chain"),
                metric_kind=str(binding.get("metric_kind") or ""),
                column_key=str(binding.get("column_key") or "") or None,
                aggregation=str(binding.get("aggregation") or "") or None,
                created_at=now,
                updated_at=now,
                extra=dict(binding.get("extra") or {}),
            )
        )
    session.flush()


def _latest_legacy_version(session: Session, chain_id: int, *, status: str | None = None) -> SupplyChainVersion | None:
    q = session.query(SupplyChainVersion).filter_by(chain_id=int(chain_id))
    if status:
        q = q.filter_by(status=status)
    return q.order_by(SupplyChainVersion.version.desc(), SupplyChainVersion.updated_at.desc()).first()


def ensure_base_scenario(
    session: Session,
    chain: SupplyChain,
    *,
    draft_snapshot: SupplyChainSnapshot | None = None,
    published_snapshot: SupplyChainSnapshot | None = None,
) -> SupplyChainScenario:
    scenario = _scenario_by_slug(session, int(chain.id), slug="base")
    now = utcnow()
    if scenario:
        if draft_snapshot:
            scenario.draft_snapshot_id = int(draft_snapshot.id)
        if published_snapshot:
            scenario.published_snapshot_id = int(published_snapshot.id)
            scenario.base_snapshot_id = int(published_snapshot.id)
        scenario.updated_at = now
        session.add(scenario)
        session.flush()
        return scenario
    scenario = SupplyChainScenario(
        chain_id=int(chain.id),
        slug="base",
        name="Base Case",
        description="Default operating view for the chain.",
        status="active",
        base_snapshot_id=int(published_snapshot.id) if published_snapshot else (int(draft_snapshot.id) if draft_snapshot else None),
        draft_snapshot_id=int(draft_snapshot.id) if draft_snapshot else None,
        published_snapshot_id=int(published_snapshot.id) if published_snapshot else None,
        created_at=now,
        updated_at=now,
        extra={},
    )
    session.add(scenario)
    session.flush()
    return scenario


def import_legacy_chain_if_needed(session: Session, chain: SupplyChain) -> None:
    ensure_supply_chain_snapshot_tables(session)
    existing = session.query(SupplyChainSnapshot).filter_by(chain_id=int(chain.id)).first()
    if existing:
        return
    legacy_draft = _latest_legacy_version(session, int(chain.id), status="draft")
    legacy_published = _latest_legacy_version(session, int(chain.id), status="published")
    base_data = legacy_draft.data if legacy_draft and isinstance(legacy_draft.data, dict) else (chain.data if isinstance(chain.data, dict) else _graph_template())
    draft = SupplyChainSnapshot(
        chain_id=int(chain.id),
        version=0,
        status="draft",
        label=None,
        based_on_snapshot_id=None,
        data={"layers": list(base_data.get("layers") or [])},
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(draft)
    session.flush()
    _replace_snapshot_graph(session, draft, base_data)

    published_snapshot = None
    if legacy_published and isinstance(legacy_published.data, dict):
        published_snapshot = SupplyChainSnapshot(
            chain_id=int(chain.id),
            version=int(legacy_published.version or 1),
            status="published",
            label=legacy_published.label,
            based_on_snapshot_id=int(draft.id),
            data={"layers": list((legacy_published.data or {}).get("layers") or [])},
            created_at=legacy_published.created_at or utcnow(),
            updated_at=legacy_published.updated_at or utcnow(),
        )
        session.add(published_snapshot)
        session.flush()
        _replace_snapshot_graph(session, published_snapshot, legacy_published.data)

    ensure_base_scenario(session, chain, draft_snapshot=draft, published_snapshot=published_snapshot)
    session.flush()


def ensure_draft_snapshot(session: Session, chain: SupplyChain) -> SupplyChainSnapshot:
    import_legacy_chain_if_needed(session, chain)
    scenario = ensure_base_scenario(session, chain)
    if scenario.draft_snapshot_id is not None:
        snapshot = session.query(SupplyChainSnapshot).filter_by(id=int(scenario.draft_snapshot_id)).first()
        if snapshot:
            return snapshot
    base = latest_chain_snapshot(session, int(chain.id), status="published") or latest_chain_snapshot(session, int(chain.id), status="draft")
    payload = graph_from_snapshot(session, base, chain_id=int(chain.id)) if base else _graph_template()
    now = utcnow()
    snapshot = SupplyChainSnapshot(
        chain_id=int(chain.id),
        version=0,
        status="draft",
        label=None,
        based_on_snapshot_id=int(base.id) if base else None,
        data={"layers": list(payload.get("layers") or [])},
        created_at=now,
        updated_at=now,
    )
    session.add(snapshot)
    session.flush()
    _replace_snapshot_graph(session, snapshot, payload)
    scenario.draft_snapshot_id = int(snapshot.id)
    scenario.updated_at = now
    session.add(scenario)
    session.flush()
    return snapshot


def create_supply_chain(
    session: Session,
    *,
    title: str,
    description: str | None = None,
    category: str = "general",
    product: str | None = None,
    status: str = "active",
    slug: str | None = None,
    data: dict[str, Any] | None = None,
) -> SupplyChain:
    ensure_supply_chain_snapshot_tables(session)
    graph = dict(data or _graph_template())
    now = utcnow()
    chain = SupplyChain(
        slug=_chain_unique_slug(session, title, slug=slug),
        title=title.strip() or "Untitled Supply Chain",
        description=(description or "").strip() or None,
        category=(category or "general").strip() or "general",
        product=(product or "").strip() or None,
        status=(status or "active").strip() or "active",
        created_at=now,
        updated_at=now,
        data=graph,
    )
    session.add(chain)
    session.flush()
    draft = SupplyChainSnapshot(
        chain_id=int(chain.id),
        version=0,
        status="draft",
        label=None,
        based_on_snapshot_id=None,
        data={"layers": list(graph.get("layers") or [])},
        created_at=now,
        updated_at=now,
    )
    session.add(draft)
    session.flush()
    _replace_snapshot_graph(session, draft, graph)
    ensure_base_scenario(session, chain, draft_snapshot=draft)
    session.add(chain)
    session.flush()
    return chain


def serialize_supply_chain(
    session: Session,
    chain: SupplyChain,
    *,
    snapshot: SupplyChainSnapshot | None = None,
    include_graph: bool = True,
    focus_node: str | None = None,
    ticker_display: str = "local",
) -> dict[str, Any]:
    import_legacy_chain_if_needed(session, chain)
    draft = latest_chain_snapshot(session, int(chain.id), status="draft")
    published = latest_chain_snapshot(session, int(chain.id), status="published")
    resolved = snapshot or draft or published
    raw_graph = graph_from_snapshot(session, resolved, chain_id=int(chain.id))
    display_graph = _display_graph(raw_graph)
    display_graph = _annotate_graph_exposure(session, display_graph, ticker_display=_ticker_display_mode(ticker_display))
    lints, node_lints, edge_lints = _compute_supply_chain_lints(display_graph)
    about = _merge_chain_about(
        chain,
        display_graph.get("about") if isinstance(display_graph.get("about"), dict) else {},
        layers=[dict(layer) for layer in (display_graph.get("layers") or []) if isinstance(layer, dict)],
        nodes=[dict(node) for node in (display_graph.get("nodes") or []) if isinstance(node, dict)],
        lints=lints,
    )
    for node in display_graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "")
        node["lint"] = [dict(item) for item in node_lints.get(node_id, [])]
    for edge in display_graph.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        edge["lint"] = [dict(item) for item in edge_lints.get(str(edge.get("key") or ""), [])]
    display_graph["about"] = about
    selected_node = None
    dossier = None
    if _clean_text(focus_node):
        node_key = str(focus_node).strip()
        selected_node = next(
            (
                dict(node)
                for node in (display_graph.get("nodes") or [])
                if isinstance(node, dict) and str(node.get("id") or "") == node_key
            ),
            None,
        )
        dossier = _build_node_dossier(session, chain, display_graph, node_key, ticker_display=_ticker_display_mode(ticker_display))
    payload = {
        "id": int(chain.id),
        "slug": chain.slug,
        "title": chain.title,
        "description": chain.description,
        "category": chain.category,
        "product": chain.product,
        "status": chain.status,
        "created_at": chain.created_at.isoformat() if chain.created_at else None,
        "updated_at": chain.updated_at.isoformat() if chain.updated_at else None,
        "about": about,
        "families": supply_chain_stage_family_legend(),
        "legend": {"families": supply_chain_stage_family_legend()},
        "ticker_display_mode": _ticker_display_mode(ticker_display),
        "counts": {
            "layers": len(raw_graph.get("layers") or []),
            "nodes": len(raw_graph.get("nodes") or []),
            "edges": len(raw_graph.get("edges") or []),
            "bindings": len(raw_graph.get("bindings") or []),
            "alerts": len(raw_graph.get("alerts") or []),
        },
        "version": version_payload(resolved),
        "draft_version": version_payload(draft),
        "published_version": version_payload(published),
        "lints": lints,
        "scenarios": [
            {
                "id": int(row.id),
                "slug": row.slug,
                "name": row.name,
                "status": row.status,
                "draft_snapshot_id": int(row.draft_snapshot_id) if row.draft_snapshot_id is not None else None,
                "published_snapshot_id": int(row.published_snapshot_id) if row.published_snapshot_id is not None else None,
            }
            for row in session.query(SupplyChainScenario).filter_by(chain_id=int(chain.id)).order_by(SupplyChainScenario.id.asc()).all()
        ],
        "exposure_summary": _build_exposure_summary([dict(node) for node in (display_graph.get("nodes") or []) if isinstance(node, dict)]),
    }
    if include_graph:
        payload["data"] = display_graph
    if selected_node is not None:
        payload["selected_node"] = selected_node
    if dossier is not None:
        payload["dossier"] = dossier
    return payload


def supply_chain_state_summary(session: Session, chain: SupplyChain, *, snapshot: SupplyChainSnapshot | None = None) -> str:
    payload = serialize_supply_chain(session, chain, snapshot=snapshot, include_graph=True)
    counts = payload.get("counts") or {}
    version = payload.get("version") or {}
    parts = [
        str(chain.title or f"chain {chain.id}"),
        f"layers={counts.get('layers', 0)}",
        f"nodes={counts.get('nodes', 0)}",
        f"edges={counts.get('edges', 0)}",
        f"bindings={counts.get('bindings', 0)}",
        f"status={chain.status}",
    ]
    if version:
        parts.append(f"version={version.get('status')}:{version.get('version')}")
    return " | ".join(parts)


def _normalize_ratio(value: object) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except Exception:
        return None
    if num > 1:
        num = num / 100.0
    return max(0.0, min(1.0, num))


def _unique_key(base: str, existing: set[str], *, fallback: str) -> str:
    raw = _slugify(base, default=fallback)
    candidate = raw
    idx = 2
    while candidate in existing:
        candidate = f"{raw}-{idx}"
        idx += 1
    return candidate


def _set_alert_rules(session: Session, chain: SupplyChain, alerts: list[dict[str, Any]]) -> None:
    session.query(SupplyChainAlertRule).filter_by(chain_id=int(chain.id)).delete(synchronize_session=False)
    now = utcnow()
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        name = str(alert.get("name") or "").strip()
        metric_kind = str(alert.get("metric_kind") or "").strip()
        if not name or not metric_kind:
            continue
        session.add(
            SupplyChainAlertRule(
                chain_id=int(chain.id),
                scope_type=str(alert.get("scope_type") or "chain"),
                scope_key=str(alert.get("scope_key") or "chain"),
                name=name,
                metric_kind=metric_kind,
                operator=str(alert.get("operator") or "gte"),
                threshold_value=float(alert.get("threshold_value") or 0),
                window_days=int(alert.get("window_days")) if str(alert.get("window_days") or "").isdigit() else None,
                status=str(alert.get("status") or "active"),
                last_triggered_at=None,
                created_at=now,
                updated_at=now,
                extra=dict(alert.get("extra") or {}),
            )
        )


def replace_draft_snapshot_graph(session: Session, chain: SupplyChain, data: dict[str, Any]) -> SupplyChainSnapshot:
    draft = ensure_draft_snapshot(session, chain)
    _replace_snapshot_graph(session, draft, data)
    _set_alert_rules(session, chain, [dict(alert) for alert in (data.get("alerts") or []) if isinstance(alert, dict)])
    chain.data = graph_from_snapshot(session, draft, chain_id=int(chain.id))
    chain.updated_at = utcnow()
    session.add(chain)
    session.flush()
    return draft


def apply_supply_chain_ops(session: Session, chain: SupplyChain, ops: list[dict[str, Any]]) -> tuple[SupplyChainSnapshot, dict[str, Any]]:
    draft = ensure_draft_snapshot(session, chain)
    data = graph_from_snapshot(session, draft, chain_id=int(chain.id))
    about: dict[str, Any] = dict(data.get("about") or {}) if isinstance(data.get("about"), dict) else {}
    layers: list[dict[str, Any]] = [dict(layer) for layer in (data.get("layers") or []) if isinstance(layer, dict)]
    nodes: list[dict[str, Any]] = [dict(node) for node in (data.get("nodes") or []) if isinstance(node, dict)]
    edges: list[dict[str, Any]] = [dict(edge) for edge in (data.get("edges") or []) if isinstance(edge, dict)]
    bindings: list[dict[str, Any]] = [dict(binding) for binding in (data.get("bindings") or []) if isinstance(binding, dict)]
    alerts: list[dict[str, Any]] = [dict(alert) for alert in (data.get("alerts") or []) if isinstance(alert, dict)]
    warnings: list[str] = []
    id_map: dict[str, str] = {}
    applied = 0

    layer_ids = {str(layer.get("id")) for layer in layers if layer.get("id")}
    node_ids = {str(node.get("id")) for node in nodes if node.get("id")}

    def find_layer(layer_id: str) -> dict[str, Any] | None:
        return next((row for row in layers if str(row.get("id")) == layer_id), None)

    def find_node(node_id: str) -> dict[str, Any] | None:
        return next((row for row in nodes if str(row.get("id")) == node_id), None)

    for op in ops:
        if not isinstance(op, dict):
            warnings.append("Skipping non-object op.")
            continue
        name = str(op.get("op") or op.get("action") or "").strip().lower()
        if not name:
            warnings.append("Skipping op without name.")
            continue

        if name == "set_meta":
            if op.get("title"):
                chain.title = str(op.get("title") or chain.title)
            if "description" in op:
                chain.description = str(op.get("description") or "") or None
            if "category" in op:
                chain.category = str(op.get("category") or chain.category or "general")
            if "product" in op:
                chain.product = str(op.get("product") or "") or None
            if "status" in op:
                chain.status = str(op.get("status") or chain.status or "active")
            if "about" in op and isinstance(op.get("about"), dict):
                about.update({str(key): value for key, value in dict(op.get("about") or {}).items()})
            applied += 1
            continue

        if name == "upsert_layer":
            label = str(op.get("name") or "").strip()
            if not label:
                warnings.append("Layer op missing name.")
                continue
            raw_id = str(op.get("layer_id") or "").strip()
            layer_id = raw_id or _unique_key(label, layer_ids, fallback="layer")
            layer = find_layer(layer_id)
            if layer is None:
                layer = {"id": layer_id, "name": label}
                layers.append(layer)
                layer_ids.add(layer_id)
            layer["name"] = label
            if "color" in op:
                layer["color"] = op.get("color")
            if "order" in op:
                try:
                    layer["order"] = int(op.get("order"))
                except Exception:
                    pass
            if "description" in op:
                layer["description"] = op.get("description")
            if "family" in op:
                layer["family"] = str(op.get("family") or "").strip().lower() or None
            if not raw_id:
                id_map[f"layer:{label}"] = layer_id
            applied += 1
            continue

        if name == "upsert_node":
            label = str(op.get("name") or "").strip()
            layer_key = str(op.get("layer") or "").strip()
            if not label or not layer_key:
                warnings.append("Node op missing name or layer.")
                continue
            raw_id = str(op.get("node_id") or "").strip()
            node_id = raw_id or _unique_key(label, node_ids, fallback="node")
            node = find_node(node_id)
            if node is None:
                node = {"id": node_id, "name": label, "layer": layer_key}
                nodes.append(node)
                node_ids.add(node_id)
            node["name"] = label
            node["layer"] = layer_key
            for key in (
                "type",
                "manufacturer",
                "location",
                "notes",
                "image_url",
                "evidence",
                "chain_ref_id",
                "chain_ref_title",
                "options",
                "capacity_unit",
                "definition",
                "why_it_matters",
                "glossary",
                "bottleneck_model",
                "marginal_system_leverage",
                "supplier_qualification",
                "qualified_suppliers",
            ):
                if key in op:
                    node[key] = op.get(key)
            for key in ("who_matters", "watchpoints"):
                if key in op:
                    node[key] = _text_list(op.get(key))
            if "display_family_override" in op:
                node["display_family_override"] = str(op.get("display_family_override") or "").strip().lower() or None
            if "lead_time_months" in op:
                try:
                    node["lead_time_months"] = int(op.get("lead_time_months"))
                except Exception:
                    pass
            if "capacity" in op and op.get("capacity") not in (None, ""):
                try:
                    node["capacity"] = float(op.get("capacity"))
                except Exception:
                    pass
            for key in ("pricing_power", "bottleneck", "confidence"):
                if key in op:
                    val = _normalize_ratio(op.get(key))
                    if val is not None:
                        node[key] = val
            if not raw_id:
                id_map[f"node:{label}"] = node_id
            applied += 1
            continue

        if name == "upsert_edge":
            source = str(op.get("source") or "").strip()
            target = str(op.get("target") or "").strip()
            if not source or not target:
                warnings.append("Edge op missing source/target.")
                continue
            edge_type = str(op.get("type") or "materials")
            row = next((edge for edge in edges if str(edge.get("source")) == source and str(edge.get("target")) == target and str(edge.get("type") or "materials") == edge_type), None)
            if row is None:
                row = {"source": source, "target": target, "type": edge_type}
                edges.append(row)
            if "criticality" in op:
                val = _normalize_ratio(op.get("criticality"))
                if val is not None:
                    row["criticality"] = val
            if "notes" in op:
                row["notes"] = op.get("notes")
            if "evidence" in op:
                row["evidence"] = op.get("evidence")
            applied += 1
            continue

        if name == "delete_node":
            node_id = str(op.get("node_id") or "").strip()
            nodes = [row for row in nodes if str(row.get("id")) != node_id]
            edges = [row for row in edges if str(row.get("source")) != node_id and str(row.get("target")) != node_id]
            applied += 1
            continue

        if name == "delete_edge":
            source = str(op.get("source") or "").strip()
            target = str(op.get("target") or "").strip()
            edge_type = str(op.get("type") or "")
            edges = [
                row
                for row in edges
                if not (
                    str(row.get("source")) == source
                    and str(row.get("target")) == target
                    and (not edge_type or str(row.get("type") or "") == edge_type)
                )
            ]
            applied += 1
            continue

        if name == "bind_metric":
            metric_kind = str(op.get("metric_kind") or "").strip()
            dataset_id = op.get("dataset_id")
            if not metric_kind or not str(dataset_id or "").isdigit():
                warnings.append("Metric binding requires metric_kind and dataset_id.")
                continue
            candidate = {
                "scope_type": str(op.get("scope_type") or "chain"),
                "scope_key": str(op.get("scope_key") or "chain"),
                "metric_kind": metric_kind,
                "dataset_id": int(dataset_id),
                "dataset_view_id": int(op["dataset_view_id"]) if str(op.get("dataset_view_id") or "").isdigit() else None,
                "column_key": str(op.get("column_key") or "") or None,
                "aggregation": str(op.get("aggregation") or "") or None,
                "extra": dict(op.get("extra") or {}),
            }
            bindings = [
                row
                for row in bindings
                if not (
                    str(row.get("scope_type") or "chain") == candidate["scope_type"]
                    and str(row.get("scope_key") or "chain") == candidate["scope_key"]
                    and str(row.get("metric_kind") or "") == candidate["metric_kind"]
                )
            ]
            bindings.append(candidate)
            applied += 1
            continue

        if name == "upsert_alert":
            alert_name = str(op.get("name") or "").strip()
            metric_kind = str(op.get("metric_kind") or "").strip()
            if not alert_name or not metric_kind:
                warnings.append("Alert op missing name or metric_kind.")
                continue
            candidate = {
                "scope_type": str(op.get("scope_type") or "chain"),
                "scope_key": str(op.get("scope_key") or "chain"),
                "name": alert_name,
                "metric_kind": metric_kind,
                "operator": str(op.get("operator") or "gte"),
                "threshold_value": float(op.get("threshold_value") or 0),
                "window_days": int(op.get("window_days")) if str(op.get("window_days") or "").isdigit() else None,
                "status": str(op.get("status") or "active"),
                "extra": dict(op.get("extra") or {}),
            }
            alerts = [
                row
                for row in alerts
                if not (
                    str(row.get("scope_type") or "chain") == candidate["scope_type"]
                    and str(row.get("scope_key") or "chain") == candidate["scope_key"]
                    and str(row.get("name") or "") == candidate["name"]
                )
            ]
            alerts.append(candidate)
            applied += 1
            continue

        if name == "delete_alert":
            alert_name = str(op.get("name") or "").strip()
            alerts = [row for row in alerts if str(row.get("name") or "") != alert_name]
            applied += 1
            continue

        warnings.append(f"Unknown op: {name}")

    merged = {
        "about": about,
        "layers": layers,
        "nodes": nodes,
        "edges": edges,
        "bindings": bindings,
        "alerts": alerts,
    }
    draft = replace_draft_snapshot_graph(session, chain, merged)
    return draft, {"applied": applied, "warnings": warnings, "id_map": id_map}


def _exposure_delta(left_nodes: list[dict[str, Any]], right_nodes: list[dict[str, Any]]) -> dict[str, Any]:
    left_summary = _build_exposure_summary(left_nodes)
    right_summary = _build_exposure_summary(right_nodes)
    left_map = {
        str(row.get("company_slug") or row.get("display_name") or "").strip().lower(): row
        for row in (left_summary.get("companies") or [])
        if isinstance(row, dict) and str(row.get("company_slug") or row.get("display_name") or "").strip()
    }
    right_map = {
        str(row.get("company_slug") or row.get("display_name") or "").strip().lower(): row
        for row in (right_summary.get("companies") or [])
        if isinstance(row, dict) and str(row.get("company_slug") or row.get("display_name") or "").strip()
    }
    added = [right_map[key] for key in sorted(set(right_map) - set(left_map))]
    removed = [left_map[key] for key in sorted(set(left_map) - set(right_map))]
    increased = []
    for key in sorted(set(left_map) & set(right_map)):
        before = left_map[key]
        after = right_map[key]
        before_count = int(before.get("linked_node_count") or 0)
        after_count = int(after.get("linked_node_count") or 0)
        if after_count > before_count:
            increased.append(
                {
                    "display_name": after.get("display_name"),
                    "company_slug": after.get("company_slug"),
                    "company_href": after.get("company_href"),
                    "primary_security": after.get("primary_security"),
                    "before_count": before_count,
                    "after_count": after_count,
                }
            )
    return {
        "left_summary": left_summary,
        "right_summary": right_summary,
        "companies_added": added,
        "companies_removed": removed,
        "companies_increased": increased,
    }


def compare_snapshots(
    session: Session,
    left: SupplyChainSnapshot | None,
    right: SupplyChainSnapshot | None,
    *,
    chain_id: int,
    ticker_display: str = "local",
) -> dict[str, Any]:
    left_graph = graph_from_snapshot(session, left, chain_id=chain_id)
    right_graph = graph_from_snapshot(session, right, chain_id=chain_id)
    left_display = _annotate_graph_exposure(session, _display_graph(left_graph), ticker_display=_ticker_display_mode(ticker_display))
    right_display = _annotate_graph_exposure(session, _display_graph(right_graph), ticker_display=_ticker_display_mode(ticker_display))
    left_nodes = {str(row.get("id")): row for row in (left_graph.get("nodes") or []) if row.get("id")}
    right_nodes = {str(row.get("id")): row for row in (right_graph.get("nodes") or []) if row.get("id")}
    left_edges = {(str(row.get("source")), str(row.get("target")), str(row.get("type") or "materials")): row for row in (left_graph.get("edges") or [])}
    right_edges = {(str(row.get("source")), str(row.get("target")), str(row.get("type") or "materials")): row for row in (right_graph.get("edges") or [])}
    left_bindings = {(str(row.get("scope_type")), str(row.get("scope_key")), str(row.get("metric_kind"))): row for row in (left_graph.get("bindings") or [])}
    right_bindings = {(str(row.get("scope_type")), str(row.get("scope_key")), str(row.get("metric_kind"))): row for row in (right_graph.get("bindings") or [])}

    changed_nodes = []
    for key in sorted(set(left_nodes) & set(right_nodes)):
        if json.dumps(left_nodes[key], sort_keys=True) != json.dumps(right_nodes[key], sort_keys=True):
            changed_nodes.append({"node_id": key, "before": left_nodes[key], "after": right_nodes[key]})

    changed_edges = []
    for key in sorted(set(left_edges) & set(right_edges)):
        if json.dumps(left_edges[key], sort_keys=True) != json.dumps(right_edges[key], sort_keys=True):
            changed_edges.append({"edge": key, "before": left_edges[key], "after": right_edges[key]})

    changed_bindings = []
    for key in sorted(set(left_bindings) & set(right_bindings)):
        if json.dumps(left_bindings[key], sort_keys=True) != json.dumps(right_bindings[key], sort_keys=True):
            changed_bindings.append({"binding": key, "before": left_bindings[key], "after": right_bindings[key]})

    exposure_delta = _exposure_delta(
        [dict(node) for node in (left_display.get("nodes") or []) if isinstance(node, dict)],
        [dict(node) for node in (right_display.get("nodes") or []) if isinstance(node, dict)],
    )
    return {
        "left": version_payload(left),
        "right": version_payload(right),
        "nodes_added": [right_nodes[key] for key in sorted(set(right_nodes) - set(left_nodes))],
        "nodes_removed": [left_nodes[key] for key in sorted(set(left_nodes) - set(right_nodes))],
        "nodes_changed": changed_nodes,
        "edges_added": [right_edges[key] for key in sorted(set(right_edges) - set(left_edges))],
        "edges_removed": [left_edges[key] for key in sorted(set(left_edges) - set(right_edges))],
        "edges_changed": changed_edges,
        "bindings_added": [right_bindings[key] for key in sorted(set(right_bindings) - set(left_bindings))],
        "bindings_removed": [left_bindings[key] for key in sorted(set(left_bindings) - set(right_bindings))],
        "bindings_changed": changed_bindings,
        "exposure_delta": exposure_delta,
    }


def create_supply_chain_snapshot(session: Session, chain: SupplyChain, *, label: str | None = None) -> SupplyChainSnapshot:
    base = ensure_draft_snapshot(session, chain)
    payload = graph_from_snapshot(session, base, chain_id=int(chain.id))
    now = utcnow()
    snapshot = SupplyChainSnapshot(
        chain_id=int(chain.id),
        version=0,
        status="draft",
        label=label,
        based_on_snapshot_id=int(base.id),
        data={"layers": list(payload.get("layers") or [])},
        created_at=now,
        updated_at=now,
    )
    session.add(snapshot)
    session.flush()
    _replace_snapshot_graph(session, snapshot, payload)
    scenario = ensure_base_scenario(session, chain)
    scenario.draft_snapshot_id = int(snapshot.id)
    scenario.updated_at = now
    session.add(scenario)
    session.flush()
    return snapshot


def publish_supply_chain_snapshot(session: Session, chain: SupplyChain, *, label: str | None = None) -> SupplyChainSnapshot:
    draft = ensure_draft_snapshot(session, chain)
    payload = graph_from_snapshot(session, draft, chain_id=int(chain.id))
    now = utcnow()
    published = SupplyChainSnapshot(
        chain_id=int(chain.id),
        version=_next_snapshot_version_num(session, int(chain.id)),
        status="published",
        label=label,
        based_on_snapshot_id=int(draft.id),
        data={"layers": list(payload.get("layers") or [])},
        created_at=now,
        updated_at=now,
    )
    session.add(published)
    session.flush()
    _replace_snapshot_graph(session, published, payload)
    scenario = ensure_base_scenario(session, chain, draft_snapshot=draft, published_snapshot=published)
    scenario.base_snapshot_id = int(published.id)
    scenario.published_snapshot_id = int(published.id)
    scenario.updated_at = now
    session.add(scenario)
    chain.updated_at = now
    chain.data = payload
    session.add(chain)
    session.flush()
    return published


def bind_supply_chain_dataset(
    session: Session,
    chain: SupplyChain,
    *,
    dataset_id: int,
    dataset_view_id: int | None = None,
    scope_type: str = "chain",
    scope_key: str = "chain",
    metric_kind: str,
    column_key: str | None = None,
    aggregation: str | None = None,
    extra: dict[str, Any] | None = None,
) -> SupplyChainMetricBinding:
    draft = ensure_draft_snapshot(session, chain)
    existing = (
        session.query(SupplyChainMetricBinding)
        .filter_by(
            snapshot_id=int(draft.id),
            scope_type=scope_type,
            scope_key=scope_key,
            metric_kind=metric_kind,
        )
        .first()
    )
    now = utcnow()
    if existing:
        existing.dataset_id = int(dataset_id)
        existing.dataset_view_id = int(dataset_view_id) if dataset_view_id is not None else None
        existing.column_key = column_key
        existing.aggregation = aggregation
        existing.updated_at = now
        existing.extra = dict(extra or {})
        session.add(existing)
        session.flush()
        return existing
    row = SupplyChainMetricBinding(
        snapshot_id=int(draft.id),
        dataset_id=int(dataset_id),
        dataset_view_id=int(dataset_view_id) if dataset_view_id is not None else None,
        scope_type=scope_type,
        scope_key=scope_key,
        metric_kind=metric_kind,
        column_key=column_key,
        aggregation=aggregation,
        created_at=now,
        updated_at=now,
        extra=dict(extra or {}),
    )
    session.add(row)
    session.flush()
    return row


def list_supply_chain_alerts(session: Session, chain_id: int) -> list[dict[str, Any]]:
    ensure_supply_chain_snapshot_tables(session)
    return [
        _alert_payload(rule)
        for rule in session.query(SupplyChainAlertRule).filter_by(chain_id=int(chain_id)).order_by(SupplyChainAlertRule.id.asc()).all()
    ]


def list_supply_chain_lints(
    session: Session,
    chain: SupplyChain,
    *,
    snapshot: SupplyChainSnapshot | None = None,
) -> list[dict[str, Any]]:
    payload = serialize_supply_chain(session, chain, snapshot=snapshot, include_graph=True)
    return [dict(row) for row in (payload.get("lints") or []) if isinstance(row, dict)]


def get_supply_chain_node_details(
    session: Session,
    chain: SupplyChain,
    node_key: str,
    *,
    snapshot: SupplyChainSnapshot | None = None,
) -> dict[str, Any] | None:
    payload = serialize_supply_chain(
        session,
        chain,
        snapshot=snapshot,
        include_graph=True,
        focus_node=str(node_key or "").strip(),
    )
    selected = payload.get("selected_node") if isinstance(payload.get("selected_node"), dict) else None
    dossier = payload.get("dossier") if isinstance(payload.get("dossier"), dict) else None
    if not selected or not dossier:
        return None
    return {
        "node": selected,
        "dossier": dossier,
        "legend": payload.get("legend") if isinstance(payload.get("legend"), dict) else {"families": supply_chain_stage_family_legend()},
        "lints": [dict(row) for row in (payload.get("lints") or []) if isinstance(row, dict)],
    }


def get_supply_chain_subchain_preview(
    session: Session,
    chain: SupplyChain,
    node_key: str,
    *,
    snapshot: SupplyChainSnapshot | None = None,
) -> dict[str, Any] | None:
    details = get_supply_chain_node_details(session, chain, node_key, snapshot=snapshot)
    if not details:
        return None
    dossier = details.get("dossier") if isinstance(details.get("dossier"), dict) else {}
    subchain = dossier.get("subchain") if isinstance(dossier.get("subchain"), dict) else None
    return dict(subchain) if subchain else None


def link_supply_chain_object(
    session: Session,
    *,
    chain_id: int,
    object_type: str,
    object_id: int,
    relation: str = "context",
    extra: dict[str, Any] | None = None,
) -> SupplyChainLink:
    object_type_v = str(object_type or "").strip().lower()
    if object_type_v not in SUPPLY_CHAIN_OBJECT_TYPES:
        raise ValueError("Unsupported supply-chain link object_type.")
    existing = (
        session.query(SupplyChainLink)
        .filter_by(
            chain_id=int(chain_id),
            object_type=object_type_v,
            object_id=int(object_id),
            relation=str(relation or "context"),
        )
        .first()
    )
    if existing:
        return existing
    row = SupplyChainLink(
        chain_id=int(chain_id),
        object_type=object_type_v,
        object_id=int(object_id),
        relation=str(relation or "context"),
        created_at=utcnow(),
        extra=dict(extra or {}),
    )
    session.add(row)
    session.flush()
    return row
