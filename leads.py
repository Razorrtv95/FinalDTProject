from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session, session_scope
from app.models import Lead, LeadStatus
from app.services.conversation import start_conversation
from app.utils import normalise_phone

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/leads", tags=["leads"])


class LeadIn(BaseModel):
    phone: str
    name: str | None = None
    source: str = "manual"
    suburb: str | None = None
    property_address: str | None = None
    timezone_name: str | None = Field(
        default=None, description="Defaults to the agency timezone."
    )


class LeadOut(BaseModel):
    id: str
    phone: str
    name: str | None
    status: str
    score: int
    created: bool


def _start_conversation_task(lead_id: str) -> None:
    try:
        with session_scope() as session:
            start_conversation(session, lead_id)
    except Exception:
        logger.exception("Failed to start conversation for lead=%s", lead_id)


@router.post("", response_model=LeadOut, status_code=201)
def create_lead(
    payload: LeadIn,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
) -> LeadOut:
    try:
        phone = normalise_phone(payload.phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing = session.execute(
        select(Lead).where(Lead.phone == phone)
    ).scalar_one_or_none()

    if existing is not None:
        logger.info("Duplicate lead for %s (status=%s)", phone, existing.status.value)
        return LeadOut(
            id=existing.id,
            phone=existing.phone,
            name=existing.name,
            status=existing.status.value,
            score=existing.score,
            created=False,
        )

    lead = Lead(
        phone=phone,
        name=payload.name,
        source=payload.source,
        suburb=payload.suburb,
        property_address=payload.property_address,
        timezone_name=payload.timezone_name or settings.timezone,
        status=LeadStatus.NEW,
    )
    session.add(lead)
    session.commit()

    background.add_task(_start_conversation_task, lead.id)

    return LeadOut(
        id=lead.id,
        phone=lead.phone,
        name=lead.name,
        status=lead.status.value,
        score=lead.score,
        created=True,
    )


@router.get("/{phone}", response_model=dict)
def get_lead(phone: str, session: Session = Depends(get_session)) -> dict:
    lead = session.execute(
        select(Lead).where(Lead.phone == normalise_phone(phone))
    ).scalar_one_or_none()
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    return lead_detail(lead)


def lead_detail(lead: Lead) -> dict:
    return {
        "id": lead.id,
        "phone": lead.phone,
        "name": lead.name,
        "status": lead.status.value,
        "score": lead.score,
        "facts": {
            "property_address": lead.property_address,
            "suburb": lead.suburb,
            "timeframe": lead.timeframe,
            "motivation": lead.motivation,
            "is_owner": lead.is_owner,
            "has_had_appraisal": lead.has_had_appraisal,
            "already_listed": lead.already_listed,
            "price_expectation": lead.price_expectation,
        },
        "opted_out": lead.opted_out,
        "message_count": lead.message_count,
        "transcript": [
            {
                "direction": m.direction.value,
                "body": m.body,
                "at": m.created_at.isoformat(),
                "queued": m.queued,
            }
            for m in lead.messages
        ],
        "appointments": [
            {
                "start": a.start_at.isoformat(),
                "end": a.end_at.isoformat(),
                "status": a.status,
                "calendar_event_id": a.calendar_event_id,
            }
            for a in lead.appointments
        ],
    }
