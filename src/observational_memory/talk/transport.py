"""Pluggable voice/text transports for `om talk`.

A transport is the I/O surface of the conversation: it produces user utterances
(`listen`) and renders Om's replies (`speak`). The default `TextTransport` reads
stdin and writes stdout, so the feature is fully usable and testable headless.

Real audio (mic capture + STT + TTS) is intentionally a documented follow-up:
it needs system audio libraries and hardware that can't be exercised in CI, so
it would ship with no test coverage. The conversation + background-recall core
is what makes this "talk to your memories", and it is fully covered here.
"""

from __future__ import annotations

import sys
from typing import Protocol, TextIO, runtime_checkable

# Words that end the conversation when typed as a whole utterance.
_EXIT_WORDS = {"exit", "quit", "bye", "goodbye", ":q"}


@runtime_checkable
class VoiceTransport(Protocol):
    """The I/O surface of a conversation."""

    def listen(self) -> str | None:
        """Return the next user utterance, or None to end the conversation."""
        ...

    def speak(self, text: str) -> None:
        """Render one assistant reply."""
        ...

    def close(self) -> None:
        """Release any resources held by the transport."""
        ...


class TextTransport:
    """Read utterances from a text stream and write replies to another.

    Defaults to stdin/stdout. An empty line is ignored; EOF or an exit word
    ("exit", "quit", "bye", ...) ends the conversation.
    """

    def __init__(
        self,
        *,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
        prompt: str = "you> ",
        reply_prefix: str = "om> ",
    ) -> None:
        self._in = input_stream if input_stream is not None else sys.stdin
        self._out = output_stream if output_stream is not None else sys.stdout
        self._prompt = prompt
        self._reply_prefix = reply_prefix

    def listen(self) -> str | None:
        while True:
            self._out.write(self._prompt)
            self._out.flush()
            line = self._in.readline()
            if not line:  # EOF
                self._out.write("\n")
                self._out.flush()
                return None
            text = line.strip()
            if not text:
                continue
            if text.lower() in _EXIT_WORDS:
                return None
            return text

    def speak(self, text: str) -> None:
        self._out.write(f"{self._reply_prefix}{text}\n")
        self._out.flush()

    def close(self) -> None:  # nothing to release for text streams
        pass
