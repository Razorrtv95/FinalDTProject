from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import settings
from app.models import Lead

OPT_OUT_KEYWORDS = {
    "stop", "stopall", "unsubscribe", "unsub", "cancel", "end", "quit",
    "optout", "opt out", "no more",
}

OPT_OUT_PHRASES = (
    "unsubscribe",
    "opt out",
    "optout",
    "remove me",
    "remove my number",
    "delete my number",
    "take me off",
    "do not contact",
    "dont contact",
    "do not text",
    "dont text",
    "do not message",
    "dont message",
    "stop texting",
    "stop messaging",
    "stop contacting",
    "stop sending",
    "leave me alone",
    "lose my number",
    "not interested at all",
)

_FILLER = {"please", "just", "now", "thanks", "thank", "you", "ok", "okay", "pls", "immediately"}

_PUNCT = re.compile(r"[^a-z0-9\s']")

OPT_OUT_CONFIRMATION = (
    "You won't hear from us again. Sorry for the interruption - {agency}."
)


def is_opt_out(body: str) -> bool:

    cleaned = " ".join(_PUNCT.sub(" ", (body or "").lower()).split())
    if not cleaned:
        return False

    if cleaned in OPT_OUT_KEYWORDS:
        return True
    stripped = " ".join(w for w in cleaned.split() if w not in _FILLER)
    if stripped in OPT_OUT_KEYWORDS:
        return True

    return any(phrase in cleaned for phrase in OPT_OUT_PHRASES)


def opt_out_confirmation() -> str:
    return OPT_OUT_CONFIRMATION.format(agency=settings.agency_name)


def sign_message(body: str, *, first_message: bool, name: str | None = None) -> str:
    if not first_message:
        return body
    name = name or settings.agent_name
    suffix = f" - {name}, {settings.agency_name}. Reply STOP to opt out."
    if settings.agency_name.lower() in body.lower() and "stop" in body.lower():
        return body
    return body.rstrip() + suffix


def in_quiet_hours(when: datetime, tz: ZoneInfo | None = None) -> bool:
    tz = tz or settings.tz
    hour = when.astimezone(tz).hour
    start, end = settings.quiet_hours_start, settings.quiet_hours_end
    if start > end: 
        return hour >= start or hour < end
    return start <= hour < end


def next_allowed_send(when: datetime, tz: ZoneInfo | None = None) -> datetime:
    tz = tz or settings.tz
    local = when.astimezone(tz)
    if not in_quiet_hours(local, tz):
        return when
    target = local.replace(
        hour=settings.quiet_hours_end, minute=0, second=0, microsecond=0
    )
    if target <= local:
        target += timedelta(days=1)
    return target


@dataclass
class SendDecision:
    allowed: bool
    reason: str = ""
    queue_until: datetime | None = None

    @property
    def should_queue(self) -> bool:
        return self.queue_until is not None


def check_can_send(lead: Lead, *, now: datetime | None = None) -> SendDecision:
    now = now or datetime.now(settings.tz)

    if lead.opted_out:
        return SendDecision(False, "lead has opted out")

    if lead.message_count >= settings.max_messages_per_lead:
        return SendDecision(False, "message cap reached for this lead")

    tz = ZoneInfo(lead.timezone_name) if lead.timezone_name else settings.tz
    if in_quiet_hours(now, tz):
        return SendDecision(
            False, "quiet hours", queue_until=next_allowed_send(now, tz)
        )

    return SendDecision(True)


_GSM7_BASIC = set(
    "@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\u00d8\u00f8\u00c5\u00e5"
    "\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u0398\u039e\u00c6\u00e6"
    "\u00df\u00c9 !\"#\u00a4%&'()*+,-./0123456789:;<=>?"
    "\u00a1ABCDEFGHIJKLMNOPQRSTUVWXYZ\u00c4\u00d6\u00d1\u00dc\u00a7"
    "\u00bfabcdefghijklmnopqrstuvwxyz\u00e4\u00f6\u00f1\u00fc\u00e0\n\r"
)
_GSM7_EXTENDED = {"^", "{", "}", chr(92), "[", "~", "]", "|", "€"}

_TRANSLITERATE = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u2026": "...", "\u2022": "-", "\u00b7": "-",
    "\u00a0": " ", "\u2007": " ", "\u202f": " ", "\u200b": "",
    "\u2122": "(TM)", "\u00ae": "(R)", "\u00a9": "(C)",
    "\u00bd": "1/2", "\u00bc": "1/4", "\u00be": "3/4",
    "\u2192": "->", "\u2264": "<=", "\u2265": ">=",
    "\u00b0": " degrees",
}


def to_gsm7(text: str) -> str:
    out = []
    for char in text or "":
        if char in _GSM7_BASIC or char in _GSM7_EXTENDED:
            out.append(char)
        elif char in _TRANSLITERATE:
            out.append(_TRANSLITERATE[char])
        else:
            decomposed = unicodedata.normalize("NFKD", char)
            out.append("".join(c for c in decomposed if c in _GSM7_BASIC))
    return "".join(out)


def is_gsm7(text: str) -> bool:
    return all(c in _GSM7_BASIC or c in _GSM7_EXTENDED for c in text or "")


def segment_count(text: str) -> int:
    if not text:
        return 0
    if is_gsm7(text):
        length = sum(2 if c in _GSM7_EXTENDED else 1 for c in text)
        return 1 if length <= 160 else -(-length // 153)
    units = sum(2 if ord(c) > 0xFFFF else 1 for c in text)
    return 1 if units <= 70 else -(-units // 67)
