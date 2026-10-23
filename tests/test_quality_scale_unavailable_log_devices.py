"""Three rules of the Home Assistant integration quality scale.

entity-unavailable (Silver): the two sensors fed by the P2P earthquake socket read unavailable once the socket has
been down for longer than HEALTH_QUAKE_MAX_DOWN (the feed-problem sensor's rule), and available again as soon as it
reconnects. log-when-unavailable (Silver): one WARNING when a source is lost, one INFO when it is back, DEBUG between.
devices (Gold): the device carries the integration's version.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_capture_events, async_fire_time_changed

import custom_components.jma.sensor as sensor_module
from custom_components.jma.const import DOMAIN, HEALTH_QUAKE_MAX_DOWN, VERSION
from custom_components.jma.errors import JmaFetchError
from custom_components.jma.quake_ws import QuakeClient

from .conftest import AREA, QUAKE_PLACE, mock_jma, setup
from .test_tsunami import NS, REAL_RUN, _FrameSocket, _yield_loop

LAST_QUAKE = "sensor.jma_chiyoda_last_earthquake"
TSUNAMI = "sensor.jma_chiyoda_tsunami"
BOTH = (LAST_QUAKE, TSUNAMI)
QUAKE_LOG = "custom_components.jma.quake_ws"
COORDINATOR_LOG = "custom_components.jma.coordinator"


# ---------------------------------------------------------------- A: entity-unavailable

def _states(hass: HomeAssistant) -> list[str]:
    return [hass.states.get(entity_id).state for entity_id in BOTH]


async def _minutes_pass(hass: HomeAssistant, freezer, minutes: int) -> None:
    """Time passes with no message from the socket: only the sensors' own timer can notice."""
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=90))
    await hass.async_block_till_done()


async def test_the_socket_sensors_follow_how_long_the_socket_has_been_down(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    qc = entry.runtime_data.quake
    assert HEALTH_QUAKE_MAX_DOWN == timedelta(minutes=10)

    # connected: available, however long it lasts
    qc.connected = True
    qc._on_quake()
    await hass.async_block_till_done()
    await _minutes_pass(hass, freezer, 60)
    assert STATE_UNAVAILABLE not in _states(hass)

    # the socket drops: the client sets `connected` and `down_since` and notifies, as its run loop does
    qc.connected = False
    qc.down_since = dt_util.utcnow()
    qc._on_quake()
    await hass.async_block_till_done()
    assert STATE_UNAVAILABLE not in _states(hass)  # down for no time yet
    await _minutes_pass(hass, freezer, 9)
    assert STATE_UNAVAILABLE not in _states(hass)  # down 9 minutes: still within the limit

    await _minutes_pass(hass, freezer, 2)  # 11 minutes, and nothing has been signalled in between
    assert _states(hass) == [STATE_UNAVAILABLE, STATE_UNAVAILABLE]

    # it reconnects: available at once, with the dispatcher's notification and no wait for the timer
    qc.connected = True
    qc._on_quake()
    await hass.async_block_till_done()
    assert STATE_UNAVAILABLE not in _states(hass)


async def test_a_reconnect_inside_the_limit_never_makes_the_sensors_unavailable(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    qc = entry.runtime_data.quake
    qc.connected = True
    qc._on_quake()
    await hass.async_block_till_done()
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)

    qc.connected = False
    qc.down_since = dt_util.utcnow()
    qc._on_quake()
    await hass.async_block_till_done()
    for _ in range(9):  # nine minutes, a tick each
        await _minutes_pass(hass, freezer, 1)
    qc.connected = True
    qc._on_quake()
    await hass.async_block_till_done()
    await _minutes_pass(hass, freezer, 30)

    seen = [e.data["new_state"].state for e in changes if e.data["entity_id"] in BOTH]
    assert STATE_UNAVAILABLE not in seen


async def test_a_socket_that_never_connected_reads_unavailable_after_the_limit(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    """`down_since` is set when the client is built: down until the first connect."""
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    assert entry.runtime_data.quake.connected is False
    await _minutes_pass(hass, freezer, 9)
    assert STATE_UNAVAILABLE not in _states(hass)
    await _minutes_pass(hass, freezer, 2)
    assert _states(hass) == [STATE_UNAVAILABLE, STATE_UNAVAILABLE]


async def test_the_alerts_event_stays_available_while_the_socket_is_down(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    await _minutes_pass(hass, freezer, 30)
    assert _states(hass) == [STATE_UNAVAILABLE, STATE_UNAVAILABLE]
    assert hass.states.get("event.jma_chiyoda_alerts").state != STATE_UNAVAILABLE


async def test_unload_cancels_the_sensors_timers(tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    real = sensor_module.async_track_time_interval
    timers: list[dict[str, Any]] = []

    def spy(hass_, action, interval, **kwargs):
        unsub = real(hass_, action, interval, **kwargs)
        timer = {"interval": interval, "cancelled": 0}
        timers.append(timer)

        def cancel() -> None:
            timer["cancelled"] += 1
            unsub()

        return cancel

    with patch.object(sensor_module, "async_track_time_interval", side_effect=spy):
        await setup(hass, entry)
        assert [t["interval"] for t in timers] == [sensor_module.QUAKE_DOWN_CHECK] * 2  # one per sensor, no more
        assert all(t["cancelled"] == 0 for t in timers)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    assert [t["cancelled"] for t in timers] == [1, 1]
    # a later tick finds nothing to run
    await _minutes_pass(hass, freezer, 60)


# ---------------------------------------------------------------- B1: log-when-unavailable, the socket

def _records(caplog, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level and r.name == QUAKE_LOG]


async def _run_socket(steps: list[Any], caplog, *, after=None) -> bool:
    """Run the real connection loop: `steps` are what ws_connect does in turn (an Exception is raised, anything else
    is the socket); the last step repeats. Back-off sleeps return at once. `after(qc)` may act once the loop is idle.
    Returns whether the client was connected when the loop went idle (cancelling it marks it disconnected)."""
    real_sleep = asyncio.sleep
    queue = list(steps)

    def ws_connect(*_a, **_k):
        step = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(step, Exception):
            raise step
        return step

    async def get_json(url, params=None):
        return []

    async def quick(_seconds, *_a, **_k):
        await real_sleep(0)

    qc = QuakeClient(NS(session=NS(ws_connect=ws_connect), get_json=get_json), lambda t, d: None, lambda: None,
                     place=QUAKE_PLACE)
    with caplog.at_level(logging.DEBUG, logger=QUAKE_LOG), patch(f"{QUAKE_LOG}.asyncio.sleep", new=quick):
        task = asyncio.ensure_future(REAL_RUN(qc))
        try:
            await _yield_loop(400)
            if after is not None:
                after(qc)
                await _yield_loop(400)
            connected = qc.connected
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    return connected


async def test_a_socket_that_cannot_be_opened_at_start_up_warns_once_and_says_when_it_is_back(caplog) -> None:
    refused = aiohttp.ClientConnectionError("Cannot connect to host api.p2pquake.net:443")
    steps = [refused, refused, OSError("network unreachable"), TimeoutError(), _FrameSocket([], asyncio.Event())]
    assert await _run_socket(steps, caplog) is True
    warnings = _records(caplog, logging.WARNING)
    assert warnings == [
        "P2P earthquake connection cannot be opened: Cannot connect to host api.p2pquake.net:443; retrying"]
    assert _records(caplog, logging.DEBUG).count("P2P socket: Cannot connect to host api.p2pquake.net:443") == 1
    assert "P2P socket: network unreachable" in _records(caplog, logging.DEBUG)
    assert "P2P socket: TimeoutError" in _records(caplog, logging.DEBUG)
    assert _records(caplog, logging.INFO) == ["P2P earthquake connection is back"]
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


async def test_a_dropped_connection_warns_once_per_outage_and_the_reconnect_logs_info_once(caplog) -> None:
    first_closed, second_closed = asyncio.Event(), asyncio.Event()
    first, second = _FrameSocket([], first_closed), _FrameSocket([], second_closed)
    down = OSError("network unreachable")
    # up, closed by the server, three refused attempts, up, closed again, up
    steps = [first, down, down, down, second, _FrameSocket([], asyncio.Event())]

    def second_drop(_qc: QuakeClient) -> None:
        second_closed.set()

    first_closed.set()  # the first connection ends as soon as it is up
    await _run_socket(steps, caplog, after=second_drop)
    assert _records(caplog, logging.WARNING) == [
        "P2P earthquake connection lost: closed by the server; reconnecting",
        "P2P earthquake connection lost: closed by the server; reconnecting",
    ]
    assert _records(caplog, logging.INFO) == ["P2P earthquake connection is back"] * 2
    # the three refused attempts of the first outage are all DEBUG
    assert _records(caplog, logging.DEBUG).count("P2P socket: network unreachable") == 3


async def test_an_unexpected_error_keeps_its_traceback_for_the_first_failure_of_an_outage_only(caplog) -> None:
    odd = RuntimeError("odd")
    await _run_socket([odd, odd, odd, _FrameSocket([], asyncio.Event())], caplog)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        "P2P earthquake connection cannot be opened: unexpected RuntimeError; retrying"]
    assert warnings[0].exc_info is not None
    later = [r for r in caplog.records if r.levelno == logging.DEBUG and "unexpected RuntimeError" in r.getMessage()]
    assert len(later) == 2
    assert _records(caplog, logging.INFO) == ["P2P earthquake connection is back"]


async def test_the_log_changes_neither_the_back_off_nor_the_bookkeeping(caplog) -> None:
    """connected / down_since / the notifications are as before: down at the start, up once connected."""
    pings: list[bool] = []
    real_sleep = asyncio.sleep
    waits: list[float] = []
    steps = [OSError("a"), OSError("b"), OSError("c"), _FrameSocket([], asyncio.Event())]

    def ws_connect(*_a, **_k):
        step = steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    async def get_json(url, params=None):
        return []

    async def record(seconds, *_a, **_k):
        waits.append(seconds)
        await real_sleep(0)

    holder: dict[str, QuakeClient] = {}
    qc = QuakeClient(NS(session=NS(ws_connect=ws_connect), get_json=get_json), lambda t, d: None,
                     lambda: pings.append(holder["qc"].connected), place=QUAKE_PLACE)
    holder["qc"] = qc
    began = qc.down_since
    with patch(f"{QUAKE_LOG}.asyncio.sleep", new=record):
        task = asyncio.ensure_future(REAL_RUN(qc))
        try:
            await _yield_loop(300)
            connected = qc.connected
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    assert [w for w in waits if w in (1, 2, 4, 8)] == [1, 2, 4]  # the back-off doubles as it always did
    assert pings[0] is True and connected is True and qc.down_since == began


# ---------------------------------------------------------------- B2: log-when-unavailable, the polling feeds

def _coordinator_log(caplog, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level and r.name == COORDINATOR_LOG]


async def test_five_unexpected_payloads_in_a_row_are_one_warning_and_one_info_when_data_is_back(
        tokyo: HomeAssistant, aioclient_mock, entry, caplog) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    coordinator = entry.runtime_data.forecast
    good = coordinator.data
    assert good is not None
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=COORDINATOR_LOG):
        coordinator._fetch = AsyncMock(side_effect=KeyError("areas"))
        for _ in range(5):
            await coordinator.async_refresh()
        warnings = _coordinator_log(caplog, logging.WARNING)
        assert len(warnings) == 1 and warnings[0].startswith("jma forecast: unexpected payload: KeyError('areas')")
        assert len([m for m in _coordinator_log(caplog, logging.DEBUG) if "unexpected payload" in m]) == 4
        assert _coordinator_log(caplog, logging.INFO) == []
        # keep-last-good and last_error are what they were
        assert coordinator.data is good and coordinator.last_update_success is True
        assert coordinator.last_error == "'areas'"

        coordinator._fetch = AsyncMock(return_value=good)
        await coordinator.async_refresh()
    assert _coordinator_log(caplog, logging.INFO) == ["jma forecast: data is back after 5 failed poll(s)"]
    assert len(_coordinator_log(caplog, logging.WARNING)) == 1
    assert coordinator.last_error is None

    with caplog.at_level(logging.DEBUG, logger=COORDINATOR_LOG):  # a later run warns again
        coordinator._fetch = AsyncMock(side_effect=ValueError("bad"))
        await coordinator.async_refresh()
        await coordinator.async_refresh()
    assert len(_coordinator_log(caplog, logging.WARNING)) == 2


async def test_a_fetch_error_stays_silent_at_warning_and_says_nothing_when_it_ends(
        tokyo: HomeAssistant, aioclient_mock, entry, caplog) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    coordinator = entry.runtime_data.forecast
    good = coordinator.data
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=COORDINATOR_LOG):
        coordinator._fetch = AsyncMock(side_effect=JmaFetchError("HTTP 503"))
        for _ in range(4):
            await coordinator.async_refresh()
        coordinator._fetch = AsyncMock(return_value=good)
        await coordinator.async_refresh()
    assert [r for r in caplog.records if r.name == COORDINATOR_LOG and r.levelno >= logging.INFO] == []


# ---------------------------------------------------------------- C: devices

async def test_the_device_carries_the_integration_version(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, AREA), entry.entry_id)
    assert device is not None
    assert device.sw_version == VERSION
