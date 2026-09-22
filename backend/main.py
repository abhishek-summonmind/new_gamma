from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .app.api import refresh_service, router
from .app.config import (
    get_settings,
    log_market_data_credential_status,
    validate_market_data_credentials,
    validate_live_market_data_connection,
)
from .app.db import init_db
from .app.services.scheduler_service import RefreshScheduler
from .app.utils.logger import configure_logging

logger = logging.getLogger(__name__)

settings = get_settings()
configure_logging(settings.log_level)
log_market_data_credential_status(settings)


scheduler = RefreshScheduler(
    refresh_service=refresh_service,
    interval_seconds=settings.refresh_interval_seconds,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Fail startup before scheduling or serving any request when the selected
    # provider cannot authenticate. No credential values are logged.
    validate_market_data_credentials(settings)
    init_db()
    refresh_service.restore_s9_top_opportunities_from_db(limit=6)
    validate_live_market_data_connection(settings)

    logger.info(
        "FastAPI startup starting refresh scheduler service_id=%s scheduler_service_id=%s cache_id=%s same_instance=%s",
        id(refresh_service),
        id(scheduler._refresh_service),  # noqa: SLF001
        id(refresh_service._cache),  # noqa: SLF001
        scheduler._refresh_service is refresh_service,  # noqa: SLF001
    )
    await scheduler.start()
    if settings.run_refresh_on_startup:
        asyncio.create_task(
            asyncio.to_thread(refresh_service.run_refresh, trigger="startup", force=True),
            name="startup-refresh",
        )
    try:
        yield
    finally:
        await scheduler.stop()
        print(" ")


app = FastAPI(
    title=settings.app_name,
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Primary API routes 
app.include_router(router)

# Backward-compatible legacy prefix.
app.include_router(router, prefix="/api", include_in_schema=False)




