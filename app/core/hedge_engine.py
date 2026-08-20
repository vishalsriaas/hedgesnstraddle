import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, List
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.schema import (
    HedgeConfig, HedgeStrategyConfig, PendingConfig, ConfigAuditLog, HedgeSession, 
    HedgeTradeOrder, HedgeFill, HedgeOpenPosition, HedgePaperLedgerEntry
)
from app.core.binance_client import (
    get_btc_spot_price, get_btc_futures_mark_price, get_btc_options_mark_prices
)

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
        
        self.last_futures_mark: float = 64000.0
        self.last_spot_price: float = 64000.0

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

        s1_put_strk = getattr(self, "preview_slot1_put_strike", round(self.last_futures_mark / 250.0) * 250.0)
        s1_put_mark = getattr(self, "preview_slot1_put_mark", 0.0)
        s1_call_strk = getattr(self, "preview_slot1_call_strike", round(self.last_futures_mark / 250.0) * 250.0)
        s1_call_mark = getattr(self, "preview_slot1_call_mark", 0.0)

        s1_put_tv = self.calculate_time_value(s1_put_mark, s1_put_strk, "PUT", self.last_spot_price)
        s1_call_tv = self.calculate_time_value(s1_call_mark, s1_call_strk, "CALL", self.last_spot_price)

        # Slot 2 Bullish & Bearish live data
        s2_max_prem = slot2_cfg.max_premium if slot2_cfg else 400.0
        s2_max_tv = slot2_cfg.max_time_value if slot2_cfg else 229.0
        s2_qty = slot2_cfg.contract_qty if slot2_cfg else 1.0

        s2_put_strk = getattr(self, "preview_slot2_put_strike", round(self.last_futures_mark / 250.0) * 250.0)
        s2_put_mark = getattr(self, "preview_slot2_put_mark", 0.0)
        s2_call_strk = getattr(self, "preview_slot2_call_strike", round(self.last_futures_mark / 250.0) * 250.0)
        s2_call_mark = getattr(self, "preview_slot2_call_mark", 0.0)

        s2_put_tv = self.calculate_time_value(s2_put_mark, s2_put_strk, "PUT", self.last_spot_price)
        s2_call_tv = self.calculate_time_value(s2_call_mark, s2_call_strk, "CALL", self.last_spot_price)

        current_session_key = get_current_binance_session_date()

        # Determine Idle / Rejection Reasons for Slot 1
        s1_idle_reason = None
        if not self.slot1_session_id:
            if getattr(self, "slot1_completed", False) or self.slot1_traded_session_key == current_session_key:
                s1_idle_reason = f"Session {current_session_key} Traded & Completed"
            elif is_weekend_session() and cfg.get("SKIP_WEEKENDS", "1") == "1":
                s1_idle_reason = "Weekend Expiry Skipped (Sat/Sun)"
            elif not (w1_start_rel <= now_rel <= get_session_relative_minutes(w1_end)):
                s1_idle_reason = f"Outside Window Range ({w1_start} - {w1_end})"
            elif s1_put_mark > s1_max_prem and s1_call_mark > s1_max_prem:
                s1_idle_reason = f"Option Mark (${max(s1_put_mark, s1_call_mark):.2f}) > Max Prem Cap (${s1_max_prem:.2f})"
            elif s1_put_tv > s1_max_tv and s1_call_tv > s1_max_tv:
                s1_idle_reason = f"Time Value (${max(s1_put_tv, s1_call_tv):.2f}) > TV Cap (${s1_max_tv:.2f})"
            else:
                s1_idle_reason = "Awaiting Market Condition Trigger"

        # Determine Idle / Rejection Reasons for Slot 2
        s2_idle_reason = None
        if not self.slot2_session_id:
            if getattr(self, "slot2_completed", False) or self.slot2_traded_session_key == current_session_key:
                s2_idle_reason = f"Session {current_session_key} Traded & Completed"
            elif is_weekend_session() and cfg.get("SKIP_WEEKENDS", "1") == "1":
                s2_idle_reason = "Weekend Expiry Skipped (Sat/Sun)"
            elif not (w2_start_rel <= now_rel <= get_session_relative_minutes(w2_end)):
                s2_idle_reason = f"Outside Window Range ({w2_start} - {w2_end})"
            elif self.slot1_session_id and self.slot1_strike is not None and s2_put_strk < self.slot1_strike:
                s2_idle_reason = f"Rule C Strike Clash (${s2_put_strk:.0f} < ${self.slot1_strike:.0f})"
            elif s2_put_mark > s2_max_prem and s2_call_mark > s2_max_prem:
                s2_idle_reason = f"Option Mark (${max(s2_put_mark, s2_call_mark):.2f}) > Max Prem Cap (${s2_max_prem:.2f})"
            elif s2_put_tv > s2_max_tv and s2_call_tv > s2_max_tv:
                s2_idle_reason = f"Time Value (${max(s2_put_tv, s2_call_tv):.2f}) > TV Cap (${s2_max_tv:.2f})"
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
            
            pnl_fut1 = (self.last_futures_mark - fut_entry1) * s1_qty if dir_str1 == "Bullish" else (fut_entry1 - self.last_futures_mark) * s1_qty
            opt_cur_mark1 = s1_put_mark if dir_str1 == "Bullish" else s1_call_mark
            pnl_opt1 = (opt_cur_mark1 - opt_mark1) * s1_qty
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
                "pnl_usdt": round(pnl_total1, 2),
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
            
            pnl_fut2 = (self.last_futures_mark - fut_entry2) * s2_qty if dir_str2 == "Bullish" else (fut_entry2 - self.last_futures_mark) * s2_qty
            opt_cur_mark2 = s2_put_mark if dir_str2 == "Bullish" else s2_call_mark
            pnl_opt2 = (opt_cur_mark2 - opt_mark2) * s2_qty
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
                "pnl_usdt": round(pnl_total2, 2),
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
                "filled_fut_entry": self.last_futures_mark if self.slot1_session_id else 0.0,
                "filled_fut_tp": (self.last_futures_mark + (self.slot1_option_mark or 0.0)) if (getattr(self, "slot1_direction", "Bullish") == "Bullish") else (self.last_futures_mark - (self.slot1_option_mark or 0.0)),
                "bullish": {
                    "strike": s1_put_strk,
                    "option_type": "PUT",
                    "option_mark": s1_put_mark,
                    "time_value": round(s1_put_tv, 2),
                    "rule_b_valid": (s1_put_mark <= s1_max_prem) if s1_put_mark > 0 else True,
                    "tv_valid": (s1_put_tv <= s1_max_tv) if s1_put_mark > 0 else True,
                    "futures_tp": self.last_futures_mark + s1_put_mark
                },
                "bearish": {
                    "strike": s1_call_strk,
                    "option_type": "CALL",
                    "option_mark": s1_call_mark,
                    "time_value": round(s1_call_tv, 2),
                    "rule_b_valid": (s1_call_mark <= s1_max_prem) if s1_call_mark > 0 else True,
                    "tv_valid": (s1_call_tv <= s1_max_tv) if s1_call_mark > 0 else True,
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
                "filled_fut_entry": self.last_futures_mark if self.slot2_session_id else 0.0,
                "filled_fut_tp": (self.last_futures_mark + (self.slot2_option_mark or 0.0)) if (getattr(self, "slot2_direction", "Bullish") == "Bullish") else (self.last_futures_mark - (self.slot2_option_mark or 0.0)),
                "bullish": {
                    "strike": s2_put_strk,
                    "option_type": "PUT",
                    "option_mark": s2_put_mark,
                    "time_value": round(s2_put_tv, 2),
                    "rule_b_valid": (s2_put_mark <= s2_max_prem) if s2_put_mark > 0 else True,
                    "tv_valid": (s2_put_tv <= s2_max_tv) if s2_put_mark > 0 else True,
                    "futures_tp": self.last_futures_mark + s2_put_mark
                },
                "bearish": {
                    "strike": s2_call_strk,
                    "option_type": "CALL",
                    "option_mark": s2_call_mark,
                    "time_value": round(s2_call_tv, 2),
                    "rule_b_valid": (s2_call_mark <= s2_max_prem) if s2_call_mark > 0 else True,
                    "tv_valid": (s2_call_tv <= s2_max_tv) if s2_call_mark > 0 else True,
                    "futures_tp": self.last_futures_mark - s2_call_mark
                }
            },
            "cond_time_window_valid": cond_time_window_valid,
            "cond_rule_a_valid": True,
            "cond_rule_b_valid": True,
            "cond_rule_c_valid": True,
            "cond_max_spend_valid": True
        }

    def get_role_strategy_config(self, db: Session, role_name: str) -> Optional[HedgeStrategyConfig]:
        """Fetch dynamic Hedge Strategy Config parameters by role name ('1st Trader' vs '2nd Trader')."""
        return db.query(HedgeStrategyConfig).filter(HedgeStrategyConfig.strategy_name == role_name).first()

    def validate_option_spend(self, option_ask: float = 0.0, qty: float = 1.0, max_option_spend: float = 400.0, option_mark: Optional[float] = None) -> bool:
        """Enforces MAX_OPTION_SPEND limit: reject option purchase if (cost) > MAX_OPTION_SPEND."""
        price = option_mark if option_mark is not None else option_ask
        total_cost = price * qty
        if total_cost > max_option_spend:
            logger.warning("Option spend $%.2f exceeds limit $%.2f - REJECTED", total_cost, max_option_spend)
            return False
        return True

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

    async def find_nearest_itm_option(self, futures_mark: float, direction: str) -> Tuple[float, float, str, str]:
        """
        Rule A: Finds nearest In-The-Money (ITM) option contract for today's expiry.
        Bullish -> Nearest PUT (Strike K >= futures_mark)
        Bearish -> Nearest CALL (Strike K <= futures_mark)
        Returns: (strike_price, option_mark, expiry_sym, symbol)
        """
        mark_prices = await get_btc_options_mark_prices()
        target_type = "P" if direction == "Bullish" else "C"

        candidates = []
        if mark_prices:
            for item in mark_prices:
                sym = item.get("symbol", "")
                parts = sym.split("-")
                if len(parts) == 4 and parts[0] == "BTC":
                    exp_sym, strk_str, opt_type = parts[1], parts[2], parts[3]
                    if opt_type == target_type:
                        try:
                            strk = float(strk_str)
                            mark_p = float(item.get("markPrice", 0.0))
                            candidates.append((strk, mark_p, exp_sym, opt_type, sym))
                        except ValueError:
                            pass

        if candidates:
            # Pick nearest active expiry from available Binance expiries
            expiries = sorted(list(set(c[2] for c in candidates)))
            nearest_expiry = expiries[0]
            exp_candidates = [c for c in candidates if c[2] == nearest_expiry]

            if direction == "Bullish":
                itm = [c for c in exp_candidates if c[0] >= futures_mark]
                best = min(itm, key=lambda x: x[0]) if itm else max(exp_candidates, key=lambda x: x[0])
            else:
                itm = [c for c in exp_candidates if c[0] <= futures_mark]
                best = max(itm, key=lambda x: x[0]) if itm else min(exp_candidates, key=lambda x: x[0])

            return (best[0], best[1], best[2], best[4])

        now_dt = datetime.now(ist)
        today_sym = now_dt.strftime("%y%m%d")
        strike = round(futures_mark / 250.0) * 250.0
        return (strike, 150.0, today_sym, f"BTC-{today_sym}-{int(strike)}-{target_type}")

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

    async def execute_slot_entry(
        self, db: Session, role_name: str, role_config: HedgeStrategyConfig, 
        futures_mark: float, spot_price: float
    ) -> Optional[int]:
        """
        Executes atomic slot trade: BUY Option @ option_mark + OPEN Futures @ futures_mark.
        Sets Futures TP = futures_entry ± option_mark.
        """
        qty = role_config.contract_qty
        max_premium = role_config.max_premium
        max_tv = role_config.max_time_value

        current_session_key = get_current_binance_session_date()

        # Strict Single-Trade Per Binance Expiry Session Lock
        if role_name == "1st Trader":
            if self.slot1_traded_session_key == current_session_key:
                logger.info("1st Trader already traded for Binance Expiry Session [%s] - BLOCKED", current_session_key)
                return None
        else:
            if self.slot2_traded_session_key == current_session_key:
                logger.info("2nd Trader already traded for Binance Expiry Session [%s] - BLOCKED", current_session_key)
                return None

        # Determine direction: Check if explicit (Bullish/Bearish) or Auto (evaluate both)
        pref_direction = role_config.direction or "Auto"
        if pref_direction in ["Bullish", "Bearish"]:
            strike, option_mark, expiry_sym, opt_symbol = await self.find_nearest_itm_option(futures_mark, pref_direction)
            direction = pref_direction
        else:
            # Auto-detect direction: evaluate Bullish vs Bearish ITM options
            strike_b, mark_b, exp_b, sym_b = await self.find_nearest_itm_option(futures_mark, "Bullish")
            strike_r, mark_r, exp_r, sym_r = await self.find_nearest_itm_option(futures_mark, "Bearish")
            
            valid_b = (mark_b <= max_premium) and self.validate_time_value(mark_b, strike_b, "PUT", spot_price, max_tv)
            valid_r = (mark_r <= max_premium) and self.validate_time_value(mark_r, strike_r, "CALL", spot_price, max_tv)

            if valid_b and not valid_r:
                direction, strike, option_mark, expiry_sym, opt_symbol = "Bullish", strike_b, mark_b, exp_b, sym_b
            elif valid_r and not valid_b:
                direction, strike, option_mark, expiry_sym, opt_symbol = "Bearish", strike_r, mark_r, exp_r, sym_r
            else:
                # Default to Bullish if both valid
                direction, strike, option_mark, expiry_sym, opt_symbol = "Bullish", strike_b, mark_b, exp_b, sym_b

        # Rule B: Premium Cap Check (option_mark <= max_premium) and Time Value Limit Check
        if option_mark > max_premium:
            logger.warning("Hedge Slot [%s] option_mark $%.2f > max_premium limit $%.2f - REJECTED", role_name, option_mark, max_premium)
            return None

        opt_type_str = "PUT" if direction == "Bullish" else "CALL"
        if not self.validate_time_value(option_mark, strike, opt_type_str, spot_price, max_tv):
            tv_calculated = self.calculate_time_value(option_mark, strike, opt_type_str, spot_price)
            logger.warning("Hedge Slot [%s] Time Value $%.2f > max_time_value limit $%.2f - REJECTED", role_name, tv_calculated, max_tv)
            return None

        now_ist = datetime.now(ist).replace(tzinfo=None)

        # Calculate Futures TP Level based on option_mark
        is_bullish = (direction == "Bullish")
        fut_side = "BUY" if is_bullish else "SELL"
        fut_tp_price = futures_mark + option_mark if is_bullish else futures_mark - option_mark

        # 1. Create HedgeSession
        sess = HedgeSession(
            symbol="BTCUSDT",
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

    async def execute_squareoff(self, db: Session, reason: str = "11:30 AM Universal Squareoff"):
        """Forces 11:30 AM universal market squareoff for all open hedge slots."""
        logger.info("Executing Hedge Universal Squareoff: %s", reason)
        cfg = self.load_config(db)
        now_ist = datetime.now(ist).replace(tzinfo=None)

        futures_mark = await get_btc_futures_mark_price()
        open_positions = db.query(HedgeOpenPosition).all()

        total_realized_pnl = 0.0

        for pos in open_positions:
            qty = pos.qty
            if "FUTURES" in pos.symbol:
                if pos.side == "LONG":
                    pnl = (futures_mark - pos.entry_price) * qty
                else:
                    pnl = (pos.entry_price - futures_mark) * qty
                
                close_order = HedgeTradeOrder(
                    session_id=pos.session_id,
                    symbol=pos.symbol,
                    side="SELL" if pos.side == "LONG" else "BUY",
                    trader_leg=self.active_role,
                    order_type="MARKET",
                    qty=qty,
                    price=futures_mark,
                    status="FILLED",
                    created_at=now_ist
                )
                db.add(close_order)
            else:
                # Option close at market
                pnl = (100.0 - pos.entry_price) * qty  # fallback option close mark
                close_order = HedgeTradeOrder(
                    session_id=pos.session_id,
                    symbol=pos.symbol,
                    side="SELL",
                    trader_leg=self.active_role,
                    order_type="MARKET",
                    qty=qty,
                    price=100.0,
                    status="FILLED",
                    created_at=now_ist
                )
                db.add(close_order)

            total_realized_pnl += pnl
            db.delete(pos)

        open_sessions = db.query(HedgeSession).filter(HedgeSession.status == "Open").all()
        for sess in open_sessions:
            sess.status = "Completed"
            sess.exit_reason = reason
            sess.realized_pnl = total_realized_pnl

        # Update Ledger
        cash_item = db.query(HedgeConfig).filter(HedgeConfig.key == "PAPER_WALLET_USDT").first()
        old_cash = float(cash_item.value) if cash_item else 100000.0
        new_cash = old_cash + total_realized_pnl
        if cash_item:
            cash_item.value = str(new_cash)

        ledger = HedgePaperLedgerEntry(
            session_id=self.slot1_session_id or self.slot2_session_id,
            entry_type="SQUAREOFF_SETTLEMENT",
            amount=total_realized_pnl,
            balance_after=new_cash,
            detail=f"Hedge Squareoff Settlement ({reason}) - Net PnL: ${total_realized_pnl:.2f}",
            created_at=now_ist
        )
        db.add(ledger)

        db.commit()

        if self.slot1_session_id:
            self.slot1_completed = True
        if self.slot2_session_id:
            self.slot2_completed = True

        self.slot1_session_id = None
        self.slot2_session_id = None
        self.slot1_strike = None
        self.slot2_strike = None
        self.tp_rank_1_slot = None
        self.tp_rank_2_slot = None
        self.state = "COMPLETED"

    async def run_loop(self):
        self.is_running = True
        logger.info("Hedge Engine async loop started.")
        while self.is_running:
            try:
                db = SessionLocal()
                cfg = self.load_config(db)
                bot_enabled = cfg.get("BOT_ENABLED", "1") == "1"

                if not bot_enabled:
                    self.state = "DISABLED"
                    db.close()
                    await asyncio.sleep(2.0)
                    continue

                skip_weekends = cfg.get("SKIP_WEEKENDS", "1") == "1"
                if skip_weekends and is_weekend_session():
                    self.state = "SKIP_WEEKEND"
                    db.close()
                    await asyncio.sleep(2.0)
                    continue

                spot_price = await get_btc_spot_price()
                futures_mark = await get_btc_futures_mark_price()
                self.last_spot_price = spot_price
                self.last_futures_mark = futures_mark

                now_time_full = datetime.now(ist).strftime("%H:%M:%S")
                now_time_str = now_time_full[:5]
                now_rel = get_session_relative_minutes(now_time_str)

                slot1_cfg = self.get_role_strategy_config(db, "1st Trader")
                slot2_cfg = self.get_role_strategy_config(db, "2nd Trader")

                # Live options mark price feed polling for active telemetry (both Bullish PUT & Bearish CALL)
                if slot1_cfg:
                    put_strk, put_mark, _, _ = await self.find_nearest_itm_option(futures_mark, "Bullish")
                    call_strk, call_mark, _, _ = await self.find_nearest_itm_option(futures_mark, "Bearish")
                    self.preview_slot1_put_strike = put_strk
                    self.preview_slot1_put_mark = put_mark
                    self.preview_slot1_call_strike = call_strk
                    self.preview_slot1_call_mark = call_mark

                if slot2_cfg:
                    put_strk, put_mark, _, _ = await self.find_nearest_itm_option(futures_mark, "Bullish")
                    call_strk, call_mark, _, _ = await self.find_nearest_itm_option(futures_mark, "Bearish")
                    self.preview_slot2_put_strike = put_strk
                    self.preview_slot2_put_mark = put_mark
                    self.preview_slot2_call_strike = call_strk
                    self.preview_slot2_call_mark = call_mark

                w_start_h = slot1_cfg.trade_start_h if slot1_cfg else 6
                w_start_m = slot1_cfg.trade_start_m if slot1_cfg else 0
                w_end_h = slot1_cfg.trade_end_h if slot1_cfg else 7
                w_end_m = slot1_cfg.trade_end_m if slot1_cfg else 30
                sq_h = slot1_cfg.force_close_h if slot1_cfg else 11
                sq_m = slot1_cfg.force_close_m if slot1_cfg else 30

                w_start_rel = get_session_relative_minutes(f"{w_start_h:02d}:{w_start_m:02d}")
                w_end_rel = get_session_relative_minutes(f"{w_end_h:02d}:{w_end_m:02d}")
                sq_end_rel = get_session_relative_minutes(f"{sq_h:02d}:{sq_m:02d}")

                current_session_key = get_current_binance_session_date()
                if self.active_session_key != current_session_key:
                    # New Binance Expiry Session started! (Rollover past 13:30 PM IST)
                    logger.info("Binance Expiry Session Rollover detected -> New Session Key: %s", current_session_key)
                    self.active_session_key = current_session_key
                    self.slot1_completed = False
                    self.slot2_completed = False
                    self.slot1_traded_session_key = None
                    self.slot2_traded_session_key = None
                    if self.state in ["COMPLETED", "SQUAREOFF"]:
                        self.state = "IDLE"

                # 1. State Transition: Entry Window Active
                if w_start_rel <= now_rel <= w_end_rel and self.state in ["IDLE"]:
                    self.state = "ENTRY_WINDOW"
                    logger.info("Entering Hedge Entry Window (%02d:%02d - %02d:%02d) for Session [%s]", w_start_h, w_start_m, w_end_h, w_end_m, current_session_key)

                # 2. Phase 1: Slot 1 & Slot 2 Entry Evaluation
                if self.state == "ENTRY_WINDOW":
                    # Evaluate Slot 1
                    if not self.slot1_session_id and slot1_cfg and slot1_cfg.enabled:
                        await self.execute_slot_entry(db, "1st Trader", slot1_cfg, futures_mark, spot_price)

                    # Evaluate Slot 2 with Rule C (Clash Check)
                    if not self.slot2_session_id and slot2_cfg and slot2_cfg.enabled:
                        should_evaluate_slot2 = True
                        if self.slot1_session_id and self.slot1_strike is not None:
                            # Rule C Clash Check: Slot 2 strike must be >= Slot 1 strike
                            temp_strike, _, _, _ = await self.find_nearest_itm_option(futures_mark, slot2_cfg.direction)
                            if temp_strike < self.slot1_strike:
                                logger.info("Slot 2 Clash Check Failed: Strike $%.0f < Slot 1 Strike $%.0f", temp_strike, self.slot1_strike)
                                should_evaluate_slot2 = False

                        if should_evaluate_slot2:
                            await self.execute_slot_entry(db, "2nd Trader", slot2_cfg, futures_mark, spot_price)

                    if self.slot1_session_id or self.slot2_session_id:
                        self.state = "IN_TRADE"

                # 3. Phase 3: Universal Squareoff Check
                if self.state in ["SQUAREOFF"] or (now_rel >= sq_end_rel and self.state in ["ENTRY_WINDOW", "IN_TRADE"]):
                    await self.execute_squareoff(db, "11:30 AM Universal Squareoff" if self.state != "SQUAREOFF" else "Manual Emergency Squareoff")

                # 4. Flush Pending Configs on Session Complete
                if self.state == "COMPLETED":
                    self.flush_pending_config_on_session_close(db)

                db.close()
            except Exception as e:
                logger.error("Error in Hedge Engine loop: %s", str(e), exc_info=True)

            await asyncio.sleep(2.0)

    def start(self):
        if not self.is_running:
            self._task = asyncio.create_task(self.run_loop())

    def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()

hedge_engine = HedgeEngine()
