"""A server that accepts the P2P WebSocket and closes it at once (the case the back-off docstring names) is ONE
outage, in the log, in `down_since` and in the availability of the two socket sensors: only a connection that has
lasted LASTING_S (60 s) ends it (one INFO, `down_since` cleared, the sensors look again). A connection that drops
sooner is part of the outage that was running; one that drops after 60 s starts a new one.

Found by the hostile review of 0.7.0: every flap logged one INFO and one WARNING, and once the outage was past
HEALTH_QUAKE_MAX_DOWN the sensors flipped available / unavailable on every flap."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_capture_events, async_fire_time_changed

from custom_components.jma.binary_sensor import feed_conditions
from custom_components.jma.quake_ws import LASTING_S, QuakeClient

from .conftest import QUAKE_PLACE, mock_jma, setup
from .test_quality_scale_unavailable_log_devices import BOTH, QUAKE_LOG, _minutes_pass, _records, _run_socket, _states
from .test_tsunami import NS, REAL_RUN, _FrameSocket, _yield_loop

_REAL_SLEEP = asyncio.sleep  # taken at import, before a test patches the back-off sleep
BACK = "P2P earthquake connection is back"
LOST = "P2P earthquake connection lost: closed by the server; reconnecting"
REFUSED = "P2P earthquake connection cannot be opened: network unreachable; retrying"


class _Server:
    """What ws_connect does in turn: an Exception is raised, anything else is the socket; the last step repeats.
    `waits` are the back-off sleeps of the loop (a sleep of 0 is somebody yielding, not the back-off)."""

    def __init__(self, *steps: Any) -> None:
        self.steps = list(steps)
        self.waits: list[float] = []

    def ws_connect(self, *_a: Any, **_k: Any) -> Any:
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        return step

    async def quick(self, seconds: float, *_a: Any, **_k: Any) -> None:
        if seconds >= 1:
            self.waits.append(seconds)
        await _REAL_SLEEP(0)


async def _no_history(url: str, params: Any = None) -> list[Any]:
    return []


@contextlib.asynccontextmanager
async def _running(hass: HomeAssistant, caplog: Any, server: _Server,
                   on_quake: Callable[[], None] = lambda: None) -> AsyncIterator[QuakeClient]:
    """The real connection loop of a client built for `server`, ended (and its timer with it) on the way out."""
    qc = QuakeClient(hass, NS(session=NS(ws_connect=server.ws_connect), get_json=_no_history),
                     lambda t, d: None, on_quake, place=QUAKE_PLACE)
    with caplog.at_level(logging.DEBUG, logger=QUAKE_LOG), patch(f"{QUAKE_LOG}.asyncio.sleep", new=server.quick):
        task = asyncio.ensure_future(REAL_RUN(qc))
        try:
            yield qc
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _lasts(hass: HomeAssistant, freezer: Any) -> None:
    """LASTING_S + 1 s go by with the connection up: the timer that ends the outage is due."""
    freezer.tick(timedelta(seconds=LASTING_S + 1))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=LASTING_S + 1))


# ---------------------------------------------------------------- the reviewer's repro

async def test_review_flapping_socket_warns_once_per_outage(hass: HomeAssistant, caplog) -> None:
    # every connect is accepted and closed by the server at once; the last step repeats
    await _run_socket(hass, [_FrameSocket([], None)], caplog)
    warnings = _records(caplog, logging.WARNING)
    infos = _records(caplog, logging.INFO)
    assert len(warnings) == 1, f"{len(warnings)} WARNINGs and {len(infos)} INFOs for one flapping outage"
    assert infos == [], "a connection that did not last 60 s ended the outage"
    assert warnings == [LOST]


# ---------------------------------------------------------------- the log and down_since follow the 60 s rule

async def test_a_connection_that_lasts_ends_the_outage_with_one_info(
        hass: HomeAssistant, freezer, caplog) -> None:
    server = _Server(OSError("network unreachable"), _FrameSocket([], asyncio.Event()))
    pings: list[bool] = []
    async with _running(hass, caplog, server, lambda: pings.append(True)) as qc:
        await _yield_loop(200)
        assert qc.connected is True and _records(caplog, logging.WARNING) == [REFUSED]
        assert _records(caplog, logging.INFO) == [] and qc.down_since is not None  # up, but not for 60 s yet
        seen = len(pings)

        _lasts(hass, freezer)
        await hass.async_block_till_done()
        assert _records(caplog, logging.INFO) == [BACK]
        assert qc.down_since is None
        assert len(pings) == seen + 1  # the sensors were told, once

        _lasts(hass, freezer)  # the timer ran once and is gone: nothing more to say
        await hass.async_block_till_done()
        assert _records(caplog, logging.INFO) == [BACK] and len(pings) == seen + 1
    assert _records(caplog, logging.WARNING) == [REFUSED]


async def test_a_start_up_whose_first_connection_lasts_logs_nothing_and_clears_down_since(
        hass: HomeAssistant, freezer, caplog) -> None:
    server = _Server(_FrameSocket([], asyncio.Event()))
    async with _running(hass, caplog, server) as qc:
        assert qc.down_since is not None  # "down" from the moment the client is built
        await _yield_loop(200)
        assert qc.connected is True and qc.down_since is not None
        _lasts(hass, freezer)
        await hass.async_block_till_done()
        assert qc.down_since is None
    assert [r for r in caplog.records if r.levelno >= logging.INFO and r.name == QUAKE_LOG] == []


async def test_a_drop_inside_a_minute_is_the_same_outage_and_a_drop_after_it_is_a_new_one(
        hass: HomeAssistant, freezer, caplog) -> None:
    first_closed, second_closed = asyncio.Event(), asyncio.Event()
    server = _Server(OSError("network unreachable"), _FrameSocket([], first_closed),
                     _FrameSocket([], second_closed), _FrameSocket([], asyncio.Event()))
    async with _running(hass, caplog, server) as qc:
        began = qc.down_since
        assert began is not None
        await _yield_loop(200)  # refused, then connection 1 is up
        assert qc.connected is True and _records(caplog, logging.WARNING) == [REFUSED]

        freezer.tick(timedelta(seconds=30))
        first_closed.set()  # connection 1 is closed by the server after 30 s
        await _yield_loop(200)  # ... and connection 2 is up
        assert qc.connected is True
        assert _records(caplog, logging.WARNING) == [REFUSED]  # no new WARNING
        assert _records(caplog, logging.INFO) == [] and qc.down_since == began  # the outage that was running
        assert _records(caplog, logging.DEBUG).count("P2P socket: closed by the server") == 1
        assert server.waits == [1, 2]  # the back-off was not reset by a connection of 30 s

        _lasts(hass, freezer)  # connection 2 has been up for more than 60 s
        await hass.async_block_till_done()
        assert _records(caplog, logging.INFO) == [BACK] and qc.down_since is None

        second_closed.set()  # ... and then the server closes it
        await _yield_loop(200)  # connection 3 is up
        assert _records(caplog, logging.WARNING) == [REFUSED, LOST]  # a new outage, its own WARNING
        assert qc.down_since == dt_util.utcnow() and qc.down_since != began  # begun when connection 2 dropped
        assert server.waits == [1, 2, 1]  # a connection that lasted starts the back-off again
        assert qc.connected is True and _records(caplog, logging.INFO) == [BACK]


# ---------------------------------------------------------------- the sensors follow it too

async def test_the_socket_sensors_stay_unavailable_through_a_flapping_outage_until_a_connection_lasts(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    qc = entry.runtime_data.quake
    await _minutes_pass(hass, freezer, 11)  # no connection yet, for more than HEALTH_QUAKE_MAX_DOWN
    assert _states(hass) == [STATE_UNAVAILABLE, STATE_UNAVAILABLE]

    server = _Server(_FrameSocket([], None))  # accepts and closes at once, again and again
    qc._api = NS(session=NS(ws_connect=server.ws_connect), get_json=_no_history)
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    with patch(f"{QUAKE_LOG}.asyncio.sleep", new=server.quick):
        task = asyncio.ensure_future(REAL_RUN(qc))
        try:
            await _yield_loop(400)
            await hass.async_block_till_done()
            assert _states(hass) == [STATE_UNAVAILABLE, STATE_UNAVAILABLE]
            assert [e for e in changes if e.data["entity_id"] in BOTH] == []  # not one flip, across all those flaps
            assert feed_conditions(entry.runtime_data, dt_util.utcnow())["quake"] is not None  # the feed problem holds

            server.steps[:] = [_FrameSocket([], asyncio.Event())]  # the next connection stays up
            await _yield_loop(400)
            await hass.async_block_till_done()
            assert qc.connected is True
            assert _states(hass) == [STATE_UNAVAILABLE, STATE_UNAVAILABLE]  # up, but not for 60 s yet

            _lasts(hass, freezer)  # it has lasted: available at once, no wait for the sensors' own timer
            await hass.async_block_till_done()
            assert qc.down_since is None
            assert STATE_UNAVAILABLE not in _states(hass)
            assert feed_conditions(entry.runtime_data, dt_util.utcnow())["quake"] is None
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
