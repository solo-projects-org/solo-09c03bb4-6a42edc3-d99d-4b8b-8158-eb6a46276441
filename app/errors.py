"""Domain errors shared by parsing, validation and persistence."""

from __future__ import annotations

from typing import Iterable


class ReportError(Exception):
    """A user-facing rejection of a whole report.

    ``messages`` is a list of human-readable reasons.  Callers must treat the
    report as entirely rejected: no partial data is ever stored.
    """

    def __init__(self, messages: Iterable[str] | str) -> None:
        if isinstance(messages, str):
            messages = [messages]
        self.messages = list(messages)
        super().__init__("; ".join(self.messages))
