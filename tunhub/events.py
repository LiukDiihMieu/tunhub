"""tunhub-wide events for the dashboard: the recent log lines of the whole hub.

Everything tunhub logs (network changes, reloads, config warnings and errors,
each service's log lines) in one place, so that "what just happened?" does not
require finding the log file.
"""

from __future__ import annotations

import logging
from collections import deque

LOGGER_NAME = "tunhub"
MAX_EVENTS = 500


class EventLog(logging.Handler):
    """Keeps the latest log records of the tunhub logger in memory."""

    def __init__(self, maxlen: int = MAX_EVENTS) -> None:
        super().__init__(logging.INFO)
        # deque.append is thread-safe: the ssh stderr readers log from threads.
        self.entries: deque[tuple[float, str, str]] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.entries.append((record.created, record.levelname.lower(), record.getMessage()))
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)


_event_log: EventLog | None = None


def event_log() -> EventLog:
    """The process-wide event log, attached to the tunhub logger on first use."""
    global _event_log
    if _event_log is None:
        _event_log = EventLog()
        logger = logging.getLogger(LOGGER_NAME)
        logger.addHandler(_event_log)
        if logger.getEffectiveLevel() > logging.INFO:
            logger.setLevel(logging.INFO)  # INFO records must be created to be kept
    return _event_log
