from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class LeadStatus(str, enum.Enum):
    NEW = "new" 
    ENGAGING = "engaging" 
    QUALIFIED = "qualified"
    NURTURE = "nurture"
    DISQUALIFIED = "disqualified"
    BOOKED = "booked"
    HANDED_OFF = "handed_off"
    OPTED_OUT = "opted_out"


class Direction(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    phone: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(64), default="manual")

    status: Mapped[LeadStatus] = mapped_column(
        Enum(LeadStatus, native_enum=False, length=32), default=LeadStatus.NEW
    )

    property_address: Mapped[str | None] = mapped_column(String(255))
    suburb: Mapped[str | None] = mapped_column(String(120))
    timeframe: Mapped[str | None] = mapped_column(String(32))
    motivation: Mapped[str | None] = mapped_column(Text)
    is_owner: Mapped[bool | None] = mapped_column(Boolean)
    has_had_appraisal: Mapped[bool | None] = mapped_column(Boolean)
    already_listed: Mapped[bool | None] = mapped_column(Boolean)
    price_expectation: Mapped[str | None] = mapped_column(String(120))

    score: Mapped[int] = mapped_column(Integer, default=0)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    offered_slots: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    handoff_reason: Mapped[str | None] = mapped_column(String(255))
    timezone_name: Mapped[str] = mapped_column(String(64), default="Australia/Sydney")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    followup_count: Mapped[int] = mapped_column(Integer, default=0)

    messages: Mapped[list[Message]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", order_by="Message.created_at"
    )
    appointments: Mapped[list[Appointment]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Lead {self.phone} {self.status.value} score={self.score}>"


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[Direction] = mapped_column(
        Enum(Direction, native_enum=False, length=16)
    )
    body: Mapped[str] = mapped_column(Text)

    provider_sid: Mapped[str | None] = mapped_column(String(64), index=True)
    ai_action: Mapped[str | None] = mapped_column(String(32))
    queued: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lead: Mapped[Lead] = relationship(back_populates="messages")


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    calendar_event_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="booked")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lead: Mapped[Lead] = relationship(back_populates="appointments")


class Handoff(Base):
    __tablename__ = "handoffs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text, default="")
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssistantSettings(Base):
    __tablename__ = "assistant_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    assistant_name: Mapped[str | None] = mapped_column(String(20))
    personality: Mapped[str] = mapped_column(String(32), default="friendly")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class WebhookEvent(Base):

    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("provider", "external_id", name="uq_webhook_event"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
