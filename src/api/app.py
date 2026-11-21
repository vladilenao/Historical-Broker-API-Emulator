"""Фабрика FastAPI-приложения и настройка OpenAPI."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import yaml
from fastapi import FastAPI

from src import __version__
from src.api.deps import ApiDeps
from src.api.errors import register_error_handlers
from src.api.read_snapshots import ReadSnapshotStore
from src.api.routes import (
    candles,
    coverage,
    data_quality,
    downloads,
    health,
    instrument_api,
    instruments,
    inventory,
    market_import,
    trading_schedules,
)
from src.storage.schedule_repository import ScheduleRepository

_APP_NAME = "Historical Broker API Emulator"


def create_app(deps: ApiDeps) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if deps.market_import is not None:
            deps.market_import.start()
        try:
            yield
        finally:
            if deps.market_import is not None:
                deps.market_import.close()

    app = FastAPI(
        title=_APP_NAME,
        version=__version__,
        lifespan=lifespan,
    )
    # Тесты и встраиваемые пользователи могут собирать ApiDeps напрямую.
    if deps.read_snapshots is None:
        deps = replace(deps, read_snapshots=ReadSnapshotStore(deps.instruments, deps.candles))
    if deps.schedules is None:
        deps = replace(deps, schedules=ScheduleRepository(deps.candles.connection))
    app.state.deps = deps
    register_error_handlers(app)
    app.include_router(health.router)
    # Tinkoff-compatible UID surface for the currently deployed My_roBot.
    app.include_router(candles.router)
    app.include_router(coverage.router)
    app.include_router(data_quality.router)
    app.include_router(instruments.router)
    app.include_router(instrument_api.router)
    app.include_router(downloads.router)
    app.include_router(inventory.router)
    app.include_router(market_import.router)
    app.include_router(trading_schedules.router)
    # docs/openapi.yaml is the reviewed public contract and backs /openapi.json and /docs.
    def documented_openapi() -> dict:
        if app.openapi_schema is None:
            path = Path(__file__).resolve().parents[2] / "docs" / "openapi.yaml"
            app.openapi_schema = yaml.safe_load(path.read_text(encoding="utf-8"))
        return app.openapi_schema

    app.openapi = documented_openapi
    return app
