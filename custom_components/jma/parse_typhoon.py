"""Parse JMA typhoon data: active storms, forecast track, distance to the configured place. No HA imports.

/bosai/typhoon/data/targetTc.json lists the active tropical cyclones
([{tropicalCyclone: "TC2632", typhoonNumber: "2626", category: "TY", issue}]).
Per storm, /{tc}/forecast.json and /{tc}/specifications.json are LISTS of parts: a title part
({part: "title", issue, typhoonNumber, name{jp,en}}) then one part per `advancedHours`
(0 = 実況 analysis, 1 = 推定 estimate, 2..119 = 予報 forecast). forecast.json has the centres,
the 予報円 radius (metres) and, at hour 0, the past track; specifications.json has pressure and wind.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from .const import (
    EARTH_RADIUS_KM,
    INTERVAL_TYPHOON,
    TYPHOON_ATTR_MAX_BYTES,
    TYPHOON_CLEAR_POLLS,
    TYPHOON_NOTICE_HOURS,
    TYPHOON_NOTICE_KM,
    TYPHOON_PAST_TRACK_MAX,
    TYPHOON_STAGE_RANK,
    TYPHOON_URGENT_HOURS,
)
from .errors import JmaParseError

_JST = timezone(timedelta(hours=9))
_ZONE_SAMPLES = 50  # f = 0, 0.02 ... 1 on every leg between two storm-zone circles

_AT_CENTRE = 1e-9  # a segment fraction this close to 0 or 1 is the centre itself


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km (mean Earth radius 6371 km)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _dlon(lon: float, lon0: float) -> float:
    return (lon - lon0 + 180) % 360 - 180


def _plane(lat: float, lon: float, home: tuple[float, float]) -> tuple[float, float]:
    """km east / north of home on an equirectangular plane centred on it."""
    k = math.pi / 180 * EARTH_RADIUS_KM
    return _dlon(lon, home[1]) * k * math.cos(math.radians(home[0])), (lat - home[0]) * k


def _time_between(ta: Any, tb: Any, f: float) -> str | None:
    """ISO time a fraction f of the way from ta to tb, to the minute; None if either is missing."""
    try:
        a, b = datetime.fromisoformat(ta), datetime.fromisoformat(tb)
        t = a + (b - a) * f + timedelta(seconds=30)
    except (TypeError, ValueError):
        return None
    return t.replace(second=0, microsecond=0).isoformat()


def closest_approach(now: dict[str, Any], track: list[dict[str, Any]],
                     home: tuple[float, float]) -> dict[str, Any] | None:
    """Nearest point to home on the polyline now -> track[0] -> track[1] ... (straight segments).

    The nearest forecast CENTRE can miss the pass between two centres by a wide margin: a place far from
    both centres can lie much closer to the straight line between them.
    """
    if not track:
        return None
    points = [{"hours": 0, "time": now.get("time"), "lat": now["lat"], "lon": now["lon"],
               "circle_km": 0}, *track]
    best: tuple[float, dict, dict, float] | None = None
    for a, b in zip(points, points[1:], strict=False):
        ax, ay = _plane(a["lat"], a["lon"], home)
        bx, by = _plane(b["lat"], b["lon"], home)
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        f = 0.0 if seg == 0 else min(1.0, max(0.0, -(ax * dx + ay * dy) / seg))
        dist = math.hypot(ax + f * dx, ay + f * dy)
        if best is None or dist < best[0]:  # a tie keeps the earlier segment
            best = (dist, a, b, f)
    _, a, b, f = best  # type: ignore[misc]  # track is not empty: at least one segment
    if f < _AT_CENTRE:
        f = 0.0
    elif f > 1 - _AT_CENTRE:
        f = 1.0
    lat = a["lat"] + (b["lat"] - a["lat"]) * f
    lon = a["lon"] + _dlon(b["lon"], a["lon"]) * f
    ca, cb = a.get("circle_km"), b.get("circle_km")
    return {
        "distance_km": round(haversine_km(lat, lon, *home)),
        "time": _time_between(a.get("time"), b.get("time"), f),
        "hours": round(a["hours"] + (b["hours"] - a["hours"]) * f, 1),
        "circle_km": None if ca is None or cb is None else round(ca + (cb - ca) * f, 1),
        "lat": round(lat, 2),
        "lon": round(lon, 2),
        "between": [a["hours"], b["hours"]],
        "interpolated": 0.0 < f < 1.0,
    }


def thin(points: list, limit: int) -> list:
    """At most `limit` points, evenly spaced, always keeping the first and the last."""
    if len(points) <= limit:
        return list(points)
    if limit < 2:
        return [points[-1]]
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def _size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _without_zone_steps(storm: dict[str, Any]) -> dict[str, Any]:
    zone = storm.get("storm_zone")
    if isinstance(zone, dict) and "steps" in zone:
        return {**storm, "storm_zone": {k: v for k, v in zone.items() if k != "steps"}}
    return storm


def fit_storms(storms: list[dict[str, Any]], max_bytes: int = TYPHOON_ATTR_MAX_BYTES) -> list[dict[str, Any]]:
    """One storm is ~2.4 KB (~1.6 KB of it without the past track): five or more at once would
    pass 12 KB. First drop every storm's `storm_zone.steps` (keeping `enters` / `margin_km`),
    then halve every past track (down to its two ends); if that is not enough, leave out the
    storms farthest from home. The forecast track is never cut. The sensor's state still
    counts every storm."""
    limit, out = TYPHOON_PAST_TRACK_MAX, storms
    if _size(out) >= max_bytes:
        storms = out = [_without_zone_steps(s) for s in storms]
    while _size(out) >= max_bytes and limit > 2:
        limit //= 2
        out = [{**s, "past_track": thin(s["past_track"], limit)} for s in storms]
    if _size(out) >= max_bytes:
        out = sorted(out, key=lambda s: s.get("distance_km") or 0)
        while len(out) > 1 and _size(out) >= max_bytes:
            out = out[:-1]
        order = {s["id"]: i for i, s in enumerate(storms)}
        out.sort(key=lambda s: order.get(s["id"], 0))
    return out


def parse_target_tc(data: Any) -> list[dict[str, Any]]:
    """Active storms: [{id, number, category, issue}]. An empty list is valid (no storm)."""
    if not isinstance(data, list):
        raise JmaParseError(f"typhoon targetTc: not a list ({type(data).__name__})")
    out = []
    for tc in data:
        if not isinstance(tc, dict) or not isinstance(tc.get("tropicalCyclone"), str):
            raise JmaParseError(f"typhoon targetTc: bad entry {tc!r}")
        out.append({
            "id": tc["tropicalCyclone"],
            "number": tc.get("typhoonNumber"),
            "category": tc.get("category"),
            "issue": tc.get("issue"),
        })
    return out


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parts(data: Any, what: str) -> tuple[dict, dict[int, dict]]:
    if not isinstance(data, list) or not data:
        raise JmaParseError(f"typhoon {what}: not a list of parts")
    title: dict = {}
    by_hour: dict[int, dict] = {}
    for part in data:
        if not isinstance(part, dict):
            continue
        if part.get("part") == "title":
            title = part
        elif isinstance(part.get("advancedHours"), int):
            by_hour[part["advancedHours"]] = part
    if 0 not in by_hour:
        raise JmaParseError(f"typhoon {what}: no analysis (advancedHours 0)")
    return title, by_hour


def storm_zone_steps(fparts: dict[int, dict]) -> list[dict[str, Any]]:
    """The 暴風警戒域 circle each forecast step adds (JMA `stormWarningArea.arc`, radii in metres).

    A step's arcs repeat the arcs of every earlier step, so its OWN circle is the (centre, radius)
    pair not seen in an earlier step (the largest when several are new). A step with no new pair,
    or no `stormWarningArea`, has no circle and is skipped. The arc centre can differ from the
    step's `center` (TC2633 48 h). Bad shapes are skipped, never raise.
    """
    steps: list[dict[str, Any]] = []
    seen: set[tuple[float, float, float]] = set()
    for hours in sorted(h for h in fparts if isinstance(h, int)):
        part = fparts[hours]
        try:
            arcs = (part.get("stormWarningArea") or {}).get("arc") or []
            pairs = []
            for arc in arcs:
                pair = (float(arc[0][0]), float(arc[0][1]), float(arc[1]))
                pairs.append(pair)
        except (AttributeError, TypeError, IndexError, ValueError):
            continue
        new = [p for p in pairs if p not in seen]
        seen.update(pairs)
        if not new:
            continue
        lat, lon, radius = max(new, key=lambda p: p[2])
        steps.append({
            "hours": hours,
            "time": (part.get("validtime") or {}).get("JST") if isinstance(part.get("validtime"), dict) else None,
            "lat": lat, "lon": lon, "radius_km": round(radius / 1000, 1),
        })
    return steps


def _zone_discs(steps: list[dict[str, Any]]):
    """The sampled discs of the zone in time order: the convex hull of two circles is the union of
    the linearly interpolated discs, so each leg A -> B is sampled at f = 0, 0.02 ... 1."""
    if len(steps) == 1:
        a = steps[0]
        yield a["hours"], _time_between(a.get("time"), a.get("time"), 0.0), a["lat"], a["lon"], a["radius_km"]
        return
    for a, b in zip(steps, steps[1:], strict=False):
        for i in range(_ZONE_SAMPLES + 1):
            f = i / _ZONE_SAMPLES
            yield (
                a["hours"] + (b["hours"] - a["hours"]) * f,
                _time_between(a.get("time"), b.get("time"), f),
                a["lat"] + (b["lat"] - a["lat"]) * f,
                a["lon"] + _dlon(b["lon"], a["lon"]) * f,
                a["radius_km"] + (b["radius_km"] - a["radius_km"]) * f,
            )


def zone_entry(steps: list[dict[str, Any]], home: tuple[float, float]) -> dict[str, Any] | None:
    """First moment (time order) home is inside the storm-warning zone, or None."""
    for hours, when, lat, lon, radius in _zone_discs(steps):
        dist = haversine_km(lat, lon, *home)
        if dist <= radius:
            return {"hours": round(hours, 1), "time": when, "radius_km": round(radius, 1),
                    "distance_km": round(dist, 1)}
    return None


def zone_margin_km(steps: list[dict[str, Any]], home: tuple[float, float]) -> float | None:
    """Smallest (distance - radius) over the sampled discs; negative = inside. None without steps."""
    margins = [haversine_km(lat, lon, *home) - radius for _h, _t, lat, lon, radius in _zone_discs(steps)] \
        if steps else []
    return round(min(margins), 1) if margins else None


def parse_storm(tc: dict[str, Any], forecast: Any, specs: Any, home: tuple[float, float]) -> dict[str, Any]:
    """One storm for the `storms` attribute: now, forecast track, past track, distances."""
    title, fparts = _parts(forecast, "forecast")
    stitle, sparts = _parts(specs, "specifications")
    try:
        now_f, now_s = fparts[0], sparts.get(0, {})
        lat, lon = (float(v) for v in now_f["center"])
        wind = ((now_s.get("maximumWind") or {}).get("sustained") or {}).get("m/s")
        now = {
            "lat": lat, "lon": lon,
            "pressure_hpa": _int(now_s.get("pressure")),
            "max_wind_ms": _int(wind),
            "time": (now_f.get("validtime") or {}).get("JST"),
        }
        track = []
        for hours in sorted(h for h in fparts if h > 0):
            p = fparts[hours]
            radius = (p.get("probabilityCircle") or {}).get("radius")
            track.append({
                "hours": hours,
                "time": (p.get("validtime") or {}).get("JST"),
                "lat": float(p["center"][0]), "lon": float(p["center"][1]),
                "circle_km": round(radius / 1000, 1) if isinstance(radius, int | float) else None,
            })
        past: list[list[float]] = []
        for key in ("preTyphoon", "typhoon"):
            for pt in (now_f.get("track") or {}).get(key) or []:
                point = [float(pt[0]), float(pt[1])]
                if not past or past[-1] != point:  # the typhoon track starts where preTyphoon ends
                    past.append(point)
    except (KeyError, TypeError, IndexError, ValueError) as err:
        raise JmaParseError(f"typhoon {tc.get('id')}: {err!r}") from err

    zone_steps = storm_zone_steps(fparts)
    storm_zone = {
        "steps": zone_steps,
        "enters": zone_entry(zone_steps, home),
        "margin_km": zone_margin_km(zone_steps, home),
    } if zone_steps else None

    name = title.get("name") or stitle.get("name") or {}
    category = ((sparts.get(0) or {}).get("category") or {}).get("en") or tc.get("category")
    return {
        "id": tc.get("id"),
        "number": title.get("typhoonNumber") or tc.get("number"),
        "name_ja": name.get("jp"),
        "name_en": name.get("en"),
        "category": category,
        "issue": (title.get("issue") or {}).get("JST") or tc.get("issue"),
        "now": now,
        "track": track,
        "past_track": thin(past, TYPHOON_PAST_TRACK_MAX),
        "distance_km": round(haversine_km(lat, lon, *home)),
        "closest": closest_approach(now, track, home),
        "storm_zone": storm_zone,
    }


def _when(value: Any) -> datetime | None:
    """An ISO time as an aware datetime (JMA times carry +09:00; a naive one is read as JST)."""
    try:
        t = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=_JST)


def approach_stage(storm: dict[str, Any], now: datetime) -> str | None:
    """"urgent" / "notice" / "none" for one storm; None for an incomplete storm (not evaluated)."""
    if storm.get("incomplete"):
        return None
    enters = (storm.get("storm_zone") or {}).get("enters") or {}
    enter_at = _when(enters.get("time"))
    if enter_at is not None and enter_at - now <= timedelta(hours=TYPHOON_URGENT_HOURS):
        return "urgent"  # already inside counts: the difference is then negative
    notice = timedelta(hours=TYPHOON_NOTICE_HOURS)
    closest = storm.get("closest") or {}
    closest_at = _when(closest.get("time"))
    dist = closest.get("distance_km")
    if (closest_at is not None and isinstance(dist, int | float)
            and dist <= TYPHOON_NOTICE_KM and closest_at - now <= notice):
        return "notice"
    if enter_at is not None and enter_at - now <= notice:
        return "notice"
    # No track at all (no usable closest approach) and no zone entry: the storm's own distance.
    if closest_at is None or not isinstance(dist, int | float):
        distance = storm.get("distance_km")
        if enter_at is None and isinstance(distance, int | float) and distance <= TYPHOON_NOTICE_KM:
            return "notice"
    return "none"


def _number_short(number: Any) -> int | None:
    try:
        return int(str(number)[-2:])
    except ValueError:
        return None


def _receding(storm: dict[str, Any], now: datetime) -> bool:
    """True when the storm's closest approach is not in the future (closest.time <= now), or it has
    no usable closest and is farther than TYPHOON_NOTICE_KM. A storm that is still approaching is
    not receding even when its forecast moved outside the stage thresholds."""
    closest = storm.get("closest")
    at = _when(closest.get("time")) if isinstance(closest, dict) else None
    if at is not None:
        return at <= now
    distance = storm.get("distance_km")
    return isinstance(distance, int | float) and distance > TYPHOON_NOTICE_KM


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class ApproachTracker:
    """typhoon_approach / typhoon_cleared decisions. Pure: `memory` is the "typhoon" section of
    AlertMemory, {storm id: {"stage", "quiet", "last_issue", "away", "absent_since", "number",
    "name_en", "name_ja"}}; it is changed in place (every key is read defensively) and `dirty`
    tells the owner to save. `storms` carry a "stage" (None = not evaluated this poll: untouched).

    Clearing: a storm at stage none counts as quiet only while RECEDING (see _receding), once per
    LATER JMA `issue` (strictly after `last_issue`); TYPHOON_CLEAR_POLLS quiet issues clear it
    ("passed"). A storm at stage none that is still approaching keeps its remembered stage, except
    that a remembered urgent decays silently to notice after TYPHOON_CLEAR_POLLS such later issues
    (`away`), so a storm that turns back into the zone is announced as urgent again. A remembered
    storm missing from the list is cleared ("gone") on a successful poll at least INTERVAL_TYPHOON
    after the first poll without it (`absent_since`)."""

    def __init__(self, memory: dict[str, Any]) -> None:
        self._memory = memory
        self.dirty = False

    def _entry(self, storm_id: str) -> dict[str, Any]:
        mem = self._memory.get(storm_id)
        return mem if isinstance(mem, dict) else {}

    def _stage_of(self, storm_id: str) -> str:
        stage = self._entry(storm_id).get("stage")
        return stage if isinstance(stage, str) and stage in TYPHOON_STAGE_RANK else "none"

    def _put(self, storm_id: str, storm: dict[str, Any], stage: str, quiet: int,
             last_issue: str | None, away: int = 0) -> None:
        old = self._entry(storm_id)
        new = {
            "stage": stage, "quiet": quiet, "last_issue": last_issue, "away": away,
            "number": _text(storm.get("number")) or _text(old.get("number")),
            "name_en": _text(storm.get("name_en")) or _text(old.get("name_en")),
            "name_ja": _text(storm.get("name_ja")) or _text(old.get("name_ja")),
        }  # a storm that is present has no absent_since
        if self._memory.get(storm_id) != new:
            self._memory[storm_id] = new
            self.dirty = True

    def _forget(self, storm_id: str) -> None:
        if self._memory.pop(storm_id, None) is not None:
            self.dirty = True

    def update(self, storms: list[dict[str, Any]], now: datetime) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        present: set[str] = set()
        for storm in storms:
            sid = storm.get("id")
            if not isinstance(sid, str):
                continue
            present.add(sid)
            old = self._entry(sid)
            if "absent_since" in old or "absent" in old:  # seen again: the absence is over
                self._memory[sid] = {k: v for k, v in old.items() if k not in ("absent_since", "absent")}
                self.dirty = True
                old = self._entry(sid)
            stage = storm.get("stage")
            if not isinstance(stage, str) or stage not in TYPHOON_STAGE_RANK:
                continue  # incomplete, or kept from last-good: not a quiet poll
            prev = self._stage_of(sid)
            issue = _text(storm.get("issue"))
            last = _text(old.get("last_issue"))
            issue_at, last_at = _when(issue), _when(last)
            later = issue_at is not None and (last_at is None or issue_at > last_at)
            newest = issue if later else last  # last_issue only ever moves forward
            quiet, away = _count(old.get("quiet")), _count(old.get("away"))
            if TYPHOON_STAGE_RANK[stage] > TYPHOON_STAGE_RANK[prev]:
                events.append(("typhoon_approach", self._approach_payload(storm, stage, prev)))
                self._put(sid, storm, stage, 0, newest)
            elif stage != "none":
                self._put(sid, storm, prev, 0, newest)  # eased or unchanged: no event
            elif prev == "none":
                continue  # never remembered
            elif _receding(storm, now):
                if not later:
                    self._put(sid, storm, prev, quiet, last, away)
                    continue
                quiet += 1
                if quiet >= TYPHOON_CLEAR_POLLS:
                    events.append(("typhoon_cleared", self._cleared_payload(sid, storm, prev, "passed")))
                    self._forget(sid)
                else:
                    self._put(sid, storm, prev, quiet, newest, away)
            elif prev == "urgent" and later:  # still inbound, but the forecast moved away
                away += 1
                if away >= TYPHOON_CLEAR_POLLS:
                    self._put(sid, storm, "notice", quiet, newest, 0)  # silent decay
                else:
                    self._put(sid, storm, prev, quiet, newest, away)
            # a remembered notice that is still approaching is left alone
        for sid in [k for k in self._memory if k not in present]:
            old = self._entry(sid)
            since = _when(old.get("absent_since"))
            if since is None:  # first poll without it (an old-format `absent` entry starts here too)
                self._memory[sid] = {**{k: v for k, v in old.items() if k != "absent"},
                                     "absent_since": now.isoformat()}
                self.dirty = True
            # One minute of slack: Home Assistant schedules the next poll up to about a second
            # short of the interval, which would push "gone" to a third poll.
            elif now - since >= INTERVAL_TYPHOON - timedelta(minutes=1):
                events.append(("typhoon_cleared",
                               self._cleared_payload(sid, {}, self._stage_of(sid), "gone")))
                self._forget(sid)
        return events

    @staticmethod
    def _approach_payload(storm: dict[str, Any], stage: str, prev: str) -> dict[str, Any]:
        closest = storm.get("closest") or {}
        enters = (storm.get("storm_zone") or {}).get("enters") or {}
        now = storm.get("now") or {}
        return {
            "storm_id": storm.get("id"),
            "number": storm.get("number"),
            "number_short": _number_short(storm.get("number")),
            "name_en": storm.get("name_en"),
            "name_ja": storm.get("name_ja"),
            "category": storm.get("category"),
            "stage": stage,
            "previous_stage": prev,
            "closest_km": closest.get("distance_km"),
            "closest_time": closest.get("time"),
            "closest_hours": closest.get("hours"),
            "zone_enters_time": enters.get("time"),
            "zone_enters_hours": enters.get("hours"),
            "distance_km": storm.get("distance_km"),
            "pressure_hpa": now.get("pressure_hpa"),
            "max_wind_ms": now.get("max_wind_ms"),
            "issue": storm.get("issue"),
        }

    def _cleared_payload(self, sid: str, storm: dict[str, Any], prev: str, reason: str) -> dict[str, Any]:
        """Names from the storm when it is at hand, else from memory (a restart, a "gone" storm)."""
        old = self._entry(sid)
        number = _text(storm.get("number")) or _text(old.get("number"))
        return {
            "storm_id": sid,
            "number": number,
            "number_short": _number_short(number) if number else None,
            "name_en": _text(storm.get("name_en")) or _text(old.get("name_en")),
            "name_ja": _text(storm.get("name_ja")) or _text(old.get("name_ja")),
            "previous_stage": prev,
            "reason": reason,
        }
