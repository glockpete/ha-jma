"""Tsunami bulletins from P2P地震情報 (code 552). No HA imports.

Every 552 message is the whole picture at its issue time (areas[] with a grade each); nothing ties
the bulletins of one tsunami together, so the tracker compares each new picture with what Home Assistant
was last told. A cancellation has `cancelled: true` and no areas.
"""

from __future__ import annotations

import logging
import math
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .const import (
    TSUNAMI_AREAS_MAX,
    TSUNAMI_GRADE_JA,
    TSUNAMI_GRADE_KEY,
    TSUNAMI_GRADE_RANK,
    TSUNAMI_LIVE_MAX_AGE_S,
    TSUNAMI_MAX_AGE,
)
from .parse_quake import parse_p2p_time

_LOGGER = logging.getLogger(__name__)
SEEN_MAX = 200
# A bulletin "issued" later than this after now is a bad clock or a wrong year, not a bulletin
# (it would otherwise become `current` and make every real bulletin after it look older).
FUTURE_TOLERANCE = timedelta(minutes=10)

# firstHeight.condition as P2P sends it -> English for the event payload.
CONDITION_EN: dict[str, str] = {
    "ただちに津波来襲と予測": "expected immediately",
    "津波到達中と推測": "arriving now",
    "第１波の到達を確認": "first wave observed",
}


@dataclass(frozen=True)
class TsunamiArea:
    name: str
    rank: int  # 0 unknown / 1 watch / 2 warning / 3 major warning
    immediate: bool
    height: str | None  # maxHeight.description as sent, e.g. "３ｍ"
    height_m: float | None
    arrival: datetime | None
    condition: str | None  # firstHeight.condition as sent (Japanese)


@dataclass(frozen=True)
class TsunamiBulletin:
    id: str
    issued: datetime
    cancelled: bool
    areas: tuple[TsunamiArea, ...]


@dataclass(frozen=True)
class TsunamiPicture:
    home: str  # key of TSUNAMI_GRADE_KEY
    max: str
    home_area_entry: TsunamiArea | None
    counts: dict[str, int]
    areas_major: list[str]
    areas_warning: list[str]
    expired: bool


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_area(raw: Any) -> TsunamiArea | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        return None
    grade = raw.get("grade")
    rank = TSUNAMI_GRADE_RANK.get(grade, 0) if isinstance(grade, str) else 0
    first = raw.get("firstHeight")
    first = first if isinstance(first, dict) else {}
    height = raw.get("maxHeight")
    height = height if isinstance(height, dict) else {}
    value = height.get("value")
    arrival = first.get("arrivalTime")
    return TsunamiArea(
        name=name,
        rank=rank,
        immediate=raw.get("immediate") is True,
        height=_str_or_none(height.get("description")),
        height_m=_finite_metres(value),
        arrival=parse_p2p_time(arrival) if isinstance(arrival, str) else None,
        condition=_str_or_none(first.get("condition")),
    )


def _finite_metres(value: Any) -> float | None:
    """A wave height in metres, or None when it is not a finite number (a bool, text, NaN, infinity, an integer too
    big for a float): one bad area must not drop the bulletin."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        metres = float(value)
    except OverflowError:
        return None
    return metres if math.isfinite(metres) else None


def parse_552(msg: Any) -> TsunamiBulletin | None:
    if not isinstance(msg, dict) or msg.get("code") != 552:
        return None
    # Defensive: the 552 schema has no `test` key today.
    if msg.get("test") is True:
        return None
    bulletin_id = msg.get("id")
    if not isinstance(bulletin_id, str) or not bulletin_id:
        return None
    issue = msg.get("issue")
    issued_raw = issue.get("time") if isinstance(issue, dict) else None
    issued = parse_p2p_time(issued_raw) if isinstance(issued_raw, str) else None
    if issued is None:
        return None
    areas_raw = msg.get("areas")
    areas = tuple(
        a for a in (_parse_area(r) for r in (areas_raw if isinstance(areas_raw, list) else []))
        if a is not None
    )
    cancelled = msg.get("cancelled") is True
    if not cancelled and not areas:
        # Only a cancellation has no areas. A bulletin that is not one and carries no readable
        # area would picture as "nothing in force" and lift a warning that was announced.
        return None
    if not cancelled and not any(a.rank for a in areas):
        # Same for areas whose grade is "Unknown" (a value of the P2P schema) or not one of ours: every rank
        # would read 0, "nothing in force", although nothing was cancelled. Ignored; state is left as it was.
        _LOGGER.debug("tsunami bulletin %s ignored: no known grade in %d area(s)", bulletin_id, len(areas))
        return None
    return TsunamiBulletin(bulletin_id, issued, cancelled, areas)


def summarise(bulletin: TsunamiBulletin, home_area: str | None, now: datetime) -> TsunamiPicture:
    if bulletin.cancelled:
        return TsunamiPicture("none", "none", None, _counts(()), [], [], False)
    if now - bulletin.issued > TSUNAMI_MAX_AGE:
        return TsunamiPicture("none", "none", None, _counts(()), [], [], True)
    entry = next((a for a in bulletin.areas if home_area and a.name == home_area), None)
    top = max((a.rank for a in bulletin.areas), default=0)
    return TsunamiPicture(
        home=TSUNAMI_GRADE_KEY[entry.rank] if entry else "none",
        max=TSUNAMI_GRADE_KEY[top],
        home_area_entry=entry,
        counts=_counts(bulletin.areas),
        areas_major=[a.name for a in bulletin.areas if a.rank == 3][:TSUNAMI_AREAS_MAX],
        areas_warning=[a.name for a in bulletin.areas if a.rank == 2][:TSUNAMI_AREAS_MAX],
        expired=False,
    )


def _counts(areas: Iterable[TsunamiArea]) -> dict[str, int]:
    areas = list(areas)
    return {
        "major_warning": sum(1 for a in areas if a.rank == 3),
        "warning": sum(1 for a in areas if a.rank == 2),
        "watch": sum(1 for a in areas if a.rank == 1),
    }


def relevance(picture: TsunamiPicture) -> str:
    """'home' / 'national' (a warning or major warning somewhere) / 'none'."""
    if picture.home != "none":
        return "home"
    if picture.max in ("warning", "major_warning"):
        return "national"
    return "none"


def _rank(key: Any) -> int:
    return TSUNAMI_GRADE_KEY.index(key) if key in TSUNAMI_GRADE_KEY else 0


class TsunamiTracker:
    """Turns bulletins into `tsunami` / `tsunami_cancelled` events; pure, `memory` is mutated."""

    def __init__(self, home_area: str | None, memory: dict[str, Any]) -> None:
        self.home_area = home_area
        self.memory = memory
        self.current: TsunamiBulletin | None = None
        self.replayed = False
        self.dirty = False
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._warned_future: OrderedDict[str, None] = OrderedDict()
        # The newest bulletin refused as future-dated (a slow host clock): P2P does not send it
        # again, so tick() offers it once more when the clock has caught up.
        self._deferred: TsunamiBulletin | None = None

    def picture(self, now: datetime) -> TsunamiPicture | None:
        if self.current is None:
            return None
        return summarise(self.current, self.home_area, now)

    def _mark_seen(self, bulletin_id: str) -> None:
        self._seen[bulletin_id] = None
        while len(self._seen) > SEEN_MAX:
            self._seen.popitem(last=False)

    def _is_future(self, bulletin: TsunamiBulletin, now: datetime) -> bool:
        """A bulletin issued more than FUTURE_TOLERANCE ahead of the clock: warned about once per
        id and NOT remembered as seen, so it is accepted when the clock is right again."""
        if bulletin.issued - now <= FUTURE_TOLERANCE:
            return False
        if bulletin.id not in self._warned_future:
            self._warned_future[bulletin.id] = None
            while len(self._warned_future) > SEEN_MAX:
                self._warned_future.popitem(last=False)
            _LOGGER.warning(
                "tsunami bulletin %s ignored: issued %s is more than %d min ahead of the clock (%s)",
                bulletin.id, bulletin.issued.isoformat(), FUTURE_TOLERANCE.total_seconds() // 60,
                now.isoformat())
        return True

    def feed(self, msg: Any, now: datetime, backfill: bool = False) -> list[dict[str, Any]]:
        bulletin = parse_552(msg)
        if bulletin is None or bulletin.id in self._seen:
            return []
        if self._is_future(bulletin, now):
            self._defer(bulletin)
            return []
        return self._accept(bulletin, now, backfill)

    def _defer(self, bulletin: TsunamiBulletin) -> None:
        if self._deferred is None or bulletin.issued > self._deferred.issued:
            self._deferred = bulletin

    def _accept(self, bulletin: TsunamiBulletin, now: datetime, backfill: bool) -> list[dict[str, Any]]:
        self._mark_seen(bulletin.id)
        if self.current is not None and bulletin.issued < self.current.issued:
            return []
        self.current = bulletin
        return self._decide(now, backfill)

    def replay(self, msgs: Any, now: datetime) -> list[dict[str, Any]]:
        if not isinstance(msgs, list):
            return []  # an error object or None is not an answer: stay "not replayed"
        parsed = [b for b in (parse_552(m) for m in msgs) if b is not None]
        parsed.sort(key=lambda b: b.issued)
        for b in parsed:
            if self._is_future(b, now):
                self._defer(b)
        parsed = [b for b in parsed if b.issued - now <= FUTURE_TOLERANCE]
        for b in parsed:
            self._mark_seen(b.id)
        self.replayed = True
        if parsed and (self.current is None or parsed[-1].issued > self.current.issued):
            self.current = parsed[-1]
        if self.current is None:
            return []
        old = (now - self.current.issued).total_seconds() > TSUNAMI_LIVE_MAX_AGE_S
        return self._decide(now, backfill=old)

    def tick(self, now: datetime) -> list[dict[str, Any]]:
        """The clock moved on with no message: let the 48 h expiry act (once), and take up a
        bulletin that was refused as future-dated once the clock has reached it."""
        held = self._deferred
        if held is not None and held.issued - now <= FUTURE_TOLERANCE:
            self._deferred = None
            if held.id not in self._seen:
                return self._accept(held, now, backfill=False)
        if self.current is None:
            return []
        return self._decide(now, backfill=False)

    def _memory_is_stale(self, bulletin: TsunamiBulletin) -> bool:
        """Memory left by an episode whose ending was never seen: its `issued` is more than
        TSUNAMI_MAX_AGE before the bulletin now being judged (or unreadable). Measured against the
        bulletin, not the wall clock, so the expiry of the bulletin itself still reports once."""
        raw = self.memory.get("issued")
        if not isinstance(raw, str):  # fromisoformat would raise TypeError for it, which is the same answer
            return True
        try:
            issued = datetime.fromisoformat(raw)
            return bulletin.issued - issued > TSUNAMI_MAX_AGE
        except (TypeError, ValueError):
            return True

    def _decide(self, now: datetime, backfill: bool) -> list[dict[str, Any]]:
        bulletin = self.current
        picture = self.picture(now)
        if bulletin is None or picture is None:
            return []
        m = self.memory
        remembered_active = m.get("active") is True
        active = remembered_active and not self._memory_is_stale(bulletin)
        prev_home = m.get("home") if active else "none"
        prev_max = m.get("max") if active else "none"
        prev_class = "home" if _rank(m.get("home")) else "national"
        rel = relevance(picture)
        state = {"active": rel != "none", "home": picture.home, "max": picture.max,
                 "id": bulletin.id, "issued": bulletin.issued.isoformat()}
        events: list[dict[str, Any]] = []
        if rel != "none":
            if (not active or _rank(picture.home) != _rank(m.get("home"))
                    or _rank(picture.max) > _rank(m.get("max")) or rel != prev_class):
                events.append(self._event_tsunami(bulletin, picture, prev_home, prev_max, backfill))
            self._store(state)
        elif active:
            events.append({
                "type": "tsunami_cancelled", "id": bulletin.id,
                "issued": bulletin.issued.isoformat(), "home_area": self.home_area,
                "previous_home_grade": prev_home, "previous_max_grade": prev_max,
                "cancelled": bulletin.cancelled, "expired": picture.expired,
                "backfill": backfill,
            })
            self._store(state)
        elif remembered_active:
            self._store(state)  # stale memory, nothing relevant now: clear it, say nothing
        return events

    def _store(self, state: dict[str, Any]) -> None:
        if self.memory != state:
            self.memory.clear()
            self.memory.update(state)
            self.dirty = True

    def _event_tsunami(self, bulletin: TsunamiBulletin, p: TsunamiPicture, prev_home: Any,
                       prev_max: Any, backfill: bool) -> dict[str, Any]:
        entry = p.home_area_entry
        return {
            "type": "tsunami",
            "id": bulletin.id,
            "issued": bulletin.issued.isoformat(),
            "home_area": self.home_area,
            "home_grade": p.home,
            "home_grade_ja": TSUNAMI_GRADE_JA.get(p.home),
            "home_immediate": entry.immediate if entry else None,
            "home_height": entry.height if entry else None,
            "home_height_m": entry.height_m if entry else None,
            "home_arrival": entry.arrival.isoformat() if entry and entry.arrival else None,
            "home_condition": CONDITION_EN.get(entry.condition) if entry and entry.condition
            else None,
            "max_grade": p.max,
            "areas_major": p.areas_major,
            "areas_warning": p.areas_warning,
            "count_major": p.counts["major_warning"],
            "count_warning": p.counts["warning"],
            "count_watch": p.counts["watch"],
            "previous_home_grade": prev_home,
            "previous_max_grade": prev_max,
            "backfill": backfill,
        }
