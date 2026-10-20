"""Read JMA high-resolution precipitation nowcast (hrpns) tiles at the configured place. No HA imports.

Tiles are 256x256 PNGs, either paletted (colour type 3) or RGBA (colour type 6, used for
empty tiles). Colours map to 8 rain bands (const.RAIN_PALETTE); transparent = no rain.
"""

from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from PIL import Image

from .const import BAND_MMH, RAIN_BAND_RAIN, RAIN_PALETTE, RAIN_REARM
from .errors import JmaParseError

_LOGGER = logging.getLogger(__name__)
_UNKNOWN_SEEN: set[tuple[int, int, int]] = set()
TILE_SIZE = 256  # what the JMA tile servers send (hrpns, rasrf, risk); any other size is not a tile
# A pixel farther than this (squared RGB distance) from EVERY palette colour is unknown and reads as 0, not as the
# nearest band: a black or grey placeholder served with HTTP 200 must not read as rain or as キキクル level 5.
# 225 = 15**2: above the 200 of the near colours the tests use ((250, 225, 10) next to #f2e700), below the 288 between
# black and the level-5 colour #0c000c and the 338 between white and the lightest rain band #f2f2ff. Two palette
# colours are at least 5300 apart, so a colour inside it has one nearest.
PALETTE_TOLERANCE_SQ = 225


def tile_for(lat: float, lon: float, z: int) -> tuple[int, int, int, int]:
    """(x, y, px, py): the Web-Mercator tile holding the point and the pixel inside it."""
    n = 2**z
    xf = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    yf = (1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n
    x, y = int(xf), int(yf)
    return x, y, int((xf - x) * 256), int((yf - y) * 256)


def nearest_colour(
    palette: dict[tuple[int, int, int], int], rgb: tuple[int, int, int]
) -> tuple[int, int, int] | None:
    """The palette colour nearest to `rgb`, or None when none is within PALETTE_TOLERANCE_SQ."""
    r, g, b = rgb
    nearest = min(palette, key=lambda c: (c[0] - r) ** 2 + (c[1] - g) ** 2 + (c[2] - b) ** 2)
    if (nearest[0] - r) ** 2 + (nearest[1] - g) ** 2 + (nearest[2] - b) ** 2 > PALETTE_TOLERANCE_SQ:
        return None
    return nearest


def _band_of(rgba: tuple[int, int, int, int]) -> int:
    r, g, b, a = rgba
    if a < 128:
        return 0
    band = RAIN_PALETTE.get((r, g, b))
    if band is not None:
        return band
    nearest = nearest_colour(RAIN_PALETTE, (r, g, b))
    band = 0 if nearest is None else RAIN_PALETTE[nearest]
    if (r, g, b) not in _UNKNOWN_SEEN:
        _UNKNOWN_SEEN.add((r, g, b))
        _LOGGER.info("Unlisted nowcast colour %s mapped to band %s", (r, g, b), band)
    return band


def tile_block(png: bytes, px: int, py: int, radius: int, what: str) -> list[tuple[int, int, int, int]]:
    """RGBA pixels of the (2r+1)^2 block around (px, py), clamped to the tile (empty when the block lies outside it).

    JmaParseError unless `png` is a TILE_SIZE x TILE_SIZE PNG. Pillow opens lazily, so the size is read from the
    header before a pixel is decoded: a few hundred bytes declaring 9000x9000 are refused, not inflated. Only the
    cropped block is converted to RGBA.
    """
    try:
        img = Image.open(io.BytesIO(png), formats=["PNG"])
    except Exception as err:  # Pillow raises several unrelated types (DecompressionBombError among them)
        raise JmaParseError(f"{what}: {err!r}") from err
    with img:
        if img.size != (TILE_SIZE, TILE_SIZE):
            raise JmaParseError(f"{what}: {img.size[0]}x{img.size[1]} is not a {TILE_SIZE}x{TILE_SIZE} tile")
        box = (max(0, px - radius), max(0, py - radius),
               min(TILE_SIZE, px + radius + 1), min(TILE_SIZE, py + radius + 1))
        if box[2] <= box[0] or box[3] <= box[1]:
            return []
        try:
            block = img.crop(box).convert("RGBA")
        except Exception as err:  # a truncated or corrupt stream
            raise JmaParseError(f"{what}: {err!r}") from err
        return [block.getpixel((x, y)) for y in range(block.height) for x in range(block.width)]  # type: ignore[misc]


def band_at(png: bytes, px: int, py: int, radius: int = 2) -> int:
    """Highest rain band in the (2r+1)^2 pixel block around (px, py); clamped to the tile."""
    return max((_band_of(rgba) for rgba in tile_block(png, px, py, radius, "nowcast tile")), default=0)


def parse_jma_time(value: str) -> datetime:
    """targetTimes use UTC 'YYYYMMDDHHMMSS'."""
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def frame_lists(n1: Any, n2: Any, past: int) -> tuple[str, list[str], list[str]]:
    """(basetime, last `past` observed validtimes ascending, forecast validtimes ascending).

    JMA publishes N2 (forecast) about 70 s after N1 (observed) and the CDN caches both for 60 s,
    so a poll can see a basetime in N1 that N2 does not carry yet. The basetime is therefore the
    newest observed one that N2 HAS forecasts for (falling back to the newest observed), and the
    past frames stop there. Without this, some polls would find no forecast at all.
    """
    try:
        observed = sorted(
            (t for t in n1 if t["basetime"] == t["validtime"] and "hrpns" in t["elements"]),
            key=lambda t: t["validtime"],
        )
        forecasts: dict[str, list[str]] = {}
        for t in n2:
            if t["validtime"] > t["basetime"] and "hrpns" in t["elements"]:
                forecasts.setdefault(t["basetime"], []).append(t["validtime"])
        with_forecast = [t["basetime"] for t in observed if t["basetime"] in forecasts]
        basetime = with_forecast[-1] if with_forecast else observed[-1]["basetime"]
        forecast = sorted(forecasts.get(basetime, []))
        past_frames = [t["validtime"] for t in observed if t["validtime"] <= basetime][-past:]
    except (KeyError, TypeError, IndexError) as err:
        raise JmaParseError(f"nowcast targetTimes: {err!r}") from err
    return basetime, past_frames, forecast


@dataclass(frozen=True)
class RainSummary:
    """Rain at the configured place: now + the next hour in 5-minute steps."""

    basetime: str
    series: list[dict[str, Any]]
    max_mmh: float | None
    minutes_to_rain: int | None
    raining_now: bool


class RainArming:
    """`rain_arriving` fires once per episode.

    Fires when rain (>= 1 mm/h) is forecast within 30 minutes while it is dry now. Re-arms
    only after 60 minutes in which no frame of the series showed rain. A poll in which no frame
    could be read at all (every band None) shows nothing: it is neither rain nor dry, so it ends
    the dry stretch instead of adding to it (an hour of tile failures must not re-arm the
    push in the middle of an episode).
    """

    def __init__(self, lead_minutes: int = 30, rearm: timedelta = RAIN_REARM) -> None:
        self.armed = True
        self._lead = lead_minutes
        self._rearm = rearm
        self._dry_since: datetime | None = None

    def update(self, summary: RainSummary, now: datetime) -> bool:
        wet = any((s["band"] or 0) >= RAIN_BAND_RAIN for s in summary.series)
        unknown = all(s["band"] is None for s in summary.series)  # also true for an empty series
        if wet or unknown:
            self._dry_since = None
        else:
            self._dry_since = self._dry_since or now
            if not self.armed and now - self._dry_since >= self._rearm:
                self.armed = True
        if summary.raining_now:
            self.armed = False  # this episode has started; no "arriving" push in the middle of it
            return False
        if self.armed and summary.minutes_to_rain is not None and summary.minutes_to_rain <= self._lead:
            self.armed = False
            return True
        return False


class WetHold:
    """The weather state goes rainy on the first wet observed frame and lets go only after `dry_frames`
    dry observed frames in a row (without it the state would flip between cloudy and rainy on single dry frames)."""

    def __init__(self, dry_frames: int = 2) -> None:
        self._need = dry_frames
        self._dry = 0
        self._last: str | None = None
        self.wet = False

    def update(self, basetime: str, raining_now: bool | None) -> bool:
        if basetime == self._last or raining_now is None:
            return self.wet
        self._last = basetime
        if raining_now:
            self.wet, self._dry = True, 0
        elif self.wet:
            self._dry += 1
            if self._dry >= self._need:
                self.wet, self._dry = False, 0
        return self.wet


def rain_series(basetime: str, frames: list[tuple[str, int | None]]) -> RainSummary:
    """frames: (validtime, band or None if the tile could not be read), observed frame first."""
    base = parse_jma_time(basetime)
    series = []
    for validtime, band in frames:
        when = parse_jma_time(validtime)
        series.append({
            "time": when.isoformat(),
            "minutes": int((when - base) / timedelta(minutes=1)),
            "band": band,
            "mmh_upper": None if band is None else BAND_MMH[band][1],
        })
    known = [s["band"] for s in series if s["band"] is not None]
    raining_now = bool(series) and (series[0]["band"] or 0) >= RAIN_BAND_RAIN
    minutes = next(
        (s["minutes"] for s in series[1:] if (s["band"] or 0) >= RAIN_BAND_RAIN), None
    )
    return RainSummary(
        basetime=basetime,
        series=series,
        max_mmh=BAND_MMH[max(known)][1] if known else None,
        minutes_to_rain=minutes,
        raining_now=raining_now,
    )
