"""Diagnostics hide the place: coordinates, area code and names, station ids and the entry title never
leave in a download; the office code (prefecture level) stays."""

from __future__ import annotations

import json
from types import SimpleNamespace

from homeassistant.core import HomeAssistant

from custom_components.jma.const import NOWCAST_ZOOM, URL_NOWC_TILE
from custom_components.jma.diagnostics import _scrub, _secrets, async_get_config_entry_diagnostics

from .conftest import AREA, ENTRY_DATA, LAT, LON, TILE_X, TILE_Y, TITLE, mock_jma, setup

REDACTED_VALUES = [
    str(LAT), str(LON), "35.68", "139.77",           # coordinates, exact and rounded
    AREA, ENTRY_DATA["area_name"], ENTRY_DATA["class10"],
    ENTRY_DATA["station_primary"], ENTRY_DATA["station_secondary"],
    TITLE, "Chiyoda",
]


async def test_diagnostics_hold_none_of_the_place(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    out = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(out, ensure_ascii=False, default=str)
    for value in REDACTED_VALUES:
        assert value not in text, value
    assert out["config"]["office"] == ENTRY_DATA["office"]  # prefecture level stays
    assert out["entry"]["data"]["office"] == ENTRY_DATA["office"]
    for key in ("latitude", "longitude", "area", "area_name", "class10", "station_primary", "station_secondary"):
        assert out["config"][key] == "**REDACTED**", key
    assert out["entry"]["title"] == "**REDACTED**"
    assert out["entry"]["data"]["area"] == "**REDACTED**"


async def test_diagnostics_scrub_errors_that_name_the_place(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    """A failed fetch keeps its URL in last_error, and the URL of an AMeDAS file holds the station id."""
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    rt = entry.runtime_data
    url = "https://www.jma.go.jp/bosai/amedas/data/point/44132/20260926_12.json"
    rt.forecast.last_error = f"{url}: HTTP 500 (area {AREA}, {ENTRY_DATA['area_name']})"
    rt.warnings.xml_error = f"no bulletin for {AREA} at {LAT},{LON}"
    out = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(out, ensure_ascii=False, default=str)
    for value in (AREA, "44132", ENTRY_DATA["area_name"], str(LAT), str(LON)):
        assert value not in text, value
    assert "HTTP 500" in text  # the rest of the message is kept


async def test_diagnostics_scrub_the_tile_numbers_of_a_failed_tile_download(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    """The tile a download asked for follows from the coordinates (zoom 10: about 32 km wide at this latitude):
    its x and y are the place."""
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    rt = entry.runtime_data
    url = URL_NOWC_TILE.format(basetime="20260926041500", validtime="20260926042500", z=NOWCAST_ZOOM, x=TILE_X, y=TILE_Y)
    rt.nowcast.last_error = f"{url}: HTTP 500"
    rt.layers.sources["risk"].last_error = f"{url.replace('hrpns', 'land')}: HTTP 404"
    out = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(out, ensure_ascii=False, default=str)
    assert f"{TILE_X}/{TILE_Y}" not in text
    assert "HTTP 500" in text and "HTTP 404" in text and f"hrpns/{NOWCAST_ZOOM}/" in text  # the rest is kept


def test_diagnostics_scrub_an_area_name_of_two_characters() -> None:
    """Japanese area names are often two characters long; they are scrubbed inside free text too (digits that
    short are not: a 2- or 3-digit value would cut up every number)."""
    # The tests' place (conftest) with one invented value: a two-character name, "試験" (it names no place).
    short_name = "試験"
    cfg = SimpleNamespace(area=AREA, area_name=short_name, class10=ENTRY_DATA["class10"],
                          station_primary=ENTRY_DATA["station_primary"],
                          station_secondary=ENTRY_DATA["station_secondary"], latitude=LAT, longitude=LON)
    entry = SimpleNamespace(runtime_data=SimpleNamespace(cfg=cfg), title=TITLE,
                            data={"area_name_ja": short_name}, options={})
    secrets = _secrets(entry)
    assert short_name in secrets
    out = _scrub(f"area {short_name} not in the list; HTTP 500", secrets)
    assert short_name not in out and "HTTP 500" in out
