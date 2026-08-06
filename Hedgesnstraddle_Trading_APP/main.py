import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.config import settings
from app.database import init_database
from app.api.auth_routes import router as auth_router
from app.api.dashboard_routes import router as dashboard_router
from app.api.config_routes import router as config_router
from app.api.audit_routes import router as audit_router
from app.api.report_routes import router as report_router
from app.core.straddle_engine import straddle_engine
from app.core.hedge_engine import hedge_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("hedgesnstraddle")

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="Enterprise Zero-Downtime Quantitative Trading Platform"
)

STATIC_DIR = Path(__file__).resolve().parent / "app" / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(config_router)
app.include_router(audit_router)
app.include_router(report_router)

@app.get("/")
async def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))

@app.on_event("startup")
async def startup_event():
    logger.info("Initializing Hedgesnstraddle database and engines...")
    init_database()
    straddle_engine.start()
    hedge_engine.start()
    logger.info("Hedgesnstraddle Platform Online on port 8080!")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down Hedgesnstraddle engines...")
    straddle_engine.stop()
    hedge_engine.stop()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
