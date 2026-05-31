"""Talk to your memories — a voice/text conversation grounded in Om recall.

`om talk` runs a turn-based conversation loop. On each utterance it kicks off a
background recall over your memories (via the configured search backend — Moss,
bm25, or qmd), then grounds the assistant's reply in what it found. Voice I/O is
a pluggable transport; the default `TextTransport` works everywhere and headless.
"""

from __future__ import annotations

from .conversation import Conversation, ConversationTurn
from .recall import RecallEngine, RecallResult, RecallSnippet
from .transport import TextTransport, VoiceTransport

__all__ = [
    "Conversation",
    "ConversationTurn",
    "RecallEngine",
    "RecallResult",
    "RecallSnippet",
    "TextTransport",
    "VoiceTransport",
]
