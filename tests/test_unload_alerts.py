"""The unload of an entry (custom_components/jma/__init__.py async_unload_entry) and the alert memory.

W1: a frame handled after the platforms are unloaded (the event entity is gone) and before `memory.async_flush()` has
    read `self.data`: the tracker has decided and mutated its section, `fire` goes to a signal nobody listens to. The
    memory is frozen before the platforms go, so the file does not say "announced" for an event no entity received.
W2: a `save()` after the flush has begun its write must not schedule a delayed write on the OLD Store, which would
    write its data over the file of the reloaded entry two seconds later.
W3: events still queued in the event entity when the unload starts are written before the entity is removed (a
    removed entity writes no state), because the unload drains the queue first. The queue holds events while Home
    Assistant starts, and for the gap between two alerts sent in a row while it runs.

The W1 and W2 tests drive `QuakeClient.handle` directly (conftest.py `no_quake_socket` stops `run`), standing in for
the frame handled at quake_ws.py:309; the injection point is a wrapper round `AlertMemory.async_flush`, because that
is the first await of `async_unload_entry` after the entity is gone. In production the W2 frame can only run while the
flush awaits its write; here it runs just after, the same Store state (`_data` consumed, `_unsaved` False).
`hass_storage` stands in for the file.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.jma import async_unload_entry
from custom_components.jma.const import EVENT_TYPES, TSUNAMI_COAST_AREA
from custom_components.jma.coordinator import signal_alert
from custom_components.jma.memory import AlertMemory
from custom_components.jma.parse_tsunami import parse_552

from .conftest import mock_heat_alert, mock_jma, setup
from .helpers import clone, load_json

HOME = TSUNAMI_COAST_AREA["130010"]
EVENT = "event.jma_chiyoda_alerts"


def _warning_bulletin() -> dict:
    """SYNTHETIC, as test_tsunami._variant: the real 2025-07-30 20:45 bulletin with the home coast at Warning."""
    msg = clone(load_json("p2p_552_tokyo_bay.json"))
    for area in msg["areas"]:
        if area["name"] == HOME:
            area["grade"] = "Warning"
    msg["id"], msg["issue"]["time"] = "warn-1", "2025/07/30 20:45:06"
    return msg


async def _loaded(hass, aioclient_mock, entry, freezer):
    msg = _warning_bulletin()
    freezer.move_to((parse_552(msg).issued + timedelta(minutes=3)).astimezone(UTC))
    mock_heat_alert(aioclient_mock, {(20250730, 17): "alert_20261003_17.csv"})
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    return msg


async def _after_the_delay(hass: HomeAssistant, freezer) -> None:
    freezer.tick(timedelta(seconds=3))  # AlertMemory.SAVE_DELAY_S is 2 (test_setup_not_ready.py:173)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def test_w1_a_frame_between_the_entity_removal_and_the_flush_is_not_marked_announced(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage) -> None:
    hass = tokyo
    msg = await _loaded(hass, aioclient_mock, entry, freezer)
    key = f"jma.{entry.entry_id}.alert_memory"
    old_qc = entry.runtime_data.quake  # runtime_data is deleted once the unload has finished (config_entries.py:1139)
    sent: list[str] = []  # the signal was sent: it is NOT proof that an entity received it
    async_dispatcher_connect(hass, signal_alert(entry.entry_id), lambda t, d: sent.append(t))
    real_flush = AlertMemory.async_flush

    async def flush_after_a_frame(self) -> None:
        state = hass.states.get(EVENT)
        assert state is None or state.state == "unavailable"  # the platforms are unloaded: no entity to receive it
        old_qc.handle(msg, dt_util.now())  # the frame of quake_ws.py:309
        await real_flush(self)

    with patch.object(AlertMemory, "async_flush", flush_after_a_frame):
        assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert sent == ["tsunami"]  # decided and fired ...
    assert hass.states.get(EVENT) is None or hass.states.get(EVENT).state == "unavailable"  # ... to nobody

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # the file must not say "announced" for an event nobody received
    assert key not in hass_storage or hass_storage[key]["data"]["tsunami"] == {}
    entry.runtime_data.quake.handle(msg, dt_util.now())  # the list replay of the next connection, the same bulletin
    await hass.async_block_till_done()
    assert hass.states.get(EVENT).attributes.get("event_type") == "tsunami"


async def test_w2_a_save_after_the_flush_writes_the_old_object_over_the_file_two_seconds_later(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage) -> None:
    hass = tokyo
    msg = await _loaded(hass, aioclient_mock, entry, freezer)
    key = f"jma.{entry.entry_id}.alert_memory"
    old_qc = entry.runtime_data.quake
    real_flush = AlertMemory.async_flush

    async def frame_after_the_flush(self) -> None:
        await real_flush(self)  # nothing was unsaved: the file is not written
        old_qc.handle(msg, dt_util.now())  # save() -> async_delay_save on the OLD Store (memory.py)

    with patch.object(AlertMemory, "async_flush", frame_after_the_flush):
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    memory = entry.runtime_data.memory
    assert memory.tsunami == {}  # the reloaded entry read the file before the old timer could fire
    assert hass.states.get(EVENT).state == "unknown"  # and no event reached the new entity

    await _after_the_delay(hass, freezer)
    # the file never holds an alert that the running entry's memory does not hold
    assert key not in hass_storage or hass_storage[key]["data"]["tsunami"] == memory.tsunami


W3_TYPES = [t for t in EVENT_TYPES if t in ("heat_alert", "earthquake", "eew")]


def _watch(hass: HomeAssistant) -> tuple[list[str], Callable[[], None]]:
    """The W3 event types as the entity's state shows them, in order, and the function that stops watching."""
    assert len(W3_TYPES) == 3
    seen: list[str] = []

    def on_change(event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if new is not None and new.attributes.get("event_type") in W3_TYPES:
            seen.append(new.attributes["event_type"])

    return seen, async_track_state_change_event(hass, [EVENT], on_change)


@contextmanager
def _a_gap_of_many_loop_turns() -> Iterator[None]:
    """The gap between two events is made long in loop turns (not in seconds: the test clock does not run in real
    time), so that the entity is removed long before it is over unless the unload waits for the queue. conftest.py
    sets the gap to 0, which asks for no wait at all; here it is the 5 ms of the release."""
    real_sleep = asyncio.sleep

    async def long_gap(_delay: float) -> None:
        for _ in range(2000):
            await real_sleep(0)

    with (patch("custom_components.jma.event.EVENT_SPACING", 0.005),
          patch("custom_components.jma.event.asyncio", SimpleNamespace(sleep=long_gap, wait=asyncio.wait))):
        yield


async def test_w3_events_still_queued_when_the_unload_starts_reach_the_entity(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage) -> None:
    """Events held while Home Assistant starts are written one after the other once it has started; an unload that
    begins between the first and the last must still deliver all of them (a removed entity writes no state)."""
    hass = tokyo
    hass.set_state(CoreState.starting)  # the entity holds what it receives until Home Assistant has started
    await _loaded(hass, aioclient_mock, entry, freezer)
    seen, unsub = _watch(hass)
    for event_type in W3_TYPES:
        async_dispatcher_send(hass, signal_alert(entry.entry_id), event_type, {})
    assert seen == []  # held

    with _a_gap_of_many_loop_turns():
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0)
        assert seen == W3_TYPES[:1]  # the first is written, the other two are queued
        assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    unsub()
    assert seen == W3_TYPES


async def test_w3_alerts_sent_in_a_row_while_running_reach_the_entity_before_it_is_removed(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage) -> None:
    """Alerts sent in a row while Home Assistant runs are written a gap apart (see test_event_spacing.py), so a
    reload right after them finds the later ones still queued."""
    hass = tokyo
    await _loaded(hass, aioclient_mock, entry, freezer)
    assert hass.states.get(EVENT).state == "unknown"  # no event yet: the first one has no gap to wait for
    seen, unsub = _watch(hass)
    with _a_gap_of_many_loop_turns():
        for event_type in W3_TYPES:
            async_dispatcher_send(hass, signal_alert(entry.entry_id), event_type, {})
        assert seen == W3_TYPES[:1]  # the first is written at once, the other two wait for their gap
        assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    unsub()
    assert seen == W3_TYPES


async def test_a_failed_unload_unfreezes_the_memory(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage) -> None:
    hass = tokyo
    await _loaded(hass, aioclient_mock, entry, freezer)
    key = f"jma.{entry.entry_id}.alert_memory"
    memory = entry.runtime_data.memory
    # called directly: through config_entries.async_unload a False would leave the entry in FAILED_UNLOAD for good
    with patch.object(hass.config_entries, "async_unload_platforms", new_callable=AsyncMock, return_value=False):
        assert not await async_unload_entry(hass, entry)
    memory.heat.append("20260101")  # decided while the entry stays loaded
    memory.save()
    await _after_the_delay(hass, freezer)
    assert hass_storage[key]["data"]["heat"] == ["20260101"]
    memory.heat.append("20260102")
    memory.save()
    await _after_the_delay(hass, freezer)
    assert hass_storage[key]["data"]["heat"] == ["20260101", "20260102"]
    assert await hass.config_entries.async_unload(entry.entry_id)  # the entry is still loaded and unloads for real


async def test_an_alert_saved_before_the_unload_is_still_flushed(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer, hass_storage) -> None:
    hass = tokyo
    await _loaded(hass, aioclient_mock, entry, freezer)
    key = f"jma.{entry.entry_id}.alert_memory"
    memory = entry.runtime_data.memory
    memory.heat.append("20260101")
    memory.save()  # the delayed write has not run
    assert key not in hass_storage or hass_storage[key]["data"]["heat"] == []
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hass_storage[key]["data"]["heat"] == ["20260101"]


async def test_save_while_frozen_schedules_nothing(hass: HomeAssistant, freezer, hass_storage) -> None:
    memory = AlertMemory(hass, "frozen_save")
    key = "jma.frozen_save.alert_memory"
    memory.freeze()
    memory.heat.append("20260101")
    memory.save()
    await _after_the_delay(hass, freezer)
    assert key not in hass_storage
    await memory.async_flush()  # nothing was saved: nothing is written
    assert key not in hass_storage


async def test_the_flush_writes_the_copy_and_not_what_a_tracker_changed_afterwards(
        hass: HomeAssistant, freezer, hass_storage) -> None:
    memory = AlertMemory(hass, "frozen_flush")
    key = "jma.frozen_flush.alert_memory"
    memory.heat.append("20260101")
    memory.save()  # pending delayed write
    memory.freeze()
    memory.heat.append("20260102")
    memory.tsunami["home"] = "warning"  # decided after the freeze
    memory.save()
    await memory.async_flush()
    assert hass_storage[key]["data"]["heat"] == ["20260101"]
    assert hass_storage[key]["data"]["tsunami"] == {}
    await _after_the_delay(hass, freezer)  # no delayed write lands over it
    assert hass_storage[key]["data"]["heat"] == ["20260101"]
    assert hass_storage[key]["data"]["tsunami"] == {}


async def test_a_pending_delayed_write_of_a_frozen_memory_writes_the_copy(
        hass: HomeAssistant, freezer, hass_storage) -> None:
    memory = AlertMemory(hass, "frozen_pending")
    key = "jma.frozen_pending.alert_memory"
    memory.heat.append("20260101")
    memory.save()
    memory.freeze()
    memory.heat.append("20260102")
    await _after_the_delay(hass, freezer)
    assert hass_storage[key]["data"]["heat"] == ["20260101"]


async def test_unfreeze_writes_the_live_data(hass: HomeAssistant, freezer, hass_storage) -> None:
    memory = AlertMemory(hass, "unfrozen")
    key = "jma.unfrozen.alert_memory"
    memory.freeze()
    memory.heat.append("20260101")
    memory.save()
    memory.unfreeze()
    await _after_the_delay(hass, freezer)
    assert hass_storage[key]["data"]["heat"] == ["20260101"]
