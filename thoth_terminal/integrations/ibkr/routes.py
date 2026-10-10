from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from thoth_terminal.web.json_response import JSONResponse
from sqlalchemy.orm import Session
from fastapi import Depends

from thoth_terminal.config import settings
from thoth_terminal.db import get_session
from thoth_terminal.integrations.ibkr.client import IbkrAuthError, IbkrClientError
from thoth_terminal.integrations.ibkr.config import IbkrConfig
from thoth_terminal.integrations.ibkr.service import IbkrService
from thoth_terminal.security import build_security_posture


_SERVICE: IbkrService | None = None
_SERVICE_KEY: tuple[Any, ...] | None = None


def ibkr_service() -> IbkrService:
    global _SERVICE, _SERVICE_KEY
    config = IbkrConfig.from_settings(settings)
    key = (
        config.enabled,
        config.mock_enabled,
        config.gateway_base_url,
        config.verify_ssl,
        config.timeout_s,
        config.auth_backoff_s,
        config.default_account_id,
        tuple(sorted(config.symbol_conids.items())),
    )
    if _SERVICE is None or key != _SERVICE_KEY:
        _SERVICE = IbkrService(config)
        _SERVICE_KEY = key
    return _SERVICE


def _guard_public() -> None:
    if build_security_posture(settings).public:
        raise HTTPException(status_code=404, detail="Not found")


def _guard_active(service: IbkrService) -> None:
    _guard_public()
    if not service.config.active:
        raise HTTPException(status_code=503, detail="IBKR integration is disabled")


def _json(payload: Any, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code)


def _ticker_lookup_query(query: str) -> bool:
    text = str(query or "").strip()
    if not text or len(text) > 24 or any(ch.isspace() for ch in text):
        return False
    if not all(ch.isalnum() or ch in ".-/" for ch in text):
        return False
    if any(ch.isdigit() or ch in ".-/" for ch in text):
        return True
    return len(text) <= 5


def _broker_error(exc: Exception) -> JSONResponse:
    status_code = 401 if isinstance(exc, IbkrAuthError) else 502
    return _json(
        {
            "ok": False,
            "error": type(exc).__name__,
            "message": str(exc),
            "read_only": True,
        },
        status_code=status_code,
    )


def _broker_read_error(exc: Exception, *, message: str = "IBKR Client Portal read is degraded.") -> JSONResponse:
    status_code = 401 if isinstance(exc, IbkrAuthError) else 502
    detail = "IBKR authentication is required." if isinstance(exc, IbkrAuthError) else message
    return _json(
        {
            "ok": False,
            "error": type(exc).__name__,
            "message": detail,
            "read_only": True,
        },
        status_code=status_code,
    )


def register_ibkr_routes(app: FastAPI) -> None:
    @app.get("/api/ibkr/status")
    def api_ibkr_status() -> JSONResponse:
        _guard_public()
        return _json(ibkr_service().status())

    @app.get("/api/ibkr/accounts")
    def api_ibkr_accounts() -> JSONResponse:
        service = ibkr_service()
        _guard_active(service)
        return _json({"items": service.accounts()})

    @app.get("/api/ibkr/subaccounts")
    def api_ibkr_subaccounts() -> JSONResponse:
        service = ibkr_service()
        _guard_active(service)
        return _json({"items": service.subaccounts()})

    @app.get("/api/ibkr/account-summary")
    def api_ibkr_account_summary(account_id: str | None = None) -> JSONResponse:
        service = ibkr_service()
        _guard_active(service)
        return _json(service.account_summary(account_id))

    @app.get("/api/ibkr/positions")
    def api_ibkr_positions(account_id: str | None = None) -> JSONResponse:
        service = ibkr_service()
        _guard_active(service)
        return _json({"items": service.positions(account_id)})

    @app.get("/api/ibkr/pnl")
    def api_ibkr_pnl() -> JSONResponse:
        service = ibkr_service()
        _guard_active(service)
        try:
            return _json(service.pnl())
        except IbkrClientError as exc:
            return _broker_error(exc)

    @app.get("/api/ibkr/trades")
    def api_ibkr_trades() -> JSONResponse:
        service = ibkr_service()
        _guard_active(service)
        return _json({"items": service.trades()})

    @app.get("/api/ibkr/performance")
    def api_ibkr_performance() -> JSONResponse:
        service = ibkr_service()
        _guard_active(service)
        return _json(service.performance({}))

    @app.get("/api/ibkr/transactions")
    def api_ibkr_transactions() -> JSONResponse:
        service = ibkr_service()
        _guard_active(service)
        return _json({"items": service.transactions({})})

    @app.get("/api/ibkr/marketdata/snapshot")
    def api_ibkr_marketdata_snapshot(symbols: str = Query(default="")) -> JSONResponse:
        service = ibkr_service()
        _guard_active(service)
        parsed = [item.strip() for item in str(symbols or "").split(",") if item.strip()]
        try:
            return _json(service.marketdata_snapshot(symbols=parsed))
        except IbkrClientError as exc:
            return _broker_read_error(exc, message="IBKR market data is degraded.")

    @app.get("/api/ibkr/security-search")
    def api_ibkr_security_search(
        q: str = Query(default=""),
        sec_type: str = Query(default="STK"),
        allow_secdef: bool | None = None,
    ) -> JSONResponse:
        service = ibkr_service()
        _guard_active(service)
        ticker_lookup = _ticker_lookup_query(q)
        use_secdef = bool(allow_secdef) if allow_secdef is not None else not ticker_lookup
        try:
            return _json(
                {
                    "items": service.security_search(
                        q,
                        name=not ticker_lookup,
                        sec_type=sec_type,
                        allow_secdef=use_secdef,
                    )
                }
            )
        except IbkrClientError as exc:
            return _broker_read_error(exc, message="IBKR Client Portal search is degraded.")

    @app.get("/api/ibkr/watchlists")
    def api_ibkr_watchlists() -> JSONResponse:
        service = ibkr_service()
        _guard_active(service)
        return _json(service.watchlists())

    @app.get("/api/ibkr/watchlist")
    def api_ibkr_watchlist(id: str = Query(default="")) -> JSONResponse:
        service = ibkr_service()
        _guard_active(service)
        return _json(service.watchlist(id))

    @app.get("/api/portfolio/mood")
    def api_portfolio_mood(
        account_id: str | None = None,
        db: Session = Depends(get_session),
    ) -> JSONResponse:
        _guard_public()
        payload = ibkr_service().portfolio_mood(db, account_id=account_id, settings=settings)
        db.commit()
        return _json(payload)
