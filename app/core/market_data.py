"""Strict quote lookup for paper fills; missing data is not a fill price."""
import math
from datetime import datetime, timezone


def option_mark(quotes, symbol):
    for quote in quotes:
        if quote.get("symbol") == symbol:
            try:
                value = float(quote["markPrice"])
            except (KeyError, TypeError, ValueError):
                break
            if math.isfinite(value) and value >= 0:
                return value
            break
    raise ValueError(f"DATA_GAP: No valid mark for {symbol}")


def unexpired(symbol, now=None):
    try:
        expiry = datetime.strptime(symbol.split("-")[1], "%y%m%d").replace(
            hour=8, tzinfo=timezone.utc)
        return expiry > (now or datetime.now(timezone.utc))
    except (ValueError, IndexError):
        return False
