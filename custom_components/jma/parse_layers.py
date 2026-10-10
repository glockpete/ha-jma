"""Map-layer frame lists for map consumers, and the キキクル / rasrf readings at the configured place. No HA imports.

targetTimes files are lists of {basetime, validtime, elements[, member]}:
- N3 (/jmatile/data/nowc/targetTimes_N3.json): thns/trns observed every 10 min (basetime ==
  validtime) + the newest basetime's forecasts at +10..+60 min; 5-min rows carry `liden` only.
- risk (/jmatile/data/risk/targetTimes.json): one frame per 10 min, member immed0/immed1/immed2
  for the newest three, "none" before.
- rasrf (/jmatile/data/rasrf/targetTimes.json): analyses (basetime == validtime), hourly forecasts
  and `sjfcstmap` rows. Note: the forecasts of one validtime can come from different runs and members:
  for example the latest run (member "immed") may carry +1..+6 h while the previous run ("none") carries
  +8..+15 h, and a later hourly run carries all of +1..+15 h. JMA's map keeps, per validtime, the newest
  basetime that carries it; this parser does the same.
- himawari (/himawari/data/satimg/targetTimes_jp.json): every 2.5 min, no elements.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Generic, TypeVar

from PIL import Image

from .const import (
    BAND_MMH,
    GSI_FLOOD_ATTRIBUTION,
    HIMAWARI_FRAMES,
    HIMAWARI_STEP_MIN,
    KIKIKURU_FLOOD_PALETTE,
    KIKIKURU_PALETTE,
    LIDEN_FRAMES,
    N3_PAST,
    RAIN_BAND_RAIN,
    RASRF_AFTER,
    RISK_FRAMES,
    URL_GSI_FLOOD_TILE,
    URL_HIMAWARI_TILE,
    URL_LIDEN_GEOJSON,
    URL_RASRF_TILE,
    URL_RISK_TILE,
    URL_THNS_TILE,
    URL_TRNS_TILE,
)
from .errors import JmaParseError
from .parse_nowcast import parse_jma_time

_LOGGER = logging.getLogger(__name__)
_UNKNOWN_SEEN: set[tuple[str, tuple[int, int, int]]] = set()
T = TypeVar("T")

RISK_LAYERS: dict[str, str] = {"risk_inund": "inund", "risk_land": "land", "risk_flood": "flood_mesh"}


# ---------- frame selection ----------

def _rows(data: Any, what: str) -> list[dict]:
    if not isinstance(data, list):
        raise JmaParseError(f"{what} targetTimes: not a list")
    try:
        return [r for r in data if isinstance(r["basetime"], str) and isinstance(r["validtime"], str)]
    except (KeyError, TypeError) as err:
        raise JmaParseError(f"{what} targetTimes: {err!r}") from err


def n3_frames(data: Any, element: str, past: timedelta = N3_PAST) -> list[dict[str, str]]:
    """thns / trns: observed frames of the last `past` + the newest basetime's forecasts, ascending."""
    rows = [r for r in _rows(data, "N3") if element in (r.get("elements") or [])]
    observed = sorted({r["validtime"] for r in rows if r["basetime"] == r["validtime"]})
    if not observed:
        raise JmaParseError(f"N3 targetTimes: no observed {element} frame")
    base = observed[-1]
    since = parse_jma_time(base) - past
    frames = [{"basetime": v, "validtime": v} for v in observed if parse_jma_time(v) >= since]
    frames += [
        {"basetime": base, "validtime": v}
        for v in sorted({r["validtime"] for r in rows if r["basetime"] == base and r["validtime"] > base})
    ]
    return frames


def liden_frames(data: Any, count: int = LIDEN_FRAMES) -> list[dict[str, str]]:
    """雷 (LIDEN) strike frames: the newest `count` 5-min rows, ascending. None listed is [] (no
    data, not an error): the thunder layers of the same file still count."""
    rows = [r for r in _rows(data, "N3") if "liden" in (r.get("elements") or [])]
    times = sorted({r["validtime"] for r in rows if r["basetime"] == r["validtime"]})
    return [{"basetime": v, "validtime": v} for v in times[-count:]] if count > 0 else []


def _newest_per_validtime(rows: list[dict]) -> dict[str, dict]:
    best: dict[str, dict] = {}
    for r in rows:
        v = r["validtime"]
        if v not in best or r["basetime"] > best[v]["basetime"]:
            best[v] = r
    return best


def risk_frames(data: Any, count: int = RISK_FRAMES) -> list[dict[str, str]]:
    """キキクル: the newest `count` frames (10-min steps: the last hour), ascending."""
    rows = _rows(data, "risk")
    best = _newest_per_validtime(rows)
    if not best:
        raise JmaParseError("risk targetTimes: empty")
    return [
        {"basetime": r["basetime"], "member": r.get("member") or "none", "validtime": r["validtime"]}
        for r in (best[v] for v in sorted(best)[-count:])
    ]


def rasrf_frames(data: Any, after: timedelta = RASRF_AFTER) -> tuple[str, list[dict[str, str]]]:
    """(basetime, frames): the newest run with forecasts and every validtime more than `after`
    past it, each from the newest run that carries it. rasrf is the 1-h amount ENDING at the
    validtime, so its +1 h frame is the hour the nowcast already covers."""
    rows = [
        r for r in _rows(data, "rasrf")
        if "rasrf" in (r.get("elements") or []) and r["validtime"] > r["basetime"]
    ]
    if not rows:
        raise JmaParseError("rasrf targetTimes: no forecast frame")
    base = max(r["basetime"] for r in rows)
    since = parse_jma_time(base) + after
    best = _newest_per_validtime([r for r in rows if parse_jma_time(r["validtime"]) > since])
    frames = [
        {"basetime": r["basetime"], "member": r.get("member") or "none", "validtime": r["validtime"]}
        for r in (best[v] for v in sorted(best))
    ]
    return base, frames


def himawari_frames(data: Any, count: int = HIMAWARI_FRAMES,
                    step_min: int = HIMAWARI_STEP_MIN) -> list[dict[str, str]]:
    """Newest `count` frames on the 10-minute grid (the 2.5-min rapid scans in between dropped)."""
    rows = _rows(data, "himawari")
    on_grid = sorted({
        r["validtime"] for r in rows
        if r["basetime"] == r["validtime"] and r["validtime"][12:14] == "00"
        and int(r["validtime"][10:12]) % step_min == 0
    })
    if not on_grid:
        raise JmaParseError("himawari targetTimes: no 10-minute frame")
    return [{"basetime": v, "validtime": v} for v in on_grid[-count:]]


def layers_attribute(
    n3: dict[str, list] | None = None,
    risk: list | None = None,
    rasrf: tuple[str, list] | None = None,
    himawari: list | None = None,
) -> dict[str, dict[str, Any]]:
    """The `layers` attribute. A source that is None (failed and too old) is left out; the static
    GSI flood-depth layer is always there. Templates keep every placeholder."""
    out: dict[str, dict[str, Any]] = {}
    if n3 is not None:
        for key, template in (("thns", URL_THNS_TILE), ("trns", URL_TRNS_TILE)):
            if n3.get(key) is not None:
                # JMA's own map: maxNativeZoom 9, even zooms only, so 8 (z10 answers a blank tile)
                out[key] = {"template": template, "frames": n3[key], "zmax": 8}
        if n3.get("liden"):  # GeoJSON, not tiles: no {z}/{x}/{y}, no zoom range
            out["liden"] = {"template": URL_LIDEN_GEOJSON, "frames": n3["liden"], "format": "geojson"}
    if risk is not None:
        for key, elem in RISK_LAYERS.items():
            out[key] = {"template": URL_RISK_TILE.replace("{elem}", elem), "frames": risk, "zmax": 11}
    if rasrf is not None:
        out["rasrf"] = {"template": URL_RASRF_TILE, "frames": rasrf[1], "zmax": 10}
    if himawari is not None:
        out["himawari_ir"] = {"template": URL_HIMAWARI_TILE, "frames": himawari, "zmin": 3, "zmax": 6}
    out["flood_hazard"] = {
        "template": URL_GSI_FLOOD_TILE, "static": True, "zmax": 14, "attribution": GSI_FLOOD_ATTRIBUTION,
    }
    return out


class LastGood(Generic[T]):
    """One source of a multi-source poll: its last good value, dropped once older than max_age.

    A failing source keeps its own last value, so one bad list never drops the others.
    """

    def __init__(self, max_age: timedelta) -> None:
        self.max_age = max_age
        self.value: T | None = None
        self.last_ok: datetime | None = None
        self.last_error: str | None = None

    def ok(self, value: T, now: datetime) -> None:
        self.value, self.last_ok, self.last_error = value, now, None

    def fail(self, err: Exception | str) -> None:
        self.last_error = str(err)

    def get(self, now: datetime) -> T | None:
        if self.last_ok is None or now - self.last_ok >= self.max_age:
            return None
        return self.value


# ---------- readings at the configured place ----------

def _level_of(rgba: tuple[int, int, int, int], palette: dict[tuple[int, int, int], int], name: str) -> int:
    r, g, b, a = rgba
    if a < 128:
        return 0
    level = palette.get((r, g, b))
    if level is not None:
        return level
    nearest = min(palette, key=lambda c: (c[0] - r) ** 2 + (c[1] - g) ** 2 + (c[2] - b) ** 2)
    if (name, (r, g, b)) not in _UNKNOWN_SEEN:
        _UNKNOWN_SEEN.add((name, (r, g, b)))
        _LOGGER.info("Unlisted %s colour %s mapped to level %s", name, (r, g, b), palette[nearest])
    return palette[nearest]


def kikikuru_level_at(png: bytes, px: int, py: int, element: str, radius: int = 2) -> int:
    """Highest キキクル level (0-5) in the (2r+1)^2 block around (px, py), as the nowcast pixel."""
    palette = KIKIKURU_FLOOD_PALETTE if element == "flood_mesh" else KIKIKURU_PALETTE
    try:
        img = Image.open(io.BytesIO(png)).convert("RGBA")
    except Exception as err:  # Pillow raises several unrelated types
        raise JmaParseError(f"{element} tile: {err!r}") from err
    w, h = img.size
    best = 0
    for y in range(max(0, py - radius), min(h, py + radius + 1)):
        for x in range(max(0, px - radius), min(w, px + radius + 1)):
            best = max(best, _level_of(img.getpixel((x, y)), palette, element))  # type: ignore[arg-type]
    return best


@dataclass(frozen=True)
class Kikikuru:
    """キキクル at the configured place for the newest risk frame. A level is None when its tile was missing."""

    basetime: str
    inund: int | None
    land: int | None
    flood: int | None

    @property
    def level(self) -> int | None:
        known = [v for v in (self.inund, self.land, self.flood) if v is not None]
        return max(known) if known else None


@dataclass(frozen=True)
class RainOutlook:
    """降水短時間予報 at the configured place beyond the nowcast hour."""

    basetime: str
    series: list[dict[str, Any]]
    max_mmh: float | None
    first_rain: str | None


def rain_outlook(basetime: str, frames: list[tuple[str, int | None]]) -> RainOutlook:
    """frames: (validtime, band or None if the tile could not be read), ascending."""
    series = [
        {
            "time": parse_jma_time(v).isoformat(),
            "band": band,
            "mmh_upper": None if band is None else BAND_MMH[band][1],
        }
        for v, band in frames
    ]
    known = [s["band"] for s in series if s["band"] is not None]
    first = next((s["time"] for s in series if (s["band"] or 0) >= RAIN_BAND_RAIN), None)
    return RainOutlook(
        basetime=basetime,
        series=series,
        max_mmh=BAND_MMH[max(known)][1] if known else None,
        first_rain=first,
    )
