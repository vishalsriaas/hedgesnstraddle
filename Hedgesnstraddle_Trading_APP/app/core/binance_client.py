import httpx
import logging
import time
from typing import Optional, Dict, Any

logger = logging.getLogger("hedgesnstraddle.binance_client")

BINANCE_FUTURES_URL = "https://fapi.binance.com"
BINANCE_SPOT_URL = "https://api.binance.com"

_price_cache: Dict[str, tuple[float, float]] = {}

async def get_btc_futures_mark_price() -> float:
    now = time.time()
    if "BTCUSDT" in _price_cache and (now - _price_cache["BTCUSDT"][1]) < 2.0:
        return _price_cache["BTCUSDT"][0]

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{BINANCE_FUTURES_URL}/fapi/v1/premiumIndex", params={"symbol": "BTCUSDT"})
            if resp.status_code == 200:
                price = float(resp.json().get("markPrice", 64000.0))
                _price_cache["BTCUSDT"] = (price, now)
                return price
    except Exception as e:
        logger.warning("Error fetching Binance mark price: %s", str(e))

    return _price_cache.get("BTCUSDT", (64000.0, 0.0))[0]

async def get_btc_spot_price() -> float:
    now = time.time()
    if "BTC_SPOT" in _price_cache and (now - _price_cache["BTC_SPOT"][1]) < 2.0:
        return _price_cache["BTC_SPOT"][0]

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{BINANCE_SPOT_URL}/api/v3/ticker/price", params={"symbol": "BTCUSDT"})
            if resp.status_code == 200:
                price = float(resp.json().get("price", 64000.0))
                _price_cache["BTC_SPOT"] = (price, now)
                return price
    except Exception as e:
        logger.warning("Error fetching Binance spot price: %s", str(e))

    return _price_cache.get("BTC_SPOT", (64000.0, 0.0))[0]
