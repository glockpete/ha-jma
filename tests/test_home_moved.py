"""The rain-nowcast tile is read for the coordinates copied from Home Assistant's home when the entry sets up, so
moving the home in Settings reloads the entry (EVENT_CORE_CONFIG_UPDATE, the pattern the core's sun and zone
integrations use). Any other change to the core configuration leaves the entry alone."""
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.jma.const import NOWCAST_ZOOM, URL_NOWC_TILE
from custom_components.jma.parse_nowcast import frame_lists, tile_for

from .conftest import LAT, LON, mock_jma, setup
from .helpers import load_bytes, load_json

OSAKA_LAT, OSAKA_LON = 34.7025, 135.4959  # Osaka Station


def _serve_tile_for(aioclient_mock, lat: float, lon: float) -> None:
    """mock_jma serves the nowcast tile of the tests' place only: serve the same picture for another one."""
    x, y, _px, _py = tile_for(lat, lon, NOWCAST_ZOOM)
    base, _past, forecast = frame_lists(load_json("targetTimes_N1.json"), load_json("targetTimes_N2.json"), 12)
    png = load_bytes("tile_hrpns_z10_909_403.png")
    for valid in [base, *forecast]:
        aioclient_mock.get(URL_NOWC_TILE.format(basetime=base, validtime=valid, z=NOWCAST_ZOOM, x=x, y=y), content=png)


async def test_moving_the_home_reloads_the_entry_with_the_new_coordinates(
        tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    _serve_tile_for(aioclient_mock, OSAKA_LAT, OSAKA_LON)
    await setup(hass, entry)
    before = entry.runtime_data
    assert (before.cfg.latitude, before.cfg.longitude) == (LAT, LON)

    await hass.config.async_update(latitude=OSAKA_LAT, longitude=OSAKA_LON)
    await hass.async_block_till_done()

    after = entry.runtime_data
    assert after is not before  # the entry set up again
    assert (after.cfg.latitude, after.cfg.longitude) == (OSAKA_LAT, OSAKA_LON)


async def test_moving_only_the_latitude_reloads_the_entry_too(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    _serve_tile_for(aioclient_mock, OSAKA_LAT, LON)
    await setup(hass, entry)
    before = entry.runtime_data

    await hass.config.async_update(latitude=OSAKA_LAT)
    await hass.async_block_till_done()

    assert entry.runtime_data is not before
    assert (entry.runtime_data.cfg.latitude, entry.runtime_data.cfg.longitude) == (OSAKA_LAT, LON)


@pytest.mark.parametrize("change", [
    {"latitude": LAT, "longitude": LON},     # saved again with the place it has
    {"location_name": "Somewhere else"},     # only the name
    {"elevation": 12},
    {"time_zone": "Asia/Tokyo"},
    {},                                      # the event the core fires at start-up carries no data at all
])
async def test_any_other_change_of_the_core_configuration_leaves_the_entry_alone(
        tokyo: HomeAssistant, aioclient_mock, entry, change: dict) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    before = entry.runtime_data

    await hass.config.async_update(**change)
    await hass.async_block_till_done()

    assert entry.runtime_data is before  # not set up again
    assert (before.cfg.latitude, before.cfg.longitude) == (LAT, LON)


async def test_an_unloaded_entry_no_longer_listens(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    calls_before = aioclient_mock.call_count

    await hass.config.async_update(latitude=OSAKA_LAT, longitude=OSAKA_LON)
    await hass.async_block_till_done()

    assert aioclient_mock.call_count == calls_before  # nothing set up again, nothing fetched
