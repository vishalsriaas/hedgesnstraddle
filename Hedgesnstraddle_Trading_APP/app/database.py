import logging
import hashlib
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from app.config import settings
from app.models.schema import Base, User, StraddleConfig, HedgeConfig

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
        # Seed Default Admin User if not exists
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

        # Seed Default Straddle Configurations
        default_straddle_config = {
            "WINDOW_START": "18:50",
            "WINDOW_END": "18:55",
            "FUTURES_ENTRY_CUTOFF": "18:56",
            "SQ_START": "18:57",
            "SQ_END": "19:02",
            "FUTURES_SQUAREOFF": "19:02",
            "STRADDLE_EXPIRY_TIME": "19:00",
            "TRADE_QTY": "0.1",
            "MIN_EXPIRY_HOURS": "0.0",
            "MIN_STRIKE_GAP": "50.0",
            "MAX_TOTAL_MARK": "1500.0",
            "MAX_PREMIUM_GAP": "500.0",
            "FUTURES_TP_MULTIPLIER": "1.0",
            "SCAN_INTERVAL": "1.0",
            "RETRY_TIMEOUT": "5.0",
            "FUTURES_LEVERAGE": "10",
            "FUTURES_MM_RATE": "0.004",
            "PAPER_WALLET_USDT": "100000.0",
            "BOT_ENABLED": "1"
        }
        for k, v in default_straddle_config.items():
            existing = db.query(StraddleConfig).filter(StraddleConfig.key == k).first()
            if not existing:
                db.add(StraddleConfig(key=k, value=str(v)))

        # Seed Default Hedge Configurations
        default_hedge_config = {
            "SYMBOL": "BTCUSDT",
            "LEVERAGE": "10",
            "TRADE_QTY": "0.05",
            "BULL_TARGET_PCT": "0.5",
            "BEAR_TARGET_PCT": "0.5",
            "PAPER_WALLET_USDT": "100000.0",
            "BOT_ENABLED": "1"
        }
        for k, v in default_hedge_config.items():
            existing = db.query(HedgeConfig).filter(HedgeConfig.key == k).first()
            if not existing:
                db.add(HedgeConfig(key=k, value=str(v)))

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("Error seeding initial database parameters: %s", str(e))
    finally:
        db.close()
