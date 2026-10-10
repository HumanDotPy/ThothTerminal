from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
import time


class YahooFinanceError(RuntimeError):
    batch_errors: list[dict[str, Any]] | None = None


class YahooFinanceHttpError(YahooFinanceError):
    def __init__(self, status_code: int, message: str | None = None) -> None:
        self.status_code = int(status_code)
        super().__init__(message or f"Yahoo HTTP {self.status_code}")


class _YahooBatchResult(dict[str, Any]):
    """A normal symbol mapping retaining failed batch provenance for callers."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__()
        self.errors = errors
        self.complete_failure = False


@dataclass(frozen=True)
class YahooQuote:
    symbol: str
    name: str | None
    price: float
    currency: str | None
    market_time_utc: datetime | None
    exchange: str | None = None
    full_exchange_name: str | None = None
    regular_market_change: float | None = None
    regular_market_change_percent: float | None = None
    market_cap: float | None = None
    trailing_pe: float | None = None
    forward_pe: float | None = None
    one_month_return: float | None = None
    ytd_return: float | None = None
    one_year_return: float | None = None


@dataclass(frozen=True)
class AdjCloseBar:
    ts_utc: datetime
    adj_close: float


@dataclass(frozen=True)
class YahooSymbolSearchResult:
    symbol: str
    name: str | None
    exchange: str | None


_yahoo_circuit_open_until_ts: float = 0.0
_YAHOO_V7_QUOTE_CHUNK_SIZE = 50
# Yahoo spark returns HTTP 400 for the live mixed-region Discord watchlist at ~25+ symbols.
_YAHOO_SPARK_CHUNK_SIZE = 20


def _symbol_chunks(symbols: list[str], chunk_size: int) -> list[list[str]]:
    size = max(1, int(chunk_size))
    return [symbols[start : start + size] for start in range(0, len(symbols), size)]


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return None
    if isinstance(value, dict):
        # Yahoo sometimes wraps values as {"raw": ..., "fmt": ...}
        raw = value.get("raw")
        if raw is not None:
            return _as_float(raw)
        fmt = value.get("fmt")
        if isinstance(fmt, str):
            s0 = fmt.strip()
            is_pct = s0.endswith("%")
            s = s0.replace("%", "")
            try:
                val = float(s)
                return (val / 100.0) if is_pct else val
            except Exception:
                return None
    return None


def _get_json(
    url: str,
    *,
    params: dict[str, Any] | None,
    headers: dict[str, str],
    timeout_s: float,
    retries: int = 3,
) -> dict[str, Any]:
    global _yahoo_circuit_open_until_ts
    last_exc: Exception | None = None
    last_status: int | None = None
    for attempt in range(max(1, int(retries))):
        now_ts = time.time()
        if now_ts < _yahoo_circuit_open_until_ts:
            wait_s = int(_yahoo_circuit_open_until_ts - now_ts)
            raise YahooFinanceError(f"Yahoo temporarily unavailable (rate limited). Retry in ~{wait_s}s.")
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout_s)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt + 1 < max(1, int(retries)):
                time.sleep(min(3.0, 0.4 * (2**attempt)))
            continue

        last_status = int(resp.status_code)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            try:
                wait_s = float(retry_after) if retry_after else 0.0
            except Exception:
                wait_s = 0.0
            if wait_s <= 0:
                wait_s = min(60.0, 1.0 * (2**attempt))
            # Trip a short circuit so callers can fall back to cached DB values.
            _yahoo_circuit_open_until_ts = max(_yahoo_circuit_open_until_ts, time.time() + float(wait_s))
            raise YahooFinanceHttpError(429, "Yahoo rate limited (HTTP 429).")

        if resp.status_code != 200:
            raise YahooFinanceHttpError(int(resp.status_code), f"Yahoo HTTP {resp.status_code}")

        try:
            data = resp.json()
        except Exception as exc:
            raise YahooFinanceError("Yahoo returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise YahooFinanceError("Yahoo returned unexpected payload")
        return data

    if last_exc:
        raise YahooFinanceError(f"Yahoo request failed: {last_exc}") from last_exc
    if last_status == 429:
        raise YahooFinanceHttpError(429, "Yahoo rate limited (HTTP 429).")
    if last_status:
        raise YahooFinanceHttpError(int(last_status), f"Yahoo request failed (HTTP {last_status}).")
    raise YahooFinanceError("Yahoo request failed.")


def resolve_symbol(symbol: str, *, user_agent: str, timeout_s: float = 12.0) -> str:
    sym = (symbol or "").strip().lstrip("$").upper()
    if not sym:
        raise ValueError("Missing ticker.")

    quotes = fetch_quotes([sym], user_agent=user_agent, timeout_s=timeout_s)
    if sym in quotes:
        return sym

    if "." in sym:
        alt = sym.replace(".", "-")
        quotes = fetch_quotes([alt], user_agent=user_agent, timeout_s=timeout_s)
        if alt in quotes:
            return alt

    raise ValueError(f"Ticker not found on Yahoo: {sym}")


def _fetch_v7_quotes(
    symbols: list[str],
    *,
    user_agent: str,
    timeout_s: float,
) -> dict[str, YahooQuote]:
    """
    Batch quote fetch via /v7/finance/quote (single request).
    This endpoint is sometimes blocked; callers should be ready to fall back.
    """
    uniq = [str(s or "").strip().upper() for s in symbols]
    uniq = [s for s in uniq if s]
    if not uniq:
        return {}

    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
    }
    data = _get_json(url, params={"symbols": ",".join(uniq)}, headers=headers, timeout_s=timeout_s, retries=2)
    qr = data.get("quoteResponse") or {}
    if not isinstance(qr, dict):
        return {}
    results = qr.get("result") or []
    if not isinstance(results, list):
        return {}

    out: dict[str, YahooQuote] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        sym = item.get("symbol")
        if not sym:
            continue
        sym_u = str(sym).strip().upper()
        if not sym_u:
            continue
        price = _as_float(item.get("regularMarketPrice"))
        if price is None:
            continue

        nm = item.get("longName") or item.get("shortName") or item.get("displayName") or item.get("name")
        name = str(nm).strip() if nm else None

        cur = item.get("currency")
        currency = str(cur).strip() if cur else None

        ts = item.get("regularMarketTime")
        market_dt = None
        if ts:
            try:
                market_dt = datetime.fromtimestamp(int(ts), timezone.utc).replace(tzinfo=None)
            except Exception:
                market_dt = None

        exchange = item.get("exchange") or item.get("exchangeName")
        exchange_s = str(exchange).strip() if exchange else None
        full_ex = item.get("fullExchangeName") or item.get("exchDisp") or item.get("exchangeName")
        full_exchange_name = str(full_ex).strip() if full_ex else None

        # Optional shortcuts (may not be present).
        ytd_ret = _as_float(item.get("ytdReturn"))
        y1_ret = _as_float(item.get("fiftyTwoWeekChangePercent"))
        change = _as_float(item.get("regularMarketChange"))
        change_pct = _as_float(item.get("regularMarketChangePercent"))
        market_cap = _as_float(item.get("marketCap"))
        trailing_pe = _as_float(item.get("trailingPE"))
        forward_pe = _as_float(item.get("forwardPE"))

        out[sym_u] = YahooQuote(
            symbol=sym_u,
            name=name,
            price=float(price),
            currency=currency,
            market_time_utc=market_dt,
            exchange=exchange_s,
            full_exchange_name=full_exchange_name,
            regular_market_change=change,
            regular_market_change_percent=change_pct,
            market_cap=market_cap,
            trailing_pe=trailing_pe,
            forward_pe=forward_pe,
            one_month_return=None,
            ytd_return=(float(ytd_ret) if ytd_ret is not None else None),
            one_year_return=(float(y1_ret) if y1_ret is not None else None),
        )

    return out


def _fetch_spark_quotes(
    symbols: list[str],
    *,
    user_agent: str,
    timeout_s: float,
) -> dict[str, YahooQuote]:
    """
    Batch quote fetch via /v7/finance/spark.
    Useful when /v7/finance/quote is blocked (401/403) but chart-backed spark still works.
    """
    uniq = [str(s or "").strip().upper() for s in symbols]
    uniq = [s for s in uniq if s]
    if not uniq:
        return {}

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
    }
    errors: list[dict[str, Any]] = []
    out: dict[str, YahooQuote] = _YahooBatchResult(errors)
    for data in _spark_chunk_payloads(uniq, range_="5d", headers=headers, timeout_s=timeout_s, errors=errors):
        out.update(_spark_quotes_from_payload(data))
    return out


def _spark_quotes_from_payload(data: dict[str, Any]) -> dict[str, YahooQuote]:
    spark = data.get("spark") or {}
    if not isinstance(spark, dict):
        return {}
    results = spark.get("result") or []
    if not isinstance(results, list):
        return {}
    out: dict[str, YahooQuote] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        response_list = item.get("response") or []
        response = response_list[0] if isinstance(response_list, list) and response_list else {}
        if not isinstance(response, dict):
            response = {}
        meta = response.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}

        symbol = str(item.get("symbol") or meta.get("symbol") or "").strip().upper()
        if not symbol:
            continue

        price = _as_float(meta.get("regularMarketPrice"))
        if price is None:
            indicators = response.get("indicators") or {}
            if isinstance(indicators, dict):
                quote_series = indicators.get("quote") or []
                if isinstance(quote_series, list) and quote_series:
                    first = quote_series[0]
                    if isinstance(first, dict):
                        closes = first.get("close")
                        if isinstance(closes, list):
                            for candidate in reversed(closes):
                                f = _as_float(candidate)
                                if f is not None:
                                    price = f
                                    break
        if price is None:
            continue

        market_dt = None
        ts = meta.get("regularMarketTime")
        if ts is not None:
            try:
                market_dt = datetime.fromtimestamp(int(ts), timezone.utc).replace(tzinfo=None)
            except Exception:
                market_dt = None
        if market_dt is None:
            timestamps = response.get("timestamp") or []
            if isinstance(timestamps, list) and timestamps:
                try:
                    market_dt = datetime.fromtimestamp(int(timestamps[-1]), timezone.utc).replace(tzinfo=None)
                except Exception:
                    market_dt = None

        name_raw = meta.get("longName") or meta.get("shortName") or meta.get("name")
        name = str(name_raw).strip() if name_raw is not None else None
        cur = meta.get("currency")
        currency = str(cur).strip() if cur is not None else None
        exchange_raw = meta.get("exchange") or meta.get("exchangeName")
        exchange = str(exchange_raw).strip() if exchange_raw is not None else None
        full_raw = meta.get("fullExchangeName") or meta.get("exchangeName")
        full_exchange_name = str(full_raw).strip() if full_raw is not None else None

        out[symbol] = YahooQuote(
            symbol=symbol,
            name=name,
            price=float(price),
            currency=currency,
            market_time_utc=market_dt,
            exchange=exchange,
            full_exchange_name=full_exchange_name,
            one_month_return=None,
            ytd_return=None,
            one_year_return=None,
        )

    return out


def _spark_chunk_payloads(
    symbols: list[str], *, range_: str, headers: dict[str, str], timeout_s: float,
    errors: list[dict[str, Any]],
) -> Iterator[dict[str, Any]]:
    """Fetch independent batches with two workers; retain healthy partial batches.

    Executor exit joins workers, including exceptional paths: no provider work
    remains running after the caller returns. The shared HTTP circuit prevents
    queued chunks from issuing new requests after a rate limit response.
    """
    chunks = _symbol_chunks(list(dict.fromkeys(symbols)), _YAHOO_SPARK_CHUNK_SIZE)

    def fetch(chunk: list[str]):
        try:
            return _get_json(
                "https://query1.finance.yahoo.com/v7/finance/spark",
                params={"symbols": ",".join(chunk), "range": range_, "interval": "1d", "indicators": "close"},
                headers=headers, timeout_s=timeout_s, retries=2,
            ), None
        except YahooFinanceError as exc:
            return None, exc

    first_error = None
    succeeded = False
    with ThreadPoolExecutor(max_workers=min(2, max(1, len(chunks))), thread_name_prefix="yahoo-spark") as executor:
        for chunk, (data, error) in zip(chunks, executor.map(fetch, chunks)):
            if error is not None:
                first_error = first_error or error
                errors.append({"symbols": chunk, "message": str(error), "error_type": type(error).__name__})
                continue
            succeeded = True
            yield data
    if not succeeded and first_error is not None:
        first_error.batch_errors = errors
        raise first_error


def _pick_close_around(
    *,
    timestamps: list[Any],
    closes: list[Any],
    target_utc: datetime,
) -> float | None:
    points: list[tuple[int, float]] = []
    for ts, close in zip(timestamps, closes):
        f = _as_float(close)
        if f is None:
            continue
        try:
            ts_i = int(ts)
        except Exception:
            continue
        points.append((ts_i, float(f)))
    if not points:
        return None

    target_ts = int(target_utc.timestamp())
    last_before: float | None = None
    first_after: float | None = None
    for ts_i, value in points:
        if ts_i <= target_ts:
            last_before = value
            continue
        first_after = value
        break
    return last_before if last_before is not None else first_after


def fetch_bulk_returns(
    symbols: list[str],
    *,
    user_agent: str,
    timeout_s: float = 15.0,
) -> dict[str, dict[str, float | None]]:
    """
    Bulk-only return snapshots using Yahoo spark (range=1y, interval=1d).
    Returns keyed by normalized provider symbol.
    """
    uniq = [str(s or "").strip().upper() for s in symbols]
    uniq = [s for s in uniq if s]
    if not uniq:
        return {}

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
    }
    errors: list[dict[str, Any]] = []
    out: dict[str, dict[str, float | None]] = _YahooBatchResult(errors)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for data in _spark_chunk_payloads(uniq, range_="1y", headers=headers, timeout_s=timeout_s, errors=errors):
        out.update(_spark_returns_from_payload(data, now=now))
    return out


def _spark_returns_from_payload(
    data: dict[str, Any], *, now: datetime,
) -> dict[str, dict[str, float | None]]:
    out: dict[str, dict[str, float | None]] = {}
    spark = data.get("spark") or {}
    if not isinstance(spark, dict):
        return out
    results = spark.get("result") or []
    if not isinstance(results, list):
        return out
    for item in results:
        if not isinstance(item, dict):
            continue
        response_list = item.get("response") or []
        response = response_list[0] if isinstance(response_list, list) and response_list else {}
        if not isinstance(response, dict):
            continue
        meta = response.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        symbol = str(item.get("symbol") or meta.get("symbol") or "").strip().upper()
        if not symbol:
            continue

        timestamps = response.get("timestamp") or []
        if not isinstance(timestamps, list):
            timestamps = []
        indicators = response.get("indicators") or {}
        quote_series: list[Any] = []
        if isinstance(indicators, dict):
            quote = indicators.get("quote") or []
            if isinstance(quote, list) and quote:
                first = quote[0]
                if isinstance(first, dict):
                    closes = first.get("close")
                    if isinstance(closes, list):
                        quote_series = closes
        if not quote_series:
            continue

        current = _as_float(meta.get("regularMarketPrice"))
        if current is None:
            for value in reversed(quote_series):
                f = _as_float(value)
                if f is not None:
                    current = float(f)
                    break
        if current is None or current == 0:
            continue

        m1_base = _pick_close_around(
            timestamps=timestamps,
            closes=quote_series,
            target_utc=now - timedelta(days=30),
        )
        y1_base = _pick_close_around(
            timestamps=timestamps,
            closes=quote_series,
            target_utc=now - timedelta(days=365),
        )
        ytd_base = _pick_close_around(
            timestamps=timestamps,
            closes=quote_series,
            target_utc=datetime(now.year, 1, 1),
        )

        def pct(base: float | None) -> float | None:
            if base is None or base == 0:
                return None
            return (float(current) / float(base)) - 1.0

        out[symbol] = {
            "m1": pct(m1_base),
            "ytd": pct(ytd_base),
            "y1": pct(y1_base),
        }
    return out


def _fetch_chart_quote(
    symbol: str,
    *,
    user_agent: str,
    timeout_s: float,
) -> YahooQuote | None:
    sym = (symbol or "").strip().lstrip("$").upper()
    if not sym:
        return None

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
    }
    params = {
        "range": "5d",
        "interval": "1d",
        "includeAdjustedClose": "true",
    }
    try:
        data = _get_json(url, params=params, headers=headers, timeout_s=timeout_s, retries=2)
    except YahooFinanceHttpError as exc:
        if exc.status_code == 404:
            return None
        raise

    chart = data.get("chart")
    if not isinstance(chart, dict):
        return None
    if chart.get("error"):
        err = chart.get("error") or {}
        if isinstance(err, dict):
            code = str(err.get("code") or "").strip().lower()
            if code in {"not found", "not_found"}:
                return None
            desc = err.get("description") or err.get("code") or "unknown"
        else:
            desc = str(err)
        raise YahooFinanceError(f"Yahoo chart error: {desc}")

    result = (chart.get("result") or [None])[0]
    if not isinstance(result, dict):
        return None
    meta = result.get("meta") or {}
    if not isinstance(meta, dict):
        return None

    price = meta.get("regularMarketPrice")
    if price is None:
        return None
    try:
        price_f = float(price)
    except Exception:
        return None

    cur = meta.get("currency")
    currency = str(cur) if cur is not None else None

    ts = meta.get("regularMarketTime")
    market_dt = None
    if ts:
        try:
            market_dt = datetime.fromtimestamp(int(ts), timezone.utc).replace(tzinfo=None)
        except Exception:
            market_dt = None

    ex = meta.get("exchangeName") or meta.get("exchange")
    exchange = str(ex) if ex is not None else None
    full_ex = meta.get("fullExchangeName") or meta.get("exchangeName")
    full_exchange_name = str(full_ex) if full_ex is not None else None

    nm = meta.get("longName") or meta.get("shortName") or meta.get("name")
    name = str(nm).strip() if nm is not None else None

    return YahooQuote(
        symbol=str(meta.get("symbol") or sym).strip().upper(),
        name=name,
        price=price_f,
        currency=currency,
        market_time_utc=market_dt,
        exchange=exchange,
        full_exchange_name=full_exchange_name,
        one_month_return=None,
        ytd_return=None,
        one_year_return=None,
    )


def _fetch_v7_quote_batches(
    uniq: list[str], *, user_agent: str, timeout_s: float,
) -> tuple[dict[str, YahooQuote], Exception | None]:
    # Prefer batch calls to avoid rate limiting, then fall back to per-symbol chart meta.
    v7_error: Exception | None = None
    out: dict[str, YahooQuote] = _YahooBatchResult([])
    try:
        # Avoid oversized URLs when symbols lists get large (masterlists can be hundreds of tickers).
        if len(uniq) <= _YAHOO_V7_QUOTE_CHUNK_SIZE:
            out = _fetch_v7_quotes(uniq, user_agent=user_agent, timeout_s=timeout_s)
        else:
            out = {}
            for chunk in _symbol_chunks(uniq, _YAHOO_V7_QUOTE_CHUNK_SIZE):
                try:
                    out.update(_fetch_v7_quotes(chunk, user_agent=user_agent, timeout_s=timeout_s))
                except YahooFinanceHttpError as exc:
                    v7_error = v7_error or exc
                    # Authorization blocks this endpoint, not an individual
                    # chunk; spark can still serve the full symbol set.
                    if exc.status_code in {401, 403, 429}:
                        break
                    continue
                except Exception as exc:
                    v7_error = v7_error or exc
                    continue
    except Exception as exc:
        v7_error = v7_error or exc
        out = out or {}

    return out, v7_error


def fetch_quotes(
    symbols: list[str],
    *,
    user_agent: str,
    timeout_s: float = 12.0,
    allow_chart_fallback: bool = True,
) -> dict[str, YahooQuote]:
    syms = [str(s or "").strip().upper() for s in symbols]
    syms = [s for s in syms if s]
    if not syms:
        return {}

    uniq = list(dict.fromkeys(syms))
    if not uniq:
        return {}

    out, v7_error = _fetch_v7_quote_batches(uniq, user_agent=user_agent, timeout_s=timeout_s)
    return _complete_quotes(
        uniq, out, v7_error=v7_error, user_agent=user_agent, timeout_s=timeout_s,
        allow_chart_fallback=allow_chart_fallback,
    )


def _complete_quotes(
    uniq: list[str], out: dict[str, YahooQuote], *, v7_error: Exception | None,
    user_agent: str, timeout_s: float, allow_chart_fallback: bool,
) -> dict[str, YahooQuote]:
    spark_error: Exception | None = None

    missing = [s for s in uniq if s.upper() not in out]
    if missing:
        try:
            spark_quotes = _fetch_spark_quotes(missing, user_agent=user_agent, timeout_s=timeout_s)
            out.update(spark_quotes)
            if getattr(spark_quotes, "errors", None):
                if not isinstance(out, _YahooBatchResult):
                    v7_quotes = out
                    out = _YahooBatchResult([])
                    out.update(v7_quotes)
                out.errors.extend(spark_quotes.errors)
        except Exception as exc:
            spark_error = exc
            if getattr(exc, "batch_errors", None):
                if not isinstance(out, _YahooBatchResult):
                    previous = out
                    out = _YahooBatchResult([])
                    out.update(previous)
                out.errors.extend(exc.batch_errors)

    missing = [s for s in uniq if s.upper() not in out]
    if not missing:
        return out

    if not allow_chart_fallback:
        if out:
            return out
        if spark_error:
            raise YahooFinanceError(str(spark_error)) from spark_error
        if v7_error:
            raise YahooFinanceError(str(v7_error)) from v7_error
        return out

    # Chart fallback: keep concurrency low to reduce 429s.
    max_workers = min(2, max(1, len(missing)))
    first_error: Exception | None = None
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {
            ex.submit(_fetch_chart_quote, sym, user_agent=user_agent, timeout_s=timeout_s): sym
            for sym in missing
        }
        for fut in as_completed(futs):
            try:
                quote = fut.result()
            except YahooFinanceHttpError as exc:
                # Treat common symbol errors as "not found" so one bad ticker doesn't kill the batch.
                if exc.status_code in {401, 403, 404}:
                    continue
                first_error = first_error or exc
                if exc.status_code == 429:
                    break
                continue
            except Exception as exc:
                first_error = first_error or exc
                continue
            if not quote:
                continue
            out[quote.symbol.upper()] = quote

    if first_error and not out:
        raise YahooFinanceError(str(first_error)) from first_error

    if not out and spark_error:
        raise YahooFinanceError(str(spark_error)) from spark_error

    if not out and v7_error:
        raise YahooFinanceError(str(v7_error)) from v7_error

    return out


def fetch_quotes_and_returns(
    symbols: list[str],
    *,
    user_agent: str,
    timeout_s: float = 15.0,
    quote_timeout_s: float | None = None,
    allow_chart_fallback: bool = False,
) -> tuple[dict[str, YahooQuote], dict[str, dict[str, float | None]]]:
    """Fetch cold quotes and history together, reusing one-year Spark metadata.

    V7 quotes keep their richer fields. Missing current quotes fall back to the
    existing five-day Spark/chart path, independently of history availability.
    Each result mapping retains its own ``errors`` batch provenance. The history
    mapping's ``complete_failure`` is true only when every request failed;
    successful empty series remain distinct from provider unavailability.
    ``quote_timeout_s`` defaults to ``timeout_s`` and applies to V7/five-day/chart
    requests. Spark concurrency remains bounded to two workers.
    """
    uniq = list(dict.fromkeys(str(s or "").strip().upper() for s in symbols))
    uniq = [s for s in uniq if s]
    if not uniq:
        return _YahooBatchResult([]), _YahooBatchResult([])

    quote_timeout = timeout_s if quote_timeout_s is None else quote_timeout_s
    quotes, v7_error = _fetch_v7_quote_batches(uniq, user_agent=user_agent, timeout_s=quote_timeout)
    quote_out: dict[str, YahooQuote] = _YahooBatchResult([])
    quote_out.update(quotes)
    missing_current = set(uniq).difference(quote_out)
    history_errors: list[dict[str, Any]] = []
    returns: dict[str, dict[str, float | None]] = _YahooBatchResult(history_errors)
    headers = {"User-Agent": user_agent, "Accept": "application/json, text/plain, */*"}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        for data in _spark_chunk_payloads(uniq, range_="1y", headers=headers, timeout_s=timeout_s, errors=history_errors):
            returns.update(_spark_returns_from_payload(data, now=now))
            if missing_current:
                spark_quotes = _spark_quotes_from_payload(data)
                for symbol, quote in spark_quotes.items():
                    if symbol not in quote_out:
                        quote_out[symbol] = quote
                missing_current.difference_update(spark_quotes)
    except YahooFinanceError:
        # The iterator has already recorded every failed chunk. Current-price
        # fallback still runs after total history failure.
        returns.complete_failure = True

    missing = [symbol for symbol in uniq if symbol not in quote_out]
    if missing:
        try:
            quote_out = _complete_quotes(
                uniq, quote_out, v7_error=v7_error, user_agent=user_agent,
                timeout_s=quote_timeout, allow_chart_fallback=allow_chart_fallback,
            )
        except YahooFinanceError as exc:
            if not quote_out.errors:
                quote_out.errors.append({"symbols": missing, "message": str(exc), "error_type": type(exc).__name__})
    return quote_out, returns


def fetch_adjclose_1y(
    symbol: str,
    *,
    user_agent: str,
    timeout_s: float = 15.0,
) -> list[AdjCloseBar]:
    sym = (symbol or "").strip().upper()
    if not sym:
        raise ValueError("Missing ticker.")

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
    }
    params = {
        "range": "1y",
        "interval": "1d",
        "includeAdjustedClose": "true",
    }
    data = _get_json(url, params=params, headers=headers, timeout_s=timeout_s)
    chart = data.get("chart")
    if not isinstance(chart, dict):
        raise YahooFinanceError("Yahoo chart payload malformed")
    if chart.get("error"):
        err = chart.get("error") or {}
        if isinstance(err, dict):
            desc = err.get("description") or err.get("code") or "unknown"
        else:
            desc = str(err)
        raise YahooFinanceError(f"Yahoo chart error: {desc}")
    result = (chart.get("result") or [None])[0]
    if not isinstance(result, dict):
        raise YahooFinanceError("Yahoo chart result missing")

    timestamps = result.get("timestamp") or []
    if not isinstance(timestamps, list):
        raise YahooFinanceError("Yahoo chart timestamps malformed")

    indicators = result.get("indicators") or {}
    adjclose_series: list[float | None] | None = None
    if isinstance(indicators, dict):
        adjclose = indicators.get("adjclose") or []
        if isinstance(adjclose, list) and adjclose:
            first = adjclose[0]
            if isinstance(first, dict):
                series = first.get("adjclose")
                if isinstance(series, list):
                    adjclose_series = series

    if adjclose_series is None:
        # Fallback to close if adjclose is missing.
        quote = indicators.get("quote") if isinstance(indicators, dict) else None
        if isinstance(quote, list) and quote:
            first = quote[0]
            if isinstance(first, dict):
                series = first.get("close")
                if isinstance(series, list):
                    adjclose_series = series

    if adjclose_series is None:
        raise YahooFinanceError("Yahoo chart missing price series")

    bars: list[AdjCloseBar] = []
    for ts, val in zip(timestamps, adjclose_series):
        if val is None:
            continue
        try:
            dt = datetime.fromtimestamp(int(ts), timezone.utc).replace(tzinfo=None)
            price = float(val)
        except Exception:
            continue
        bars.append(AdjCloseBar(ts_utc=dt, adj_close=price))

    return bars


def pick_adjclose(
    bars: list[AdjCloseBar],
    *,
    target_utc: datetime,
    fallback_after: bool = True,
) -> float | None:
    if not bars:
        return None
    bars_sorted = bars if all(bars[i].ts_utc <= bars[i + 1].ts_utc for i in range(len(bars) - 1)) else sorted(bars, key=lambda b: b.ts_utc)
    before = [b for b in bars_sorted if b.ts_utc <= target_utc]
    if before:
        return before[-1].adj_close
    if fallback_after:
        after = [b for b in bars_sorted if b.ts_utc >= target_utc]
        if after:
            return after[0].adj_close
    return None


def search_symbols(
    query: str,
    *,
    user_agent: str,
    timeout_s: float = 12.0,
    limit: int = 8,
) -> list[YahooSymbolSearchResult]:
    q = (query or "").strip()
    if not q:
        return []
    limit = max(1, min(int(limit or 8), 25))

    url = "https://query1.finance.yahoo.com/v1/finance/search"
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
    }
    params = {
        "q": q,
        "quotesCount": str(limit),
        "newsCount": "0",
        "enableFuzzyQuery": "true",
        "enableEnhancedTrivialQuery": "true",
    }
    data = _get_json(url, params=params, headers=headers, timeout_s=timeout_s)
    quotes = data.get("quotes") or []
    if not isinstance(quotes, list):
        return []

    out: list[YahooSymbolSearchResult] = []
    for item in quotes:
        if not isinstance(item, dict):
            continue
        sym = item.get("symbol")
        if not sym:
            continue
        sym_u = str(sym).strip().upper()
        if not sym_u:
            continue
        name = item.get("shortname") or item.get("longname") or item.get("name")
        name_s = str(name).strip() if name else None
        exch = item.get("exchDisp") or item.get("fullExchangeName") or item.get("exchange") or item.get("exchangeDisp")
        exch_s = str(exch).strip() if exch else None
        out.append(YahooSymbolSearchResult(symbol=sym_u, name=name_s, exchange=exch_s))
        if len(out) >= limit:
            break
    return out
