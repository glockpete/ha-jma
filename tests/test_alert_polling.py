"""The alert feeds are polled for the event entity, not for the sensors.

A DataUpdateCoordinator schedules its next poll only while something listens to it, and the alerts are decided inside
the poll. A user who keeps `event.jma_<place>_alerts` and disables the sensors must still get every alert, so the
event entity itself listens to the five feeds that raise events. With the event entity disabled too, nothing listens
and the feeds rest."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.jma.const import URL_VOLCANO_WARNINGS

from .conftest import mock_jma, setup

WARNINGS = "/warning/data/r8/"
VOLCANO = URL_VOLCANO_WARNINGS.rsplit("/", 1)[-1]
EMITTERS = ("warnings", "nowcast", "typhoon", "heat_alert", "volcano")


def _hits(aioclient_mock, needle: str) -> int:
    return sum(1 for call in aioclient_mock.mock_calls if needle in str(call[1]))


async def _disable_and_run(hass: HomeAssistant, aioclient_mock, entry, freezer, *, keep_event: bool) -> dict[str, int]:
    """Disable every entity of the entry (but the event entity when `keep_event`), reload, run 30 minutes;
    the number of requests per feed in those 30 minutes."""
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    reg = er.async_get(hass)
    for item in er.async_entries_for_config_entry(reg, entry.entry_id):
        if keep_event and item.domain == "event":
            continue
        reg.async_update_entity(item.entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    before = {needle: _hits(aioclient_mock, needle) for needle in (WARNINGS, VOLCANO)}
    for _ in range(30):
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    return {needle: _hits(aioclient_mock, needle) - count for needle, count in before.items()}


async def test_only_the_event_entity_enabled_keeps_the_alert_feeds_polled(
    tokyo: HomeAssistant, aioclient_mock, entry, freezer
) -> None:
    polls = await _disable_and_run(tokyo, aioclient_mock, entry, freezer, keep_event=True)
    assert polls[WARNINGS] >= 25, polls  # every 60 s
    assert polls[VOLCANO] >= 5, polls  # every 5 minutes
    runtime = entry.runtime_data
    for name in EMITTERS:
        assert getattr(runtime, name)._listeners, name
    # The feeds that raise no event still follow their own entities.
    for name in ("forecast", "hourly", "amedas", "timeline", "wbgt", "layers", "japan"):
        assert not getattr(runtime, name)._listeners, name


async def test_event_entity_disabled_too_lets_the_feeds_rest(
    tokyo: HomeAssistant, aioclient_mock, entry, freezer
) -> None:
    polls = await _disable_and_run(tokyo, aioclient_mock, entry, freezer, keep_event=False)
    assert polls == {WARNINGS: 0, VOLCANO: 0}
