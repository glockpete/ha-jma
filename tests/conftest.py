"""HA test fixtures: every JMA / Open-Meteo URL is served from samples of the services' published files
(see tests/fixtures/README.md; the optional feeds are registered in mock_typhoon_wbgt_and_layers)."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.jma.const import (
    DOMAIN,
    KIKIKURU_ELEMENTS,
    NOWCAST_ZOOM,
    URL_AMEDAS_LATEST,
    URL_AMEDAS_POINT,
    URL_AMEDAS_TABLE,
    URL_AREA,
    URL_FORECAST,
    URL_HIMAWARI_TIMES,
    URL_JAPAN_FORECAST,
    URL_NOWC_TILE,
    URL_NOWC_TIMES,
    URL_OPEN_METEO,
    URL_OVERVIEW,
    URL_RASRF_TILE,
    URL_RASRF_TIMES,
    URL_RISK_TILE,
    URL_RISK_TIMES,
    URL_TYPHOON_FORECAST,
    URL_TYPHOON_SPECS,
    URL_TYPHOON_TARGET,
    URL_WARNING_TIMELINE,
    URL_WARNINGS,
    URL_WBGT,
    URL_XML_EXTRA,
)
from custom_components.jma.parse_layers import rasrf_frames, risk_frames
from custom_components.jma.parse_nowcast import frame_lists, tile_for
from custom_components.jma.parse_quake import QuakePlace

from .helpers import load_bytes, load_json, load_text

# THE TESTS' PLACE: one neutral public place, set here and nowhere else. The area of Chiyoda (class20
# 1310100) with the coordinates of Tokyo Station; the entry title and every entity id follow
# (JMA Chiyoda gives weather.jma_chiyoda). The entry's stations are the ones the integration's own
# nearest-station rule gives for that place (AMeDAS 44132 for both). A test that needs two different
# stations sets the second one through the entry options (what the options flow stores): OTHER_STATION, a
# synthetic point file for a station in Osaka prefecture.
LAT, LON = 35.6812, 139.7671
AREA = "1310100"
TITLE = "JMA Chiyoda"
ENTRY_DATA = {
    "office": "130000",
    "area": AREA,
    "area_name": "千代田区",
    "area_name_ja": "千代田区",
    "class10": "130010",
    "station_primary": "44132",
    "station_secondary": "44132",
}
OTHER_STATION = "62016"
# What the quake part asks of the feed for that entry: its prefecture (office 130000) and its local name.
QUAKE_PLACE = QuakePlace("東京都", ENTRY_DATA["area_name"])
# The tile and the pixel of the tests' place at zoom 10 (the tile is the one of
# tests/fixtures/tile_hrpns_z10_909_403.png).
TILE_X, TILE_Y, TILE_PX, TILE_PY = tile_for(LAT, LON, NOWCAST_ZOOM)
NOW = "2026-09-26T04:12:00+00:00"  # a moment after the newest AMeDAS sample file


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/."""


@pytest.fixture(autouse=True)
def no_quake_socket() -> Generator[None]:
    """The P2P socket never runs in tests; QuakeClient.handle is driven directly.

    Event spacing is 0 here: asyncio.sleep never returns while the freezer holds the clock.
    """
    with (
        patch("custom_components.jma.quake_ws.QuakeClient.run", new=AsyncMock()),
        patch("custom_components.jma.event.EVENT_SPACING", 0),
    ):
        yield


@pytest.fixture
async def tokyo(hass: HomeAssistant, freezer) -> HomeAssistant:
    freezer.move_to(NOW)
    hass.config.latitude = LAT
    hass.config.longitude = LON
    await hass.config.async_set_time_zone("Asia/Tokyo")
    return hass


def mock_jma(aioclient_mock: AiohttpClientMocker, *, warnings=None, tile: str | None = None,
             xml: str | None = None, area_ok: bool = True) -> None:
    """Register every endpoint the integration polls. area_ok=False: the area list answers HTTP 503."""
    if area_ok:
        aioclient_mock.get(URL_AREA, json=load_json("area.json"))
    else:
        aioclient_mock.get(URL_AREA, status=503)
    aioclient_mock.get(URL_AMEDAS_TABLE, json=load_json("amedastable.json"))
    aioclient_mock.get(URL_FORECAST.format(office="130000"), json=load_json("forecast_130000.json"))
    aioclient_mock.get(URL_OVERVIEW.format(office="130000"), json=load_json("overview_130000.json"))
    aioclient_mock.get(URL_OPEN_METEO, json=load_json("openmeteo_msm.json"))
    aioclient_mock.get(URL_AMEDAS_LATEST, text="2026-09-26T12:50:00+09:00")
    for station in ("44132", OTHER_STATION):
        aioclient_mock.get(URL_AMEDAS_POINT.format(station=station, ymd="20260926", hh="12"),
                           json=load_json(f"point_{station}_12.json"))
    aioclient_mock.get(URL_WARNINGS.format(office="130000"),
                       json=warnings if warnings is not None else load_json("warning_r8_130000.json"))
    aioclient_mock.get(URL_XML_EXTRA, text=xml if xml is not None else load_text("extra.xml"))
    n1, n2 = load_json("targetTimes_N1.json"), load_json("targetTimes_N2.json")
    aioclient_mock.get(URL_NOWC_TIMES.format(n="N1"), json=n1)
    aioclient_mock.get(URL_NOWC_TIMES.format(n="N2"), json=n2)
    base, _past, forecast = frame_lists(n1, n2, 12)
    png = load_bytes(tile or "tile_hrpns_z10_909_403.png")
    for valid in [base, *forecast]:
        aioclient_mock.get(URL_NOWC_TILE.format(basetime=base, validtime=valid, z=10, x=TILE_X, y=TILE_Y),
                           content=png)
    mock_typhoon_wbgt_and_layers(aioclient_mock)


def mock_typhoon_wbgt_and_layers(aioclient_mock: AiohttpClientMocker) -> None:
    """The optional feeds: timeline, typhoon, WBGT, layers."""
    aioclient_mock.get(URL_WARNING_TIMELINE.format(office="130000"),
                       json=load_json("warning_timeline_130000.json"))
    aioclient_mock.get(URL_TYPHOON_TARGET, json=load_json("typhoon_targetTc.json"))
    aioclient_mock.get(URL_TYPHOON_FORECAST.format(tc="TC2632"), json=load_json("typhoon_TC2632_forecast.json"))
    aioclient_mock.get(URL_TYPHOON_SPECS.format(tc="TC2632"), json=load_json("typhoon_TC2632_specs.json"))
    aioclient_mock.get(URL_WBGT.format(pref="tokyo"), text=load_text("wbgt_yohou_tokyo.csv"))
    aioclient_mock.get(URL_NOWC_TIMES.format(n="N3"), json=load_json("targetTimes_N3_b.json"))
    aioclient_mock.get(URL_HIMAWARI_TIMES, json=load_json("himawari_targetTimes_jp.json"))
    risk = load_json("risk_targetTimes.json")
    aioclient_mock.get(URL_RISK_TIMES, json=risk)
    newest = risk_frames(risk)[-1]
    blank = load_bytes("tile_risk_transparent_z10.png")  # a sample risk tile with no coloured pixel
    for elem in KIKIKURU_ELEMENTS.values():
        aioclient_mock.get(URL_RISK_TILE.format(elem=elem, z=10, x=TILE_X, y=TILE_Y, **newest), content=blank)
    rasrf = load_json("rasrf_targetTimes.json")
    aioclient_mock.get(URL_RASRF_TIMES, json=rasrf)
    rain = load_bytes("tile_rasrf_z10_sample.png")
    for frame in rasrf_frames(rasrf)[1]:
        aioclient_mock.get(URL_RASRF_TILE.format(z=10, x=TILE_X, y=TILE_Y, **frame), content=rain)
    # nationwide city forecast
    aioclient_mock.get(URL_JAPAN_FORECAST, json=load_json("forecast_010000.json"))


@pytest.fixture
def entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, title=TITLE, unique_id=AREA, data=ENTRY_DATA)


@pytest.fixture
def entry_two_stations() -> MockConfigEntry:
    """The same entry with a different secondary station, as the options flow stores it."""
    return MockConfigEntry(domain=DOMAIN, title=TITLE, unique_id=AREA, data=ENTRY_DATA,
                           options={"station_secondary": OTHER_STATION})


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
