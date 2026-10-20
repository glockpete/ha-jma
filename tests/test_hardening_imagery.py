"""Hardening, imagery: how a map tile is decoded and read, and how many requests one poll fans out to.

The PNGs here are built in the test (no real tile is hostile); the feeds are the real fixtures plus thousands of
extra entries, served by a counting stand-in for JmaApi.
"""

from __future__ import annotations

import asyncio
import collections
import io
import json
import logging
import math
import struct
import zlib
from datetime import datetime, timedelta

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from PIL import Image, PngImagePlugin

from custom_components.jma.api import JmaApi
from custom_components.jma.const import (
    KIKIKURU_FLOOD_PALETTE,
    KIKIKURU_PALETTE,
    RAIN_PALETTE,
    URL_AMEDAS_POINT,
    URL_RASRF_TIMES,
)
from custom_components.jma.coordinator import (
    AmedasCoordinator,
    JmaConfig,
    LayersCoordinator,
    NowcastCoordinator,
    TyphoonCoordinator,
)
from custom_components.jma.errors import JmaError, JmaParseError
from custom_components.jma.parse_amedas import merge_observations, parse_point
from custom_components.jma.parse_layers import kikikuru_level_at, rasrf_frames
from custom_components.jma.parse_nowcast import band_at

from .conftest import ENTRY_DATA, LAT, LON, OTHER_STATION, mock_jma, setup
from .helpers import load_bytes, load_json

CFG = JmaConfig(office="130000", area=ENTRY_DATA["area"], area_name="x", class10="130010",
                station_primary="44132", station_secondary="44132", latitude=LAT, longitude=LON)
BLANK_TILE = load_bytes("tile_hrpns_z10_909_403.png")


# ---------- helpers ----------

def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _solid(rgba: tuple[int, int, int, int], size: tuple[int, int] = (256, 256)) -> bytes:
    return _png(Image.new("RGBA", size, rgba))


def _solid_paletted(rgb: tuple[int, int, int]) -> bytes:
    """A paletted tile (colour type 3, as the JMA servers send them) with one opaque colour."""
    img = Image.new("P", (256, 256), 0)
    img.putpalette(list(rgb))
    return _png(img)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def _bomb_png(width: int, height: int, *, fill: bool) -> bytes:
    """A 1-bit greyscale PNG that DECLARES width x height. fill=True carries every (zero) row, compressed in a
    stream, so a decoder really would have to inflate width x height pixels; fill=False carries one row.
    Pillow's sniff and the test never allocate the image."""
    row = b"\0" * (1 + (width + 7) // 8)
    comp = zlib.compressobj(9)
    out: list[bytes] = []
    rows = height if fill else 1
    per_block = max(1, (4 << 20) // len(row))
    while rows > 0:
        n = min(per_block, rows)
        out.append(comp.compress(row * n))
        rows -= n
    out.append(comp.flush())
    ihdr = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", b"".join(out)) + _chunk(b"IEND", b"")


class _Decoded(BaseException):
    """Raised by the spies when an oversized image is decoded. A BaseException, so the code under test cannot
    turn it into a JmaParseError and the test pass for the wrong reason."""


@pytest.fixture
def no_big_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the moment Pillow decodes, crops or converts an image bigger than one tile."""

    def guard(real):
        def spy(self, *args, **kwargs):
            if self.size[0] * self.size[1] > 256 * 256:
                raise _Decoded(f"{real.__name__} on a {self.size} image")
            return real(self, *args, **kwargs)
        return spy

    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", guard(PngImagePlugin.PngImageFile.load))
    monkeypatch.setattr(Image.Image, "convert", guard(Image.Image.convert))
    monkeypatch.setattr(Image.Image, "crop", guard(Image.Image.crop))


def _both_readers():
    """(name, reader(png, px, py)) for band_at and the three キキクル layers."""
    return [
        ("band_at", lambda png, px, py: band_at(png, px, py)),
        ("land", lambda png, px, py: kikikuru_level_at(png, px, py, "land")),
        ("inund", lambda png, px, py: kikikuru_level_at(png, px, py, "inund")),
        ("flood_mesh", lambda png, px, py: kikikuru_level_at(png, px, py, "flood_mesh")),
    ]


READERS = _both_readers()


# ---------- I1: the tile is checked before it is decoded ----------

@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
@pytest.mark.parametrize("name,reader", READERS, ids=[n for n, _ in READERS])
@pytest.mark.parametrize("size,fill", [((12000, 12000), True), ((9000, 9000), True), ((40000, 40000), False)])
def test_oversized_png_is_rejected_without_decoding(no_big_decode, name, reader, size, fill) -> None:
    png = _bomb_png(*size, fill=fill)
    assert len(png) < 200_000  # the file is small, what it declares is not
    with pytest.raises(JmaParseError):
        reader(png, 100, 100)


@pytest.mark.parametrize("name,reader", READERS, ids=[n for n, _ in READERS])
@pytest.mark.parametrize("size", [(1, 1), (255, 256), (256, 255), (257, 256), (512, 512)])
def test_a_tile_that_is_not_256_by_256_is_rejected(name, reader, size) -> None:
    with pytest.raises(JmaParseError):
        reader(_solid((242, 242, 255, 255), size), 100, 100)


@pytest.mark.parametrize("name,reader", READERS, ids=[n for n, _ in READERS])
def test_only_the_block_is_converted_to_rgba(monkeypatch: pytest.MonkeyPatch, name, reader) -> None:
    """The (2r+1)^2 block is cropped first; the 256x256 tile itself is never converted."""
    converted: list[tuple[int, int]] = []
    real = Image.Image.convert

    def spy(self, *args, **kwargs):
        converted.append(self.size)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "convert", spy)
    reader(load_bytes("tile_rasrf_z10_sample.png"), 100, 100)
    assert converted and all(w <= 5 and h <= 5 for w, h in converted), converted


@pytest.mark.parametrize("name,reader", READERS, ids=[n for n, _ in READERS])
def test_garbage_and_non_png_bytes_are_a_parse_error(name, reader) -> None:
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), (255, 40, 0)).save(buf, "JPEG")  # right size, not a PNG
    for body in (b"not a png", b"", b"\x89PNG\r\n\x1a\n" + b"\0" * 20, buf.getvalue()):
        with pytest.raises(JmaParseError):
            reader(body, 100, 100)


def test_block_at_the_tile_edge_is_clamped() -> None:
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    img.putpixel((255, 255), (255, 40, 0, 255))
    png = _png(img)
    assert band_at(png, 255, 255) == 7
    assert band_at(png, 253, 253) == 7  # the 5x5 block reaches the corner
    assert band_at(png, 252, 252) == 0
    assert band_at(png, 0, 0) == 0
    assert band_at(png, 300, 300) == 0  # a point outside the tile has an empty block


# ---------- I2: a colour that is on no palette is "unknown" (0), not the nearest band ----------

SOLID = {
    "black": (0, 0, 0, 255),
    "white": (255, 255, 255, 255),
    "grey": (128, 128, 128, 255),
    "transparent": (0, 0, 0, 0),
}


@pytest.mark.parametrize("colour", sorted(SOLID))
def test_solid_placeholder_tile_reads_zero_for_the_rain_bands(colour) -> None:
    assert band_at(_solid(SOLID[colour]), 100, 100) == 0
    assert band_at(_solid(SOLID[colour]), 100, 100, radius=0) == 0


@pytest.mark.parametrize("colour", sorted(SOLID))
@pytest.mark.parametrize("element", ["land", "inund", "flood_mesh"])
def test_solid_placeholder_tile_reads_zero_for_kikikuru(colour, element) -> None:
    # #ffffff is the legend colour of level 1 on 土砂 and 浸水 (const.KIKIKURU_PALETTE): an exact palette
    # colour is not unknown, so only flood_mesh (whose level 1 is cyan) reads a white tile as 0.
    expected = 1 if colour == "white" and element != "flood_mesh" else 0
    assert kikikuru_level_at(_solid(SOLID[colour]), 100, 100, element) == expected


@pytest.mark.parametrize("rgb", [(0, 0, 0), (255, 255, 255), (128, 128, 128)])
def test_solid_paletted_placeholder_reads_zero(rgb) -> None:
    png = _solid_paletted(rgb)
    assert band_at(png, 100, 100) == 0
    assert kikikuru_level_at(png, 100, 100, "flood_mesh") == 0
    if rgb != (255, 255, 255):
        assert kikikuru_level_at(png, 100, 100, "land") == 0


@pytest.mark.parametrize("rgb,band", sorted(RAIN_PALETTE.items()))
def test_every_rain_palette_colour_still_reads_its_band(rgb, band) -> None:
    assert band_at(_solid((*rgb, 255)), 100, 100, radius=0) == band
    assert band_at(_solid_paletted(rgb), 100, 100, radius=0) == band


@pytest.mark.parametrize("element,palette", [("land", KIKIKURU_PALETTE), ("inund", KIKIKURU_PALETTE),
                                             ("flood_mesh", KIKIKURU_FLOOD_PALETTE)])
def test_every_kikikuru_palette_colour_still_reads_its_level(element, palette) -> None:
    for rgb, level in palette.items():
        assert kikikuru_level_at(_solid((*rgb, 255)), 100, 100, element, radius=0) == level
        assert kikikuru_level_at(_solid_paletted(rgb), 100, 100, element, radius=0) == level


def test_tolerance_boundary_is_the_documented_squared_distance() -> None:
    from custom_components.jma.parse_nowcast import PALETTE_TOLERANCE_SQ

    k = math.isqrt(PALETTE_TOLERANCE_SQ)
    near = (255 - k, 40, 0, 255)  # k**2 <= tolerance from #ff2800 (band 7)
    far = (255 - k - 1, 40, 0, 255)  # (k+1)**2 > tolerance
    assert band_at(_solid(near), 100, 100, radius=0) == 7
    assert band_at(_solid(far), 100, 100, radius=0) == 0
    # the near colours the earlier tests used still map to their nearest palette colour
    assert band_at(_solid((254, 42, 3, 255)), 100, 100, radius=0) == 7
    assert kikikuru_level_at(_solid((250, 225, 10, 255)), 100, 100, "land", radius=0) == 2


def test_an_unknown_colour_is_still_logged_once_at_info(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    from custom_components.jma import parse_layers, parse_nowcast

    monkeypatch.setattr(parse_nowcast, "_UNKNOWN_SEEN", set())
    monkeypatch.setattr(parse_layers, "_UNKNOWN_SEEN", set())
    caplog.set_level(logging.INFO)
    for _ in range(3):
        assert band_at(_solid((0, 0, 0, 255)), 100, 100) == 0
        assert kikikuru_level_at(_solid((0, 0, 0, 255)), 100, 100, "flood_mesh") == 0
    lines = [r.getMessage() for r in caplog.records if "Unlisted" in r.getMessage()]
    assert len(lines) == 2, lines
    assert any("nowcast colour (0, 0, 0)" in m for m in lines)
    assert any("flood_mesh colour (0, 0, 0)" in m for m in lines)


# ---------- I3: what one poll fans out to is bounded ----------

class CountingApi:
    """Stands in for JmaApi: answers every URL from `routes` (substring -> payload), blank tile otherwise;
    records the URLs and, per kind of URL, the most requests in flight at once."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.urls: list[str] = []
        self.inflight: collections.Counter = collections.Counter()
        self.peak: collections.Counter = collections.Counter()

    @staticmethod
    def kind(url: str) -> str:
        if url.endswith(".png"):
            return "rasrf_tile" if "/rasrf/" in url else "tile"
        return "file"

    async def _hit(self, url: str):
        kind = self.kind(url)
        self.urls.append(url)
        self.inflight[kind] += 1
        self.peak[kind] = max(self.peak[kind], self.inflight[kind])
        await asyncio.sleep(0)
        self.inflight[kind] -= 1
        for key, value in self.routes.items():
            if key in url:
                return value
        return BLANK_TILE

    async def get_json(self, url, params=None):
        return await self._hit(url)

    async def get_bytes(self, url):
        return await self._hit(url)

    async def get_text(self, url, params=None):
        return await self._hit(url)

    def count(self, kind: str) -> int:
        return sum(1 for u in self.urls if self.kind(u) == kind)


def _debug_lines(caplog, needle: str) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.DEBUG and r.name == "custom_components.jma.coordinator"
            and needle in r.getMessage()]


async def test_typhoon_list_size_does_not_drive_the_request_count(hass, entry, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.jma.coordinator")
    listed = [{"tropicalCyclone": f"TC{i:04d}", "typhoonNumber": None} for i in range(500)]
    api = CountingApi({"targetTc.json": json.dumps(listed).encode(),
                       "forecast.json": load_json("typhoon_TC2632_forecast.json"),
                       "specifications.json": load_json("typhoon_TC2632_specs.json")})
    entry.add_to_hass(hass)
    state = await TyphoonCoordinator(hass, entry, api, CFG)._fetch()
    assert len(api.urls) == 1 + 2 * 8  # the list, then forecast + specifications of the first 8 storms
    assert api.peak["file"] <= 6
    assert state.count == 8 and len(state.storms) <= 8
    assert len(_debug_lines(caplog, "only the first 8")) == 1


async def test_typhoon_normal_feed_is_unchanged(hass, entry, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.jma.coordinator")
    api = CountingApi({"targetTc.json": json.dumps(load_json("typhoon_targetTc.json")).encode(),
                       "forecast.json": load_json("typhoon_TC2632_forecast.json"),
                       "specifications.json": load_json("typhoon_TC2632_specs.json")})
    entry.add_to_hass(hass)
    state = await TyphoonCoordinator(hass, entry, api, CFG)._fetch()
    assert len(api.urls) == 3 and state.count == 1 and len(state.storms) == 1
    assert not _debug_lines(caplog, "only the first")


def _nowcast_feed(extra: int) -> dict[str, object]:
    n1, n2 = load_json("targetTimes_N1.json"), load_json("targetTimes_N2.json")
    base = max(t["basetime"] for t in n2)
    start = datetime.strptime(base, "%Y%m%d%H%M%S")
    more = [{"basetime": base, "validtime": (start + timedelta(minutes=5 * (i + 30))).strftime("%Y%m%d%H%M%S"),
             "elements": ["hrpns"]} for i in range(extra)]
    return {"targetTimes_N1": n1, "targetTimes_N2": n2 + more}


async def test_nowcast_list_size_does_not_drive_the_tile_count(hass, entry, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.jma.coordinator")
    api = CountingApi(_nowcast_feed(2000))
    entry.add_to_hass(hass)
    coord = NowcastCoordinator(hass, entry, api, CFG, lambda *_: None)
    state = await coord._fetch()
    assert api.count("tile") == 1 + 13  # the observed frame and at most 13 forecast frames
    assert api.peak["tile"] <= 6
    assert len(state.forecast) == 13 and len(state.rain.series) == 14 and len(coord._bands) == 14
    assert len(_debug_lines(caplog, "only the first 13")) == 1


async def test_nowcast_normal_feed_is_unchanged(hass, entry, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.jma.coordinator")
    api = CountingApi(_nowcast_feed(0))
    entry.add_to_hass(hass)
    state = await NowcastCoordinator(hass, entry, api, CFG, lambda *_: None)._fetch()
    assert len(state.forecast) == 12 and api.count("tile") == 13 and len(state.rain.series) == 13
    assert api.peak["tile"] <= 6
    assert not _debug_lines(caplog, "only the first")


def _layers_routes(extra: int) -> dict[str, object]:
    rasrf = load_json("rasrf_targetTimes.json")
    base = max(r["basetime"] for r in rasrf)
    start = datetime.strptime(base, "%Y%m%d%H%M%S")
    more = [{"basetime": base, "validtime": (start + timedelta(minutes=5 * (i + 30))).strftime("%Y%m%d%H%M%S"),
             "member": "none", "elements": ["rasrf"]} for i in range(extra)]
    return {URL_RASRF_TIMES: rasrf + more,
            "risk/targetTimes": load_json("risk_targetTimes.json"),
            "targetTimes_N3": load_json("targetTimes_N3_b.json"),
            "targetTimes_jp": load_json("himawari_targetTimes_jp.json")}


async def test_rasrf_list_size_does_not_drive_the_tile_count(hass, entry, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.jma.coordinator")
    api = CountingApi(_layers_routes(2000))
    entry.add_to_hass(hass)
    state = await LayersCoordinator(hass, entry, api, CFG)._fetch()
    assert api.count("rasrf_tile") == 16
    assert api.peak["rasrf_tile"] <= 6
    assert state.rain_outlook is not None and len(state.rain_outlook.series) == 16
    assert len(_debug_lines(caplog, "only the first 16")) == 1


async def test_rasrf_normal_feed_is_unchanged(hass, entry, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.jma.coordinator")
    api = CountingApi(_layers_routes(0))
    entry.add_to_hass(hass)
    state = await LayersCoordinator(hass, entry, api, CFG)._fetch()
    listed = len(rasrf_frames(load_json("rasrf_targetTimes.json"))[1])
    assert 0 < listed <= 16
    assert api.count("rasrf_tile") == listed and len(state.rain_outlook.series) == listed
    assert api.peak["rasrf_tile"] <= 6
    assert not _debug_lines(caplog, "only the first")


# ---------- I4: one station is fetched once ----------

def _point_calls(aioclient_mock, station: str) -> int:
    url = URL_AMEDAS_POINT.format(station=station, ymd="20260926", hh="12")
    return sum(1 for c in aioclient_mock.mock_calls if str(c[1]) == url)


async def test_same_primary_and_secondary_station_is_fetched_once(tokyo, aioclient_mock, entry) -> None:
    mock_jma(aioclient_mock)
    entry.add_to_hass(tokyo)
    coord = AmedasCoordinator(tokyo, entry, JmaApi(async_get_clientsession(tokyo)), CFG)
    obs = await coord._fetch()
    assert _point_calls(aioclient_mock, "44132") == 1
    assert obs == merge_observations(parse_point(load_json("point_44132_12.json"), "44132"), None)
    assert coord.station_ok.get("44132") is not None and coord.station_error.get("44132") is None


async def test_same_station_down_is_still_an_error(tokyo, aioclient_mock, entry) -> None:
    aioclient_mock.get(URL_AMEDAS_POINT.format(station="44132", ymd="20260926", hh="12"), status=503)
    mock_jma(aioclient_mock)  # the first registered match answers
    entry.add_to_hass(tokyo)
    coord = AmedasCoordinator(tokyo, entry, JmaApi(async_get_clientsession(tokyo)), CFG)
    with pytest.raises(JmaError, match="neither station answered"):
        await coord._fetch()
    assert _point_calls(aioclient_mock, "44132") == 1


async def test_two_different_stations_are_both_fetched_and_merged(tokyo, aioclient_mock, entry_two_stations) -> None:
    mock_jma(aioclient_mock)
    await setup(tokyo, entry_two_stations)
    assert _point_calls(aioclient_mock, "44132") == 1
    assert _point_calls(aioclient_mock, OTHER_STATION) == 1


async def test_setup_with_one_station_fetches_its_point_file_once(tokyo, aioclient_mock, entry) -> None:
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    assert _point_calls(aioclient_mock, "44132") == 1
