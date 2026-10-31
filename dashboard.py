from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.ai.personas import PERSONALITIES, get_assistant, save_assistant
from app.api.leads import lead_detail
from app.config import settings
from app.db import get_session
from app.models import Appointment, Handoff, Lead, LeadStatus, Message
from app.utils import normalise_phone

router = APIRouter(prefix="/dashboard", tags=["dashboard"], include_in_schema=False)

templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")


def _local(value: datetime | str | None) -> str:
    if not value:
        return "-"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(settings.tz).strftime("%d %b %I:%M %p")


templates.env.filters["local"] = _local


@router.get("", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    counts = dict(
        session.execute(select(Lead.status, func.count()).group_by(Lead.status)).all()
    )
    leads = session.execute(
        select(Lead)
        .options(selectinload(Lead.messages))
        .order_by(Lead.updated_at.desc())
        .limit(50)
    ).scalars().all()
    appointments = session.execute(
        select(Appointment, Lead)
        .join(Lead, Appointment.lead_id == Lead.id)
        .where(Appointment.start_at >= datetime.now(timezone.utc))
        .order_by(Appointment.start_at)
        .limit(10)
    ).all()
    handoffs = session.execute(
        select(Handoff, Lead)
        .join(Lead, Handoff.lead_id == Lead.id)
        .order_by(Handoff.created_at.desc())
        .limit(10)
    ).all()
    queued = session.scalar(
        select(func.count()).select_from(Message).where(Message.queued.is_(True))
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "statuses": [(s.value, counts.get(s, 0)) for s in LeadStatus],
            "leads": leads,
            "appointments": appointments,
            "handoffs": handoffs,
            "queued": queued,
            "assistant": get_assistant(session),
        },
    )


@router.get("/leads/{phone}", response_class=HTMLResponse)
def lead_page(
    phone: str, request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    lead = session.execute(
        select(Lead).where(Lead.phone == normalise_phone(phone))
    ).scalar_one_or_none()
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    return templates.TemplateResponse(request, "lead.html", {"lead": lead_detail(lead)})


def _settings_page(
    request: Request, session: Session, *, error: str = "", saved: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "assistant": get_assistant(session),
            "personalities": PERSONALITIES,
            "agent_name": settings.agent_name,
            "error": error,
            "saved": saved,
        },
        status_code=status_code,
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request, saved: bool = False, session: Session = Depends(get_session)
) -> HTMLResponse:
    return _settings_page(request, session, saved=saved)


@router.post("/settings", response_class=HTMLResponse, response_model=None)
def save_settings(
    request: Request,
    assistant_name: str = Form(...),
    personality: str = Form(...),
    session: Session = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    try:
        save_assistant(session, name=assistant_name, personality=personality)
    except ValueError as exc:
        return _settings_page(request, session, error=str(exc), status_code=400)
    return RedirectResponse("/dashboard/settings?saved=1", status_code=303)
