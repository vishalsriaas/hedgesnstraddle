import httpx
import logging
import time
import re
from typing import Optional, Dict, Any, List

logger = logging.getLogger("hedgesnstraddle.binance_client")

BINANCE_FUTURES_URL = "https://fapi.binance.com"
BINANCE_SPOT_URL = "https://api.binance.com"
BINANCE_OPTIONS_URL = "https://eapi.binance.com"

_price_cache: Dict[str, tuple] = {}

# Track when each key is rate-limited so we back off entirely
_rate_limit_until: Dict[str, float] = {}


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
                price = float(resp.json().get("markPrice", 64000.0))
                _price_cache["BTCUSDT"] = (price, now)
                return price
    except Exception as e:
        logger.warning("Error fetching Binance futures mark price: %s", str(e))

    return float(_price_cache.get("BTCUSDT", (64000.0, 0.0))[0])


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
                price = float(resp.json().get("price", 64000.0))
                _price_cache["BTC_SPOT"] = (price, now)
                return price
    except Exception as e:
        logger.warning("Error fetching Binance spot price: %s", str(e))

    return float(_price_cache.get("BTC_SPOT", (64000.0, 0.0))[0])


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
        logger.warning("Error fetching Binance options tickers: %s", str(e))

    return _price_cache.get(cache_key, ([], 0.0))[0]


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
        return _price_cache.get(cache_key, ([], 0.0))[0]

    # 30-second cache to avoid hammering the endpoint
    if cache_key in _price_cache and (now - _price_cache[cache_key][1]) < 30.0:
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
        logger.warning("Error fetching Binance options mark prices: %s", str(e))

    # Return last good cached data
    return _price_cache.get(cache_key, ([], 0.0))[0]
