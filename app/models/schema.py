from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, func
from sqlalchemy.orm import declarative_base

Base = declarative_base()

# ---------------------------------------------------------
# 1. USER & SYSTEM AUDIT LOGS
# ---------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(50), default="TRADER")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class ConfigAuditLog(Base):
    __tablename__ = "config_audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String(255), nullable=False)
    config_type = Column(String(50), nullable=False)  # 'STRADDLE' or 'HEDGE'
    field_name = Column(String(100), nullable=False)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    apply_mode = Column(String(50), nullable=False)  # 'IMMEDIATE' or 'DEFERRED_ON_WINDOW_CLOSE'
    status = Column(String(50), default="APPLIED")
    ip_address = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class PendingConfig(Base):
    __tablename__ = "pending_config_queue"

    id = Column(Integer, primary_key=True, index=True)
    config_type = Column(String(50), nullable=False)
    field_name = Column(String(100), nullable=False)
    pending_value = Column(Text, nullable=False)
    user_email = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

# ---------------------------------------------------------
# 2. STRADDLE BOT DOCTYPES (10 DOCTYPES)
# ---------------------------------------------------------
class StraddleConfig(Base):
    __tablename__ = "straddle_config"

    key = Column(String(255), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class StraddleConfigItem(Base):
    __tablename__ = "straddle_config_items"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(255), unique=True, nullable=False)
    value = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    is_secret = Column(Boolean, default=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

class StraddleSession(Base):
    __tablename__ = "straddle_sessions"

    id = Column(Integer, primary_key=True, index=True)
    expiry_sym = Column(String(100), nullable=False)
    expiry_dt = Column(String(100), nullable=False)
    status = Column(String(50), default="OPEN")
    btc_entry_spot = Column(Float, default=0.0)
    btc_entry_mark = Column(Float, default=0.0)
    call_sym = Column(String(100), nullable=True)
    call_strike = Column(Float, default=0.0)
    call_ask = Column(Float, default=0.0)
    put_sym = Column(String(100), nullable=True)
    put_strike = Column(Float, default=0.0)
    put_ask = Column(Float, default=0.0)
    net_straddle_ask = Column(Float, default=0.0)
    futures_entry_price = Column(Float, default=0.0)
    futures_tp_price = Column(Float, default=0.0)
    futures_exit_price = Column(Float, default=0.0)
    opt_call_close_price = Column(Float, default=0.0)
    opt_put_close_price = Column(Float, default=0.0)
    pnl_realized = Column(Float, default=0.0)
    exit_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class StraddleTradeOrder(Base):
    __tablename__ = "straddle_trade_orders"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=True)
    paper_order_id = Column(Integer, unique=True, index=True)
    symbol = Column(String(100), nullable=False)
    asset_type = Column(String(50), nullable=False)
    side = Column(String(50), nullable=False)
    leg_label = Column(String(50), nullable=False)
    order_type = Column(String(50), nullable=False)
    qty = Column(Float, nullable=False)
    price = Column(Float, default=0.0)
    status = Column(String(50), default="SUBMITTED")
    cancel_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class StraddleFill(Base):
    __tablename__ = "straddle_fills"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=False)
    instrument = Column(String(100), nullable=False)
    side = Column(String(50), nullable=False)
    fill_price = Column(Float, nullable=False)
    fill_qty = Column(Float, nullable=False)
    fee = Column(Float, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class StraddleWalletLedger(Base):
    __tablename__ = "straddle_wallet_ledger"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=True)
    entry_type = Column(String(100), nullable=False)
    amount = Column(Float, nullable=False)
    balance_after = Column(Float, nullable=False)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class StraddlePnLSnapshot(Base):
    __tablename__ = "straddle_pnl_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=False)
    btc_mark = Column(Float, default=0.0)
    call_mark = Column(Float, default=0.0)
    put_mark = Column(Float, default=0.0)
    futures_mark = Column(Float, default=0.0)
    unrealized_pnl = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class StraddleSessionEvent(Base):
    __tablename__ = "straddle_session_events"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=False)
    event_type = Column(String(100), nullable=False)
    message = Column(Text, nullable=False)
    payload_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class StraddleRuntimeStatus(Base):
    __tablename__ = "straddle_runtime_status"

    id = Column(Integer, primary_key=True, index=True)
    worker_id = Column(String(100), nullable=False)
    state = Column(String(50), nullable=False)
    last_heartbeat = Column(DateTime(timezone=True), server_default=func.now())
    active_session_id = Column(Integer, nullable=True)

class StraddleRuntimeCommand(Base):
    __tablename__ = "straddle_runtime_commands"

    id = Column(Integer, primary_key=True, index=True)
    command = Column(String(100), nullable=False)
    status = Column(String(50), default="QUEUED")
    payload_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

# ---------------------------------------------------------
# 3. HEDGE TRADER DOCTYPES (12 DOCTYPES)
# ---------------------------------------------------------
class HedgeConfig(Base):
    __tablename__ = "hedge_config"

    key = Column(String(255), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class HedgeStrategyConfig(Base):
    __tablename__ = "hedge_strategy_configs"

    id = Column(Integer, primary_key=True, index=True)
    strategy_name = Column(String(100), unique=True, nullable=False)
    strategy_key = Column(String(100), nullable=False)
    enabled = Column(Boolean, default=True)
    locked = Column(Boolean, default=False)
    strategy_type = Column(String(50), default="Directional Hedge")
    direction = Column(String(50), default="Bullish")
    executor_name = Column(String(100), nullable=True)
    trade_start_h = Column(Integer, default=5)
    trade_start_m = Column(Integer, default=0)
    trade_end_h = Column(Integer, default=7)
    trade_end_m = Column(Integer, default=0)
    force_close_h = Column(Integer, default=13)
    force_close_m = Column(Integer, default=0)
    skip_weekends = Column(Boolean, default=True)
    blackout_dates = Column(Text, nullable=True)
    contract_qty = Column(Float, default=1.0)
    max_premium = Column(Float, default=250.0)
    max_time_value = Column(Float, default=229.0)
    price_diff_percent = Column(Float, default=4.0)
    partial_profit_ratio = Column(Float, default=1.1)
    partial_tp_multiplier = Column(Float, default=1.1)
    rebuy_mode = Column(String(50), default="tv_based")
    extra_config_json = Column(Text, nullable=True)

class HedgeSession(Base):
    __tablename__ = "hedge_sessions"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(50), default="BTCUSDT")
    expiry_session = Column(String(50), nullable=True)
    status = Column(String(50), default="Open")
    bull_entry = Column(Float, default=0.0)
    bear_entry = Column(Float, default=0.0)
    bull_exit = Column(Float, default=0.0)
    bear_exit = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)
    exit_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class HedgeOpenPosition(Base):
    __tablename__ = "hedge_open_positions"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=False)
    symbol = Column(String(100), nullable=False)
    side = Column(String(50), nullable=False)
    entry_price = Column(Float, nullable=False)
    qty = Column(Float, nullable=False)
    leverage = Column(Integer, default=10)
    unrealized_pnl = Column(Float, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class HedgeTradeOrder(Base):
    __tablename__ = "hedge_trade_orders"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=True)
    paper_order_id = Column(Integer, unique=True, index=True)
    symbol = Column(String(100), nullable=False)
    side = Column(String(50), nullable=False)
    trader_leg = Column(String(50), nullable=False)
    order_type = Column(String(50), nullable=False)
    qty = Column(Float, nullable=False)
    price = Column(Float, default=0.0)
    status = Column(String(50), default="Requested")
    cancel_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class HedgeFill(Base):
    __tablename__ = "hedge_fills"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=False)
    trader_leg = Column(String(50), nullable=False)
    side = Column(String(50), nullable=False)
    fill_price = Column(Float, nullable=False)
    fill_qty = Column(Float, nullable=False)
    fee = Column(Float, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class HedgePaperLedgerEntry(Base):
    __tablename__ = "hedge_paper_ledger_entries"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=True)
    entry_type = Column(String(100), nullable=False)
    amount = Column(Float, nullable=False)
    balance_after = Column(Float, nullable=False)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class HedgeMacroEvent(Base):
    __tablename__ = "hedge_macro_events"

    id = Column(Integer, primary_key=True, index=True)
    event_name = Column(String(100), nullable=False)
    event_time = Column(DateTime(timezone=True), nullable=False)
    impact_level = Column(String(50), default="HIGH")
    action_taken = Column(Text, nullable=True)

class HedgeOrderBlockZone(Base):
    __tablename__ = "hedge_order_block_zones"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(50), default="BTCUSDT")
    timeframe = Column(String(20), default="15m")
    zone_type = Column(String(50), nullable=False)  # 'BULLISH_OB' or 'BEARISH_OB'
    high_price = Column(Float, nullable=False)
    low_price = Column(Float, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class HedgeSessionEvent(Base):
    __tablename__ = "hedge_session_events"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=False)
    event_type = Column(String(100), nullable=False)
    message = Column(Text, nullable=False)
    payload_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class HedgeRuntimeStatus(Base):
    __tablename__ = "hedge_runtime_status"

    id = Column(Integer, primary_key=True, index=True)
    worker_id = Column(String(100), nullable=False)
    state = Column(String(50), nullable=False)
    last_heartbeat = Column(DateTime(timezone=True), server_default=func.now())

class HedgeRuntimeCommand(Base):
    __tablename__ = "hedge_runtime_commands"

    id = Column(Integer, primary_key=True, index=True)
    command = Column(String(100), nullable=False)
    status = Column(String(50), default="QUEUED")
    payload_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class HedgeSystemHealthSnapshot(Base):
    __tablename__ = "hedge_system_health_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    healthy = Column(Boolean, default=True)
    database_connected = Column(Boolean, default=True)
    open_positions_count = Column(Integer, default=0)
    issues_count = Column(Integer, default=0)
    snapshot_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
