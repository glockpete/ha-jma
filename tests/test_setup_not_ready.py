"""HA quality scale `test-before-setup`: an instance with no route to JMA at all (all four core feeds fail to fetch) is
retried by Home Assistant instead of showing a loaded entry whose entities are all unavailable. Every other mix keeps
the non-blocking setup."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.jma.const import (
    DOMAIN,
    URL_AMEDAS_LATEST,
    URL_FORECAST,
    URL_NOWC_TIMES,
    URL_WARNINGS,
)
from custom_components.jma.errors import JmaFetchError
from custom_components.jma.memory import AlertMemory

from .conftest import mock_jma

OFFICE = "130000"
NOT_JSON = "<html>maintenance</html>"


def _core_urls() -> list[str]:
    """The files the four core feeds (forecast, warnings, AMeDAS, nowcast) read first."""
    return [URL_FORECAST.format(office=OFFICE), URL_WARNINGS.format(office=OFFICE), URL_AMEDAS_LATEST,
            URL_NOWC_TIMES.format(n="N1"), URL_NOWC_TIMES.format(n="N2")]


def _no_route(aioclient_mock, urls: list[str]) -> None:
    """The first registration of a URL wins, so these go in before mock_jma."""
    for url in urls:
        aioclient_mock.get(url, exc=aiohttp.ClientConnectionError("no route to host"))


async def _first_setup(hass: HomeAssistant, entry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_no_route_to_jma_leaves_the_entry_retrying_with_nothing_started(
        tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    _no_route(aioclient_mock, _core_urls())
    mock_jma(aioclient_mock)  # the optional feeds answer: only the four core ones count
    unsubscribe = MagicMock()
    with (
        patch.object(AlertMemory, "async_listen_started", return_value=unsubscribe),
        patch("custom_components.jma.quake_ws.QuakeClient.run", new=AsyncMock()) as quake_run,
    ):
        await _first_setup(hass, entry)
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert entry.error_reason_translation_key == "jma_unreachable"
        # nothing set up before the raise is left behind
        unsubscribe.assert_called_once_with()
        quake_run.assert_not_called()
        assert not hass.data.get(DOMAIN, {}).get("quake_sockets")
        assert er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) == []
        assert not [s for s in hass.states.async_all() if "jma_chiyoda" in s.entity_id]
        assert not hasattr(entry, "runtime_data")
        await hass.async_block_till_done()  # no task is waiting on a half-built entry
        # the retry timer is the only thing still scheduled; unloading cancels it
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_the_entry_sets_up_when_a_retry_finds_the_route_back(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    _no_route(aioclient_mock, _core_urls())
    mock_jma(aioclient_mock)
    await _first_setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    aioclient_mock.clear_requests()
    mock_jma(aioclient_mock)
    freezer.tick(timedelta(seconds=30))  # the first back-off is 5 s plus a fraction of a second
    async_fire_time_changed(hass, dt_util.utcnow())
    async with entry.setup_lock:  # the retry runs as a background task: wait for it to finish
        pass
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("weather.jma_chiyoda").state != "unavailable"
    assert hass.data[DOMAIN]["quake_sockets"] == {entry.entry_id}  # one socket, taken once


async def test_one_core_feed_that_answers_keeps_the_entry_loaded(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    _no_route(aioclient_mock, [URL_FORECAST.format(office=OFFICE)])  # forecast is down, warnings answers
    mock_jma(aioclient_mock)
    await _first_setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    runtime = entry.runtime_data
    assert issubclass(runtime.forecast.last_error_type, JmaFetchError) and runtime.warnings.last_error_type is None
    assert runtime.unreachable is False
    assert hass.states.get("sensor.jma_chiyoda_warning_level").state == "0"


async def test_three_fetch_errors_and_one_parse_error_keep_the_entry_loaded(
        tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    """The route works (a maintenance page came back from the warnings server), so this is not "no route"."""
    hass = tokyo
    aioclient_mock.get(URL_WARNINGS.format(office=OFFICE), text=NOT_JSON)
    _no_route(aioclient_mock, [u for u in _core_urls() if u != URL_WARNINGS.format(office=OFFICE)])
    mock_jma(aioclient_mock)
    await _first_setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    runtime = entry.runtime_data
    assert not issubclass(runtime.warnings.last_error_type, JmaFetchError)
    fetch_failed = (runtime.forecast, runtime.amedas, runtime.nowcast)
    assert all(issubclass(c.last_error_type, JmaFetchError) for c in fetch_failed)
    assert runtime.unreachable is False


async def test_core_feeds_that_answer_with_a_body_that_is_not_json_keep_the_entry_loaded(
        tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    """A maintenance page served with HTTP 200 is a parse error, not a missing route: Home Assistant does not retry,
    the entities wait for a good file as they always did."""
    hass = tokyo
    for url in _core_urls():
        aioclient_mock.get(url, text=NOT_JSON)
    mock_jma(aioclient_mock)
    await _first_setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    runtime = entry.runtime_data
    core = (runtime.forecast, runtime.warnings, runtime.amedas, runtime.nowcast)
    assert all(not c.last_update_success for c in core)
    assert not any(issubclass(c.last_error_type, JmaFetchError) for c in core)
    assert runtime.unreachable is False
    assert hass.states.get("weather.jma_chiyoda").state == "unavailable"
    assert hass.data[DOMAIN]["quake_sockets"] == {entry.entry_id}


async def test_last_error_keeps_its_text_and_a_good_poll_clears_the_type(
        tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    _no_route(aioclient_mock, [URL_FORECAST.format(office=OFFICE)])
    mock_jma(aioclient_mock)
    await _first_setup(hass, entry)
    forecast = entry.runtime_data.forecast
    assert forecast.last_error and "no route to host" in forecast.last_error  # diagnostics reads the text
    aioclient_mock.clear_requests()
    mock_jma(aioclient_mock)
    await forecast.async_refresh()
    assert forecast.last_error is None and forecast.last_error_type is None
