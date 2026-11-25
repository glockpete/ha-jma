"""What Home Assistant has already announced about long-lived hazards, kept across restarts.

A typhoon stage, a heat alert for a date, a tsunami bulletin and a volcano's level stay true for hours or days;
without this a restart would send each of them again (and a level that changed while Home Assistant was down would
never be announced). Quakes, EEW and JMA warnings are NOT kept
here on purpose: those are announced again after a restart.

The trackers (parse_tsunami, parse_typhoon, parse_heat_alert, parse_volcano) are pure: they read and change the
plain dict / list of their own section and the owner calls save().

"Announced" must mean "handed to Home Assistant", not "decided": alerts
found while Home Assistant is still starting wait in the event entity's queue until it has
started. Until then save() only notes that there is something to write; the file is written once
Home Assistant is running. If it stops before that, the next start finds the old file and
announces the alert again, which is the right way round.

The same holds at unload: once the event entity is being removed nothing decided from then on reaches an entity (a
removed entity writes no state), yet a tracker would still change its section. freeze() therefore takes a copy of the
data as it was handed over; from then on the flush and any pending delayed write use that copy, and save() writes
nothing (not even a delayed write on this old Store that would land over the file of the reloaded entry). An alert
decided after the freeze is announced again by the next start; one that still reached the entity in the instant
before it was removed is then sent twice, which is again the right way round (a duplicate, never a loss). An unload
before Home Assistant has started writes nothing at all: the alerts noted during the start were still in the entity's
queue. unfreeze() undoes the freeze when the unload failed and the entry stays loaded.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, TypedDict

from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from .const import TSUNAMI_GRADE_KEY, TYPHOON_STAGE_RANK

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1
SAVE_DELAY_S = 2
_SCALARS = (str, int, float, bool, type(None))


class MemoryData(TypedDict):
    """The stored file: one section per tracker."""

    typhoon: dict[str, Any]
    heat: list[str]
    tsunami: dict[str, Any]
    volcano: dict[str, Any]


def empty_memory() -> MemoryData:
    return {"typhoon": {}, "heat": [], "tsunami": {}, "volcano": {}}


def _clean_typhoon(value: Any) -> dict[str, Any]:
    """{storm id: {"stage": notice|urgent, "quiet": int, ...scalars}}; anything else is dropped
    (the worst case of dropping an entry is one repeated alert, of keeping a bad one a poll that
    raises forever)."""
    out: dict[str, Any] = {}
    if not isinstance(value, dict):
        return out
    for storm_id, entry in value.items():
        if not isinstance(storm_id, str) or not isinstance(entry, dict):
            continue
        stage = entry.get("stage")
        if not isinstance(stage, str) or TYPHOON_STAGE_RANK.get(stage, 0) < 1:
            continue
        quiet = entry.get("quiet")
        kept = {k: v for k, v in entry.items() if isinstance(k, str) and isinstance(v, _SCALARS)}
        kept["stage"] = stage
        kept["quiet"] = quiet if isinstance(quiet, int) and not isinstance(quiet, bool) and quiet >= 0 else 0
        out[storm_id] = kept
    return out


def _clean_heat(value: Any) -> list[str]:
    return [k for k in value if isinstance(k, str)] if isinstance(value, list) else []


def _clean_tsunami(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {}
    if (not isinstance(value.get("active"), bool)
            or value.get("home") not in TSUNAMI_GRADE_KEY or value.get("max") not in TSUNAMI_GRADE_KEY
            or not isinstance(value.get("id"), str) or not isinstance(value.get("issued"), str)):
        return {}
    return {k: value[k] for k in ("active", "home", "max", "id", "issued")}


def _clean_volcano(value: Any) -> dict[str, Any]:
    """{volcano code: {"level": int 0-5, "kind_code", "name_ja", "name_en", "report_time", "relevance" ("municipality"
    or "distance"; missing in an entry written by release 0.8.0), "absent": int}}; an entry of another shape is dropped (one repeated or one missed event is the worst case of that, a poll that raises
    forever the worst case of keeping it). Only scalars are kept, and `level` and `absent` are whole numbers."""
    out: dict[str, Any] = {}
    if not isinstance(value, dict):
        return out
    for code, entry in value.items():
        if not isinstance(code, str) or not isinstance(entry, dict):
            continue
        level = entry.get("level")
        if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 5:
            continue
        kept = {k: v for k, v in entry.items() if isinstance(k, str) and isinstance(v, _SCALARS)}
        absent = entry.get("absent")
        kept["level"] = level
        kept["absent"] = absent if isinstance(absent, int) and not isinstance(absent, bool) and absent >= 0 else 0
        out[code] = kept
    return out


class AlertMemory:
    """One small JSON file per config entry (.storage/jma.<entry_id>.alert_memory)."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store: Store[MemoryData] = Store(hass, STORE_VERSION, f"jma.{entry_id}.alert_memory")
        self.data: MemoryData = empty_memory()
        # `hass.is_running` is also true while STARTING; the event entity delivers only once the
        # state is RUNNING (async_at_started), so that is the state that counts here too.
        self._running = hass.state is CoreState.running
        self._unsaved = False
        self._wrote = False  # a write was handed to the Store at least once: one may be under way
        self._frozen: MemoryData | None = None

    @property
    def typhoon(self) -> dict[str, Any]:
        return self.data["typhoon"]

    @property
    def heat(self) -> list[str]:
        return self.data["heat"]

    @property
    def tsunami(self) -> dict[str, Any]:
        return self.data["tsunami"]

    @property
    def volcano(self) -> dict[str, Any]:
        return self.data["volcano"]

    async def async_load(self) -> None:
        """A missing or damaged file is an empty memory: the worst case is one repeated alert.
        Entries of the wrong shape are dropped one by one, never raised later inside a poll."""
        stored: object  # the file on disk can hold anything: the typed shape is only what is written back
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001 - never block setup on this file
            _LOGGER.warning("alert memory could not be read; starting empty", exc_info=True)
            stored = None
        sections: dict[str, Any] = stored if isinstance(stored, dict) else {}
        self.data = {
            "typhoon": _clean_typhoon(sections.get("typhoon")),
            "heat": _clean_heat(sections.get("heat")),
            "tsunami": _clean_tsunami(sections.get("tsunami")),
            "volcano": _clean_volcano(sections.get("volcano")),
        }

    @callback
    def async_listen_started(self) -> CALLBACK_TYPE:
        """Write what was decided during startup once Home Assistant runs (see the module doc)."""

        async def started(_hass: HomeAssistant) -> None:
            self._running = True
            if self._unsaved:
                self._write()

        return async_at_started(self._hass, started)

    @callback
    def freeze(self) -> None:
        """Start of an unload: what is written from now on is a copy of the data as it is now (see the module doc).
        Before Home Assistant has started nothing was delivered, and the queue that holds it goes with the entity:
        what was noted for the start is then not written at all, also when the start ends during the unload."""
        self._frozen = copy.deepcopy(self.data)
        if not self._running:
            self._unsaved = False

    @callback
    def unfreeze(self) -> None:
        """The unload failed and the entry stays loaded: the live data is written again."""
        self._frozen = None
        self.save()

    @callback
    def save(self) -> None:
        if self._frozen is not None:
            return  # decided after the freeze: no entity can have received it, the next start announces it again
        self._unsaved = True
        if self._running:
            self._write()

    def _write(self) -> None:
        self._wrote = True
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY_S)

    def _data_to_save(self) -> MemoryData:
        self._unsaved = False
        return self._frozen if self._frozen is not None else self.data

    async def async_flush(self) -> None:
        """On unload: a reload inside the save delay must not forget an alert it just sent. Before
        Home Assistant runs nothing was delivered, so nothing is written. Once this object has written, it writes
        here again even with nothing unsaved: the Store does its writes one after the other, so this also waits for
        a delayed write that is under way and could otherwise land after the reloaded entry has read the file."""
        if self._running and (self._unsaved or self._wrote):
            self._unsaved = False
            await self._store.async_save(self._frozen if self._frozen is not None else self.data)

    async def async_remove(self) -> None:
        await self._store.async_remove()
