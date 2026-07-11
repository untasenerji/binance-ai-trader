from collections.abc import Sequence

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.control_plane import router as control_plane_router
from app.api.routes.health import router as health_router
from app.api.routes.metrics import router as metrics_router
from app.api.routes.risk_preview import router as risk_preview_router

LOCAL_DEVELOPMENT_ORIGINS: Sequence[str] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
)

app = FastAPI(
    title="Binance AI Trader API",
    version="0.1.0",
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=LOCAL_DEVELOPMENT_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["Content-Type"],
)

app.include_router(health_router, prefix="/api")
app.include_router(control_plane_router, prefix="/api")
app.include_router(metrics_router, prefix="/api")
app.include_router(risk_preview_router, prefix="/api")
