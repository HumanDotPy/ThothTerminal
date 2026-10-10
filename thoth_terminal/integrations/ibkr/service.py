from __future__ import annotations

import time
from datetime import timedelta
from threading import RLock
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.config import settings as app_settings
from thoth_terminal.db import session_scope, utcnow
from thoth_terminal.fx import ensure_fx_rates
from thoth_terminal.integrations.ibkr.asset_provider import mood_asset_payload
from thoth_terminal.integrations.ibkr.client import (
    IbkrAuthError,
    IbkrClient,
    IbkrClientError,
)
from thoth_terminal.integrations.ibkr.config import IbkrConfig
from thoth_terminal.integrations.ibkr.contract_info import (
    fetch_contract_info,
    normalize_contract_info_payload,
)
from thoth_terminal.integrations.ibkr.gateway import (
    gateway_login_url,
)
from thoth_terminal.integrations.ibkr.models import (
    MoodResult,
    PortfolioMetrics,
    PositionMetrics,
)
from thoth_terminal.integrations.ibkr.mood import classify_portfolio_mood, smooth_mood
from thoth_terminal.integrations.ibkr.storage import (
    compact_snapshot_payload,
    latest_mood_snapshot,
    latest_portfolio_snapshot,
    latest_position_snapshots,
    log_ibkr_request,
    portfolio_metrics_from_snapshot,
    store_mood_snapshot,
    store_portfolio_snapshot,
)

_PORTFOLIO_WATCHLIST_ID = "THOTH_CURRENT_PORTFOLIO"


class IbkrService:
    def __init__(self, config: IbkrConfig, *, client: IbkrClient | None = None) -> None:
        self.config = config
        self.client = client or IbkrClient(config, request_logger=self._log_request)
        self._pnl_error_backoff_until = 0.0
        self._last_pnl_error: str | None = None
        self._contract_info_backoff_until = 0.0
        self._secdef_search_backoff_until = 0.0
        self._desktop_sign_in_confirmation: str | None = None
        self._desktop_sign_in_lock = RLock()

    def refresh_after_desktop_sign_in(self, confirmation: str) -> None:
        """One invalidation per verified desktop session, including old-account data."""
        with self._desktop_sign_in_lock:
            if confirmation == self._desktop_sign_in_confirmation:
                return
            self.client.refresh_session()
            self._pnl_error_backoff_until = 0.0
            self._last_pnl_error = None
            self._contract_info_backoff_until = 0.0
            self._secdef_search_backoff_until = 0.0
            self._desktop_sign_in_confirmation = confirmation

    def status(self, *, live: bool = True) -> dict[str, Any]:
        if not self.config.active:
            return {
                "enabled": False,
                "connected": False,
                "auth_required": False,
                "mode": "disabled",
                "message": "IBKR integration is disabled.",
            }
        if not live:
            return {
                "enabled": True,
                "connected": False,
                "auth_required": False,
                "mode": "mock" if self.config.mock_enabled and not self.config.enabled else "client_portal",
                "gateway_base_url": self.config.gateway_base_url,
                "login_url": gateway_login_url(self.config.gateway_base_url),
                "message": "IBKR live status has not been checked yet.",
            }
        if self.config.mock_enabled and not self.config.enabled:
            return {
                "enabled": True,
                "connected": True,
                "auth_required": False,
                "mode": "mock",
                "message": "IBKR mock data is enabled.",
            }
        try:
            payload = self.client.get("/iserver/auth/status", ttl_s=5.0, attempts=1,
                                      timeout_s=min(2.0, float(self.config.timeout_s)))
            authenticated = isinstance(payload, dict) and payload.get("authenticated") is True
            if authenticated:
                self.client.clear_auth_backoff()
            return {
                "enabled": True,
                "connected": authenticated,
                "auth_required": not authenticated,
                "mode": "client_portal",
                "gateway_base_url": self.config.gateway_base_url,
                "competing": isinstance(payload, dict) and payload.get("competing") is True,
            }
        except IbkrAuthError as exc:
            return {
                "enabled": True,
                "connected": False,
                "auth_required": True,
                "mode": "client_portal",
                "message": str(exc),
            }
        except Exception as exc:
            return {
                "enabled": True,
                "connected": False,
                "auth_required": False,
                "mode": "client_portal",
                "message": f"{type(exc).__name__}: {exc}",
            }

    def tickle(self) -> dict[str, Any]:
        self._require_active()
        if self.config.mock_enabled and not self.config.enabled:
            return {"mock": True, "status": "ok"}
        return self.client.get("/tickle", ttl_s=5.0)

    def accounts(self) -> list[dict[str, Any]]:
        self._require_active()
        if self.config.mock_enabled and not self.config.enabled:
            return [_mock_account(self.config.default_account_id)]
        payload = self.client.get("/portfolio/accounts", ttl_s=60.0)
        return payload if isinstance(payload, list) else []

    def subaccounts(self) -> Any:
        self._require_active()
        if self.config.mock_enabled and not self.config.enabled:
            return []
        return self.client.get("/portfolio/subaccounts", ttl_s=120.0)

    def account_summary(self, account_id: str | None = None) -> dict[str, Any]:
        self._require_active()
        account = self._account_id(account_id)
        if self.config.mock_enabled and not self.config.enabled:
            return _mock_summary(account)
        payload = self.client.get(f"/portfolio/{account}/summary", ttl_s=60.0)
        return payload if isinstance(payload, dict) else {"raw": payload}

    def positions(self, account_id: str | None = None, *, page_id: int = 0) -> list[dict[str, Any]]:
        self._require_active()
        account = self._account_id(account_id)
        if self.config.mock_enabled and not self.config.enabled:
            return _mock_positions(account)
        try:
            payload = self.client.get(f"/portfolio2/{account}/positions", ttl_s=30.0)
            positions = _extract_position_items(payload)
            if positions:
                return positions
        except Exception:
            pass
        payload = self.client.get(f"/portfolio/{account}/positions/{int(page_id)}", ttl_s=30.0)
        return _extract_position_items(payload)

    def pnl(self) -> dict[str, Any]:
        self._require_active()
        if self.config.mock_enabled and not self.config.enabled:
            return _mock_pnl(self.config.default_account_id or "DU-MOCK")
        payload = self.client.get("/iserver/account/pnl/partitioned", ttl_s=10.0)
        return payload if isinstance(payload, dict) else {"raw": payload}

    def pnl_optional(self) -> dict[str, Any]:
        if self.config.mock_enabled and not self.config.enabled:
            return _mock_pnl(self.config.default_account_id or "DU-MOCK")
        now = time.monotonic()
        if now < self._pnl_error_backoff_until:
            return {"_thoth_error": self._last_pnl_error, "_thoth_skipped": True}
        try:
            self._last_pnl_error = None
            return self.pnl()
        except IbkrClientError as exc:
            self._last_pnl_error = str(exc)[:240]
            self._pnl_error_backoff_until = now + 60.0
            return {"_thoth_error": self._last_pnl_error}

    def trades(self) -> Any:
        self._require_active()
        if self.config.mock_enabled and not self.config.enabled:
            return []
        return self.client.get("/iserver/account/trades", ttl_s=60.0)

    def performance(self, payload: dict[str, Any] | None = None) -> Any:
        self._require_active()
        if self.config.mock_enabled and not self.config.enabled:
            return {"mock": True, "period": "1M", "return_pct": 2.4}
        return self.client.post_read("/pa/performance", json_payload=payload or {}, ttl_s=15.0 * 60.0)

    def performance_all_periods(self, account_ids: list[str] | None = None) -> Any:
        self._require_active()
        accounts = [str(item).strip() for item in (account_ids or []) if str(item).strip()]
        if not accounts:
            accounts = [self._account_id(None)]
        if self.config.mock_enabled and not self.config.enabled:
            return {"mock": True, "YTD": 18.42, "MTD": 4.8, "1D": 2.8}
        return self.client.post_read(
            "/pa/allperiods",
            json_payload={"acctIds": accounts},
            ttl_s=15.0 * 60.0,
            attempts=1,
            timeout_s=min(8.0, float(self.config.timeout_s or 8.0)),
        )

    def transactions(self, payload: dict[str, Any] | None = None) -> Any:
        self._require_active()
        if self.config.mock_enabled and not self.config.enabled:
            return []
        return self.client.post_read("/pa/transactions", json_payload=payload or {}, ttl_s=15.0 * 60.0)

    def marketdata_snapshot(self, *, symbols: list[str] | None = None, conids: list[str] | None = None) -> dict[str, Any]:
        self._require_active()
        cleaned = [str(sym or "").strip().upper() for sym in (symbols or []) if str(sym or "").strip()]
        if self.config.mock_enabled and not self.config.enabled:
            return {"items": [_mock_market_snapshot(sym) for sym in cleaned], "missing_symbols": []}
        explicit_conids = [str(conid or "").strip() for conid in (conids or []) if str(conid or "").strip()]
        resolved_symbols: dict[str, str] = {}
        missing: list[str] = []
        for sym in cleaned:
            mapped = self.config.symbol_conids.get(sym)
            if not mapped:
                mapped = self._resolve_symbol_conid(sym)
            if mapped:
                resolved_symbols[sym] = mapped
            else:
                missing.append(sym)
        mapped_conids = [resolved_symbols[sym] for sym in cleaned if sym in resolved_symbols]
        requested_conids = list(dict.fromkeys([*explicit_conids, *mapped_conids]))
        if not requested_conids:
            return {"items": [], "missing_symbols": missing, "resolved_symbols": resolved_symbols}
        params = {
            "conids": ",".join(requested_conids[:100]),
            "fields": "31,55,70,71,73,75,78,79,80,82,83,84,86,87,6004,6008,6070,6509,7051,7635",
        }
        payload = self.client.get("/iserver/marketdata/snapshot", params=params, ttl_s=0.0)
        items = [_normalize_market_snapshot(item) for item in (payload if isinstance(payload, list) else [])]
        if _market_snapshots_sparse(items):
            time.sleep(1.0)
            payload = self.client.get("/iserver/marketdata/snapshot", params=params, ttl_s=30.0)
            items = [_normalize_market_snapshot(item) for item in (payload if isinstance(payload, list) else [])]
        return {
            "items": items,
            "missing_symbols": missing,
            "resolved_symbols": resolved_symbols,
            "delayed_ok": True,
        }

    def security_search(
        self,
        query: str,
        *,
        name: bool = True,
        sec_type: str = "STK",
        allow_secdef: bool = True,
    ) -> list[dict[str, Any]]:
        self._require_active()
        cleaned = str(query or "").strip()
        if not cleaned:
            return []
        if self.config.mock_enabled and not self.config.enabled:
            return _mock_security_search(cleaned)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        errors: list[str] = []

        def append_rows(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = str(row.get("conid") or row.get("conId") or row.get("symbol") or repr(sorted(row.items())))
                if key in seen:
                    continue
                seen.add(key)
                out.append(row)

        def try_stocks_fallback() -> bool:
            fallback_errors: list[str] = []
            try:
                fallback_rows = self._stocks_search_fallback(cleaned, sec_type=sec_type)
            except IbkrAuthError:
                raise
            except IbkrClientError as exc:
                fallback_errors.append(str(exc)[:240])
                fallback_rows = []
            append_rows(fallback_rows)
            if not out and fallback_errors:
                errors.extend(f"TRSRV {error}" for error in fallback_errors)
            return bool(out)

        symbol_like = _is_symbol_search_query(cleaned)
        if symbol_like:
            if try_stocks_fallback():
                return out
            if not allow_secdef:
                if errors:
                    raise IbkrClientError("IBKR stock search failed: " + " | ".join(errors[-3:]))
                return out
            try:
                append_rows(self._security_search_get(cleaned, name=False, sec_type=sec_type))
            except IbkrAuthError:
                raise
            except IbkrClientError as exc:
                errors.append(f"GET {str(exc)[:180]}")
            if out:
                return out
            if not name:
                try:
                    append_rows(self._security_search_post(cleaned, name=False, sec_type=sec_type))
                except IbkrAuthError:
                    raise
                except IbkrClientError as exc:
                    errors.append(f"POST {str(exc)[:180]}")
        else:
            if not allow_secdef:
                return []
            try:
                append_rows(self._security_search_once(cleaned, name=False, sec_type=sec_type))
            except IbkrAuthError:
                raise
            except IbkrClientError as exc:
                errors.append(str(exc)[:240])
        if out:
            return out

        if allow_secdef and name:
            try:
                append_rows(self._security_search_once(cleaned, name=True, sec_type=sec_type))
            except IbkrAuthError:
                raise
            except IbkrClientError as exc:
                errors.append(str(exc)[:240])
            if out:
                return out

        if allow_secdef and not symbol_like and try_stocks_fallback():
            return out
        if errors:
            raise IbkrClientError("IBKR security search failed: " + " | ".join(errors[-3:]))
        return []

    def _security_search_once(self, query: str, *, name: bool, sec_type: str = "STK") -> list[dict[str, Any]]:
        errors: list[str] = []
        try:
            return self._security_search_get(query, name=name, sec_type=sec_type)
        except IbkrAuthError:
            raise
        except IbkrClientError as exc:
            errors.append(f"GET {str(exc)[:180]}")
        try:
            return self._security_search_post(query, name=name, sec_type=sec_type)
        except IbkrAuthError:
            raise
        except IbkrClientError as exc:
            errors.append(f"POST {str(exc)[:180]}")
        raise IbkrClientError("; ".join(errors) or "IBKR security search failed.")

    def _security_search_get(self, query: str, *, name: bool, sec_type: str = "STK") -> list[dict[str, Any]]:
        sec_type_v = sec_type or "STK"
        timeout_s = min(4.0, float(self.config.timeout_s or 4.0))
        params: dict[str, Any] = {"symbol": query, "secType": sec_type_v}
        if name:
            params["name"] = "true"
        payload = self.client.get(
            "/iserver/secdef/search",
            params=params,
            ttl_s=300.0,
            attempts=1,
            timeout_s=timeout_s,
        )
        return payload if isinstance(payload, list) else []

    def _security_search_post(self, query: str, *, name: bool, sec_type: str = "STK") -> list[dict[str, Any]]:
        sec_type_v = sec_type or "STK"
        timeout_s = min(4.0, float(self.config.timeout_s or 4.0))
        post_payload = {"symbol": query, "name": bool(name), "secType": sec_type_v}
        payload = self.client.post_read(
            "/iserver/secdef/search",
            json_payload=post_payload,
            ttl_s=300.0,
            attempts=1,
            timeout_s=timeout_s,
        )
        return payload if isinstance(payload, list) else []

    def _stocks_search_fallback(self, query: str, *, sec_type: str = "STK") -> list[dict[str, Any]]:
        if not _is_symbol_search_query(query):
            return []
        sec_type_v = sec_type or "STK"
        timeout_s = min(4.0, float(self.config.timeout_s or 4.0))
        symbol = str(query or "").strip().upper()
        payload = self.client.get(
            "/trsrv/stocks",
            params={"symbols": symbol},
            ttl_s=300.0,
            attempts=1,
            timeout_s=timeout_s,
        )
        return _stock_search_rows_from_trsrv(payload, symbol=symbol, sec_type=sec_type_v)

    def watchlists(self) -> dict[str, Any]:
        self._require_active()
        if self.config.mock_enabled and not self.config.enabled:
            return {
                "data": {
                    "user_lists": [
                        {"id": "MOCK_AI", "name": "AI Masterlist", "read_only": True, "type": "watchlist"},
                        {"id": "MOCK_SEMI", "name": "Semicap", "read_only": True, "type": "watchlist"},
                    ],
                    "system_lists": [],
                    "show_scanners": False,
                    "scanners_only": False,
                },
                "mock": True,
            }
        try:
            payload = self.client.get("/iserver/watchlists", params={"SC": "USER_WATCHLIST"}, ttl_s=300.0)
        except Exception as exc:
            return self._portfolio_watchlists_payload(error=str(exc)[:240])
        if isinstance(payload, dict):
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            user_lists = data.get("user_lists") if isinstance(data, dict) else None
            system_lists = data.get("system_lists") if isinstance(data, dict) else None
            if user_lists or system_lists:
                return payload
        return self._portfolio_watchlists_payload(
            error="IBKR Client Portal did not return user watchlists.",
        )

    def watchlist(self, watchlist_id: str) -> dict[str, Any]:
        self._require_active()
        cleaned = str(watchlist_id or "").strip()
        if not cleaned:
            raise IbkrClientError("Missing IBKR watchlist id.")
        if self.config.mock_enabled and not self.config.enabled:
            return _mock_watchlist(cleaned)
        if cleaned == _PORTFOLIO_WATCHLIST_ID:
            return self._portfolio_watchlist_payload()
        payload = self.client.get("/iserver/watchlist", params={"id": cleaned}, ttl_s=60.0)
        return payload if isinstance(payload, dict) else {"raw": payload}

    def contract_info(self, conid: str | int) -> dict[str, Any] | None:
        cleaned = str(conid or "").strip()
        if not cleaned:
            return None
        if self.config.mock_enabled and not self.config.enabled:
            return None
        if self.config.active and time.monotonic() >= self._contract_info_backoff_until:
            try:
                payload = self.client.get(f"/iserver/contract/{cleaned}/info", ttl_s=86400.0)
            except Exception:
                self._contract_info_backoff_until = time.monotonic() + 120.0
                payload = None
            if isinstance(payload, dict):
                normalized = normalize_contract_info_payload(payload, conid=cleaned)
                if normalized:
                    return normalized
        return fetch_contract_info(cleaned, timeout_s=min(float(self.config.timeout_s or 8.0), 8.0))

    def capture_portfolio_snapshot(self, account_id: str | None = None) -> dict[str, Any]:
        self._require_active()
        pnl = self.pnl_optional()
        accounts = self._portfolio_account_ids(account_id)
        performance = self._performance_optional(accounts)
        metrics_by_account: list[PortfolioMetrics] = []
        positions: list[PositionMetrics] = []
        for account in accounts:
            summary = self.account_summary(account)
            positions_raw = self.positions(account)
            base_currency = _summary_currency(summary) or str(getattr(app_settings, "base_currency", "USD") or "USD")
            fx_rates = self._fx_rates(base_currency)
            account_metrics, account_positions = derive_portfolio_metrics(
                account_id=account,
                summary=summary,
                positions_raw=positions_raw,
                pnl=pnl,
                performance=performance,
                base_currency=base_currency,
                fx_rates=fx_rates,
            )
            metrics_by_account.append(account_metrics)
            positions.extend(account_positions)
        if not metrics_by_account:
            account = self._account_id(account_id)
            metrics, positions = derive_portfolio_metrics(
                account_id=account,
                summary={},
                positions_raw=[],
                pnl=pnl,
                performance=performance,
                base_currency=str(getattr(app_settings, "base_currency", "USD") or "USD"),
            )
        elif len(metrics_by_account) == 1:
            metrics = metrics_by_account[0]
        else:
            metrics, positions = _aggregate_portfolio_metrics(metrics_by_account, positions, account_id="ALL")
        market_snapshots = self._market_snapshots_optional(positions)
        if market_snapshots:
            metrics = PortfolioMetrics(
                **{
                    **metrics.__dict__,
                    "metadata": dict(metrics.metadata)
                    | {
                        "market_snapshots": market_snapshots.get("items", []),
                        "market_data_status": _market_data_status(market_snapshots.get("items", [])),
                    },
                }
            )
        with session_scope() as session:
            row = store_portfolio_snapshot(session, metrics=metrics, positions=positions)
            snapshot_id = int(row.id)
        return {"ok": True, "snapshot_id": snapshot_id, "metrics": metrics.__dict__}

    def portfolio_mood(
        self,
        db: Session,
        *,
        account_id: str | None = None,
        settings: Any = None,
        max_snapshot_age_s: float = 30.0,
        allow_live_refresh: bool = True,
        persist_mood: bool = True,
    ) -> dict[str, Any]:
        account = account_id or self.config.default_account_id
        metrics: PortfolioMetrics | None = None
        positions_payload: list[dict[str, Any]] = []
        live_error = None

        if self.config.active and allow_live_refresh:
            try:
                if self.config.mock_enabled and not self.config.enabled:
                    metrics, position_metrics = derive_portfolio_metrics(
                        account_id=self._account_id(account),
                        summary=_mock_summary(self._account_id(account)),
                        positions_raw=_mock_positions(self._account_id(account)),
                        pnl=_mock_pnl(self._account_id(account)),
                    )
                    positions_payload = [pos.__dict__ for pos in position_metrics]
                elif self.config.enabled:
                    latest = latest_portfolio_snapshot(db, account_id=account)
                    latest_age = None
                    if latest is not None and latest.captured_at is not None:
                        latest_age = utcnow() - latest.captured_at
                    if latest is None or latest_age is None or latest_age > timedelta(seconds=max(1.0, max_snapshot_age_s)):
                        db.rollback()
                        self.capture_portfolio_snapshot(account)
            except Exception as exc:
                live_error = f"{type(exc).__name__}: {exc}"

        if metrics is None:
            latest = latest_portfolio_snapshot(db, account_id=account)
            if latest is not None:
                pos_rows = latest_position_snapshots(db, int(latest.id))
                metrics = portfolio_metrics_from_snapshot(latest, pos_rows)
                positions_payload = compact_snapshot_payload(latest, pos_rows)["positions"]

        if metrics is None:
            account_fallback = account or self.config.default_account_id or "mock"
            metrics = PortfolioMetrics(account_id=account_fallback, metadata={"source": "empty"})

        previous_row = latest_mood_snapshot(db, account_id=metrics.account_id)
        previous = None
        previous_at = None
        if previous_row is not None:
            previous = MoodResult(
                account_id=previous_row.account_id,
                primary_mood=previous_row.primary_mood,
                secondary_mood=previous_row.secondary_mood,
                intensity=previous_row.intensity,
                caption=previous_row.caption,
                reasons=list(previous_row.reasons_json or []),
                metrics=dict(previous_row.metrics_json or {}),
                sprite_asset_key=previous_row.sprite_asset_key or previous_row.primary_mood,
            )
            previous_at = previous_row.captured_at

        now = utcnow()
        mood = smooth_mood(
            classify_portfolio_mood(metrics),
            previous,
            previous_at=previous_at,
            now=now,
        )
        if persist_mood:
            store_mood_snapshot(db, mood=mood, captured_at=now)
            db.flush()
        asset = mood_asset_payload(settings, mood.primary_mood, mood.intensity) if settings else {}
        return {
            "ok": True,
            "account_id": mood.account_id,
            "primary_mood": mood.primary_mood,
            "secondary_mood": mood.secondary_mood,
            "intensity": mood.intensity,
            "caption": mood.caption,
            "reasons": mood.reasons,
            "metrics": mood.metrics,
            "sprite_asset_key": mood.sprite_asset_key,
            "asset": asset,
            "positions": positions_payload,
            "market_snapshots": list((metrics.metadata or {}).get("market_snapshots") or []),
            "market_data_status": (metrics.metadata or {}).get("market_data_status"),
            "live_error": live_error,
            "captured_at": now.isoformat(),
        }

    def _performance_optional(self, account_id: str | list[str]) -> dict[str, Any]:
        if self.config.mock_enabled and not self.config.enabled:
            accounts = account_id if isinstance(account_id, list) else [account_id]
            return self.performance_all_periods(accounts)
        try:
            accounts = account_id if isinstance(account_id, list) else [account_id]
            payload = self.performance_all_periods(accounts)
            return payload if isinstance(payload, dict) else {"raw": payload}
        except Exception as exc:
            return {"_thoth_error": str(exc)[:240]}

    def _market_snapshots_optional(self, positions: list[PositionMetrics]) -> dict[str, Any]:
        conids = [str(pos.conid or "").strip() for pos in positions if str(pos.conid or "").strip()]
        if not conids:
            return {}
        try:
            return self.marketdata_snapshot(conids=conids[:25])
        except Exception as exc:
            return {"items": [], "_thoth_error": str(exc)[:240]}

    def _account_id(self, account_id: str | None) -> str:
        selected = str(account_id or self.config.default_account_id or "").strip()
        if selected:
            return selected
        accounts = self.accounts()
        if accounts:
            first = accounts[0]
            for key in ("accountId", "account_id", "id", "account"):
                if isinstance(first, dict) and first.get(key):
                    return str(first[key])
        return "DU-MOCK" if self.config.mock_enabled else ""

    def _portfolio_account_ids(self, account_id: str | None) -> list[str]:
        selected = str(account_id or self.config.default_account_id or "").strip()
        if selected:
            return [selected]
        if self.config.mock_enabled and not self.config.enabled:
            return [self._account_id(None)]
        accounts = self.accounts()
        out: list[str] = []
        for item in accounts:
            if not isinstance(item, dict):
                continue
            for key in ("accountId", "account_id", "id", "account"):
                value = str(item.get(key) or "").strip()
                if value:
                    out.append(value)
                    break
        return list(dict.fromkeys(out)) or [self._account_id(None)]

    def _portfolio_watchlists_payload(self, *, error: str | None = None) -> dict[str, Any]:
        watchlist = self._portfolio_watchlist_payload()
        summary = {
            "id": watchlist["id"],
            "name": watchlist["name"],
            "read_only": True,
            "readOnly": True,
            "type": "portfolio",
            "source": "portfolio_positions",
            "count": len(watchlist.get("instruments") or []),
        }
        payload: dict[str, Any] = {
            "data": {
                "user_lists": [summary] if summary["count"] else [],
                "system_lists": [],
                "show_scanners": False,
                "scanners_only": False,
            },
            "fallback": True,
            "source": "portfolio_positions",
        }
        if error:
            payload["_thoth_error"] = error
        if watchlist.get("_thoth_errors"):
            payload["_thoth_errors"] = watchlist["_thoth_errors"]
        return payload

    def _portfolio_watchlist_payload(self) -> dict[str, Any]:
        instruments, errors = self._portfolio_position_watchlist_instruments()
        payload: dict[str, Any] = {
            "id": _PORTFOLIO_WATCHLIST_ID,
            "name": "Current Portfolio",
            "readOnly": True,
            "source": "portfolio_positions",
            "fallback": True,
            "instruments": instruments,
        }
        if errors:
            payload["_thoth_errors"] = errors
        return payload

    def _portfolio_position_watchlist_instruments(self) -> tuple[list[dict[str, Any]], list[str]]:
        instruments: list[dict[str, Any]] = []
        by_key: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        try:
            accounts = self._portfolio_account_ids(None)
        except Exception as exc:
            fallback_account = str(self.config.default_account_id or "").strip()
            accounts = [fallback_account] if fallback_account else []
            errors.append(str(exc)[:240])
        for account in accounts:
            try:
                rows = self.positions(account)
            except Exception as exc:
                errors.append(str(exc)[:240])
                continue
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                item = _watchlist_item_from_position(raw, account_id=account)
                key = str(item.get("conid") or item.get("ticker") or item.get("name") or "").strip()
                if not key:
                    continue
                existing = by_key.get(key)
                if existing is not None:
                    metadata = existing.setdefault("metadata", {})
                    if isinstance(metadata, dict):
                        accounts_meta = metadata.setdefault("source_accounts", [])
                        if isinstance(accounts_meta, list) and account not in accounts_meta:
                            accounts_meta.append(account)
                    continue
                by_key[key] = item
                instruments.append(item)
        if instruments:
            return instruments, errors
        cached, cached_errors = self._cached_portfolio_watchlist_instruments()
        return cached, errors + cached_errors

    def _cached_portfolio_watchlist_instruments(self) -> tuple[list[dict[str, Any]], list[str]]:
        try:
            with session_scope() as session:
                snapshot = latest_portfolio_snapshot(session, account_id=self.config.default_account_id)
                if snapshot is None:
                    snapshot = latest_portfolio_snapshot(session)
                if snapshot is None:
                    return [], ["No cached portfolio snapshot is available."]
                rows = latest_position_snapshots(session, int(snapshot.id))
                out: list[dict[str, Any]] = []
                for pos in rows:
                    metadata = dict(pos.metadata_json or {})
                    raw = dict(metadata) | {
                        "conid": pos.conid,
                        "ticker": pos.symbol,
                        "symbol": pos.symbol,
                        "assetClass": pos.asset_class,
                        "currency": metadata.get("position_currency") or metadata.get("currency"),
                        "position": pos.quantity,
                        "marketValue": pos.market_value,
                        "averageCost": pos.average_cost,
                        "unrealizedPnl": pos.unrealized_pnl,
                        "realizedPnl": pos.realized_pnl,
                    }
                    item = _watchlist_item_from_position(raw, account_id=str(snapshot.account_id or "cached"))
                    item_metadata = item.setdefault("metadata", {})
                    if isinstance(item_metadata, dict):
                        item_metadata["source"] = "cached_portfolio_snapshot"
                        item_metadata["captured_at"] = snapshot.captured_at.isoformat() if snapshot.captured_at else None
                    out.append(item)
                return out, []
        except Exception as exc:
            return [], [str(exc)[:240]]

    def _resolve_symbol_conid(self, symbol: str) -> str | None:
        cleaned = str(symbol or "").strip().upper()
        if not cleaned:
            return None
        now = time.monotonic()
        if now < self._secdef_search_backoff_until:
            return None
        try:
            payload = self.security_search(cleaned, name=False, sec_type="STK")
        except Exception:
            self._secdef_search_backoff_until = now + 300.0
            return None
        return _contract_search_conid(payload, cleaned)

    def _fx_rates(self, base_currency: str) -> dict[str, float]:
        base = str(base_currency or getattr(app_settings, "base_currency", "USD") or "USD").upper().strip()
        try:
            with session_scope() as session:
                snapshot = ensure_fx_rates(session, base=base)
                if snapshot:
                    return dict(snapshot.rates)
        except Exception:
            pass
        return {base: 1.0}

    def _require_active(self) -> None:
        if not self.config.active:
            raise IbkrClientError("IBKR integration is disabled.")

    @staticmethod
    def _log_request(
        endpoint: str,
        method: str,
        status_code: int | None,
        duration_ms: float | None,
        cache_hit: bool,
        error_summary: str | None,
    ) -> None:
        try:
            with session_scope() as session:
                log_ibkr_request(
                    session,
                    endpoint=endpoint,
                    method=method,
                    status_code=status_code,
                    duration_ms=duration_ms,
                    cache_hit=cache_hit,
                    error_summary=error_summary,
                )
        except Exception:
            pass


def derive_portfolio_metrics(
    *,
    account_id: str,
    summary: dict[str, Any],
    positions_raw: list[dict[str, Any]],
    pnl: dict[str, Any],
    performance: dict[str, Any] | None = None,
    base_currency: str = "USD",
    fx_rates: dict[str, float] | None = None,
) -> tuple[PortfolioMetrics, list[PositionMetrics]]:
    net_liq = _summary_number(summary, "NetLiquidation", "netLiquidation", "net_liquidation")
    cash = _summary_number(summary, "TotalCashValue", "cash", "CashBalance")
    daily_pnl = _pnl_number(pnl, "dpl", "dailyPnL", "daily_pnl", account_id=account_id)
    if daily_pnl is None:
        daily_pnl = _summary_number(summary, "DailyPnL", "dailyPnl", "daily_pnl")
    unrealized = _pnl_number(pnl, "upl", "unrealizedPnL", "unrealized_pnl", account_id=account_id)
    realized = _pnl_number(pnl, "rpl", "realizedPnL", "realized_pnl", account_id=account_id)
    mtd_pct = _summary_number(summary, "mtd_pnl_pct", "MTDReturn", "mtdReturn")
    ytd_pct = _summary_number(summary, "ytd_pnl_pct", "YTDReturn", "ytdReturn")
    perf_periods = _performance_periods(performance or {}, account_id=account_id)
    mtd_pct = mtd_pct if mtd_pct is not None else perf_periods.get("MTD")
    ytd_pct = ytd_pct if ytd_pct is not None else perf_periods.get("YTD")

    positions: list[PositionMetrics] = []
    gross = 0.0
    net = 0.0
    largest_weight = 0.0
    contributor = None
    detractor = None
    contributor_value = 0.0
    detractor_value = 0.0
    unrealized_total = 0.0
    realized_total = 0.0
    has_unrealized = False
    has_realized = False
    base_currency = str(base_currency or "USD").upper().strip()
    fx = dict(fx_rates or {})
    fx.setdefault(base_currency, 1.0)
    for raw in positions_raw:
        source_currency = _position_currency(raw, base_currency)
        mv_raw = _first_number(raw, "mktValue", "marketValue", "market_value", "value") or 0.0
        mv = _convert_to_base_number(mv_raw, source_currency, base_currency, fx)
        qty = _first_number(raw, "position", "quantity", "qty")
        avg_raw = _first_number(raw, "avgCost", "averageCost", "average_cost", "avgPrice", "averagePrice")
        avg = _convert_to_base_number(avg_raw, source_currency, base_currency, fx) if avg_raw is not None else None
        upnl_raw = _first_number(raw, "unrealizedPnl", "unrealizedPNL", "unrealized_pnl")
        rpnl_raw = _first_number(raw, "realizedPnl", "realizedPNL", "realized_pnl")
        upnl = _convert_to_base_number(upnl_raw, source_currency, base_currency, fx) if upnl_raw is not None else None
        rpnl = _convert_to_base_number(rpnl_raw, source_currency, base_currency, fx) if rpnl_raw is not None else None
        if upnl is not None:
            has_unrealized = True
            unrealized_total += upnl
        if rpnl is not None:
            has_realized = True
            realized_total += rpnl
        gross += abs(mv)
        net += mv
        weight = (abs(mv) / net_liq) if net_liq else None
        if weight is not None:
            largest_weight = max(largest_weight, weight)
        symbol = str(
            raw.get("ticker")
            or raw.get("symbol")
            or raw.get("contractDesc")
            or raw.get("description")
            or ""
        ).strip() or None
        if upnl is not None and upnl > contributor_value:
            contributor_value = upnl
            contributor = symbol
        if upnl is not None and upnl < detractor_value:
            detractor_value = upnl
            detractor = symbol
        positions.append(
            PositionMetrics(
                account_id=account_id,
                symbol=symbol,
                conid=str(raw.get("conid") or raw.get("conId") or "").strip() or None,
                asset_class=str(raw.get("assetClass") or raw.get("asset_class") or "").strip() or None,
                quantity=qty,
                market_value=mv,
                average_cost=avg,
                unrealized_pnl=upnl,
                realized_pnl=rpnl,
                weight=weight,
                metadata=dict(raw)
                | {
                    "base_currency": base_currency,
                    "position_currency": source_currency,
                    "market_value_raw": mv_raw,
                    "market_value_base": mv,
                },
            )
        )

    if unrealized is None and has_unrealized:
        unrealized = unrealized_total
    if realized is None and has_realized:
        realized = realized_total

    day_base = (net_liq - daily_pnl) if (daily_pnl is not None and net_liq is not None) else None
    daily_pct = (daily_pnl / day_base * 100.0) if (daily_pnl is not None and day_base) else None
    cash_pct = (cash / net_liq) if (cash is not None and net_liq) else None
    gross_pct = (gross / net_liq) if net_liq else None
    return (
        PortfolioMetrics(
            account_id=account_id,
            net_liquidation=net_liq,
            cash=cash,
            gross_exposure=gross,
            net_exposure=net,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pct,
            unrealized_pnl=unrealized,
            realized_pnl=realized,
            mtd_pnl_pct=mtd_pct,
            ytd_pnl_pct=ytd_pct,
            cash_pct=cash_pct,
            gross_exposure_pct=gross_pct,
            largest_position_weight=largest_weight,
            largest_contributor=contributor,
            largest_detractor=detractor,
            largest_detractor_pnl=detractor_value if detractor else None,
            metadata={
                "summary": summary,
                "pnl": pnl,
                "performance": performance or {},
                "performance_periods": perf_periods,
                "base_currency": base_currency,
                "captured_at": utcnow().isoformat(),
            },
        ),
        positions,
    )


def _aggregate_portfolio_metrics(
    metrics_by_account: list[PortfolioMetrics],
    positions: list[PositionMetrics],
    *,
    account_id: str = "ALL",
) -> tuple[PortfolioMetrics, list[PositionMetrics]]:
    net_liq = _sum_metric(metrics_by_account, "net_liquidation")
    cash = _sum_metric(metrics_by_account, "cash")
    daily_pnl = _sum_metric(metrics_by_account, "daily_pnl")
    unrealized = _sum_metric(metrics_by_account, "unrealized_pnl")
    realized = _sum_metric(metrics_by_account, "realized_pnl")
    gross = sum(abs(float(pos.market_value or 0.0)) for pos in positions)
    net = sum(float(pos.market_value or 0.0) for pos in positions)
    normalized_positions: list[PositionMetrics] = []
    for pos in positions:
        weight = (abs(float(pos.market_value or 0.0)) / net_liq) if net_liq else None
        normalized_positions.append(
            PositionMetrics(
                account_id=pos.account_id,
                symbol=pos.symbol,
                conid=pos.conid,
                asset_class=pos.asset_class,
                quantity=pos.quantity,
                market_value=pos.market_value,
                average_cost=pos.average_cost,
                unrealized_pnl=pos.unrealized_pnl,
                realized_pnl=pos.realized_pnl,
                weight=weight,
                metadata=dict(pos.metadata or {}) | {"aggregate_account_id": account_id},
            )
        )
    contributor = None
    contributor_value = 0.0
    detractor = None
    detractor_value = 0.0
    for pos in normalized_positions:
        value = float(pos.unrealized_pnl or 0.0)
        if value > contributor_value:
            contributor_value = value
            contributor = pos.symbol
        if value < detractor_value:
            detractor_value = value
            detractor = pos.symbol
    day_base = (net_liq - daily_pnl) if (daily_pnl is not None and net_liq is not None) else None
    daily_pct = (daily_pnl / day_base * 100.0) if (daily_pnl is not None and day_base) else None
    cash_pct = (cash / net_liq) if (cash is not None and net_liq) else None
    gross_pct = (gross / net_liq) if net_liq else None
    largest_weight = max((pos.weight or 0.0 for pos in normalized_positions), default=0.0)
    return (
        PortfolioMetrics(
            account_id=account_id,
            net_liquidation=net_liq,
            cash=cash,
            gross_exposure=gross,
            net_exposure=net,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pct,
            unrealized_pnl=unrealized,
            realized_pnl=realized,
            mtd_pnl_pct=_weighted_metric_pct(metrics_by_account, "mtd_pnl_pct"),
            ytd_pnl_pct=_weighted_metric_pct(metrics_by_account, "ytd_pnl_pct"),
            cash_pct=cash_pct,
            gross_exposure_pct=gross_pct,
            largest_position_weight=largest_weight,
            largest_contributor=contributor,
            largest_detractor=detractor,
            largest_detractor_pnl=detractor_value if detractor else None,
            metadata={
                "source": "aggregate",
                "account_count": len(metrics_by_account),
                "account_ids": [row.account_id for row in metrics_by_account],
                "captured_at": utcnow().isoformat(),
            },
        ),
        normalized_positions,
    )


def _sum_metric(metrics_by_account: list[PortfolioMetrics], key: str) -> float | None:
    values = [getattr(row, key) for row in metrics_by_account if getattr(row, key) is not None]
    return sum(float(value) for value in values) if values else None


def _weighted_metric_pct(metrics_by_account: list[PortfolioMetrics], key: str) -> float | None:
    total_weight = 0.0
    weighted = 0.0
    for row in metrics_by_account:
        pct = getattr(row, key)
        weight = row.net_liquidation
        if pct is None or weight is None or weight <= 0:
            continue
        total_weight += float(weight)
        weighted += float(pct) * float(weight)
    return (weighted / total_weight) if total_weight else None


def _extract_position_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("positions", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _normalize_market_snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    availability = str(raw.get("6509") or raw.get("marketDataAvailability") or "").strip()
    return {
        "conid": str(raw.get("conid") or raw.get("6008") or "").strip() or None,
        "symbol": str(raw.get("55") or raw.get("symbol") or "").strip() or None,
        "company_name": str(raw.get("7051") or "").strip() or None,
        "last": _to_float(raw.get("31")),
        "mark": _to_float(raw.get("7635")),
        "change": _to_float(raw.get("82")),
        "change_pct": _to_float(raw.get("83")),
        "bid": _to_float(raw.get("84")),
        "ask": _to_float(raw.get("86")),
        "volume": _to_float(raw.get("87")),
        "exchange": str(raw.get("6004") or "").strip() or None,
        "sec_type": str(raw.get("6070") or "").strip() or None,
        "availability": availability or None,
        "availability_label": _market_availability_label(availability),
        "raw": raw,
    }


def _market_snapshots_sparse(items: list[dict[str, Any]]) -> bool:
    if not items:
        return False
    value_keys = ("last", "mark", "change", "change_pct", "bid", "ask", "availability")
    return not any(any(item.get(key) is not None for key in value_keys) for item in items if isinstance(item, dict))


def _market_availability_label(value: str) -> str:
    flags = set(str(value or "").upper())
    if "R" in flags:
        return "real-time"
    if "D" in flags:
        return "delayed"
    if "Y" in flags:
        return "frozen-delayed"
    if "Z" in flags:
        return "frozen"
    if "N" in flags:
        return "unavailable"
    return "unknown"


def _market_data_status(items: list[dict[str, Any]]) -> str:
    labels = {_market_availability_label(str(item.get("availability") or "")) for item in items if isinstance(item, dict)}
    if not labels:
        return "unavailable"
    if "real-time" in labels:
        return "real-time"
    if "delayed" in labels or "frozen-delayed" in labels:
        return "delayed"
    if "frozen" in labels:
        return "frozen"
    return "unavailable"


def _performance_periods(payload: dict[str, Any], *, account_id: str | None = None) -> dict[str, float]:
    out: dict[str, float] = {}

    def visit(value: Any, label: str | None = None) -> None:
        if isinstance(value, dict):
            period = str(
                value.get("period")
                or value.get("periodType")
                or value.get("timePeriod")
                or value.get("name")
                or label
                or ""
            ).upper()
            if period in {"1D", "7D", "MTD", "1M", "YTD", "1Y"}:
                cps = value.get("cps")
                if isinstance(cps, list) and cps:
                    pct = _to_float(cps[-1])
                    if pct is not None:
                        out[period] = pct * 100.0
            for key in ("return_pct", "returnPct", "return", "value", "performance", "changePercent", "change_pct"):
                pct = _to_float(value.get(key))
                if period in {"1D", "7D", "MTD", "1M", "YTD", "1Y"} and pct is not None:
                    out[period] = pct
                    break
            for child_key, child in value.items():
                visit(child, str(child_key).upper())
        elif isinstance(value, list):
            for item in value:
                visit(item, label)
        else:
            pct = _to_float(value)
            period = str(label or "").upper()
            if period in {"1D", "7D", "MTD", "1M", "YTD", "1Y"} and pct is not None:
                out[period] = pct

    scoped = _account_payload(payload, account_id) if account_id else None
    visit(scoped if scoped is not None else payload)
    return out


def _summary_number(summary: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        out = _to_float(_mapping_get(summary, key))
        if out is not None:
            return out
    return None


def _pnl_number(payload: dict[str, Any], *keys: str, account_id: str | None = None) -> float | None:
    if account_id:
        scoped = _account_payload(payload, account_id)
        if scoped is not None:
            out = _pnl_number(scoped, *keys)
            if out is not None:
                return out
            return None
        if _payload_has_account_scope(payload):
            return None
    for key in keys:
        out = _to_float(_mapping_get(payload, key))
        if out is not None:
            return out
    for value in payload.values():
        if isinstance(value, dict):
            out = _pnl_number(value, *keys)
            if out is not None:
                return out
    return None


def _first_number(raw: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        out = _to_float(_mapping_get(raw, key))
        if out is not None:
            return out
    return None


def _summary_currency(summary: dict[str, Any]) -> str | None:
    for key in ("NetLiquidation", "netLiquidation", "netliquidation", "TotalCashValue", "totalcashvalue"):
        value = _mapping_get(summary, key)
        if isinstance(value, dict):
            currency = str(value.get("currency") or "").strip().upper()
            if currency:
                return currency
    return None


def _position_currency(raw: dict[str, Any], base_currency: str) -> str:
    currency = str(
        raw.get("currency")
        or raw.get("ccy")
        or raw.get("contractCurrency")
        or raw.get("listingCurrency")
        or base_currency
        or "USD"
    ).upper().strip()
    return currency or str(base_currency or "USD").upper().strip()


def _convert_to_base_number(
    amount: float | None,
    currency: str,
    base_currency: str,
    fx_rates: dict[str, float],
) -> float:
    value = float(amount or 0.0)
    cur = str(currency or base_currency or "USD").upper().strip()
    base = str(base_currency or "USD").upper().strip()
    if not cur or cur == base:
        return value
    rate = _to_float(fx_rates.get(cur))
    if not rate:
        return value
    return value / rate


def _account_payload(payload: Any, account_id: str | None) -> Any | None:
    account = _normalize_account_key(account_id)
    if not account:
        return None
    if isinstance(payload, dict):
        row_account = (
            payload.get("accountId")
            or payload.get("account_id")
            or payload.get("acctId")
            or payload.get("account")
            or payload.get("acct")
        )
        if _normalize_account_key(row_account) == account:
            return payload
        for key, value in payload.items():
            if _normalize_account_key(key) == account:
                return value
        for key in ("upnl", "accounts", "data", "result", "results"):
            value = payload.get(key)
            found = _account_payload(value, account)
            if found is not None:
                return found
        for value in payload.values():
            if isinstance(value, (dict, list)):
                found = _account_payload(value, account)
                if found is not None:
                    return found
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                row_account = (
                    item.get("accountId")
                    or item.get("account_id")
                    or item.get("acctId")
                    or item.get("account")
                    or item.get("acct")
                )
                if _normalize_account_key(row_account) == account:
                    return item
                found = _account_payload(item, account)
                if found is not None:
                    return found
    return None


def _payload_has_account_scope(payload: Any) -> bool:
    if isinstance(payload, dict):
        row_account = (
            payload.get("accountId")
            or payload.get("account_id")
            or payload.get("acctId")
            or payload.get("account")
            or payload.get("acct")
        )
        if _normalize_account_key(row_account):
            return True
        for key, value in payload.items():
            normalized_key = _normalize_account_key(key)
            if normalized_key and isinstance(value, dict) and any(
                _mapping_get(value, pnl_key) is not None
                for pnl_key in ("dpl", "dailyPnL", "daily_pnl", "upl", "rpl")
            ):
                return True
            if str(key).lower() in {"upnl", "accounts", "data", "result", "results"} and _payload_has_account_scope(value):
                return True
            if isinstance(value, (dict, list)) and _payload_has_account_scope(value):
                return True
    elif isinstance(payload, list):
        return any(_payload_has_account_scope(item) for item in payload)
    return False


def _normalize_account_key(value: Any) -> str:
    text = str(value or "").upper().strip()
    return text.rstrip(".")


def _contract_search_conid(payload: Any, symbol: str) -> str | None:
    symbol = str(symbol or "").upper().strip()
    candidates: list[dict[str, Any]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("conid") or value.get("conId"):
                candidates.append(value)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    collect(child)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(payload)
    if not candidates:
        return None
    preferred = []
    for item in candidates:
        item_symbol = str(
            item.get("symbol")
            or item.get("ticker")
            or item.get("companyHeader")
            or item.get("description")
            or ""
        ).upper()
        sec_type = str(item.get("secType") or item.get("assetClass") or item.get("ST") or "").upper()
        if symbol and symbol in item_symbol and (not sec_type or sec_type == "STK"):
            preferred.append(item)
    chosen = preferred[0] if preferred else candidates[0]
    conid = chosen.get("conid") or chosen.get("conId")
    return str(conid).strip() if conid else None


def _is_symbol_search_query(query: str) -> bool:
    text = str(query or "").strip().upper()
    if not text or len(text) > 24 or any(ch.isspace() for ch in text):
        return False
    allowed = set(".-/")
    return any(ch.isalnum() for ch in text) and all(ch.isalnum() or ch in allowed for ch in text)


def _stock_search_rows_from_trsrv(payload: Any, *, symbol: str, sec_type: str = "STK") -> list[dict[str, Any]]:
    symbol_v = str(symbol or "").strip().upper()
    if not symbol_v or not isinstance(payload, dict):
        return []
    raw_items = payload.get(symbol_v)
    if raw_items is None:
        for key, value in payload.items():
            if str(key or "").strip().upper() == symbol_v:
                raw_items = value
                break
    if isinstance(raw_items, dict):
        items = [raw_items]
    elif isinstance(raw_items, list):
        items = raw_items
    else:
        return []

    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("companyName") or item.get("companyHeader") or "").strip()
        asset_class = str(item.get("assetClass") or item.get("secType") or sec_type or "STK").strip().upper()
        contracts = item.get("contracts")
        if isinstance(contracts, dict):
            contract_rows = [contracts]
        elif isinstance(contracts, list):
            contract_rows = [contract for contract in contracts if isinstance(contract, dict)]
        else:
            contract_rows = []
        if not contract_rows:
            rows.append(
                {
                    "symbol": symbol_v,
                    "companyName": name or symbol_v,
                    "companyHeader": name or symbol_v,
                    "assetClass": asset_class,
                    "secType": asset_class,
                    "_thoth_source": "trsrv/stocks",
                }
            )
            continue
        for contract in contract_rows:
            exchange = str(contract.get("exchange") or contract.get("listingExchange") or "").strip()
            header = f"{name} - {exchange}" if name and exchange else name or symbol_v
            rows.append(
                {
                    "conid": contract.get("conid") or contract.get("conId"),
                    "symbol": symbol_v,
                    "companyName": name or symbol_v,
                    "companyHeader": header,
                    "assetClass": asset_class,
                    "secType": asset_class,
                    "exchange": exchange or None,
                    "listingExchange": exchange or None,
                    "isUS": contract.get("isUS"),
                    "_thoth_source": "trsrv/stocks",
                }
            )
    return rows


def _mapping_get(raw: dict[str, Any], key: str) -> Any:
    if key in raw:
        return raw.get(key)
    lowered = str(key).lower()
    for candidate, value in raw.items():
        if str(candidate).lower() == lowered:
            return value
    return None


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        if isinstance(value, dict):
            for key in ("amount", "value", "raw", "val", "number"):
                if key in value:
                    out = _to_float(value.get(key))
                    if out is not None:
                        return out
            return None
        if isinstance(value, str):
            value = value.replace(",", "").replace("%", "").replace("$", "").strip()
            if len(value) > 1 and value[0].upper() in {"C", "D"} and value[1].isdigit():
                value = value[1:]
            if value.lower() in {"null", "none", "nan"}:
                return None
        return float(value)
    except Exception:
        return None


def _mock_account(account_id: str | None = None) -> dict[str, Any]:
    account = account_id or "DU-MOCK"
    return {"accountId": account, "accountTitle": "Mock Portfolio", "type": "DEMO"}


def _mock_summary(account_id: str) -> dict[str, Any]:
    return {
        "accountId": account_id,
        "NetLiquidation": {"amount": 250000.0, "currency": "USD"},
        "TotalCashValue": {"amount": 37500.0, "currency": "USD"},
        "mtd_pnl_pct": 4.8,
        "ytd_pnl_pct": 18.42,
    }


def _mock_pnl(account_id: str) -> dict[str, Any]:
    return {
        "accountId": account_id,
        "daily_pnl": 6825.0,
        "unrealized_pnl": 24100.0,
        "realized_pnl": 3200.0,
    }


def _mock_positions(account_id: str) -> list[dict[str, Any]]:
    return [
        {
            "accountId": account_id,
            "ticker": "NVDA",
            "conid": "4815747",
            "assetClass": "STK",
            "position": 320,
            "mktValue": 84000.0,
            "avgCost": 205.0,
            "unrealizedPnl": 9800.0,
            "realizedPnl": 0.0,
        },
        {
            "accountId": account_id,
            "ticker": "TSM",
            "conid": "14157",
            "assetClass": "STK",
            "position": 260,
            "mktValue": 52000.0,
            "avgCost": 175.0,
            "unrealizedPnl": 4100.0,
            "realizedPnl": 1200.0,
        },
        {
            "accountId": account_id,
            "ticker": "AMD",
            "conid": "4391",
            "assetClass": "STK",
            "position": 180,
            "mktValue": 26000.0,
            "avgCost": 155.0,
            "unrealizedPnl": -1500.0,
            "realizedPnl": 0.0,
        },
    ]


def _watchlist_item_from_position(raw: dict[str, Any], *, account_id: str) -> dict[str, Any]:
    ticker = _position_watchlist_ticker(raw)
    name = str(
        raw.get("companyName")
        or raw.get("name")
        or raw.get("description")
        or raw.get("contractDesc")
        or ticker
        or ""
    ).strip()
    conid = str(raw.get("conid") or raw.get("conId") or "").strip()
    asset_class = str(raw.get("assetClass") or raw.get("asset_class") or raw.get("secType") or raw.get("sec_type") or "").strip()
    currency = str(raw.get("currency") or raw.get("currencyCode") or "").strip()
    exchange = str(raw.get("exchange") or raw.get("listingExchange") or raw.get("listing_exchange") or "").strip()
    metadata = {
        "source": "portfolio_position",
        "source_accounts": [account_id],
        "position": _first_number(raw, "position", "quantity", "qty"),
        "market_value": _first_number(raw, "mktValue", "marketValue", "market_value", "value"),
        "average_cost": _first_number(raw, "avgCost", "averageCost", "average_cost", "avgPrice", "averagePrice"),
        "unrealized_pnl": _first_number(raw, "unrealizedPnl", "unrealizedPNL", "unrealized_pnl"),
        "raw": raw,
    }
    return {
        "conid": conid or None,
        "ticker": ticker,
        "symbol": ticker,
        "name": name or ticker or (f"IBKR conid {conid}" if conid else "Portfolio position"),
        "fullName": name or ticker or "",
        "assetClass": asset_class or None,
        "currency": currency or None,
        "exchange": exchange or None,
        "metadata": metadata,
    }


def _position_watchlist_ticker(raw: dict[str, Any]) -> str | None:
    for key in ("ticker", "symbol", "localSymbol", "local_symbol"):
        value = str(raw.get(key) or "").strip().upper()
        if value:
            return value
    for key in ("contractDesc", "description"):
        value = str(raw.get(key) or "").strip().upper()
        if value and _looks_like_ticker(value):
            return value
    return None


def _looks_like_ticker(value: str) -> bool:
    text = str(value or "").strip().upper()
    if not text or len(text) > 14:
        return False
    if not any(ch.isalpha() for ch in text):
        return False
    return all(ch.isalnum() or ch in {".", "-", "/"} for ch in text)


def _mock_market_snapshot(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "last": None,
        "delayed": True,
        "message": "Mock snapshot; configure symbol-to-conid mapping for live Client Portal data.",
    }


def _mock_security_search(query: str) -> list[dict[str, Any]]:
    q = str(query or "").strip().upper()
    examples = {
        "NVDA": {"conid": 4815747, "symbol": "NVDA", "companyName": "NVIDIA CORP", "secType": "STK"},
        "TSM": {"conid": 14157, "symbol": "TSM", "companyName": "TAIWAN SEMICONDUCTOR-SP ADR", "secType": "STK"},
        "AVGO": {"conid": 13847680, "symbol": "AVGO", "companyName": "BROADCOM INC", "secType": "STK"},
    }
    if q in examples:
        return [examples[q]]
    return [
        {
            "conid": f"MOCK-{q or 'SEARCH'}",
            "symbol": q or "MOCK",
            "companyName": f"{q or 'Mock'} IBKR contract",
            "secType": "STK",
        }
    ]


def _mock_watchlist(watchlist_id: str) -> dict[str, Any]:
    name = "AI Masterlist" if "AI" in watchlist_id.upper() else "Semicap"
    rows = [
        {"name": "Hyperscalers / CSP"},
        {"conid": 3691937, "ticker": "AMZN", "name": "AMAZON.COM INC", "fullName": "AMZN", "assetClass": "STK"},
        {"conid": 208813720, "ticker": "GOOGL", "name": "ALPHABET INC-CL A", "fullName": "GOOGL", "assetClass": "STK"},
        {"name": "Accelerators / custom silicon"},
        {"conid": 4815747, "ticker": "NVDA", "name": "NVIDIA CORP", "fullName": "NVDA", "assetClass": "STK"},
        {"conid": 13847680, "ticker": "AVGO", "name": "BROADCOM INC", "fullName": "AVGO", "assetClass": "STK"},
    ]
    return {
        "id": watchlist_id,
        "name": name,
        "readOnly": True,
        "instruments": rows,
        "mock": True,
    }
