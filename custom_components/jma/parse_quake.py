"""Earthquake information and EEW from P2P地震情報 (api.p2pquake.net). No HA imports.

551 = earthquake information (points[] carry pref, addr, scale); 556 = EEW, warning grade
(areas[] carry pref, name, scaleFrom, scaleTo, arrivalTime); 552 = tsunami (recorded, not alerted).

The place: the prefecture of an entry (from its office code), its area and the local name of its
municipality (the Japanese name chosen at set-up). Three tiers, each inside the one before:
  prefecture  a 551 point whose `pref` is the entry's;
  area        only for the 23 special wards of Tokyo (東京都２３区): the point is the area-level row of the
              23 wards, or its address starts with the name of one of the wards. An entry that has an area is
              scoped to it (its events follow the area); every other entry is scoped to the prefecture;
  local       the point's `addr` holds the local name.
Addresses are compared as text (`in`, `startswith`, `==`), never as a pattern.
"""

from __future__ import annotations

import logging
import math
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from .const import (
    AREA_TOKYO_23,
    EEW_BACKFILL_MAX_AGE_S,
    EEW_FUTURE_TOLERANCE_S,
    PREFECTURES,
    TOKYO_23_WARDS,
)

JST = timezone(timedelta(hours=9))
_LOGGER = logging.getLogger(__name__)
WARNED_MAX = 200  # event ids remembered as warned about (clock far behind)
ALERTED_MAX = 256  # EEW event ids remembered as alerted (the oldest go first)
ALERTED_KEEP = timedelta(hours=6)  # ... and for how long at most


def prefecture_for_office(office: Any) -> str | None:
    """The prefecture of a forecast office: the name for the first two digits of its code."""
    if not isinstance(office, str):
        return None
    return PREFECTURES.get(office[:2])


def short_prefecture(prefecture: str) -> str:
    """The form P2P uses in the `pref` of a 556 area: 東京都 -> 東京, 京都府 -> 京都, 北海道 stays."""
    return prefecture[:-1] if prefecture[-1:] in ("都", "府", "県") else prefecture


def _nfkc(text: str) -> str:
    """Full-width and half-width forms compare equal: "東京都２３区" == "東京都23区"."""
    return unicodedata.normalize("NFKC", text)


def local_forms(local_name: Any) -> tuple[str, ...]:
    """The ways an address may write the local name: as given, and, where the name holds both a city
    and a ward ("X市Y区"), with its first 市 removed (the agency writes a ward of a designated city
    without it: "XY区"). A city or a town alone keeps its name."""
    if not isinstance(local_name, str) or not local_name:
        return ()
    city = local_name.find("市")
    if city >= 0 and "区" in local_name[city + 1:]:
        return (local_name, local_name[:city] + local_name[city + 1:])
    return (local_name,)


@dataclass(frozen=True)
class QuakePlace:
    """What an entry asks of the quake feed: its prefecture, the name of its municipality (None when unknown)
    and, for the 23 wards of Tokyo only, its area (see `for_entry`)."""

    pref: str | None
    local_name: str | None = None
    area: str | None = None  # the area-level name P2P uses; None: the entry is scoped to the prefecture
    area_members: tuple[str, ...] = ()  # the names an address of the area starts with

    @classmethod
    def for_entry(cls, pref: str | None, local_name: str | None, class20: Any) -> QuakePlace:
        """The place of an entry: its area is 東京都２３区 when its class20 code is one of the 23 wards, and none
        otherwise (the rest of Tokyo, and every other prefecture, follow the prefecture)."""
        if isinstance(class20, str) and class20 in TOKYO_23_WARDS:
            return cls(pref, local_name, AREA_TOKYO_23, tuple(TOKYO_23_WARDS.values()))
        return cls(pref, local_name)

    @property
    def scoped_to_area(self) -> bool:
        return self.area is not None

    def is_pref(self, point_pref: Any) -> bool:
        return self.pref is not None and point_pref == self.pref

    def is_area(self, point_pref: Any, addr: Any) -> bool:
        """A 551 point of the area: the area-level row (its address is the area name), or an address that, with
        a leading prefecture word ("東京都" or "東京") removed, STARTS with the name of a member. `startswith`,
        not `in`: a member's name inside another address ("東京八王子市港区町") does not count."""
        if not self.area or self.pref is None or not self.is_pref(point_pref) or not isinstance(addr, str):
            return False
        text = _nfkc(addr)
        if text == _nfkc(self.area):
            return True
        for prefix in (self.pref, short_prefecture(self.pref)):
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        return text.startswith(self.area_members)

    def is_local(self, point_pref: Any, addr: Any) -> bool:
        if not self.is_pref(point_pref) or not isinstance(addr, str):
            return False
        return any(form in addr for form in local_forms(self.local_name))


def parse_p2p_time(value: str | None) -> datetime | None:
    """P2P times are JST 'YYYY/MM/DD HH:MM:SS' with optional '.fff'."""
    if not value:
        return None
    for fmt in ("%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=JST)
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class Quake:
    """One 551 message, reduced to what a place needs."""

    id: str
    time: datetime | None
    issue_type: str | None
    hypocenter: str | None
    magnitude: float | None
    depth: int | None
    max_scale: int | None
    pref_max_scale: int | None  # the highest scale among the points of the entry's prefecture
    local_scale: int | None  # the highest among the local points; None when none reported
    domestic_tsunami: str | None
    pref: str | None = None
    local_name: str | None = None
    area: str | None = None  # the entry's area (the 23 wards of Tokyo); None for every other entry
    area_max_scale: int | None = None  # the highest among the points of the area; None when none reported

    @property
    def scope_max_scale(self) -> int | None:
        """The maximum an entry's events follow: its area's when it has one, else its prefecture's. An entry
        with an area does not fall back to the prefecture when the area has no point."""
        return self.area_max_scale if self.area else self.pref_max_scale

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "time": self.time.isoformat() if self.time else None,
            "issue_type": self.issue_type, "hypocenter": self.hypocenter,
            "magnitude": self.magnitude, "depth": self.depth, "max_scale": self.max_scale,
            "pref": self.pref, "area": self.area, "local_name": self.local_name,
            "pref_max_scale": self.pref_max_scale, "area_max_scale": self.area_max_scale,
            "local_scale": self.local_scale,
            "domestic_tsunami": self.domestic_tsunami,
        }


def _pos(value: Any) -> Any:
    """P2P uses -1 for 'unknown'."""
    return None if value in (None, -1, "") else value


def _scale(value: Any) -> int | None:
    """A P2P scale code as int. The spec says scales may carry a decimal point ('45.0', only the
    integer part counts), so floats are accepted; bools, NaN, ±Infinity and anything else are not."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None  # (math.isfinite would raise OverflowError for an int of 400 digits: ints skip it)
    return int(value)


def _scale_as_sent(value: Any) -> Any:
    """A scale for an event payload as P2P sent it, except that NaN / ±Infinity (not JSON) read as unknown."""
    return None if isinstance(value, float) and not math.isfinite(value) else value


def parse_551(msg: Any, place: QuakePlace) -> Quake | None:
    if not isinstance(msg, dict) or msg.get("code") != 551:
        return None
    quake = msg.get("earthquake") or {}
    hypo = quake.get("hypocenter") or {}
    in_pref: list[int] = []
    in_area: list[int] = []
    local: list[int] = []
    points = msg.get("points")
    for point in points if isinstance(points, list) else []:
        # One malformed point (not an object, no readable scale) is skipped; the others still make the quake.
        scale = _scale(point.get("scale")) if isinstance(point, dict) else None
        if scale is None or not place.is_pref(point.get("pref")):
            continue
        in_pref.append(scale)
        if place.is_area(point.get("pref"), point.get("addr")):
            in_area.append(scale)
        if place.is_local(point.get("pref"), point.get("addr")):
            local.append(scale)
    return Quake(
        id=str(msg.get("id") or msg.get("_id") or ""),
        time=parse_p2p_time(quake.get("time")),
        issue_type=(msg.get("issue") or {}).get("type"),
        hypocenter=_pos(hypo.get("name")),
        magnitude=_pos(hypo.get("magnitude")),
        depth=_pos(hypo.get("depth")),
        max_scale=_pos(quake.get("maxScale")),
        pref_max_scale=max(in_pref) if in_pref else None,
        local_scale=max(local) if local else None,
        domestic_tsunami=quake.get("domesticTsunami"),
        pref=place.pref,
        local_name=place.local_name,
        area=place.area,
        area_max_scale=max(in_area) if in_area else None,
    )


MAX_REMEMBERED = 500


def _hi(a: int | None, b: int | None) -> int | None:
    return b if a is None else a if b is None else max(a, b)


class QuakeTracker:
    """One running picture per quake (keyed by origin time): 震度速報 -> 震源 -> 各地の震度 are merged, and
    an event goes out when the scope's scale (the area's for an entry that has one, else the prefecture's) or
    the local scale rises, or the epicentre first becomes known."""

    def __init__(self, place: QuakePlace) -> None:
        self.place = place
        self._seen: dict[str, None] = {}
        self._merged: dict[str, Quake] = {}
        # Why the last feed() returned a quake: "new" (first report of it), "scale" (the scope's maximum
        # rose: the area's for an entry that has an area, the prefecture's for every other entry) or
        # "detail" (the scope's maximum is unchanged: only the local figure and/or the epicentre arrived).
        # An automation can alarm for the first two and only update a notification for the third (one
        # quake is three or more reports). A rise of the prefecture alone, for an entry that has an area,
        # is none of these: feed() returns None for it unless the local figure or the epicentre is news.
        self.last_change: str | None = None

    def feed(self, msg: Any) -> Quake | None:
        quake = parse_551(msg, self.place)
        if quake is None or not quake.id or quake.id in self._seen:
            return None
        self._seen[quake.id] = None
        while len(self._seen) > MAX_REMEMBERED:
            self._seen.pop(next(iter(self._seen)))
        key = quake.time.isoformat() if quake.time else quake.id
        before = self._merged.get(key)
        if before is not None and quake.hypocenter and before.hypocenter and quake.hypocenter != before.hypocenter:
            # Same origin minute, different epicentre: two distinct quakes, not one being updated.
            key = f"{key}|{quake.hypocenter}"
            before = self._merged.get(key)
        merged = quake if before is None else replace(
            quake,
            hypocenter=quake.hypocenter or before.hypocenter,
            magnitude=quake.magnitude if quake.magnitude is not None else before.magnitude,
            depth=quake.depth if quake.depth is not None else before.depth,
            max_scale=_hi(quake.max_scale, before.max_scale),
            pref_max_scale=_hi(quake.pref_max_scale, before.pref_max_scale),
            area_max_scale=_hi(quake.area_max_scale, before.area_max_scale),
            local_scale=_hi(quake.local_scale, before.local_scale),
        )
        self._merged.pop(key, None)  # evict by last update: re-inserting moves it to the end
        self._merged[key] = merged
        while len(self._merged) > 50:
            self._merged.pop(next(iter(self._merged)))
        if before is None:
            self.last_change = "new"
        elif (merged.scope_max_scale or 0) > (before.scope_max_scale or 0):
            self.last_change = "scale"
        elif ((merged.local_scale or 0) > (before.local_scale or 0)
              or (merged.hypocenter and not before.hypocenter)):
            self.last_change = "detail"
        else:
            return None
        return merged


def eew_area_matches(area: Any, prefecture: str | None, area_name: str | None = None) -> bool:
    """An area of a 556 warning counts when its prefecture is the entry's. P2P gives the prefecture of
    an area in a short form ("東京", "京都", "北海道"); the full form is accepted too, and an area without
    a `pref` counts when its name starts with the full prefecture name.

    With `area_name` (the entry's area, 東京都２３区) only an area of that name counts, whatever its `pref`:
    full-width and half-width digits are the same ("東京都２３区" == "東京都23区"). An empty `area_name` is no
    area name (the prefecture rule applies), so an alarm is never silenced by an empty string."""
    if not isinstance(area, dict) or not prefecture:
        return False
    if area_name:
        name = area.get("name")
        return isinstance(name, str) and _nfkc(name) == _nfkc(area_name)
    area_pref = area.get("pref")
    if isinstance(area_pref, str) and area_pref:
        return area_pref in (prefecture, short_prefecture(prefecture))
    name = area.get("name")
    return isinstance(name, str) and name.startswith(prefecture)


class EewTracker:
    """EEW (556) for a prefecture, or for the area of an entry that has one: first matching report, then only a
    rising forecast."""

    def __init__(self, pref: str | None, area: str | None = None) -> None:
        self.pref = pref
        self.area = area
        # event id -> (highest scale announced, when). Ordered by the last announcement; bounded (see feed()).
        self._alerted: dict[str, tuple[int, datetime]] = {}
        self._cancelled: dict[str, datetime] = {}
        self._warned_future: OrderedDict[str, None] = OrderedDict()  # event ids already warned about

    @staticmethod
    def _predicted(area: dict[str, Any]) -> int:
        # 99 = "程度以上" (this intensity or more): the lower bound is the number to use.
        bounds = [s for s in map(_scale, (area.get("scaleFrom"), area.get("scaleTo")))
                  if s is not None and s not in (-1, 99)]
        return max(bounds) if bounds else 0

    def _fresh_enough(self, event_id: str, issued: datetime | None, now: datetime, backfill: bool) -> bool:
        """The age window. Reconnect replays can arrive on the live socket as well as backfill, so
        both are held to EEW_BACKFILL_MAX_AGE_S. A live message with no usable issue time has an
        unknown age and passes (the id dedupe stops a repeat); a replay without one cannot be aged.
        A message further ahead of the clock than the tolerance means a wrong host clock: warned
        about once per event id, and not remembered, so it alerts when the clock is right again."""
        if issued is None:
            if backfill:
                _LOGGER.debug("EEW %s ignored: replayed without a readable issue time", event_id)
            return not backfill
        age = (now - issued).total_seconds()
        if age < -EEW_FUTURE_TOLERANCE_S:
            if event_id not in self._warned_future:
                self._warned_future[event_id] = None
                while len(self._warned_future) > WARNED_MAX:
                    self._warned_future.popitem(last=False)
                _LOGGER.warning(
                    "EEW %s ignored: issued %s is more than %d min ahead of the clock (%s); "
                    "the clock of this host looks wrong", event_id, issued.isoformat(),
                    EEW_FUTURE_TOLERANCE_S // 60, now.isoformat())
            return False
        if age > EEW_BACKFILL_MAX_AGE_S:
            _LOGGER.debug("EEW %s ignored: issued %d s ago", event_id, age)
            return False
        return True

    def feed(self, msg: Any, now: datetime, backfill: bool = False) -> list[dict[str, Any]]:
        if not isinstance(msg, dict) or msg.get("code") != 556 or msg.get("test") is True:
            return []
        issue = msg.get("issue") or {}
        event_id = str(issue.get("eventId") or "")
        if not event_id:
            return []
        if not self._fresh_enough(event_id, parse_p2p_time(issue.get("time")), now, backfill):
            return []
        # A cancelled id is remembered as long as a replay of its alert can still pass _fresh_enough: with a
        # slow host clock that is the age limit plus the future tolerance, counted from when we saw the cancel.
        keep_s = EEW_BACKFILL_MAX_AGE_S + EEW_FUTURE_TOLERANCE_S
        self._cancelled = {key: at for key, at in self._cancelled.items()
                           if (now - at).total_seconds() <= keep_s}
        # An id is of no use once a replay of it can no longer pass _fresh_enough: forget it after ALERTED_KEEP, and
        # never hold more than ALERTED_MAX (a flood of distinct ids must not grow the memory for good).
        self._alerted = {key: seen for key, seen in self._alerted.items() if now - seen[1] <= ALERTED_KEEP}
        if event_id in self._cancelled:
            return []
        quake = msg.get("earthquake") or {}
        hypo = quake.get("hypocenter") or {}
        base = {
            "event_id": event_id,
            "serial": issue.get("serial"),
            "origin_time": quake.get("originTime"),
            "hypocenter": _pos(hypo.get("name")),
            "magnitude": _pos(hypo.get("magnitude")),
            "pref": self.pref,
            "area": self.area,
        }
        if msg.get("cancelled") is True:
            self._cancelled[event_id] = now
            if self._alerted.pop(event_id, None) is None:
                return []
            return [{"type": "eew_cancelled", **base}]
        areas = [a for a in msg.get("areas") or [] if eew_area_matches(a, self.pref, self.area)]
        if not areas:
            return []
        area = max(areas, key=self._predicted)  # the strongest matched area gives the numbers (ties: first)
        predicted = self._predicted(area)
        before = self._alerted.get(event_id)
        if before is not None and predicted <= before[0]:
            return []
        self._alerted.pop(event_id, None)  # (re)insert at the end: the oldest announcement is the first key
        self._alerted[event_id] = (predicted, now)
        while len(self._alerted) > ALERTED_MAX:
            del self._alerted[next(iter(self._alerted))]
        return [{
            "type": "eew", **base, "areas": [a.get("name") for a in areas],
            "scale_from": _scale_as_sent(area.get("scaleFrom")), "scale_to": _scale_as_sent(area.get("scaleTo")),
            "predicted_scale": predicted, "arrival_time": area.get("arrivalTime"),
            "update": before is not None,
        }]
