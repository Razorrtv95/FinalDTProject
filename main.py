from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import scheduler
from app.api import leads, twilio_webhook
from app.config import settings
from app.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    scheduler.start()
    logger.info(
        "Started in %s mode — SMS:%s Calendar:%s",
        settings.environment,
        settings.sms_provider,
        settings.calendar_provider,
    )
    yield
    scheduler.shutdown()


app = FastAPI(
    title="AI SMS Appointment Setter",
    description=(
        "Answers seller leads coming from FB by SMS within seconds, qualifies them "
        "against standards set by real estate agents, and books appraisals into a real calendar."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(leads.router)
app.include_router(twilio_webhook.router)

if settings.is_development:
    from app.api import console, dashboard

    app.include_router(console.router)
    app.include_router(dashboard.router)
    logger.info("Dev console enabled at /console, dashboard at /dashboard")


@app.get("/health", tags=["ops"])
def health() -> dict:
    return {
        "status": "ok",
        "environment": settings.environment,
        "sms_provider": settings.sms_provider,
        "calendar_provider": settings.calendar_provider,
    }
