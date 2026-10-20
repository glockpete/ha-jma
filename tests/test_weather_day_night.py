"""weather.jma_chiyoda reads sun.sun for day/night (clear-night vs sunny, the
night JMA icon) but listened only to its coordinators, so the swap waited for the next coordinator update
(up to 5 min after sunrise / sunset). It now writes its state when sun.sun crosses the horizon, and only then.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.jma.parse_forecast import condition_for

from .conftest import mock_jma, setup


async def _sunny_today(hass: HomeAssistant, entry) -> None:
    """Today's JMA code becomes 100 (晴: sunny by day, clear-night by night) and the nowcast is dry."""
    runtime = entry.runtime_data
    runtime.nowcast.async_set_updated_data(dataclasses.replace(runtime.nowcast.data, wet=False))
    bundle = runtime.forecast.data
    today = dt_util.now().date()
    days = [dataclasses.replace(d, code="100") if d.date == today else d for d in bundle.forecast.days]
    forecast = dataclasses.replace(bundle.forecast, days=days)
    runtime.forecast.async_set_updated_data(dataclasses.replace(bundle, forecast=forecast))
    await hass.async_block_till_done()


async def test_sunset_and_sunrise_swap_the_condition_at_once(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    assert condition_for("100", False) == "sunny" and condition_for("100", True) == "clear-night"
    hass.states.async_set("sun.sun", "above_horizon")
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    await _sunny_today(hass, entry)
    assert hass.states.get("weather.jma_chiyoda").state == "sunny"

    # no coordinator update between these lines: only the sun moves
    hass.states.async_set("sun.sun", "below_horizon")
    await hass.async_block_till_done()
    weather = hass.states.get("weather.jma_chiyoda")
    assert weather.state == "clear-night"
    assert weather.attributes["jma_icon"] != "100.svg"  # the night icon

    hass.states.async_set("sun.sun", "above_horizon")
    await hass.async_block_till_done()
    weather = hass.states.get("weather.jma_chiyoda")
    assert weather.state == "sunny"
    assert weather.attributes["jma_icon"] == "100.svg"


async def test_sun_attribute_updates_do_not_rewrite_the_weather(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    hass.states.async_set("sun.sun", "above_horizon", {"elevation": 30.0})
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    await _sunny_today(hass, entry)
    entity = hass.data["entity_components"]["weather"].get_entity("weather.jma_chiyoda")
    with patch.object(entity, "async_write_ha_state", wraps=entity.async_write_ha_state) as write:
        hass.states.async_set("sun.sun", "above_horizon", {"elevation": 29.5})
        hass.states.async_set("sun.sun", "above_horizon", {"elevation": 29.0})
        await hass.async_block_till_done()
        assert write.call_count == 0, "the sun's elevation alone does not touch the weather entity"
        hass.states.async_set("sun.sun", "below_horizon", {"elevation": -0.9})
        await hass.async_block_till_done()
        assert write.call_count == 1


async def test_listener_is_removed_with_the_entry(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    hass.states.async_set("sun.sun", "above_horizon")
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("sun.sun", "below_horizon")  # must not raise or write a removed entity
    await hass.async_block_till_done()
    assert hass.states.get("weather.jma_chiyoda").state == "unavailable"
