from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import settings
from app.models import AssistantSettings

DEFAULT_PERSONALITY = "friendly"

PERSONALITIES: dict[str, dict[str, str]] = {
    "friendly": {
        "label": "Friendly",
        "prompt": (
            "- Be warm and relaxed, like you're a normal, helpful local in the area. Use their first name here and there.\n"
            "- Light, casual phrasing is fine (\"no worries\", \"sounds good\").\n"
            "- Before you ask the next thing, ensure that you acknowledge what they said (this is a very important part of communication)."
        ),
    },
    "professional": {
        "label": "Professional",
        "prompt": (
            "- Don't use any slang. Be polite, measured and businesslike.\n"
            "- There shouldn't be any small talk. All conversing should be done in full sentences and plain wording.\n"
            "- Before moving onto the next question, you should thank them briefly."
        ),
    },
    "straight_talker": {
        "label": "Straight talker",
        "prompt": (
            "- Be as direct and efficient as possible. Get to the point in as few words as possible.\n"
            "- Apart from a quick thanks. Skip most pleasantries.\n"
            "- Don't be pushy, or too blunt about their situation. You still must remain courteous."
        ),
    },
}

NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z '\-]{0,19}$")


@dataclass
class Assistant:
    name: str
    personality: str

    @property
    def personality_prompt(self) -> str:
        return PERSONALITIES[self.personality]["prompt"]


def validate_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not NAME_PATTERN.match(name):
        raise ValueError(
            "Name must be 1-20 characters: letters, spaces, hyphens or apostrophes only."
        )
    return name


def get_assistant(session: Session | None) -> Assistant:
    row = session.get(AssistantSettings, 1) if session is not None else None
    name = (row.assistant_name if row else None) or settings.agent_name
    personality = row.personality if row else DEFAULT_PERSONALITY
    if personality not in PERSONALITIES:
        personality = DEFAULT_PERSONALITY
    return Assistant(name=name, personality=personality)


def save_assistant(session: Session, *, name: str, personality: str) -> Assistant:
    name = validate_name(name)
    if personality not in PERSONALITIES:
        raise ValueError(f"Unknown personality: {personality}")
    row = session.get(AssistantSettings, 1) or AssistantSettings(id=1)
    row.assistant_name = name
    row.personality = personality
    session.add(row)
    session.commit()
    return Assistant(name=name, personality=personality)
