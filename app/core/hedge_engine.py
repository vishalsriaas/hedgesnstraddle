import asyncio
import logging
import json
import math
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, List
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.schema import (
    HedgeConfig, HedgeStrategyConfig, PendingConfig, ConfigAuditLog, HedgeSession, 
    HedgeTradeOrder, HedgeFill, HedgeOpenPosition, HedgePaperLedgerEntry, HedgeSessionEvent, HedgeRuntimeCommand
)
from app.core.binance_client import (
    get_btc_spot_price, get_btc_futures_mark_price, get_btc_options_mark_prices
)

from app.core.market_data import option_mark

logger = logging.getLogger("hedgesnstraddle.hedge_engine")

ist = timezone(timedelta(hours=5, minutes=30))

def get_session_relative_minutes(time_str: str) -> int:
    """
    Calculates minutes relative to Binance Expiry Session Start (13:31 PM IST = Minute 0).
    Binance Daily Options expire at 13:30 PM IST (08:00 UTC) every day.
    Session Cycle: 13:31 PM (Day 1) to 13:30 PM (Day 2) = Relative Minutes 0 to 1439.
    """
    try:
        parts = time_str.split(":")
        h = int(parts[0])
        m = int(parts[1])
        mins_from_midnight = h * 60 + m
        session_start_mins = 13 * 60 + 31  # 811 mins (13:31 PM IST)
        
        if mins_from_midnight >= session_start_mins:
            return mins_from_midnight - session_start_mins
        else:
            return (mins_from_midnight + 1440) - session_start_mins
    except Exception:
        return 0

def get_current_binance_session_dt() -> datetime:
    now_ist = datetime.now(ist)
    if now_ist.time() >= datetime.strptime("13:30:00", "%H:%M:%S").time():
        return now_ist + timedelta(days=1)
    else:
        return now_ist

def get_current_binance_session_date() -> str:
    """
    Returns the target Binance Expiry Date Key (YYMMDD) for the active trading session.
    If server time is >= 13:30:00 IST, the trading session targets tomorrow's 13:30 expiry date.
    If server time is < 13:30:00 IST, the trading session targets today's 13:30 expiry date.
    """
    return get_current_binance_session_dt().strftime("%y%m%d")

def is_weekend_session() -> bool:
    """Returns True if the target Binance Expiry Date falls on Saturday (5) or Sunday (6)."""
    target_dt = get_current_binance_session_dt()
    return target_dt.weekday() in [5, 6]

class HedgeEngine:
    def __init__(self):
        self.is_running = False
        self.state = "IDLE"
        self.active_role = "1st Trader"
        self._task: Optional[asyncio.Task] = None
        
        # Session state variables
        self.slot1_session_id: Optional[int] = None
        self.slot2_session_id: Optional[int] = None
        self.slot1_strike: Optional[float] = None
        self.slot2_strike: Optional[float] = None
        self.slot1_option_mark: Optional[float] = None
        self.slot2_option_mark: Optional[float] = None

        self.slot1_traded_session_key: Optional[str] = None
        self.slot2_traded_session_key: Optional[str] = None
        self.active_session_key: Optional[str] = None
        
        self.tp_rank_1_slot: Optional[str] = None  # '1st Trader' or '2nd Trader'
        self.tp_rank_2_slot: Optional[str] = None
        
        self.option_quotes = []
        self.slot1_completed = False
        self.slot2_completed = False
        self.last_futures_mark: float = 0.0
        self.last_spot_price: float = 0.0

    def load_config(self, db: Session) -> Dict[str, str]:
        configs = db.query(HedgeConfig).all()
        return {c.key: c.value for c in configs}

    def get_live_monitoring_snapshot(self, db: Session) -> Dict[str, Any]:
        """Returns dynamic Hedge workflow status, dual trader cards & condition checks for the frontend."""
        cfg = self.load_config(db)
        slot1_cfg = self.get_role_strategy_config(db, "1st Trader")
        slot2_cfg = self.get_role_strategy_config(db, "2nd Trader")

        now_time_full = datetime.now(ist).strftime("%H:%M:%S")
        now_time_str = now_time_full[:5]
        now_rel = get_session_relative_minutes(now_time_str)

        # Slot 1 Config & Countdown Timers
        w1_start_h = slot1_cfg.trade_start_h if slot1_cfg else 6
        w1_start_m = slot1_cfg.trade_start_m if slot1_cfg else 0
        w1_end_h = slot1_cfg.trade_end_h if slot1_cfg else 7
        w1_end_m = slot1_cfg.trade_end_m if slot1_cfg else 30
        sq1_h = slot1_cfg.force_close_h if slot1_cfg else 11
        sq1_m = slot1_cfg.force_close_m if slot1_cfg else 30

        w1_start = f"{w1_start_h:02d}:{w1_start_m:02d}"
        w1_end = f"{w1_end_h:02d}:{w1_end_m:02d}"
        sq1_end = f"{sq1_h:02d}:{sq1_m:02d}"

        w1_start_rel = get_session_relative_minutes(w1_start)
        sq1_end_rel = get_session_relative_minutes(sq1_end)

        diff1_open = w1_start_rel - now_rel
        if diff1_open < 0: diff1_open += 1440
        slot1_open_cd = f"{diff1_open//60:02d}:{diff1_open%60:02d}:00" if diff1_open > 0 else "OPEN NOW"

        diff1_sq = sq1_end_rel - now_rel
        if diff1_sq < 0: diff1_sq += 1440
        slot1_sq_cd = f"{diff1_sq//60:02d}:{diff1_sq%60:02d}:00"

        # Slot 2 Config & Countdown Timers
        w2_start_h = slot2_cfg.trade_start_h if slot2_cfg else 6
        w2_start_m = slot2_cfg.trade_start_m if slot2_cfg else 0
        w2_end_h = slot2_cfg.trade_end_h if slot2_cfg else 7
        w2_end_m = slot2_cfg.trade_end_m if slot2_cfg else 30
        sq2_h = slot2_cfg.force_close_h if slot2_cfg else 11
        sq2_m = slot2_cfg.force_close_m if slot2_cfg else 30

        w2_start = f"{w2_start_h:02d}:{w2_start_m:02d}"
        w2_end = f"{w2_end_h:02d}:{w2_end_m:02d}"
        sq2_end = f"{sq2_h:02d}:{sq2_m:02d}"

        w2_start_rel = get_session_relative_minutes(w2_start)
        sq2_end_rel = get_session_relative_minutes(sq2_end)

        diff2_open = w2_start_rel - now_rel
        if diff2_open < 0: diff2_open += 1440
        slot2_open_cd = f"{diff2_open//60:02d}:{diff2_open%60:02d}:00" if diff2_open > 0 else "OPEN NOW"

        diff2_sq = sq2_end_rel - now_rel
        if diff2_sq < 0: diff2_sq += 1440
        slot2_sq_cd = f"{diff2_sq//60:02d}:{diff2_sq%60:02d}:00"

        cond_time_window_valid = (w1_start_rel <= now_rel <= get_session_relative_minutes(w1_end))

        # Slot 1 Bullish & Bearish live data
        s1_max_prem = slot1_cfg.max_premium if slot1_cfg else 250.0
        s1_max_tv = slot1_cfg.max_time_value if slot1_cfg else 229.0
        s1_qty = slot1_cfg.contract_qty if slot1_cfg else 1.0

        s1_put_strk = getattr(self, "preview_slot1_put_strike", 0.0)
        s1_put_mark = getattr(self, "preview_slot1_put_mark", 0.0)
        s1_call_strk = getattr(self, "preview_slot1_call_strike", 0.0)
        s1_call_mark = getattr(self, "preview_slot1_call_mark", 0.0)

        s1_put_tv = self.calculate_time_value(s1_put_mark, s1_put_strk, "PUT", self.last_spot_price)
        s1_call_tv = self.calculate_time_value(s1_call_mark, s1_call_strk, "CALL", self.last_spot_price)

        # Slot 2 Bullish & Bearish live data
        s2_max_prem = slot2_cfg.max_premium if slot2_cfg else 400.0
        s2_max_tv = slot2_cfg.max_time_value if slot2_cfg else 229.0
        s2_qty = slot2_cfg.contract_qty if slot2_cfg else 1.0

        s2_put_strk = getattr(self, "preview_slot2_put_strike", 0.0)
        s2_put_mark = getattr(self, "preview_slot2_put_mark", 0.0)
        s2_call_strk = getattr(self, "preview_slot2_call_strike", 0.0)
        s2_call_mark = getattr(self, "preview_slot2_call_mark", 0.0)

        s2_put_tv = self.calculate_time_value(s2_put_mark, s2_put_strk, "PUT", self.last_spot_price)
        s2_call_tv = self.calculate_time_value(s2_call_mark, s2_call_strk, "CALL", self.last_spot_price)

        current_session_key = get_current_binance_session_date()

        entry_block_reason = None
        if cfg.get("BOT_ENABLED", "1") != "1" or cfg.get("ENGINE_ENABLED", "1") != "1":
            entry_block_reason = "Trading disabled; market previews remain active"
        elif cfg.get("GLOBAL_PAUSE", "0") == "1":
            entry_block_reason = "New entries paused; market previews remain active"

        # Determine Idle / Rejection Reasons for Slot 1
        s1_idle_reason = None
        if not self.slot1_session_id:
            if entry_block_reason:
                s1_idle_reason = entry_block_reason
            elif slot1_cfg and not slot1_cfg.enabled:
                s1_idle_reason = "Trader disabled; market previews remain active"
            elif getattr(self, "slot1_completed", False) or self.slot1_traded_session_key == current_session_key:
                s1_idle_reason = f"Session {current_session_key} Traded & Completed"
            elif is_weekend_session() and cfg.get("SKIP_WEEKENDS", "1") == "1":
                s1_idle_reason = "Weekend Expiry Skipped (Sat/Sun)"
            elif not (w1_start_rel <= now_rel <= get_session_relative_minutes(w1_end)):
                s1_idle_reason = f"Outside Window Range ({w1_start} - {w1_end})"
            elif s1_put_mark > s1_max_prem and s1_call_mark > s1_max_prem:
                s1_idle_reason = f"Option Mark (${max(s1_put_mark, s1_call_mark):.2f}) > Max Prem Cap (${s1_max_prem:.2f})"
            elif s1_put_tv > s1_max_tv and s1_call_tv > s1_max_tv:
                s1_idle_reason = f"Time Value (${max(s1_put_tv, s1_call_tv):.2f}) > TV Cap (${s1_max_tv:.2f})"
            elif s1_put_mark <= 0 and s1_call_mark <= 0:
                s1_idle_reason = "No qualifying ITM option within 24h expiry and premium/TV limits"
            else:
                s1_idle_reason = "Awaiting Market Condition Trigger"

        # Determine Idle / Rejection Reasons for Slot 2
        s2_idle_reason = None
        if not self.slot2_session_id:
            if entry_block_reason:
                s2_idle_reason = entry_block_reason
            elif slot2_cfg and not slot2_cfg.enabled:
                s2_idle_reason = "Trader disabled; market previews remain active"
            elif getattr(self, "slot2_completed", False) or self.slot2_traded_session_key == current_session_key:
                s2_idle_reason = f"Session {current_session_key} Traded & Completed"
            elif is_weekend_session() and cfg.get("SKIP_WEEKENDS", "1") == "1":
                s2_idle_reason = "Weekend Expiry Skipped (Sat/Sun)"
            elif not (w2_start_rel <= now_rel <= get_session_relative_minutes(w2_end)):
                s2_idle_reason = f"Outside Window Range ({w2_start} - {w2_end})"
            elif not getattr(self, "preview_slot2_selected", None) and getattr(self, "preview_slot2_required_direction", "Auto") in ("Bullish", "Bearish"):
                s2_idle_reason = f"Waiting for a qualifying {self.preview_slot2_required_direction} contract: required Trader 2 direction"
            elif s2_put_mark > s2_max_prem and s2_call_mark > s2_max_prem:
                s2_idle_reason = f"Option Mark (${max(s2_put_mark, s2_call_mark):.2f}) > Max Prem Cap (${s2_max_prem:.2f})"
            elif s2_put_tv > s2_max_tv and s2_call_tv > s2_max_tv:
                s2_idle_reason = f"Time Value (${max(s2_put_tv, s2_call_tv):.2f}) > TV Cap (${s2_max_tv:.2f})"
            elif s2_put_mark <= 0 and s2_call_mark <= 0:
                s2_idle_reason = "No qualifying ITM option within 24h expiry and premium/TV limits"
            else:
                s2_idle_reason = "Awaiting Market Condition Trigger"

        # Calculate Active Trade Live Position Telemetry for Slot 1
        s1_active_trade = None
        if self.slot1_session_id:
            dir_str1 = getattr(self, "slot1_direction", "Bullish")
            opt_mark1 = self.slot1_option_mark or 0.0

            # Query exact locked Futures Entry Price from DB position record
            fut_pos1 = db.query(HedgeOpenPosition).filter(
                HedgeOpenPosition.session_id == self.slot1_session_id,
                HedgeOpenPosition.symbol == "BTC-USDT-FUTURES"
            ).first()
            fut_entry1 = fut_pos1.entry_price if (fut_pos1 and fut_pos1.entry_price > 0) else getattr(self, "slot1_fut_entry", self.last_futures_mark)
            fut_tp1 = (fut_entry1 + opt_mark1) if dir_str1 == "Bullish" else (fut_entry1 - opt_mark1)
            
            held_option1 = db.query(HedgeOpenPosition).filter(
                HedgeOpenPosition.session_id == self.slot1_session_id,
                HedgeOpenPosition.symbol != "BTC-USDT-FUTURES").first()
            s1_qty = held_option1.qty if held_option1 else (fut_pos1.qty if fut_pos1 else s1_qty)
            try:
                held_mark1 = option_mark(self.option_quotes, held_option1.symbol) if held_option1 else None
            except ValueError:
                held_mark1 = None
            pnl_fut1 = (self.last_futures_mark - fut_entry1) * s1_qty if dir_str1 == "Bullish" else (fut_entry1 - self.last_futures_mark) * s1_qty
            if fut_pos1 is None:
                pnl_fut1 = 0.0
            opt_cur_mark1 = held_mark1
            pnl_opt1 = (opt_cur_mark1 - opt_mark1) * s1_qty if opt_cur_mark1 is not None else 0.0
            pnl_total1 = pnl_fut1 + pnl_opt1
            pnl_pct1 = (pnl_total1 / (fut_entry1 * s1_qty)) * 100 if (fut_entry1 * s1_qty) > 0 else 0.0

            rank1 = "🥇 1st TP Trader" if self.tp_rank_1_slot == "1st Trader" else ("🥈 2nd TP Trader" if self.tp_rank_2_slot == "1st Trader" else "Pending TP")

            s1_active_trade = {
                "direction": dir_str1,
                "strategy_label": "🟢 BULLISH (BUY PUT + LONG)" if dir_str1 == "Bullish" else "🔴 BEARISH (BUY CALL + SHORT)",
                "strike": self.slot1_strike or 0.0,
                "option_entry_mark": opt_mark1,
                "futures_entry": fut_entry1,
                "futures_tp": fut_tp1,
                "pnl_usdt": round(pnl_total1, 2) if held_mark1 is not None else None,
                "data_available": held_mark1 is not None,
                "pnl_pct": round(pnl_pct1, 2),
                "tp_rank": rank1
            }

        # Calculate Active Trade Live Position Telemetry for Slot 2
        s2_active_trade = None
        if self.slot2_session_id:
            dir_str2 = getattr(self, "slot2_direction", "Bullish")
            opt_mark2 = self.slot2_option_mark or 0.0

            # Query exact locked Futures Entry Price from DB position record
            fut_pos2 = db.query(HedgeOpenPosition).filter(
                HedgeOpenPosition.session_id == self.slot2_session_id,
                HedgeOpenPosition.symbol == "BTC-USDT-FUTURES"
            ).first()
            fut_entry2 = fut_pos2.entry_price if (fut_pos2 and fut_pos2.entry_price > 0) else getattr(self, "slot2_fut_entry", self.last_futures_mark)
            fut_tp2 = (fut_entry2 + opt_mark2) if dir_str2 == "Bullish" else (fut_entry2 - opt_mark2)
            
            held_option2 = db.query(HedgeOpenPosition).filter(
                HedgeOpenPosition.session_id == self.slot2_session_id,
                HedgeOpenPosition.symbol != "BTC-USDT-FUTURES").first()
            s2_qty = held_option2.qty if held_option2 else (fut_pos2.qty if fut_pos2 else s2_qty)
            try:
                held_mark2 = option_mark(self.option_quotes, held_option2.symbol) if held_option2 else None
            except ValueError:
                held_mark2 = None
            pnl_fut2 = (self.last_futures_mark - fut_entry2) * s2_qty if dir_str2 == "Bullish" else (fut_entry2 - self.last_futures_mark) * s2_qty
            if fut_pos2 is None:
                pnl_fut2 = 0.0
            opt_cur_mark2 = held_mark2
            pnl_opt2 = (opt_cur_mark2 - opt_mark2) * s2_qty if opt_cur_mark2 is not None else 0.0
            pnl_total2 = pnl_fut2 + pnl_opt2
            pnl_pct2 = (pnl_total2 / (fut_entry2 * s2_qty)) * 100 if (fut_entry2 * s2_qty) > 0 else 0.0

            rank2 = "🥇 1st TP Trader" if self.tp_rank_1_slot == "2nd Trader" else ("🥈 2nd TP Trader" if self.tp_rank_2_slot == "2nd Trader" else "Pending TP")

            s2_active_trade = {
                "direction": dir_str2,
                "strategy_label": "🟢 BULLISH (BUY PUT + LONG)" if dir_str2 == "Bullish" else "🔴 BEARISH (BUY CALL + SHORT)",
                "strike": self.slot2_strike or 0.0,
                "option_entry_mark": opt_mark2,
                "futures_entry": fut_entry2,
                "futures_tp": fut_tp2,
                "pnl_usdt": round(pnl_total2, 2) if held_mark2 is not None else None,
                "data_available": held_mark2 is not None,
                "pnl_pct": round(pnl_pct2, 2),
                "tp_rank": rank2
            }

        hedge_wallet_val = float(cfg.get("PAPER_WALLET_USDT", "100000.0"))

        return {
            "state": self.state,
            "active_role": self.active_role,
            "server_time": now_time_full,
            "last_spot_price": self.last_spot_price,
            "last_futures_mark": self.last_futures_mark,
            "hedge_paper_wallet_usdt": hedge_wallet_val,
            "active_session_key": current_session_key,
            "slot1": {
                "role": "1st Trader",
                "qty": s1_qty,
                "window_start": w1_start,
                "window_end": w1_end,
                "sq_end": sq1_end,
                "open_countdown": slot1_open_cd,
                "squareoff_countdown": slot1_sq_cd,
                "status": "Active" if self.slot1_session_id else ("Completed" if getattr(self, "slot1_completed", False) else "Idle"),
                "idle_reason": s1_idle_reason,
                "active_trade": s1_active_trade,
                "filled_direction": getattr(self, "slot1_direction", None) if self.slot1_session_id else None,
                "filled_strike": self.slot1_strike or 0.0,
                "filled_opt_mark": self.slot1_option_mark or 0.0,
                "filled_fut_entry": getattr(self, "slot1_fut_entry", 0.0) if self.slot1_session_id else 0.0,
                "filled_fut_tp": s1_active_trade["futures_tp"] if s1_active_trade else 0.0,
                "bullish": {
                    "strike": s1_put_strk,
                    "option_type": "PUT",
                    "selection_reason": getattr(self, "preview_slot1_put_reason", ""),
                    "option_mark": s1_put_mark,
                    "estimated_option_cost": round(s1_put_mark * (slot1_cfg.contract_qty if slot1_cfg else 1.0), 2) if s1_put_mark > 0 else None,
                    "time_value": round(s1_put_tv, 2),
                    "rule_b_valid": (s1_put_mark <= s1_max_prem) if s1_put_mark > 0 else False,
                    "tv_valid": (s1_put_tv <= s1_max_tv) if s1_put_mark > 0 else False,
                    "futures_tp": self.last_futures_mark + s1_put_mark
                },
                "bearish": {
                    "strike": s1_call_strk,
                    "option_type": "CALL",
                    "selection_reason": getattr(self, "preview_slot1_call_reason", ""),
                    "option_mark": s1_call_mark,
                    "estimated_option_cost": round(s1_call_mark * (slot1_cfg.contract_qty if slot1_cfg else 1.0), 2) if s1_call_mark > 0 else None,
                    "time_value": round(s1_call_tv, 2),
                    "rule_b_valid": (s1_call_mark <= s1_max_prem) if s1_call_mark > 0 else False,
                    "tv_valid": (s1_call_tv <= s1_max_tv) if s1_call_mark > 0 else False,
                    "futures_tp": self.last_futures_mark - s1_call_mark
                }
            },
            "slot2": {
                "role": "2nd Trader",
                "qty": s2_qty,
                "window_start": w2_start,
                "window_end": w2_end,
                "sq_end": sq2_end,
                "open_countdown": slot2_open_cd,
                "squareoff_countdown": slot2_sq_cd,
                "status": "Active" if self.slot2_session_id else ("Completed" if getattr(self, "slot2_completed", False) else "Idle"),
                "idle_reason": s2_idle_reason,
                "active_trade": s2_active_trade,
                "filled_direction": getattr(self, "slot2_direction", None) if self.slot2_session_id else None,
                "filled_strike": self.slot2_strike or 0.0,
                "filled_opt_mark": self.slot2_option_mark or 0.0,
                "filled_fut_entry": getattr(self, "slot2_fut_entry", 0.0) if self.slot2_session_id else 0.0,
                "filled_fut_tp": s2_active_trade["futures_tp"] if s2_active_trade else 0.0,
                "bullish": {
                    "strike": s2_put_strk,
                    "option_type": "PUT",
                    "selection_reason": getattr(self, "preview_slot2_put_reason", ""),
                    "option_mark": s2_put_mark,
                    "estimated_option_cost": round(s2_put_mark * (slot2_cfg.contract_qty if slot2_cfg else 1.0), 2) if s2_put_mark > 0 else None,
                    "time_value": round(s2_put_tv, 2),
                    "rule_b_valid": (s2_put_mark <= s2_max_prem) if s2_put_mark > 0 else False,
                    "tv_valid": (s2_put_tv <= s2_max_tv) if s2_put_mark > 0 else False,
                    "futures_tp": self.last_futures_mark + s2_put_mark
                },
                "bearish": {
                    "strike": s2_call_strk,
                    "option_type": "CALL",
                    "selection_reason": getattr(self, "preview_slot2_call_reason", ""),
                    "option_mark": s2_call_mark,
                    "estimated_option_cost": round(s2_call_mark * (slot2_cfg.contract_qty if slot2_cfg else 1.0), 2) if s2_call_mark > 0 else None,
                    "time_value": round(s2_call_tv, 2),
                    "rule_b_valid": (s2_call_mark <= s2_max_prem) if s2_call_mark > 0 else False,
                    "tv_valid": (s2_call_tv <= s2_max_tv) if s2_call_mark > 0 else False,
                    "futures_tp": self.last_futures_mark - s2_call_mark
                }
            },
            "cond_time_window_valid": cond_time_window_valid,
            "cond_rule_a_valid": any(m > 0 for m in (s1_put_mark, s1_call_mark, s2_put_mark, s2_call_mark)),
            "cond_rule_b_valid": (0 < s1_put_mark <= s1_max_prem or 0 < s1_call_mark <= s1_max_prem),
        }

    def get_role_strategy_config(self, db: Session, role_name: str) -> Optional[HedgeStrategyConfig]:
        """Fetch dynamic Hedge Strategy Config parameters by role name ('1st Trader' vs '2nd Trader')."""
        return db.query(HedgeStrategyConfig).filter(HedgeStrategyConfig.strategy_name == role_name).first()

    def flush_pending_config_on_session_close(self, db: Session):
        pending_items = db.query(PendingConfig).filter(PendingConfig.config_type == "HEDGE").all()
        if not pending_items:
            return

        logger.info("Hedge session completed. Flushing %d pending hedge config updates...", len(pending_items))
        for p in pending_items:
            old_item = db.query(HedgeConfig).filter(HedgeConfig.key == p.field_name).first()
            old_val = old_item.value if old_item else ""

            if old_item:
                old_item.value = p.pending_value
            else:
                db.add(HedgeConfig(key=p.field_name, value=p.pending_value))

            audit = ConfigAuditLog(
                user_email=p.user_email,
                config_type="HEDGE",
                field_name=p.field_name,
                old_value=old_val,
                new_value=p.pending_value,
                apply_mode="DEFERRED_ON_SESSION_CLOSE",
                status="APPLIED",
                ip_address="HEDGE_EVENT_LOOP"
            )
            db.add(audit)
            db.delete(p)

        db.commit()
        logger.info("Successfully applied pending hedge configurations!")

    def select_itm_option(self, quotes, spot_price, direction, max_premium, max_time_value, now):
        """Select minimum TV from one snapshot, with 0 < expiry remaining < 24h.

        Auto compares calls and puts together. Equal TV uses symbol order solely
        for deterministic results.
        """
        if not math.isfinite(spot_price) or spot_price <= 0:
            raise ValueError("DATA_GAP: Invalid BTC spot price")
        candidates = []
        for quote in quotes:
            if not isinstance(quote, dict):
                continue
            symbol = quote.get("symbol", "")
            if not isinstance(symbol, str):
                continue
            parts = symbol.split("-")
            if len(parts) != 4 or parts[0] != "BTC" or parts[3] not in ("C", "P"):
                continue
            expiry_code, strike_text, side = parts[1:]
            try:
                expiry = datetime.strptime(expiry_code, "%y%m%d").replace(hour=8, tzinfo=timezone.utc)
                strike, premium = float(strike_text), float(quote.get("markPrice"))
            except (ValueError, TypeError):
                continue
            if not timedelta(0) < expiry - now < timedelta(hours=24):
                continue
            if not math.isfinite(strike) or strike <= 0 or not math.isfinite(premium) or premium <= 0:
                continue
            if direction == "Bullish" and side != "P" or direction == "Bearish" and side != "C":
                continue
            # Preserve the existing inclusive ATM boundary, using spot for both sides.
            if side == "P" and strike < spot_price or side == "C" and strike > spot_price:
                continue
            tv = self.calculate_time_value(premium, strike, side, spot_price)
            if premium <= max_premium and tv <= max_time_value:
                candidates.append((tv, symbol, strike, premium, expiry_code))
        if not candidates:
            raise ValueError("No ITM contract passes premium/TV limits with 0 < expiry remaining < 24h")
        _, symbol, strike, premium, expiry_code = min(candidates)
        return strike, premium, expiry_code, symbol

    def calculate_time_value(self, option_mark: float, strike: float, option_type: str, spot_price: float) -> float:
        """
        Calculates option Time Value (TV) = Option Mark - Intrinsic Value.
        PUT Intrinsic Value = max(0, Strike - Spot)
        CALL Intrinsic Value = max(0, Spot - Strike)
        """
        if not option_mark or option_mark <= 0:
            return 0.0
        if option_type.upper() in ["PUT", "P"]:
            intrinsic = max(0.0, strike - spot_price)
        else:
            intrinsic = max(0.0, spot_price - strike)
        return max(0.0, option_mark - intrinsic)

    def validate_time_value(self, option_mark: float, strike: float, option_type: str, spot_price: float, max_time_value: float) -> bool:
        """Validates that Time Value <= max_time_value."""
        tv = self.calculate_time_value(option_mark, strike, option_type, spot_price)
        return tv <= max_time_value

    def entry_direction(self, db, role_name, configured_direction):
        """Trader 1's recorded entry locks Trader 2 to the opposite side for this expiry."""
        if role_name == "2nd Trader":
            first = db.query(HedgeSession).join(HedgeTradeOrder,
                HedgeTradeOrder.session_id == HedgeSession.id).filter(
                HedgeSession.expiry_session == get_current_binance_session_date(),
                HedgeTradeOrder.trader_leg == "1st Trader",
                HedgeTradeOrder.status == "FILLED",
                HedgeTradeOrder.symbol != "BTC-USDT-FUTURES",
                HedgeTradeOrder.side == "BUY").order_by(HedgeSession.id).first()
            if first:
                return "Bearish" if first.bull_entry else "Bullish"
        return configured_direction or "Auto"

    async def execute_slot_entry(
        self, db: Session, role_name: str, role_config: HedgeStrategyConfig, 
        futures_mark: float, spot_price: float, *, quotes=None, snapshot_time=None
    ) -> Optional[int]:
        """
        Executes atomic slot trade: BUY Option @ option_mark + OPEN Futures @ futures_mark.
        Sets Futures TP = futures_entry ± option_mark.
        """
        qty = role_config.contract_qty
        max_premium = role_config.max_premium
        max_tv = role_config.max_time_value

        current_session_key = get_current_binance_session_date()

        cfg = self.load_config(db)
        if (not role_config.enabled or cfg.get("BOT_ENABLED", "1") != "1"
                or cfg.get("ENGINE_ENABLED", "1") != "1" or cfg.get("GLOBAL_PAUSE", "0") == "1"):
            return None
        if not math.isfinite(qty) or qty <= 0:
            raise ValueError("Contract quantity must be positive")
        if qty > float(cfg.get("Q_MAX_BTC", "1000")):
            return None
        existing = db.query(HedgeTradeOrder).join(
            HedgeSession, HedgeTradeOrder.session_id == HedgeSession.id).filter(
                HedgeSession.expiry_session == current_session_key,
                HedgeTradeOrder.trader_leg == role_name).first()
        if existing:
            return None

        if quotes is None:
            quotes = await get_btc_options_mark_prices()
        snapshot_time = snapshot_time or datetime.now(timezone.utc)
        try:
            strike, option_mark, expiry_sym, opt_symbol = self.select_itm_option(
                quotes, spot_price, self.entry_direction(db, role_name, role_config.direction),
                max_premium, max_tv, snapshot_time)
        except ValueError as exc:
            logger.info("%s entry skipped: %s", role_name, exc)
            return None
        direction = "Bullish" if opt_symbol.endswith("-P") else "Bearish"

        if not math.isfinite(option_mark) or option_mark <= 0:
            return None
        now_ist = datetime.now(ist).replace(tzinfo=None)

        # Calculate Futures TP Level based on option_mark
        is_bullish = (direction == "Bullish")
        fut_side = "BUY" if is_bullish else "SELL"
        fut_tp_price = futures_mark + option_mark if is_bullish else futures_mark - option_mark

        # 1. Create HedgeSession
        sess = HedgeSession(
            symbol="BTCUSDT",
            expiry_session=expiry_sym,
            status="Open",
            bull_entry=futures_mark if is_bullish else 0.0,
            bear_entry=futures_mark if not is_bullish else 0.0,
            created_at=now_ist
        )
        db.add(sess)
        db.flush()

        # 2. Record Option BUY Trade Order
        opt_side = "BUY"
        opt_label = "PUT" if is_bullish else "CALL"
        opt_order = HedgeTradeOrder(
            session_id=sess.id,
            symbol=opt_symbol,
            side=opt_side,
            trader_leg=role_name,
            order_type="MARKET",
            qty=qty,
            price=option_mark,
            status="FILLED",
            created_at=now_ist
        )
        db.add(opt_order)

        # 3. Record Futures Open Trade Order
        fut_order = HedgeTradeOrder(
            session_id=sess.id,
            symbol="BTC-USDT-FUTURES",
            side=fut_side,
            trader_leg=role_name,
            order_type="MARKET",
            qty=qty,
            price=futures_mark,
            status="FILLED",
            created_at=now_ist
        )
        db.add(fut_order)

        # 4. Record Open Position Snapshots
        opt_pos = HedgeOpenPosition(
            session_id=sess.id,
            symbol=opt_symbol,
            side="LONG",
            entry_price=option_mark,
            qty=qty,
            unrealized_pnl=0.0
        )
        fut_pos = HedgeOpenPosition(
            session_id=sess.id,
            symbol="BTC-USDT-FUTURES",
            side="LONG" if is_bullish else "SHORT",
            entry_price=futures_mark,
            qty=qty,
            unrealized_pnl=0.0
        )
        db.add(opt_pos)
        db.add(fut_pos)

        db.commit()
        logger.info("Hedge Slot [%s] Entered: Strategy %s, Strike $%.0f, OptionMark $%.2f, FuturesEntry $%.2f, FuturesTP $%.2f",
                    role_name, direction, strike, option_mark, futures_mark, fut_tp_price)

        if role_name == "1st Trader":
            self.slot1_session_id = sess.id
            self.slot1_strike = strike
            self.slot1_option_mark = option_mark
            self.slot1_fut_entry = futures_mark
            self.slot1_direction = direction
            self.slot1_traded_session_key = current_session_key
        else:
            self.slot2_session_id = sess.id
            self.slot2_strike = strike
            self.slot2_option_mark = option_mark
            self.slot2_fut_entry = futures_mark
            self.slot2_direction = direction
            self.slot2_traded_session_key = current_session_key

        return sess.id

    def restore_sessions(self, db):
        """Rebuild slot tracking and session locks from durable orders and positions."""
        key = get_current_binance_session_date()
        self.active_session_key = key
        self.tp_rank_1_slot = self.tp_rank_2_slot = None
        for slot, role in ((1, "1st Trader"), (2, "2nd Trader")):
            prefix = f"slot{slot}"
            for field in ("session_id", "strike", "option_mark", "traded_session_key"):
                setattr(self, f"{prefix}_{field}", None)
            setattr(self, f"{prefix}_completed", False)
            sessions = db.query(HedgeSession).join(
                HedgeTradeOrder, HedgeTradeOrder.session_id == HedgeSession.id
            ).filter(HedgeTradeOrder.trader_leg == role).order_by(HedgeSession.id.desc()).all()
            for sess in sessions:
                if sess.expiry_session == key:
                    setattr(self, f"{prefix}_traded_session_key", key)
                    if sess.status not in ("Open", "OPEN"):
                        setattr(self, f"{prefix}_completed", True)
                if sess.status not in ("Open", "OPEN"):
                    continue
                positions = db.query(HedgeOpenPosition).filter_by(session_id=sess.id).all()
                fut = next((p for p in positions if "FUTURES" in p.symbol), None)
                opt = next((p for p in positions if "FUTURES" not in p.symbol), None)
                if opt:
                    setattr(self, f"{prefix}_session_id", sess.id)
                    setattr(self, f"{prefix}_strike", float(opt.symbol.split("-")[2]))
                    setattr(self, f"{prefix}_option_mark", opt.entry_price)
                    setattr(self, f"{prefix}_fut_entry", sess.bull_entry or sess.bear_entry)
                    setattr(self, f"{prefix}_direction", "Bullish" if sess.bull_entry else "Bearish")
                    break
        events = db.query(HedgeSessionEvent).join(HedgeSession,
            HedgeSessionEvent.session_id == HedgeSession.id).filter(
            HedgeSession.expiry_session == key, HedgeSessionEvent.event_type == "FUTURES_TP"
        ).order_by(HedgeSessionEvent.id).all()
        for rank, event in enumerate(events[:2], 1):
            setattr(self, f"tp_rank_{rank}_slot", json.loads(event.payload_json)["role"])

    def credit_realized(self, db, sess, amount, reason):
        """Credit only newly realized leg PnL, retaining previous settlements."""
        cash = db.query(HedgeConfig).filter_by(key="PAPER_WALLET_USDT").first()
        if cash is None:
            cash = HedgeConfig(key="PAPER_WALLET_USDT", value="100000")
            db.add(cash)
        amount = round(amount, 2)
        balance = round(float(cash.value) + amount, 2)
        cash.value = str(balance)
        sess.realized_pnl = round((sess.realized_pnl or 0) + amount, 2)
        db.add(HedgePaperLedgerEntry(session_id=sess.id, entry_type=reason,
            amount=amount, balance_after=balance, detail=reason,
            created_at=datetime.now(ist).replace(tzinfo=None)))

    async def manage_slot(self, db, sess, role):
        """Execute the documented first/second TP phases using durable orders/events."""
        positions = db.query(HedgeOpenPosition).filter_by(session_id=sess.id).all()
        fut = next((p for p in positions if "FUTURES" in p.symbol), None)
        opt = next((p for p in positions if "FUTURES" not in p.symbol), None)
        if not opt:
            raise ValueError(f"Missing held option for session {sess.id}")
        event = db.query(HedgeSessionEvent).filter_by(session_id=sess.id, event_type="FUTURES_TP").first()
        bullish = bool(sess.bull_entry)
        entry = sess.bull_entry or sess.bear_entry
        target = entry + opt.entry_price if bullish else entry - opt.entry_price
        mark = self.last_futures_mark
        hit = mark >= target if bullish else mark <= target
        if fut and not event and hit:
            prior = db.query(HedgeSessionEvent).join(HedgeSession,
                HedgeSessionEvent.session_id == HedgeSession.id).filter(
                HedgeSession.expiry_session == sess.expiry_session,
                HedgeSessionEvent.event_type == "FUTURES_TP").count()
            rank = prior + 1
            now = datetime.now(ist).replace(tzinfo=None)
            pnl = (mark - fut.entry_price) * fut.qty * (1 if bullish else -1)
            db.add(HedgeTradeOrder(session_id=sess.id, symbol=fut.symbol,
                side="SELL" if bullish else "BUY", trader_leg=role, order_type="TAKE_PROFIT",
                qty=fut.qty, price=mark, status="FILLED", created_at=now))
            if bullish: sess.bull_exit = mark
            else: sess.bear_exit = mark
            db.delete(fut)
            self.credit_realized(db, sess, pnl, "FUTURES_TP")
            db.add(HedgeSessionEvent(session_id=sess.id, event_type="FUTURES_TP",
                message=f"{role} futures TP rank {rank}",
                payload_json=json.dumps(dict(role=role, rank=rank)), created_at=now))
            strike = float(opt.symbol.split("-")[2])
            db.add(HedgeTradeOrder(session_id=sess.id,
                symbol=opt.symbol if rank == 1 else "BTC-USDT-FUTURES",
                side="SELL" if rank == 1 or not bullish else "BUY", trader_leg=role,
                order_type="OPTION_TARGET" if rank == 1 else "REENTRY_LIMIT", qty=opt.qty,
                price=opt.entry_price * 2 if rank == 1 else (strike - opt.entry_price if bullish else strike + opt.entry_price),
                status="PENDING", created_at=now))
            db.commit()
            self.restore_sessions(db)
            return
        pending = db.query(HedgeTradeOrder).filter_by(session_id=sess.id, status="PENDING").all()
        for order in pending:
            if order.order_type == "OPTION_TARGET":
                if option_mark(self.option_quotes, opt.symbol) >= order.price:
                    # Record a single filled target, not a duplicate market close.
                    await self.execute_squareoff(db, "Option Target Hit", [sess.id])
            elif order.order_type == "REENTRY_LIMIT" and fut is None:
                hit = mark <= order.price if order.side == "BUY" else mark >= order.price
                if hit:
                    order.status = "FILLED"
                    order.price = mark  # paper execution uses the observed mark
                    db.add(HedgeOpenPosition(session_id=sess.id, symbol=order.symbol,
                        side="LONG" if order.side == "BUY" else "SHORT", entry_price=mark,
                        qty=order.qty, leverage=opt.leverage))
                    db.commit()

    async def execute_squareoff(self, db, reason="Scheduled Squareoff", session_ids=None):
        query = db.query(HedgeSession).filter(HedgeSession.status.in_(["Open", "OPEN"]))
        if session_ids is not None:
            query = query.filter(HedgeSession.id.in_(session_ids))
        sessions = query.all()
        if not sessions:
            self.restore_sessions(db)
            self.state = "IN_TRADE" if self.slot1_session_id or self.slot2_session_id else "COMPLETED"
            return
        futures_mark = await get_btc_futures_mark_price()
        quotes = await get_btc_options_mark_prices()
        plans = []
        for sess in sessions:
            positions = db.query(HedgeOpenPosition).filter_by(session_id=sess.id).all()
            if not positions:
                raise ValueError(f"Open hedge session {sess.id} has no positions")
            order = db.query(HedgeTradeOrder).filter_by(session_id=sess.id).order_by(HedgeTradeOrder.id).first()
            if not order:
                raise ValueError(f"Missing entry order for hedge session {sess.id}")
            exits = [(pos, futures_mark if "FUTURES" in pos.symbol else option_mark(quotes, pos.symbol)) for pos in positions]
            plans.append((sess, order.trader_leg, exits))
        now = datetime.now(ist).replace(tzinfo=None)
        cash = db.query(HedgeConfig).filter_by(key="PAPER_WALLET_USDT").first()
        if cash is None:
            cash = HedgeConfig(key="PAPER_WALLET_USDT", value="100000")
            db.add(cash)
        balance = float(cash.value)
        for sess, role, exits in plans:
            pnl = 0.0
            details = []
            for pos, exit_price in exits:
                long = pos.side.upper() in ("LONG", "BUY")
                leg_pnl = (exit_price - pos.entry_price) * pos.qty * (1 if long else -1)
                pnl += leg_pnl
                target_order = db.query(HedgeTradeOrder).filter_by(session_id=sess.id,
                    symbol=pos.symbol, order_type="OPTION_TARGET", status="PENDING").first() if reason == "Option Target Hit" else None
                if target_order:
                    target_order.status = "FILLED"
                    target_order.price = exit_price
                else:
                    db.add(HedgeTradeOrder(session_id=sess.id, symbol=pos.symbol,
                        side="SELL" if long else "BUY", trader_leg=role, order_type="MARKET",
                        qty=pos.qty, price=exit_price, status="FILLED", created_at=now))
                if "FUTURES" in pos.symbol:
                    if long: sess.bull_exit = exit_price
                    else: sess.bear_exit = exit_price
                else:
                    details.append(dict(symbol=pos.symbol, entry_price=pos.entry_price,
                        exit_price=exit_price, qty=pos.qty, pnl=leg_pnl))
                db.delete(pos)
            for pending in db.query(HedgeTradeOrder).filter_by(session_id=sess.id, status="PENDING").all():
                if pending.status == "PENDING":
                    pending.status = "CANCELLED"
                    pending.cancel_reason = reason
            sess.status = "Completed"
            sess.exit_reason = reason
            realized_now = round(pnl, 2)
            sess.realized_pnl = round((sess.realized_pnl or 0) + realized_now, 2)
            sess.updated_at = now
            balance += realized_now
            db.add(HedgePaperLedgerEntry(session_id=sess.id, entry_type="SESSION_SQUAREOFF",
                amount=realized_now, balance_after=round(balance, 2),
                detail=json.dumps(dict(message=reason, futures_exit_price=futures_mark,
                    options_exit_details=details)), created_at=now))
        cash.value = str(round(balance, 2))
        db.commit()
        self.restore_sessions(db)
        self.state = "IN_TRADE" if self.slot1_session_id or self.slot2_session_id else "COMPLETED"

    async def tick(self, db):
        commands = db.query(HedgeRuntimeCommand).filter_by(command="SQUAREOFF", status="QUEUED").all()
        manual = self.state == "SQUAREOFF" or bool(commands)
        self.restore_sessions(db)
        cfg = self.load_config(db)
        if manual:
            await self.execute_squareoff(db, "Manual Emergency Squareoff")
            for command in commands:
                command.status = "COMPLETED"
            db.commit()
            return
        enabled = cfg.get("BOT_ENABLED", "1") == "1" and cfg.get("ENGINE_ENABLED", "1") == "1"
        paused = cfg.get("GLOBAL_PAUSE", "0") == "1"
        weekend = cfg.get("SKIP_WEEKENDS", "1") == "1" and is_weekend_session()
        active = self.slot1_session_id or self.slot2_session_id
        if not active and (not enabled or paused or weekend):
            self.state = "DISABLED" if not enabled else ("PAUSED" if paused else "SKIP_WEEKEND")
        # Entry controls must not freeze monitoring prices or selection reasons.
        # Clear old previews first so a failed fetch cannot retain old thresholds.
        for slot in (1, 2):
            setattr(self, f"preview_slot{slot}_selected", None)
            for label in ("put", "call"):
                setattr(self, f"preview_slot{slot}_{label}_strike", 0.0)
                setattr(self, f"preview_slot{slot}_{label}_mark", 0.0)
                setattr(self, f"preview_slot{slot}_{label}_reason", "Market data unavailable; awaiting refresh")
        self.last_spot_price = await get_btc_spot_price()
        self.last_futures_mark = await get_btc_futures_mark_price()
        try:
            self.option_quotes = await get_btc_options_mark_prices()
        except ValueError:
            self.option_quotes = []
            logger.warning("Options unavailable; retaining positions pending valid marks")
        now = datetime.now(ist)
        window_open = False
        for slot, role in ((1, "1st Trader"), (2, "2nd Trader")):
            config = self.get_role_strategy_config(db, role)
            if not config:
                continue
            # Preview failures must not prevent another active slot from being managed.
            for direction, label in (("Bullish", "put"), ("Bearish", "call")):
                try:
                    strike, mark, _, _ = self.select_itm_option(self.option_quotes, self.last_spot_price, direction, config.max_premium, config.max_time_value, now)
                    reason = ""
                except ValueError:
                    strike, mark = 0.0, 0.0
                    try:
                        self.select_itm_option(self.option_quotes, self.last_spot_price, direction, float("inf"), float("inf"), now)
                        reason = f"Quotes available; no contract passes premium <= {config.max_premium:g} and TV <= {config.max_time_value:g}"
                    except ValueError:
                        reason = "No eligible ITM quotes with expiry remaining between 0 and 24 hours"
                setattr(self, f"preview_slot{slot}_{label}_reason", reason)
                setattr(self, f"preview_slot{slot}_{label}_strike", strike)
                setattr(self, f"preview_slot{slot}_{label}_mark", mark)
            required_direction = self.entry_direction(db, role, config.direction)
            setattr(self, f"preview_slot{slot}_required_direction", required_direction)
            try:
                selected = self.select_itm_option(self.option_quotes, self.last_spot_price,
                    required_direction, config.max_premium, config.max_time_value, now)
            except ValueError:
                selected = None
            setattr(self, f"preview_slot{slot}_selected", selected)
            # Use the held session's entry date to handle deadlines after the expiry boundary.
            sid = getattr(self, f"slot{slot}_session_id")
            if sid:
                sess = db.get(HedgeSession, sid)
                entry = sess.created_at.replace(tzinfo=ist)
                deadline = entry.replace(hour=config.force_close_h, minute=config.force_close_m, second=0, microsecond=0)
                if deadline <= entry:
                    deadline += timedelta(days=1)
                try:
                    if now >= deadline:
                        await self.execute_squareoff(db, "Scheduled Squareoff", [sid])
                    else:
                        await self.manage_slot(db, sess, role)
                except ValueError as exc:
                    db.rollback()
                    logger.warning("%s management deferred: %s", role, exc)
                continue
            start = config.trade_start_h * 60 + config.trade_start_m
            end = config.trade_end_h * 60 + config.trade_end_m
            minute = now.hour * 60 + now.minute
            in_window = start <= minute <= end if start <= end else minute >= start or minute <= end
            window_open |= in_window
            if not (enabled and not paused and not weekend and config.enabled and in_window):
                continue
            try:
                await self.execute_slot_entry(db, role, config, self.last_futures_mark, self.last_spot_price, quotes=self.option_quotes, snapshot_time=now)
            except ValueError as exc:
                logger.warning("Slot %s entry deferred: %s", slot, exc)
        active = self.slot1_session_id or self.slot2_session_id
        self.state = "IN_TRADE" if active else ("DISABLED" if not enabled else "PAUSED" if paused else "SKIP_WEEKEND" if weekend else "ENTRY_WINDOW" if window_open else "IDLE")
        if not active and enabled and not paused and not weekend:
            self.flush_pending_config_on_session_close(db)

    async def run_loop(self):
        self.is_running = True
        while self.is_running:
            db = SessionLocal()
            try:
                await self.tick(db)
            except Exception:
                db.rollback()
                logger.exception("Error in Hedge Engine loop")
            finally:
                db.close()
            await asyncio.sleep(2.0)

    def start(self):
        if not self.is_running:
            self._task = asyncio.create_task(self.run_loop())

    def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()

hedge_engine = HedgeEngine()
