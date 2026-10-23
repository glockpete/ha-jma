"""Feed health: a problem is reported once a condition has held for 30 minutes.

Pure: the binary sensor feeds it conditions; it remembers since when each one has held.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .const import HEALTH_GRACE


class HealthTracker:
    """Tracks when each feed's problem started."""

    def __init__(self, grace: timedelta = HEALTH_GRACE) -> None:
        self._grace = grace
        self._since: dict[str, datetime] = {}
        self._reason: dict[str, str] = {}

    def evaluate(self, now: datetime, conditions: dict[str, str | None]) -> dict[str, dict[str, Any]]:
        """conditions: feed -> reason string when unhealthy, None when fine.

        Returns feed -> {problem: bool, since, reason} for every feed.
        """
        out: dict[str, dict[str, Any]] = {}
        for feed, reason in conditions.items():
            if reason is None:
                self._since.pop(feed, None)
                self._reason.pop(feed, None)
                out[feed] = {"problem": False, "since": None, "reason": None}
                continue
            since = self._since.setdefault(feed, now)
            self._reason[feed] = reason
            out[feed] = {
                "problem": now - since >= self._grace,
                "since": since.isoformat(),
                "reason": reason,
            }
        return out
