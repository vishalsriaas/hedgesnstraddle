from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, func
from sqlalchemy.orm import declarative_base

Base = declarative_base()

# 1. User Management
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(50), default="TRADER")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

# 2. Config Audit Trail
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

# 3. Pending Deferred Config Queue
class PendingConfig(Base):
    __tablename__ = "pending_config_queue"

    id = Column(Integer, primary_key=True, index=True)
    config_type = Column(String(50), nullable=False)
    field_name = Column(String(100), nullable=False)
    pending_value = Column(Text, nullable=False)
    user_email = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

# 4. Straddle & Hedge Config Tables
class StraddleConfig(Base):
    __tablename__ = "straddle_config"

    key = Column(String(255), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class HedgeConfig(Base):
    __tablename__ = "hedge_config"

    key = Column(String(255), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

# 5. Straddle Trading Session
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

# 6. Hedge Trading Session
class HedgeSession(Base):
    __tablename__ = "hedge_sessions"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(50), default="BTCUSDT")
    status = Column(String(50), default="Open")
    bull_entry = Column(Float, default=0.0)
    bear_entry = Column(Float, default=0.0)
    bull_exit = Column(Float, default=0.0)
    bear_exit = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)
    exit_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

# 7. Trade Orders
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

# 8. Fills Audit
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

# 9. Wallet Ledger
class StraddleWalletLedger(Base):
    __tablename__ = "straddle_wallet_ledger"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=True)
    entry_type = Column(String(100), nullable=False)
    amount = Column(Float, nullable=False)
    balance_after = Column(Float, nullable=False)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

# 10. PnL Snapshots
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
