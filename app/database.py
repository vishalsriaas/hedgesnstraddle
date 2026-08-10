import logging
import hashlib
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from app.config import settings
from app.models.schema import Base, User, StraddleConfig, HedgeConfig, HedgeStrategyConfig

logger = logging.getLogger("hedgesnstraddle.database")

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    echo=False
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_database():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # 1. Seed Default Admin User if not exists
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            pw_hash = hashlib.sha256(("Admin@123" + settings.SECRET_KEY).encode()).hexdigest()
            admin_user = User(
                username="admin",
                email="admin@hedgesnstraddle.com",
                password_hash=pw_hash,
                role="ADMIN",
                is_active=True
            )
            db.add(admin_user)
            logger.info("Seeded default admin user: admin / Admin@123")

        # 2. Complete Straddle Bot Settings Seed
        default_straddle_config = {
            "RUNTIME_MODE": "Paper",
            "BOT_ENABLED": "1",
            "PAPER_TRADING_ENABLED": "1",
            "WORKER_ID": "straddle-worker-1",
            "BINANCE_API_KEY": "",
            "BINANCE_SECRET_KEY": "",
            "TELEGRAM_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
            "WINDOW_START": "18:50",
            "WINDOW_END": "18:55",
            "FUTURES_ENTRY_CUTOFF": "18:56",
            "SQ_START": "18:57",
            "SQ_END": "19:02",
            "FUTURES_SQUAREOFF": "19:02",
            "STRADDLE_EXPIRY_TIME": "19:00",
            "TRADE_QTY": "0.1",
            "MIN_EXPIRY_HOURS": "0.0",
            "MAX_TOTAL_MARK": "1500.0",
            "MAX_PREMIUM_GAP": "150.0",
            "SKIP_WEEKENDS": "1",
            "FUTURES_TP_MULTIPLIER": "1.0",
            "SCAN_INTERVAL": "1.0",
            "RETRY_TIMEOUT": "5.0",
            "FUTURES_LEVERAGE": "10",
            "FUTURES_MM_RATE": "0.004",
            "PAPER_WALLET_USDT": "100000.0",
            "NOTES": "Straddle Bot Default Configuration"
        }
        for k, v in default_straddle_config.items():
            existing = db.query(StraddleConfig).filter(StraddleConfig.key == k).first()
            if not existing:
                db.add(StraddleConfig(key=k, value=str(v)))

        # 3. Complete Hedge Trader Settings Seed
        default_hedge_config = {
            "RUNTIME_MODE": "Paper",
            "ENGINE_ENABLED": "1",
            "PAPER_TRADING_ENABLED": "1",
            "WORKER_ID": "hedge-worker-1",
            "WORKER_POLL_SECONDS": "2",
            "COMMAND_TIMEOUT_SECONDS": "120",
            "GLOBAL_PAUSE": "0",
            "MAX_OPTION_SPEND": "400.0",
            "VIRTUAL_BALANCE_USDT": "100000.0",
            "MIN_PAPER_BALANCE": "1000.0",
            "Q_MAX_BTC": "1000.0",
            "SAFE_MODE_TIMEOUT_SEC": "5",
            "LATENCY_WARN_MS": "500",
            "FILL_TIMEOUT_SEC": "5",
            "SYMBOL": "BTCUSDT",
            "LEVERAGE": "10",
            "TRADE_QTY": "0.05",
            "BULL_TARGET_PCT": "0.5",
            "BEAR_TARGET_PCT": "0.5",
            "PAPER_WALLET_USDT": "100000.0",
            "BOT_ENABLED": "1",
            "NOTES": "Hedge Trader Default Configuration"
        }
        for k, v in default_hedge_config.items():
            existing = db.query(HedgeConfig).filter(HedgeConfig.key == k).first()
            if not existing:
                db.add(HedgeConfig(key=k, value=str(v)))

        # Clean legacy hedge strategy config rows if any exist
        legacy_names = ["Default Directional Hedge", "Bullish Hedge", "Bearish Hedge"]
        for leg_name in legacy_names:
            old_row = db.query(HedgeStrategyConfig).filter(HedgeStrategyConfig.strategy_name == leg_name).first()
            if old_row:
                db.delete(old_row)
        db.commit()

        # 4. Seed Role-Based Hedge Trader Strategy Config Rules (1st Trader & 2nd Trader)
        strategies = [
            {
                "strategy_name": "1st Trader",
                "strategy_key": "first_trader",
                "direction": "1st Trader Role",
                "trade_start_h": 5, "trade_start_m": 0,
                "trade_end_h": 7, "trade_end_m": 0,
                "force_close_h": 13, "force_close_m": 0,
                "contract_qty": 1.0,
                "max_premium": 250.0,
                "max_time_value": 229.0
            },
            {
                "strategy_name": "2nd Trader",
                "strategy_key": "second_trader",
                "direction": "2nd Trader Role",
                "trade_start_h": 7, "trade_start_m": 0,
                "trade_end_h": 11, "trade_end_m": 0,
                "force_close_h": 17, "force_close_m": 0,
                "contract_qty": 1.0,
                "max_premium": 400.0,
                "max_time_value": 350.0
            }
        ]
        for s in strategies:
            existing = db.query(HedgeStrategyConfig).filter(HedgeStrategyConfig.strategy_name == s["strategy_name"]).first()
            if not existing:
                strat = HedgeStrategyConfig(
                    strategy_name=s["strategy_name"],
                    strategy_key=s["strategy_key"],
                    enabled=True,
                    direction=s["direction"],
                    trade_start_h=s["trade_start_h"],
                    trade_start_m=s["trade_start_m"],
                    trade_end_h=s["trade_end_h"],
                    trade_end_m=s["trade_end_m"],
                    force_close_h=s["force_close_h"],
                    force_close_m=s["force_close_m"],
                    contract_qty=s["contract_qty"],
                    max_premium=s["max_premium"],
                    max_time_value=s["max_time_value"]
                )
                db.add(strat)

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("Error seeding initial database parameters: %s", str(e))
    finally:
        db.close()
