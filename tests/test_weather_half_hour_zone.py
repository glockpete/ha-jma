"""The hourly rows are JST hours, so "this hour" is floored in JST. Floored in Home Assistant's own time zone it lands
at :30 past the JST hour in a half-hour zone (Asia/Kolkata +05:30, Australia/Adelaide +09:30) and the next hour's row
is the one that is read."""
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from .conftest import mock_jma, setup

# 09:40 UTC is 18:40 JST; the Open-Meteo sample (tests/fixtures/openmeteo_msm.json) holds apparent temperatures of
# 25.7 at 18:00 JST and 24.8 at 19:00 JST.
INSTANT = "2026-09-26T09:40:00+00:00"


@pytest.mark.parametrize("zone", ["Asia/Tokyo", "Asia/Kolkata", "Australia/Adelaide"])
async def test_the_current_hour_row_is_the_japanese_hour_whatever_the_zone(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, zone: str) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    await hass.config.async_set_time_zone(zone)
    freezer.move_to(INSTANT)
    entry.runtime_data.hourly.async_update_listeners()  # the weather entity writes its state again
    await hass.async_block_till_done()

    assert hass.states.get("weather.jma_chiyoda").attributes["apparent_temperature"] == 25.7  # the 18:00 JST row


@pytest.mark.parametrize("zone", ["Asia/Tokyo", "Asia/Kolkata", "Australia/Adelaide"])
async def test_the_hourly_forecast_starts_at_the_current_japanese_hour(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, zone: str) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    await hass.config.async_set_time_zone(zone)
    freezer.move_to(INSTANT)
    hourly = await hass.services.async_call(
        "weather", "get_forecasts", {"entity_id": "weather.jma_chiyoda", "type": "hourly"},
        blocking=True, return_response=True)
    assert hourly["weather.jma_chiyoda"]["forecast"][0]["datetime"].startswith("2026-09-26T18:00")
