"""event.jma_<place>_alerts: every alert an automation can react to.

Events are queued and fired a few milliseconds apart, and only once Home Assistant has
started: an event entity's state is its fire time to the millisecond, and automations arm
their triggers during startup, so an event fired earlier would be missed or would replace the stored state.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.start import async_at_started

from .const import ALERTS_ATTRIBUTION, EVENT_TYPES
from .coordinator import JmaConfigEntry, signal_alert
from .entity import JmaEntity

PARALLEL_UPDATES = 0
EVENT_SPACING = 0.005


async def async_setup_entry(
    hass: HomeAssistant, entry: JmaConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([JmaAlerts(entry)])


class JmaAlerts(JmaEntity, EventEntity):
    _attr_attribution = ALERTS_ATTRIBUTION
    _attr_event_types = list(EVENT_TYPES)

    def __init__(self, entry: JmaConfigEntry) -> None:
        super().__init__(entry, "alerts")
        self._queue: list[tuple[str, dict[str, Any]]] = []
        self._started = False
        self._flushing: asyncio.Task[None] | None = None

    @property
    def available(self) -> bool:
        # Always available: its events come from the polling feeds too, so a lost earthquake socket is not its outage.
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_alert(self._entry_id), self._enqueue))
        self.async_on_remove(async_at_started(self.hass, self._on_started))

    async def _on_started(self, _hass: HomeAssistant) -> None:
        self._started = True
        self._kick()

    @callback
    def _enqueue(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type not in EVENT_TYPES:
            return
        self._queue.append((event_type, data))
        self._kick()

    @callback
    def _kick(self) -> None:
        if not self._started or not self._queue:
            return
        if self._flushing is None or self._flushing.done():
            self._flushing = self.hass.async_create_task(self._flush())

    async def _flush(self) -> None:
        while self._queue:
            event_type, data = self._queue.pop(0)
            self._trigger_event(event_type, data)
            self.async_write_ha_state()
            if self._queue:  # only space events that would otherwise share a millisecond
                await asyncio.sleep(EVENT_SPACING)
