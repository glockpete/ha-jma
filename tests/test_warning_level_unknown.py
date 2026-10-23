"""sensor.jma_<place>_warning_level: a file that does not hold the entry's area says nothing about it, so the sensor
is unknown (not 0, "nothing in force")."""
from __future__ import annotations

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from .conftest import AREA, mock_jma, setup
from .helpers import clone, load_json

SENSOR = "sensor.jma_chiyoda_warning_level"


def _without_the_area() -> list:
    records = clone(load_json("warning_r8_130000.json"))
    for record in records:
        for key in ("class20Items", "class10Items"):
            record["warning"][key] = [i for i in record["warning"].get(key, []) if i["areaCode"] != AREA]
    return records


async def test_an_area_missing_from_the_file_is_unknown_not_zero(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock, warnings=_without_the_area())
    await setup(hass, entry)
    # The coordinator holds a file without the area for two polls (a partial file would be diffed into "cleared" and
    # "issued"); the sensor is unavailable until it believes the file.
    assert hass.states.get(SENSOR).state == STATE_UNAVAILABLE
    for _ in range(2):
        await entry.runtime_data.warnings.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(SENSOR)
    assert state.state == STATE_UNKNOWN  # available, but nothing is known
    assert state.attributes["area_missing"] is True
    assert state.attributes["warnings"] == []
    assert state.attributes["area"] == AREA


async def test_an_area_in_the_file_with_nothing_in_force_is_still_zero(
        tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    state = hass.states.get(SENSOR)
    assert state.state == "0" and state.attributes["area_missing"] is False
