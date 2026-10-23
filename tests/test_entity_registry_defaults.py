"""entity-category and entity-disabled-by-default (Home Assistant Integration Quality Scale, Gold).

The feed-health sensor is a diagnostic; the two heavy map/forecast sensors are off until someone switches them on;
every other entity is primary data and stays enabled with no category.
"""

from __future__ import annotations

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler

from custom_components.jma.const import DOMAIN

from .conftest import AREA, mock_jma, setup

FEED_PROBLEM = "binary_sensor.jma_chiyoda_feed_problem"
OFF_BY_DEFAULT = ("sensor.jma_chiyoda_radar_frames", "sensor.jma_chiyoda_japan")


async def test_only_the_feed_problem_sensor_is_a_diagnostic(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    registry = er.async_get(hass)

    assert registry.async_get(FEED_PROBLEM).entity_category is EntityCategory.DIAGNOSTIC
    others = [e for e in er.async_entries_for_config_entry(registry, entry.entry_id) if e.entity_id != FEED_PROBLEM]
    assert "sensor.jma_chiyoda_warning_level" in {e.entity_id for e in others}
    assert [e.entity_id for e in others if e.entity_category is not None] == []


async def test_radar_frames_and_japan_start_disabled_and_nothing_else_does(
    tokyo: HomeAssistant, aioclient_mock, entry
) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    registry = er.async_get(hass)

    for entity_id in OFF_BY_DEFAULT:
        registered = registry.async_get(entity_id)
        assert registered.unique_id in (f"{AREA}_radar_frames", f"{AREA}_japan")
        assert registered.disabled_by is RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entity_id) is None

    everything = er.async_entries_for_config_entry(registry, entry.entry_id)
    others = [e for e in everything if e.entity_id not in OFF_BY_DEFAULT]
    assert {e.domain for e in others} == {"sensor", "binary_sensor", "event", "weather"}
    for e in others:
        assert e.disabled_by is None, e.entity_id
        assert hass.states.get(e.entity_id) is not None, e.entity_id
    assert {e.platform for e in others} == {DOMAIN}
