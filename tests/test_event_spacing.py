"""event.jma_<place>_alerts: two alerts never share a state.

The state of an event entity is its fire time to the millisecond. Home Assistant's state trigger does not fire for a
state that did not change when the automation names `to`, `from`, `not_to` or `not_from` (homeassistant/components/
homeassistant/triggers/state.py: `not match_all and old_value == new_value`), so the second of two alerts written in
the same millisecond would be missed by such an automation. Home Assistant runs the entity's writing task at once
(`async_create_task(..., eager_start=True)`), so the gap has to be kept against the last event written.

conftest.py sets the gap to 0 because the clock of the tests is frozen and a real wait would never end. Here the gap
is the 5 ms of the release and the wait moves the frozen clock by what it was asked to wait.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.jma.coordinator import signal_alert

from .conftest import mock_jma, setup

EVENT = "event.jma_chiyoda_alerts"
GAP = 0.005


async def test_alerts_sent_in_a_row_while_running_get_a_state_of_their_own(
        tokyo: HomeAssistant, aioclient_mock, entry, freezer) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    assert hass.states.get(EVENT).state == "unknown"
    written: list[tuple[str, str]] = []  # (event type, state) in the order the entity wrote them

    def on_change(event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if new is not None:
            written.append((new.attributes.get("event_type"), new.state))

    unsub = async_track_state_change_event(hass, [EVENT], on_change)
    waits: list[float] = []

    async def wait_on_the_frozen_clock(delay: float) -> None:
        waits.append(delay)
        freezer.tick(timedelta(seconds=delay))

    def send(event_type: str, n: int) -> None:
        async_dispatcher_send(hass, signal_alert(entry.entry_id), event_type, {"n": n})

    fake = SimpleNamespace(sleep=wait_on_the_frozen_clock, wait=asyncio.wait)
    with patch("custom_components.jma.event.EVENT_SPACING", GAP), patch("custom_components.jma.event.asyncio", fake):
        send("earthquake", 1)
        assert [t for t, _state in written] == ["earthquake"] and waits == []  # the first has nothing to wait for
        send("eew", 2)
        send("tsunami", 3)
        await hass.async_block_till_done()
        assert [t for t, _state in written] == ["earthquake", "eew", "tsunami"]
        assert len({state for _t, state in written}) == 3, written  # no two share a millisecond
        # each waited the gap, give or take the rounding of "written at + gap - now" on the loop's clock
        assert len(waits) == 2 and all(0 < wait <= GAP + 1e-4 for wait in waits), waits

        # an alert long after the last one is written at once
        freezer.tick(timedelta(seconds=1))
        send("heat_alert", 4)
        assert [t for t, _state in written][-1] == "heat_alert" and len(waits) == 2
    unsub()
    assert len({state for _t, state in written}) == 4, written
