import httpx
import logging
import time
import re
import math
from typing import Optional, Dict, Any, List

logger = logging.getLogger("hedgesnstraddle.binance_client")

BINANCE_FUTURES_URL = "https://fapi.binance.com"
BINANCE_SPOT_URL = "https://api.binance.com"
BINANCE_OPTIONS_URL = "https://eapi.binance.com"

_price_cache: Dict[str, tuple] = {}

# Track when each key is rate-limited so we back off entirely
_rate_limit_until: Dict[str, float] = {}


MAX_STALE_SECONDS = 30.0


async def get_futures_aggregate_trades(symbol, *, from_id=None, start_ms=None,
                                      end_ms=None, limit=1000, latest=False):
    """Exact-contract executed trade history. No mark/cache fallback on failure."""
    if symbol != "BTCUSDT":
        raise ValueError(f"DATA_GAP: Unsupported futures history symbol {symbol!r}")
    if time.time() < _rate_limit_until.get("FUTURES_TRADES", 0):
        raise ValueError("DATA_GAP: Binance futures trade history rate-limited")
    params = {"symbol": symbol, "limit": limit}
    if from_id is not None:
        params["fromId"] = from_id
    elif not latest:
        if start_ms is None or end_ms is None or not 0 <= end_ms - start_ms < 3600000:
            raise ValueError("Invalid futures trade recovery window")
        params.update(startTime=start_ms, endTime=end_ms)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{BINANCE_FUTURES_URL}/fapi/v1/aggTrades", params=params)
        if response.status_code in (418, 429):
            retry = float(response.headers.get("Retry-After", "90"))
            _rate_limit_until["FUTURES_TRADES"] = time.time() + max(90, retry)
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError("Invalid aggregate trade response")
        previous = None
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid aggregate trade")
            if row.get("symbol", symbol) != symbol:
                raise ValueError("Mismatched futures history symbol")
            if any(type(row.get(k)) is not int or row[k] < 0 for k in ("a", "T")):
                raise ValueError("Invalid aggregate trade ID/time")
            if any(not math.isfinite(float(row[k])) or float(row[k]) <= 0 for k in ("p", "q")):
                raise ValueError("Invalid aggregate trade price/quantity")
            if from_id is not None and row["a"] < from_id:
                raise ValueError("Trade history predates requested ID")
            if not latest and from_id is None and not start_ms <= row["T"] <= end_ms:
                raise ValueError("Trade history outside requested window")
            if previous and (row["a"] != previous["a"] + 1 or row["T"] < previous["T"]):
                raise ValueError("Non-contiguous or unordered trade history")
            previous = row
        return rows
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"DATA_GAP: Binance {symbol} trade history unavailable: {exc}") from exc


async def get_futures_trade_anchor():
    """Capture an ID baseline before activation and align with exchange time."""
    rows = await get_futures_aggregate_trades("BTCUSDT", limit=1, latest=True)
    if not rows:
        raise ValueError("DATA_GAP: Cannot activate futures limit without a trade ID baseline")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{BINANCE_FUTURES_URL}/fapi/v1/time")
        response.raise_for_status()
        server_ms = response.json()["serverTime"]
        local_ms = int(time.time() * 1000)
        if type(server_ms) is not int or server_ms < rows[-1]["T"]:
            raise ValueError("Invalid Binance clock baseline")
        return dict(last_id=rows[-1]["a"], last_trade_ms=rows[-1]["T"],
                    active_ms=server_ms, clock_offset_ms=server_ms-local_ms)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"DATA_GAP: Binance clock anchor unavailable: {exc}") from exc


def cached_quote(key):
    cached = _price_cache.get(key)
    if cached and time.time() - cached[1] <= MAX_STALE_SECONDS:
        return cached[0]
    raise ValueError(f"DATA_GAP: {key} quote is missing or older than {MAX_STALE_SECONDS:g}s")


async def get_btc_futures_mark_price() -> float:
    """Get BTC perpetual futures mark price from fapi."""
    now = time.time()
    if "BTCUSDT" in _price_cache and (now - _price_cache["BTCUSDT"][1]) < 2.0:
        return float(_price_cache["BTCUSDT"][0])

    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            resp = await client.get(
                f"{BINANCE_FUTURES_URL}/fapi/v1/premiumIndex",
                params={"symbol": "BTCUSDT"}
            )
            if resp.status_code == 200:
                price_val = resp.json().get("markPrice")
                if price_val is not None:
                    price = float(price_val)
                    if not math.isfinite(price) or price <= 0:
                        raise ValueError("Invalid market price")
                    _price_cache["BTCUSDT"] = (price, now)
                    return price
    except Exception as e:
        logger.error("DATA_GAP: Error fetching Binance futures mark price: %s", str(e))

    return float(cached_quote("BTCUSDT"))


async def get_btc_spot_price() -> float:
    """Get BTC spot price from api.binance.com."""
    now = time.time()
    if "BTC_SPOT" in _price_cache and (now - _price_cache["BTC_SPOT"][1]) < 2.0:
        return float(_price_cache["BTC_SPOT"][0])

    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            resp = await client.get(
                f"{BINANCE_SPOT_URL}/api/v3/ticker/price",
                params={"symbol": "BTCUSDT"}
            )
            if resp.status_code == 200:
                price_val = resp.json().get("price")
                if price_val is not None:
                    price = float(price_val)
                    if not math.isfinite(price) or price <= 0:
                        raise ValueError("Invalid market price")
                    _price_cache["BTC_SPOT"] = (price, now)
                    return price
    except Exception as e:
        logger.error("DATA_GAP: Error fetching Binance spot price: %s", str(e))

    return float(cached_quote("BTC_SPOT"))


async def get_btc_options_tickers() -> List[Dict[str, Any]]:
    """
    Fetch 24hr price-change statistics from /eapi/v1/ticker.
    NOTE: This endpoint returns lastPrice/volume stats only, NOT mark prices.
    For mark prices, use get_btc_options_mark_prices() instead.
    Kept for backward compatibility; uses 30s cache.
    """
    now = time.time()
    cache_key = "BTC_OPTIONS_TICKERS"
    if cache_key in _price_cache and (now - _price_cache[cache_key][1]) < 30.0:
        return _price_cache[cache_key][0]

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{BINANCE_OPTIONS_URL}/eapi/v1/ticker")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    btc_opts = [
                        item for item in data
                        if isinstance(item, dict) and item.get("symbol", "").startswith("BTC-")
                    ]
                    if btc_opts:
                        _price_cache[cache_key] = (btc_opts, now)
                        return btc_opts
            else:
                logger.warning("Binance EAPI ticker returned HTTP %d", resp.status_code)
    except Exception as e:
        logger.error("DATA_GAP: Error fetching Binance options tickers: %s", str(e))

    res = cached_quote(cache_key)
    if not res:
        logger.error("DATA_GAP: No cached or live Binance options tickers available.")
        raise ValueError("DATA_GAP: Options tickers feed failed")
    return res


async def get_btc_options_mark_prices() -> List[Dict[str, Any]]:
    """
    Fetch BTC option mark prices from the dedicated /eapi/v1/mark endpoint.
    This is the authoritative source for options mark prices on Binance
    (Black-Scholes model calculation — NOT order-book ask prices).

    Response fields per entry:
      symbol, markPrice, bidIV, askIV, markIV, delta, theta, gamma, vega,
      highPriceLimit, lowPriceLimit, riskFreeInterest

    Caching strategy:
      - Fresh fetch: cached for 30 seconds (mark prices are updated periodically)
      - Rate-limited: honour the full Binance ban duration from error message
    """
    now = time.time()
    cache_key = "BTC_OPTIONS_MARK"

    # Honour any active rate-limit ban
    ban_until = _rate_limit_until.get(cache_key, 0.0)
    if now < ban_until:
        logger.debug(
            "Options /mark endpoint rate-limited; using cached data (ban until %s)",
            time.strftime("%H:%M:%S", time.localtime(ban_until))
        )
        return cached_quote(cache_key)

    # 5-second cache (safe: /eapi/v1/mark costs 5 weight; budget is 6,000/min = 20 calls/sec headroom)
    if cache_key in _price_cache and (now - _price_cache[cache_key][1]) < 5.0:
        return _price_cache[cache_key][0]

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{BINANCE_OPTIONS_URL}/eapi/v1/mark")

            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    btc_marks = [
                        item for item in data
                        if isinstance(item, dict) and item.get("symbol", "").startswith("BTC-")
                    ]
                    if btc_marks:
                        _price_cache[cache_key] = (btc_marks, now)
                        # Clear any previous rate-limit state on success
                        _rate_limit_until.pop(cache_key, None)
                        return btc_marks
                    else:
                        logger.warning("No BTC-prefixed entries in /eapi/v1/mark response")

                elif isinstance(data, dict) and data.get("code") == -1003:
                    # Rate limited — parse ban-until timestamp from message
                    msg = data.get("msg", "")
                    match = re.search(r"banned until (\d+)", msg)
                    if match:
                        ban_ts_ms = int(match.group(1))
                        _rate_limit_until[cache_key] = ban_ts_ms / 1000.0
                        logger.warning(
                            "Binance EAPI /mark rate-limit active. Backing off until %s",
                            time.strftime("%H:%M:%S", time.localtime(ban_ts_ms / 1000.0))
                        )
                    else:
                        _rate_limit_until[cache_key] = now + 60.0
                        logger.warning("Binance EAPI /mark rate-limit (no timestamp). Backing off 60s.")

            elif resp.status_code in (418, 429):
                _rate_limit_until[cache_key] = now + 90.0
                logger.warning("Binance EAPI /mark returned HTTP %d. Backing off 90s.", resp.status_code)

            else:
                logger.warning(
                    "Binance EAPI /mark non-200: HTTP %d body=%s",
                    resp.status_code, resp.text[:200]
                )

    except Exception as e:
        logger.error("DATA_GAP: Error fetching Binance options mark prices: %s", str(e))

    res = cached_quote(cache_key)
    if not res:
        logger.error("DATA_GAP: No cached or live Binance options mark prices available.")
        raise ValueError("DATA_GAP: Options mark prices feed failed")
    return res
