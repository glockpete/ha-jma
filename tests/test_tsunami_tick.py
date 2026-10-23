"""The ten-minute tsunami tick: no timer for an entry without an earthquake connection, and the sensors are
notified (signal_quake) only when the tick changed something a user can see.

Real captures: p2p_552_tokyo_bay.json (2025-07-30 20:45, Watch for 東京湾内湾 and the islands), p2p_552_watch.json
(2026-07-28, a Watch elsewhere in Japan), plus the one synthetic Tokyo Bay Warning (`_warning_bulletin`).
"""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.jma import TSUNAMI_TICK
from custom_components.jma.const import DOMAIN, TSUNAMI_MAX_AGE
from custom_components.jma.coordinator import signal_alert, signal_quake
from custom_components.jma.parse_tsunami import TsunamiTracker, parse_552
from custom_components.jma.quake_ws import QuakeClient

from .conftest import ENTRY_DATA, QUAKE_PLACE, mock_heat_alert, mock_jma, setup
from .helpers import clone, load_json

BAY = "東京湾内湾"
SENSOR = "sensor.jma_chiyoda_tsunami"


def _warning_bulletin() -> dict[str, Any]:
    """SYNTHETIC: p2p_552_tokyo_bay.json with the grade of 東京湾内湾 raised to Warning (no real bulletin ever
    gave 東京湾内湾 that grade), a fresh id and a later issue time."""
    msg = clone(load_json("p2p_552_tokyo_bay.json"))
    for area in msg["areas"]:
        if area["name"] == BAY:
            area["grade"] = "Warning"
    msg["id"], msg["issue"]["time"] = "warn-1", "2025/07/30 20:45:06"
    return msg


def _client() -> tuple[QuakeClient, list[Any], list[str]]:
    events: list[Any] = []
    pings: list[str] = []
    qc = QuakeClient(hass=None, api=None, fire=lambda t, d: events.append((t, d)),  # type: ignore[arg-type]
                     on_quake=lambda: pings.append("quake"), place=QUAKE_PLACE, tsunami_area=BAY)
    return qc, events, pings


# ---------------------------------------------------------------- the client: when does a tick notify

def test_a_tick_with_nothing_to_change_notifies_nobody() -> None:
    qc, events, pings = _client()
    issued = parse_552(_warning_bulletin()).issued
    qc.tsunami_tick(issued)  # nothing received yet
    assert (events, pings) == ([], [])
    qc.handle(_warning_bulletin(), issued + timedelta(minutes=1))
    assert len(events) == 1 and len(pings) == 1
    qc.tsunami_tick(issued + timedelta(minutes=11))
    qc.tsunami_tick(issued + timedelta(hours=1))
    qc.tsunami_tick(issued + TSUNAMI_MAX_AGE - timedelta(minutes=1))
    assert len(events) == 1 and len(pings) == 1  # no event, same picture: the sensors were not written


def test_a_tick_that_expires_the_bulletin_notifies_once() -> None:
    qc, events, pings = _client()
    issued = parse_552(_warning_bulletin()).issued
    qc.handle(_warning_bulletin(), issued + timedelta(minutes=1))
    qc.tsunami_tick(issued + timedelta(hours=1))
    assert len(pings) == 1
    qc.tsunami_tick(issued + TSUNAMI_MAX_AGE + timedelta(minutes=1))
    assert [t for t, _ in events] == ["tsunami", "tsunami_cancelled"] and len(pings) == 2
    qc.tsunami_tick(issued + TSUNAMI_MAX_AGE + timedelta(minutes=11))
    qc.tsunami_tick(issued + timedelta(days=30))
    assert len(events) == 2 and len(pings) == 2  # once


def test_a_held_bulletin_taken_up_by_the_tick_notifies_even_when_the_picture_is_the_same() -> None:
    """A re-issue with the same grades is refused as future-dated (slow clock) and held; the tick takes it up.
    The grades are unchanged but the bulletin id and issue time (sensor attributes) are not."""
    qc, events, pings = _client()
    first = _warning_bulletin()
    now = parse_552(first).issued + timedelta(minutes=1)
    qc.handle(first, now)
    later = clone(first)
    later["id"], later["issue"]["time"] = "warn-2", "2025/07/30 21:20:00"
    qc.handle(later, now)  # 34 min ahead of the clock: held
    assert qc.tsunami.current.id == "warn-1"
    told = len(pings)
    qc.tsunami_tick(parse_552(later).issued - timedelta(minutes=5))
    assert qc.tsunami.current.id == "warn-2" and len(pings) == told + 1
    qc.tsunami_tick(parse_552(later).issued - timedelta(minutes=4))  # and then it is quiet again
    assert len(pings) == told + 1


def test_a_tick_that_expires_a_bulletin_nobody_was_told_about_still_changes_the_picture() -> None:
    """A Watch elsewhere in Japan raised no event (nothing for the home coast, no warning); at 48 h the
    sensor's max_grade attribute goes from watch to none, so the sensors are written, with no event."""
    qc, events, pings = _client()
    msg = load_json("p2p_552_watch.json")
    issued = parse_552(msg).issued
    qc.handle(msg, issued + timedelta(minutes=1))
    assert events == [] and qc.tsunami.picture(issued + timedelta(hours=1)).max == "watch"
    pings.clear()
    qc.tsunami_tick(issued + timedelta(hours=1))
    assert pings == []
    qc.tsunami_tick(issued + TSUNAMI_MAX_AGE + timedelta(minutes=1))
    assert events == [] and pings == ["quake"]
    qc.tsunami_tick(issued + TSUNAMI_MAX_AGE + timedelta(minutes=11))
    assert pings == ["quake"]


def test_a_client_without_an_earthquake_connection_does_not_tick() -> None:
    qc, events, pings = _client()
    msg = _warning_bulletin()
    issued = parse_552(msg).issued
    qc.handle(msg, issued + timedelta(minutes=1))
    qc.enabled = False
    pings.clear()
    with patch.object(TsunamiTracker, "tick", side_effect=AssertionError("ticked")) as tick:
        qc.tsunami_tick(issued + TSUNAMI_MAX_AGE + timedelta(minutes=1))
    assert tick.call_count == 0 and pings == [] and len(events) == 1


def test_a_tick_that_raises_is_still_swallowed() -> None:
    qc, _, pings = _client()
    qc.tsunami.tick = lambda now: 1 / 0  # type: ignore[method-assign]
    qc.tsunami_tick(parse_552(_warning_bulletin()).issued)
    assert pings == []


# ---------------------------------------------------------------- the entry: timer and signals

async def test_the_timer_runs_the_tick_and_signals_only_a_change(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    msg = _warning_bulletin()
    issued = parse_552(msg).issued
    seen: list[str] = []
    async_dispatcher_connect(hass, signal_alert(entry.entry_id), lambda t, d: seen.append(t))
    mock_heat_alert(aioclient_mock, {(20250730, 17): "alert_20261003_17.csv"})
    mock_jma(aioclient_mock)
    freezer.move_to((issued + timedelta(minutes=3)).astimezone(UTC))
    await setup(hass, entry)
    qc = entry.runtime_data.quake
    qc.connected = True  # an up, quiet socket; one that has not been up for 60 s reads unavailable after 10 minutes
    qc.down_since = None  # (what `_mark_back` does once a connection has lasted)
    qc.handle(msg, dt_util.now())
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "warning"
    signals: list[str] = []
    async_dispatcher_connect(hass, signal_quake(entry.entry_id), lambda: signals.append("quake"))

    for minutes in (11, 22, 33):  # a quiet hour: three ticks, nothing changes
        freezer.move_to((issued + timedelta(minutes=3 + minutes)).astimezone(UTC))
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=11))
        await hass.async_block_till_done()
    assert signals == [] and hass.states.get(SENSOR).state == "warning"

    freezer.move_to((issued + TSUNAMI_MAX_AGE + timedelta(minutes=5)).astimezone(UTC))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done()
    assert signals == ["quake"] and seen == ["tsunami", "tsunami_cancelled"]
    assert hass.states.get(SENSOR).state == "none"

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=22))
    await hass.async_block_till_done()
    assert signals == ["quake"]  # once


async def test_an_entry_without_an_earthquake_connection_registers_no_timer_and_ticks_nothing(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    """The earthquake service allows two connections per IP address; the third entry has none."""
    hass = tokyo
    mock_jma(aioclient_mock)
    entries = [entry]
    for area, title in (("2710000", "JMA Osaka"), ("2714000", "JMA Sakai")):
        entries.append(MockConfigEntry(domain=DOMAIN, title=title, unique_id=area, data={**ENTRY_DATA, "area": area}))
    with (
        patch("custom_components.jma.async_track_time_interval", wraps=async_track_time_interval) as track,
        patch.object(TsunamiTracker, "tick", autospec=True, wraps=TsunamiTracker.tick) as tick,
    ):
        for each in entries:
            await setup(hass, each)
        assert [each.runtime_data.quake.enabled for each in entries] == [True, True, False]
        timers = [call for call in track.call_args_list if call.args[2] == TSUNAMI_TICK]
        assert len(timers) == 2  # the two entries that have a connection
        tick.reset_mock()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=11))
        await hass.async_block_till_done()
        ticked = {call.args[0] for call in tick.call_args_list}
        assert ticked == {each.runtime_data.quake.tsunami for each in entries[:2]}
