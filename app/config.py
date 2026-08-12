import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

class Settings:
    PROJECT_NAME: str = "Hedgesnstraddle"
    VERSION: str = "2.0.0"
    
    SECRET_KEY: str = os.getenv("SECRET_KEY", "hedgesnstraddle_super_secret_jwt_key_2026_x99")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 Days
    
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", 
        f"sqlite:///{BASE_DIR}/hedgesnstraddle.db?check_same_thread=False"
    )

settings = Settings()
