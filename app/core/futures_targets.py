"""Durable paper futures limits; replay Binance trades, never mark snapshots.

Uses existing session events for per-order replay cursors and execution evidence.
The caller commits cursor, fill, position and wallet changes in one transaction.
"""
import json
import math
from datetime import datetime, timezone, timedelta

from app.core.binance_client import get_futures_aggregate_trades

IST = timezone(timedelta(hours=5, minutes=30))
EVENT = "FUTURES_TP_TRACKING"
POLL_MS = 5000
WINDOW_MS = 30 * 60 * 1000
# Leave a margin inside Binance's documented 48-hour retention.
HISTORY_MS = 47 * 60 * 60 * 1000
PAGE_LIMIT = 1000
MAX_PAGES = 5


def millis(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)
    return int(value.timestamp() * 1000)


def contract_symbol(symbol):
    # Explicit alias for the application's existing BTC perpetual position label.
    if symbol in ("BTC-USDT-FUTURES", "BTCUSDT"):
        return "BTCUSDT"
    raise ValueError(f"DATA_GAP: Unsupported futures contract {symbol!r}")


def create_target(db, order, event_model, now, anchor=None, event_type=EVENT, other_order=None):
    if order.side not in ("BUY", "SELL") or any(
        not math.isfinite(v) or v <= 0 for v in (order.price, order.qty)
    ):
        raise ValueError("Invalid futures TP limit price, quantity or side")
    symbol = contract_symbol(order.symbol)
    db.add(order)
    db.flush()
    state = dict(order_id=order.id, symbol=symbol, active_ms=millis(now),
                 checked_ms=millis(now), last_id=None, last_trade_ms=None, last_poll_ms=0,
                 limit_price=order.price, qty=order.qty, side=order.side)
    if anchor:
        state.update(anchor)
        state["checked_ms"] = state["active_ms"]
        order.created_at = exchange_datetime(state["active_ms"])
    if other_order is not None:
        if (contract_symbol(other_order.symbol) != symbol or other_order.qty != order.qty
                or other_order.side not in ("BUY", "SELL") or other_order.side == order.side or not math.isfinite(other_order.price)
                or other_order.price <= 0):
            raise ValueError("Invalid OCO counterpart")
        sell = order if order.side == "SELL" else other_order
        buy = other_order if order.side == "SELL" else order
        if sell.price <= buy.price:
            raise ValueError("Overlapping OCO limits")
        state["other"] = dict(order_id=other_order.id, limit_price=other_order.price,
                              qty=other_order.qty, side=other_order.side)
        other_order.created_at = order.created_at
    event = event_model(session_id=order.session_id, event_type=event_type,
                        message="Pending paper futures limit; awaiting trade history",
                        payload_json=json.dumps(state), created_at=now.replace(tzinfo=None))
    db.add(event)
    db.flush()
    return event


def target_for_session(db, order_model, event_model, session_id, event_type=EVENT):
    event = db.query(event_model).filter_by(session_id=session_id, event_type=event_type).first()
    if event is None:
        return None, None
    state = json.loads(event.payload_json)
    order = db.get(order_model, state["order_id"])
    if order is None or order.session_id != session_id:
        raise ValueError("DATA_GAP: Missing or mismatched futures TP order")
    return order, event


def replay_complete(event, until):
    state = json.loads(event.payload_json)
    deadline = millis(until)
    if state["active_ms"] > deadline or (state["active_ms"] == deadline and not state.get("activation_trade_id")):
        return True  # The limit had no eligible lifetime before this boundary.
    # A contiguous later event is evidence that the sequence passed the deadline.
    # Empty/short pages and elapsed wall time are not proof of final coverage.
    return bool(state.get("hit")) or state.get("boundary_ms", 0) > deadline


def exchange_datetime(timestamp_ms):
    return datetime.fromtimestamp(timestamp_ms / 1000, IST).replace(tzinfo=None)


def record_execution(order, hit):
    if hit.get("order_id", order.id) != order.id or hit["fill_price"] != order.price:
        raise ValueError("DATA_GAP: Execution evidence does not match the limit order")
    order.status = "FILLED"
    order.filled_at = exchange_datetime(hit["trade_time_ms"])
    order.processed_at = exchange_datetime(hit["processed_time_ms"])
    order.aggregate_trade_id = hit["aggregate_trade_id"]


async def replay_target(order, event, now, until=None, other_order=None):
    """Return first crossing evidence, or None. Never infer a fill from a data gap.

    Completed pages persist even when catch-up needs additional engine cycles.
    Overlapping time windows plus aggregate IDs avoid timestamp-boundary losses.
    """
    if order.status != "PENDING":
        return None
    state = json.loads(event.payload_json)
    if (contract_symbol(order.symbol) != state["symbol"] or order.id != state["order_id"]
            or order.price != state["limit_price"] or order.qty != state["qty"]
            or order.side != state["side"]):
        raise ValueError("DATA_GAP: Futures TP identity or locked terms changed")
    if state.get("hit"):
        return state["hit"]
    orders = [order]
    if other_order is not None:
        expected = state.get("other", {})
        if (other_order.status != "PENDING" or contract_symbol(other_order.symbol) != state["symbol"]
                or expected != dict(order_id=other_order.id, limit_price=other_order.price,
                                    qty=other_order.qty, side=other_order.side)):
            raise ValueError("DATA_GAP: OCO counterpart changed")
        orders.append(other_order)
    now_ms = millis(now) + state.get("clock_offset_ms", 0)
    end_ms = min(now_ms, millis(until)) if until else now_ms
    if end_ms <= state["active_ms"] or (until and replay_complete(event, until)):
        return None
    if now_ms - state["checked_ms"] >= HISTORY_MS:
        raise ValueError("DATA_GAP: Futures TP history exceeds recovery retention; manual review required")
    if now_ms - state["last_poll_ms"] < POLL_MS:
        return None
    if state["last_id"] is None:
        state["checked_ms"] = state["active_ms"]  # Repair unanchored legacy coverage too.
    state["last_poll_ms"] = now_ms
    pages = 0
    while (pages == 0 or state["checked_ms"] < end_ms) and pages < MAX_PAGES:
        window_end = min(end_ms, state["checked_ms"] + WINDOW_MS)
        from_id = state["last_id"] + 1 if state["last_id"] is not None else None
        while pages < MAX_PAGES:
            rows = await get_futures_aggregate_trades(
                state["symbol"], from_id=from_id,
                start_ms=state["checked_ms"] if from_id is None else None,
                end_ms=window_end if from_id is None else None, limit=PAGE_LIMIT)
            pages += 1
            reached_end = False
            for row in rows:
                trade_id, trade_ms = row["a"], row["T"]
                if state["last_id"] is not None and trade_id <= state["last_id"]:
                    continue
                if state["last_id"] is not None and trade_id != state["last_id"] + 1:
                    raise ValueError("DATA_GAP: Non-contiguous Binance aggregate trade history")
                if state.get("last_trade_ms") is not None and trade_ms < state["last_trade_ms"]:
                    raise ValueError("DATA_GAP: Out-of-order Binance trade timestamps")
                if trade_ms > window_end:
                    state["boundary_ms"] = trade_ms
                    reached_end = True
                    break
                state["last_id"] = trade_id
                state["last_trade_ms"] = trade_ms
                # Advance coverage only after a complete page/window, not merely
                # after seeing a trade at its end (more IDs may share that time).
                price = float(row["p"])
                # No pre-activation or ambiguous same-millisecond fills.
                qualifying = [o for o in orders if (price >= o.price if o.side == "SELL" else price <= o.price)]
                if len(qualifying) > 1:
                    raise ValueError("DATA_GAP: Both OCO limits qualify on one trade")
                after_activation = (trade_ms > state["active_ms"] or
                    (state.get("activation_trade_id") is not None and trade_id > state["activation_trade_id"]
                     and trade_ms == state["active_ms"]))
                if after_activation and qualifying:
                    filled = qualifying[0]
                    hit = dict(aggregate_trade_id=trade_id, trade_time_ms=trade_ms,
                               observed_price=price, fill_price=filled.price, order_id=filled.id,
                               processed_time_ms=now_ms, symbol=state["symbol"])
                    state["hit"] = hit
                    event.payload_json = json.dumps(state)
                    event.message = "Futures crossing recovered; paper fill uses locked limit price"
                    return hit
            if reached_end or len(rows) < PAGE_LIMIT:
                if state["last_id"] is not None or reached_end:
                    state["checked_ms"] = window_end
                break
            from_id = rows[-1]["a"] + 1
        # A full last page requires another poll; do not skip unprocessed trades.
        if pages >= MAX_PAGES and state["checked_ms"] < window_end:
            break
        if not rows and state["last_id"] is None:
            break  # Retain the unanchored interval for late first events.
    event.payload_json = json.dumps(state)
    event.message = ("Futures TP trade replay caught up" if state["checked_ms"] >= end_ms
                     else "Futures TP trade replay catching up; target remains pending")
    return None
