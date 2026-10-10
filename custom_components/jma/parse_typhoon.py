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
from datetime import datetime, timedelta
from typing import Any

from .const import EARTH_RADIUS_KM, TYPHOON_ATTR_MAX_BYTES, TYPHOON_PAST_TRACK_MAX
from .errors import JmaParseError

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


def fit_storms(storms: list[dict[str, Any]], max_bytes: int = TYPHOON_ATTR_MAX_BYTES) -> list[dict[str, Any]]:
    """One storm is ~2.4 KB (~1.6 KB of it without the past track): five or more at once would
    pass 12 KB. First halve every past track (down to its two ends); if that is not enough, leave
    out the storms farthest from home. The forecast track is never cut. The sensor's state still
    counts every storm."""
    limit, out = TYPHOON_PAST_TRACK_MAX, storms
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
                "circle_km": round(radius / 1000, 1) if isinstance(radius, (int, float)) else None,
            })
        past: list[list[float]] = []
        for key in ("preTyphoon", "typhoon"):
            for pt in (now_f.get("track") or {}).get(key) or []:
                point = [float(pt[0]), float(pt[1])]
                if not past or past[-1] != point:  # the typhoon track starts where preTyphoon ends
                    past.append(point)
    except (KeyError, TypeError, IndexError, ValueError) as err:
        raise JmaParseError(f"typhoon {tc.get('id')}: {err!r}") from err

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
    }
