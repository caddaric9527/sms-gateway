"""FastAPI 入口。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import db
from .api import router
from .config import settings
from .service import gateway

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    await gateway.start()
    try:
        yield
    finally:
        await gateway.stop()


app = FastAPI(title="SMS Gateway", version="1.1.6", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")


# 必须注册在静态目录挂载之前，否则会被 "/" 的 StaticFiles 抢走
@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "connected": gateway.state.get("connected", False)}


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8080, log_level="info")
