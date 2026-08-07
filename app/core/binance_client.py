import httpx
import logging
import time
from typing import Optional, Dict, Any, List

logger = logging.getLogger("hedgesnstraddle.binance_client")

BINANCE_FUTURES_URL = "https://fapi.binance.com"
BINANCE_SPOT_URL = "https://api.binance.com"
BINANCE_OPTIONS_URL = "https://eapi.binance.com"

_price_cache: Dict[str, tuple[Any, float]] = {}

async def get_btc_futures_mark_price() -> float:
    now = time.time()
    if "BTCUSDT" in _price_cache and (now - _price_cache["BTCUSDT"][1]) < 1.5:
        return float(_price_cache["BTCUSDT"][0])

    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            resp = await client.get(f"{BINANCE_FUTURES_URL}/fapi/v1/premiumIndex", params={"symbol": "BTCUSDT"})
            if resp.status_code == 200:
                price = float(resp.json().get("markPrice", 64000.0))
                _price_cache["BTCUSDT"] = (price, now)
                return price
    except Exception as e:
        logger.warning("Error fetching Binance mark price: %s", str(e))

    return float(_price_cache.get("BTCUSDT", (64000.0, 0.0))[0])

async def get_btc_spot_price() -> float:
    now = time.time()
    if "BTC_SPOT" in _price_cache and (now - _price_cache["BTC_SPOT"][1]) < 1.5:
        return float(_price_cache["BTC_SPOT"][0])

    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            resp = await client.get(f"{BINANCE_SPOT_URL}/api/v3/ticker/price", params={"symbol": "BTCUSDT"})
            if resp.status_code == 200:
                price = float(resp.json().get("price", 64000.0))
                _price_cache["BTC_SPOT"] = (price, now)
                return price
    except Exception as e:
        logger.warning("Error fetching Binance spot price: %s", str(e))

    return float(_price_cache.get("BTC_SPOT", (64000.0, 0.0))[0])

async def get_btc_options_tickers() -> List[Dict[str, Any]]:
    """Fetch real-time BTC Option tickers from Binance Options API (eapi.binance.com) with 5s caching to prevent HTTP -1003 rate limits."""
    now = time.time()
    if "BTC_OPTIONS_TICKERS" in _price_cache and (now - _price_cache["BTC_OPTIONS_TICKERS"][1]) < 5.0:
        return _price_cache["BTC_OPTIONS_TICKERS"][0]

    try:
        async with httpx.AsyncClient(timeout=3.5) as client:
            resp = await client.get(f"{BINANCE_OPTIONS_URL}/eapi/v1/ticker")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    btc_opts = [item for item in data if item.get("symbol", "").startswith("BTC")]
                    if btc_opts:
                        _price_cache["BTC_OPTIONS_TICKERS"] = (btc_opts, now)
                        return btc_opts
            else:
                logger.warning("Binance EAPI ticker returned HTTP %d: %s", resp.status_code, resp.text)
    except Exception as e:
        logger.warning("Error fetching Binance options tickers: %s", str(e))

    return _price_cache.get("BTC_OPTIONS_TICKERS", ([], 0.0))[0]
