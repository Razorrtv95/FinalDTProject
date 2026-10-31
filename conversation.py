from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, object_session

from app import compliance
from app.ai.client import get_engine
from app.ai.personas import get_assistant
from app.ai.prompt import build_context_block, build_system_prompt
from app.ai.schema import Action, AgentDecision
from app.config import settings
from app.db import lock_lead
from app.models import Appointment, Direction, Lead, LeadStatus, Message
from app.providers.sms import get_sms_provider
from app.services import handoff, qualification, scheduling

logger = logging.getLogger(__name__)

TRANSCRIPT_TURNS = 20

FOLLOWUP_DELAYS = [timedelta(hours=1), timedelta(days=1), timedelta(days=3)]

FALLBACK_REPLY = (
    "Thanks for getting back to me - let me get one of our agents to give you a "
    "call about that."
)


def opening_message(lead: Lead) -> str:
    first_name = (lead.name or "").split(" ")[0].strip()
    greeting = f"Hi {first_name}" if first_name else "Hi there"
    return (
        f"{greeting}, {get_assistant(object_session(lead)).name} from "
        f"{settings.agency_name} here. "
        "You enquired about selling - are you still looking into it? "
        "Reply STOP to opt out."
    )


def start_conversation(session: Session, lead_id: str) -> None:
    with lock_lead(session, lead_id) as lead:
        if lead is None or lead.opted_out:
            return
        if lead.message_count > 0:
            return
        _send(session, lead, opening_message(lead), ai_action="opening")
        lead.status = LeadStatus.ENGAGING
        _schedule_followup(lead)
        session.commit()


def handle_inbound(
    session: Session, lead_id: str, body: str, provider_sid: str | None = None
) -> None:
    with lock_lead(session, lead_id) as lead:
        if lead is None:
            logger.warning("Inbound for unknown lead_id=%s", lead_id)
            return

        _record_inbound(session, lead, body, provider_sid)

        if compliance.is_opt_out(body):
            _handle_opt_out(session, lead)
            session.commit()
            return

        if lead.opted_out:
            logger.info("Ignoring inbound from opted-out lead=%s", lead.id)
            session.commit()
            return

        if lead.status in {LeadStatus.HANDED_OFF, LeadStatus.BOOKED}:
            logger.info("Lead %s is %s — not auto-replying", lead.id, lead.status.value)
            session.commit()
            return

        decision = _decide(lead, extra_context=None)
        if decision is None:
            _send(session, lead, FALLBACK_REPLY, ai_action="fallback")
            handoff.create(
                session,
                lead,
                reason="AI unavailable",
                summary=f"Could not generate a reply for {lead.phone}. Last message:\n{body}",
            )
            lead.status = LeadStatus.HANDED_OFF
            session.commit()
            return

        _apply_facts(lead, decision)
        assessment = qualification.apply(lead)

        if decision.action is Action.OFFER_SLOTS or (
            decision.action is Action.BOOK and not lead.offered_slots
        ):
            slots = scheduling.propose_slots(count=3)
            lead.offered_slots = slots
            if slots:
                retry = _decide(lead, extra_context=None)
                if retry is not None:
                    decision = retry
                    _apply_facts(lead, decision)
                    assessment = qualification.apply(lead)
            else:
                _send(
                    session,
                    lead,
                    "Happy to get that booked in - I'll have our agent call you "
                    "shortly to lock in a time.",
                    ai_action="no_slots",
                )
                handoff.create(
                    session,
                    lead,
                    reason="No appointment slots available",
                    summary=qualification.summarise(lead, assessment),
                )
                lead.status = LeadStatus.HANDED_OFF
                session.commit()
                return

        _act(session, lead, decision, assessment)
        session.commit()


def _record_inbound(
    session: Session, lead: Lead, body: str, provider_sid: str | None
) -> None:
    now = datetime.now(settings.tz)
    session.add(
        Message(
            lead_id=lead.id,
            direction=Direction.INBOUND,
            body=body,
            provider_sid=provider_sid,
        )
    )
    lead.last_inbound_at = now
    lead.next_followup_at = None
    lead.followup_count = 0
    session.flush()


def _transcript(lead: Lead) -> list[dict[str, str]]:
    recent = lead.messages[-TRANSCRIPT_TURNS:]
    return [
        {
            "role": "user" if m.direction is Direction.INBOUND else "assistant",
            "content": m.body,
        }
        for m in recent
        if m.body
    ]


def _decide(lead: Lead, extra_context: str | None) -> AgentDecision | None:
    transcript = _transcript(lead)
    recent_inbound = " ".join(
        m["content"] for m in transcript[-4:] if m["role"] == "user"
    )
    snippets = []
    context = build_context_block(
        lead=lead, knowledge=snippets, slots=lead.offered_slots
    )
    if extra_context:
        context = f"{context}\n\n{extra_context}"

    try:
        return get_engine().decide(
            system_prompt=build_system_prompt(
                assistant=get_assistant(object_session(lead))
            ),
            context=context,
            transcript=transcript,
        )
    except Exception as exc:
        logger.error("Decision failed for lead=%s: %s", lead.id, exc)
        return None


def _apply_facts(lead: Lead, decision: AgentDecision) -> None:
    facts = decision.facts

    for field_name in (
        "property_address",
        "suburb",
        "motivation",
        "price_expectation",
        "is_owner",
        "has_had_appraisal",
        "already_listed",
    ):
        value = getattr(facts, field_name)
        if value is not None and value != "":
            setattr(lead, field_name, value)

    if facts.timeframe and facts.timeframe != "unknown":
        lead.timeframe = facts.timeframe


def _act(
    session: Session,
    lead: Lead,
    decision: AgentDecision,
    assessment: qualification.Assessment,
) -> None:
    action = decision.action

    if action is Action.OPT_OUT:
        _handle_opt_out(session, lead)
        return

    if action is Action.BOOK:
        if _try_book(session, lead, decision, assessment):
            return
        lead.offered_slots = scheduling.propose_slots(count=3)
        labels = " or ".join(s["label"] for s in (lead.offered_slots or []))
        _send(
            session,
            lead,
            f"That one's just been taken, sorry. Would {labels} work instead?"
            if labels
            else "That time's no longer free - I'll have our agent call you to sort out a time.",
            ai_action="reoffer_slots",
        )
        return

    if action is Action.HANDOFF:
        reason = decision.handoff_reason or "Escalated by AI"
        if decision.reply_text:
            _send(session, lead, decision.reply_text, ai_action=action.value)
        handoff.create(
            session,
            lead,
            reason=reason,
            summary=qualification.summarise(lead, assessment),
        )
        lead.status = LeadStatus.HANDED_OFF
        return

    if action is Action.CLOSE:
        if decision.reply_text:
            _send(session, lead, decision.reply_text, ai_action=action.value)
        lead.next_followup_at = None
        return

    if decision.reply_text:
        _send(session, lead, decision.reply_text, ai_action=action.value)
    _schedule_followup(lead)

    if assessment.is_qualified:
        handoff.create(
            session,
            lead,
            reason="Qualified lead",
            summary=qualification.summarise(lead, assessment),
        )


def _try_book(
    session: Session,
    lead: Lead,
    decision: AgentDecision,
    assessment: qualification.Assessment,
) -> bool:
    slot = scheduling.find_offered_slot(decision.chosen_slot_id or "", lead.offered_slots)
    if slot is None:
        logger.warning(
            "Model chose slot_id=%s which was never offered to lead=%s",
            decision.chosen_slot_id,
            lead.id,
        )
        return False

    notes = qualification.summarise(lead, assessment)
    try:
        start, end, event_id = scheduling.book(
            slot=slot,
            lead_name=lead.name or "",
            lead_phone=lead.phone,
            notes=notes,
        )
    except Exception as exc:
        logger.error("Calendar booking failed for lead=%s: %s", lead.id, exc)
        return False

    session.add(
        Appointment(
            lead_id=lead.id, start_at=start, end_at=end, calendar_event_id=event_id
        )
    )
    lead.status = LeadStatus.BOOKED
    lead.offered_slots = None
    lead.next_followup_at = None

    confirmation = decision.reply_text or (
        f"Booked in for {slot['label']}. You'll get a reminder beforehand."
    )
    _send(session, lead, confirmation, ai_action="book")

    handoff.create(
        session,
        lead,
        reason="Appraisal booked",
        summary=f"{notes}\n\nAppraisal: {slot['label']}",
    )
    return True


def _handle_opt_out(session: Session, lead: Lead) -> None:
    lead.opted_out = True
    lead.status = LeadStatus.OPTED_OUT
    lead.next_followup_at = None
    lead.offered_slots = None

    body = compliance.to_gsm7(compliance.opt_out_confirmation())
    try:
        sid = get_sms_provider().send(to=lead.phone, body=body)
    except Exception as exc:
        logger.error("Failed to send opt-out confirmation to %s: %s", lead.phone, exc)
        sid = None

    session.add(
        Message(
            lead_id=lead.id,
            direction=Direction.OUTBOUND,
            body=body,
            provider_sid=sid,
            ai_action="opt_out",
        )
    )
    lead.message_count += 1
    lead.last_outbound_at = datetime.now(settings.tz)
    logger.info("Lead %s opted out", lead.id)


def _send(session: Session, lead: Lead, body: str, *, ai_action: str) -> bool:
    body = compliance.sign_message(
        body,
        first_message=lead.message_count == 0,
        name=get_assistant(session).name,
    )
    body = compliance.to_gsm7(body)
    decision = compliance.check_can_send(lead)

    if not decision.allowed and not decision.should_queue:
        logger.info("Suppressed message to %s: %s", lead.phone, decision.reason)
        return False

    if decision.should_queue:
        session.add(
            Message(
                lead_id=lead.id,
                direction=Direction.OUTBOUND,
                body=body,
                ai_action=ai_action,
                queued=True,
            )
        )
        lead.next_followup_at = decision.queue_until
        logger.info("Queued message to %s until %s", lead.phone, decision.queue_until)
        return False

    try:
        sid = get_sms_provider().send(to=lead.phone, body=body)
    except Exception as exc:
        logger.error("SMS send failed to %s: %s", lead.phone, exc)
        return False

    logger.info(
        "sent lead=%s action=%s chars=%s segments=%s",
        lead.id, ai_action, len(body), compliance.segment_count(body),
    )

    session.add(
        Message(
            lead_id=lead.id,
            direction=Direction.OUTBOUND,
            body=body,
            provider_sid=sid,
            ai_action=ai_action,
        )
    )
    lead.message_count += 1
    lead.last_outbound_at = datetime.now(settings.tz)
    session.flush()
    return True


def _schedule_followup(lead: Lead) -> None:
    if lead.followup_count >= len(FOLLOWUP_DELAYS):
        lead.next_followup_at = None
        return
    delay = FOLLOWUP_DELAYS[lead.followup_count]
    lead.next_followup_at = datetime.now(settings.tz) + delay


def send_queued_and_followups(session: Session, now: datetime | None = None) -> int:
    from sqlalchemy import select

    now = now or datetime.now(settings.tz)
    sent = 0

    due = session.execute(
        select(Lead).where(
            Lead.next_followup_at.is_not(None),
            Lead.next_followup_at <= now,
            Lead.opted_out.is_(False),
        )
    ).scalars()

    for lead in due:
        queued = [m for m in lead.messages if m.queued]
        if queued:
            for message in queued:
                if _send(session, lead, message.body, ai_action=message.ai_action or "queued"):
                    session.delete(message)
                    sent += 1
            lead.next_followup_at = None
            continue

        if lead.status in {
            LeadStatus.BOOKED,
            LeadStatus.HANDED_OFF,
            LeadStatus.OPTED_OUT,
            LeadStatus.DISQUALIFIED,
        }:
            lead.next_followup_at = None
            continue

        if _send(session, lead, _followup_text(lead), ai_action="followup"):
            sent += 1
            lead.followup_count += 1
            _schedule_followup(lead)

    session.commit()
    return sent


FOLLOWUP_TEXTS = [
    "Just wondering if you got my last message - still thinking about selling?",
    "Don't wanna rush you. Happy to answer anything, or I'll leave it with you.",
    "Reply any time if things change. I'll leave it there so I'm not bothering you...",
]


def _followup_text(lead: Lead) -> str:
    index = min(lead.followup_count, len(FOLLOWUP_TEXTS) - 1)
    return FOLLOWUP_TEXTS[index]
