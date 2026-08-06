"""
Dynamic configuration — all trading parameters live here.
All agents import `cfg` directly. Updates via /api/config propagate instantly.
"""

import os
import logging
from dataclasses import dataclass, field, asdict
from typing import Dict, Any

log = logging.getLogger("config")


@dataclass
class Config:
    # ── Exchange credentials ───────────────────────────────────────────────
    binance_api_key:    str = field(default_factory=lambda: os.environ.get("BINANCE_API_KEY", ""))
    binance_api_secret: str = field(default_factory=lambda: os.environ.get("BINANCE_API_SECRET", ""))

    # ── BTC Technicals chart (display only, no trading dependency) ─────────
    chart_candle_count:    int   = 500
    chart_tf_minutes:      int   = 5

    session_expiry_h: int = 13
    session_expiry_m: int = 30

    # ── Bullish Trader — full self-contained settings ──────────────────────
    # Trading window (Mon–Fri only; weekends auto-skipped)
    bull_skip_weekends:  bool = True
    bull_blackout_dates: str  = ""
    bull_trade_start_h:          int   = 4
    bull_trade_start_m:          int   = 0
    bull_trade_end_h:            int   = 6
    bull_trade_end_m:            int   = 0
    bull_force_close_h:          int   = 12
    bull_force_close_m:          int   = 0

    # Self-contained S/R analysis (runs at window open each day)
    bull_analysis_candles:       int   = 30       # N candles to analyse
    bull_analysis_tf_minutes:    int   = 15       # timeframe per candle (minutes)
    bull_dominance_threshold:    float = 0.80     # min fraction of touches that must be LOWs
    bull_touch_tolerance:        float = 30.0     # ±pts — touches within this range count
    bull_min_touches:            int   = 3        # minimum touches to qualify as valid level

    # Entry conditions (all must pass simultaneously)
    bull_max_premium:            float = 220.0    # max nearest ITM PUT mark (USDT)
    bull_max_time_value:         float = 219.0    # max (mark − intrinsic)
    bull_price_diff_percent:     float = 5.0      # max |ask−mark|/mark × 100 %
    bull_max_distance_from_line: float = 100.0    # ±pts from locked support line

    # Position sizing
    bull_contract_qty:           float = 10.0     # BTC per trade

    # Profit management
    bull_partial_profit_ratio:   float = 1.10     # legacy — kept for DB compat, not used in new logic
    bull_partial_tp_multiplier:  float = 1.10     # full futures qty books when futures profit = N * hedge premium paid
    bull_session_pnl_target:     float = 600.0    # legacy display/config value; squareoff closes hedge/futures
    bull_rebuy_mode:             str   = "tv_based"  # legacy — kept for DB compat

    # Per-role limits — 0 means "use the main value above as fallback"
    # Applied dynamically: if peer already has position → second, else → first
    bull_first_trader_max_premium:       float = 0.0   # 0 = use bull_max_premium
    bull_second_trader_max_premium:      float = 0.0
    bull_first_trader_max_time_value:    float = 0.0   # 0 = use bull_max_time_value
    bull_second_trader_max_time_value:   float = 0.0
    bull_first_trader_contract_qty:      float = 0.0   # 0 = use bull_contract_qty
    bull_second_trader_contract_qty:     float = 0.0
    # Legacy rebuy TV multipliers kept for DB compatibility; current rebuy uses premium distance.
    bull_first_trader_rebuy_tv_mult:     float = 0.5
    bull_second_trader_rebuy_tv_mult:    float = 0.0

    # Legacy alias kept so old DB configs restore without KeyError
    bull_n_hours:                float = 2.0
    bull_n_points:               float = 150.0
    bull_full_close_target:      float = 800.0    # fallback if session_pnl_target not set

    # ── Bearish Trader — full self-contained settings ──────────────────────
    bear_skip_weekends:  bool = True
    bear_blackout_dates: str  = ""
    bear_trade_start_h:          int   = 4
    bear_trade_start_m:          int   = 0
    bear_trade_end_h:            int   = 6
    bear_trade_end_m:            int   = 0
    bear_force_close_h:          int   = 12
    bear_force_close_m:          int   = 0

    bear_analysis_candles:       int   = 30
    bear_analysis_tf_minutes:    int   = 15
    bear_dominance_threshold:    float = 0.80
    bear_touch_tolerance:        float = 30.0
    bear_min_touches:            int   = 3

    bear_max_premium:            float = 220.0
    bear_max_time_value:         float = 219.0
    bear_price_diff_percent:     float = 5.0
    bear_max_distance_from_line: float = 100.0

    bear_contract_qty:           float = 10.0

    bear_partial_profit_ratio:   float = 1.10     # legacy — kept for DB compat
    bear_partial_tp_multiplier:  float = 1.10     # full futures qty books when futures profit = N * hedge premium paid
    bear_session_pnl_target:     float = 600.0    # legacy display/config value; squareoff closes hedge/futures
    bear_rebuy_mode:             str   = "tv_based"  # legacy — kept for DB compat

    bear_first_trader_max_premium:       float = 0.0
    bear_second_trader_max_premium:      float = 0.0
    bear_first_trader_max_time_value:    float = 0.0
    bear_second_trader_max_time_value:   float = 0.0
    bear_first_trader_contract_qty:      float = 0.0
    bear_second_trader_contract_qty:     float = 0.0
    # Legacy rebuy TV multipliers kept for DB compatibility; current rebuy uses premium distance.
    bear_first_trader_rebuy_tv_mult:     float = 0.5
    bear_second_trader_rebuy_tv_mult:    float = 0.0

    bear_n_hours:                float = 2.0
    bear_n_points:               float = 150.0
    bear_full_close_target:      float = 800.0






    # Event-specific parameters (legacy — kept for backward compat)



    # ── Risk / paper ───────────────────────────────────────────────────────
    q_max_btc:             float = 1000.0
    max_option_spend:      float = 400.0
    safe_mode_timeout_sec: int   = 5
    ws_reconnect_max_sec:  int   = 30
    latency_warn_ms:       int   = 500
    fill_timeout_sec:      int   = 5

    # ── Hedge verification timeout ─────────────────────────────────────────
    verify_hedge_timeout_sec: int = 120

    # ── Per-trader virtual balances (each trader independent $100k) ────────
    virtual_balance_usdt:  float = 100_000.0
    min_paper_balance:     float = 1_000.0

    # ── Legacy / backward-compat ────────────────────────────────────────────
    candle_count:         int   = 300
    candle_tf_minutes:    int   = 5
    delta_touch:          float = 30.0
    min_touches:          int   = 3
    dominance_ratio_high: float = 0.75
    dominance_ratio_low:  float = 0.75
    analyst_h:            int   = 4
    analyst_m:            int   = 0
    options_hold_until_h: int   = 11
    options_hold_until_m: int   = 0
    squareoff_start_h:    int   = 11
    squareoff_start_m:    int   = 45
    squareoff_end_h:      int   = 12
    squareoff_end_m:      int   = 0
    window_start_h:       int   = 9
    window_start_m:       int   = 30
    window_end_h:         int   = 11
    window_end_m:         int   = 45


# Module-level singleton
cfg = Config()

_listeners: list = []


def register_listener(coro):
    _listeners.append(coro)


async def update(params: Dict[str, Any]) -> Dict[str, str]:
    changes = {}
    for key, new_val in params.items():
        if not hasattr(cfg, key):
            log.warning(f"Unknown config key ignored: {key}")
            continue
        old_val = getattr(cfg, key)
        try:
            typed = type(old_val)(new_val)
        except (TypeError, ValueError):
            typed = new_val
        if typed == old_val:
            continue
        setattr(cfg, key, typed)
        changes[key] = f"{old_val} -> {typed}"
        log.info(f"Config: {key} = {old_val} -> {typed}")

    if changes:
        for listener in _listeners:
            try:
                await listener(changes)
            except Exception as e:
                log.error(f"Config listener error: {e}")

    return changes


def as_dict() -> Dict[str, Any]:
    return asdict(cfg)
