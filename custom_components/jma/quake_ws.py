"""P2P地震情報 WebSocket client: 551 quake info, 556 EEW, 552 tsunami.

Pushes over wss://api.p2pquake.net/v2/ws; on every (re)connect the recent history is replayed
through the same trackers so nothing missed while disconnected is lost, and nothing old is
announced (EEW older than 120 s and quakes older than 10 min are recorded, not alerted).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import aiohttp
from homeassistant.util import dt as dt_util

from .api import HEADERS, JmaApi
from .const import (
    QUAKE_BACKFILL_MAX_AGE_S,
    SHINDO,
    TSUNAMI_BACKFILL_LIMIT,
    URL_P2P_HISTORY,
    URL_P2P_TSUNAMI,
    URL_P2P_WS,
)
from .errors import JmaError
from .parse_quake import EewTracker, Quake, QuakePlace, QuakeTracker, parse_551
from .parse_tsunami import TsunamiTracker

_LOGGER = logging.getLogger(__name__)
# 552 is NOT here: /v2/history?codes=552 answers [] (live 2026-10-03); see URL_P2P_TSUNAMI.
# While the tsunami list has never answered, ask again this often inside an open connection.
TSUNAMI_RETRY_S = 60
HISTORY_PARAMS = [("codes", "551"), ("codes", "556"), ("limit", "20")]


def quake_event_data(quake: Quake) -> dict[str, Any]:
    data = quake.as_dict()
    data["pref_shindo"] = SHINDO.get(quake.pref_max_scale or 0)
    data["local_shindo"] = SHINDO.get(quake.local_scale or 0)
    data["max_shindo"] = SHINDO.get(quake.max_scale or 0)
    return data


class QuakeClient:
    """Owns the socket, the trackers and the last quake."""

    def __init__(
        self,
        api: JmaApi,
        fire: Callable[[str, dict], None],
        on_quake: Callable[[], None],
        place: QuakePlace,
        memory: dict[str, Any] | None = None,
        save: Callable[[], None] | None = None,
        tsunami_area: str | None = None,
    ) -> None:
        self._api = api
        self._fire = fire
        self._on_quake = on_quake
        self.place = place
        # tsunami: `memory` is the "tsunami" section of AlertMemory.
        self._tsunami_memory: dict[str, Any] = memory if memory is not None else {}
        self._save: Callable[[], None] = save or (lambda: None)
        self._tsunami_area = tsunami_area
        self.tsunami = TsunamiTracker(tsunami_area, self._tsunami_memory)
        self._quakes = QuakeTracker(place)
        self._eew = EewTracker(place.pref)
        self.enabled = True  # False when the connection limit of the service is used up by other entries
        self.connected = False
        self.down_since: datetime | None = dt_util.utcnow()
        self.last_message: datetime | None = None
        self.last_quake: Quake | None = None
        self._retry_sleep: Callable[[float], Any] = asyncio.sleep  # injectable for tests

    def handle(self, msg: Any, now: datetime, backfill: bool = False) -> None:
        """Route one P2P message (pushed or replayed)."""
        if not isinstance(msg, dict):
            return
        code = msg.get("code")
        if code == 551:
            # Defensive: the 551 schema has no `test` key today (556 does).
            if msg.get("test") is True:
                return
            parsed = parse_551(msg, self.place)
            if parsed and parsed.time and (
                self.last_quake is None or self.last_quake.time is None
                or parsed.time >= self.last_quake.time
            ):
                self.last_quake = parsed
                self._on_quake()
            quake = self._quakes.feed(msg)
            if quake and quake.pref_max_scale is not None and not (
                backfill and quake.time
                and (now - quake.time).total_seconds() > QUAKE_BACKFILL_MAX_AGE_S
            ):
                self._fire("earthquake", {**quake_event_data(quake), "change": self._quakes.last_change})
        elif code == 556:
            for event in self._eew.feed(msg, now, backfill=backfill):
                event_type = event.pop("type")
                event["predicted_shindo"] = SHINDO.get(event.get("predicted_scale") or 0)
                self._fire(event_type, event)
        elif code == 552:
            self._tsunami_events(self.tsunami.feed(msg, now, backfill=backfill))

    def tsunami_tick(self, now: datetime) -> None:
        """Called every few minutes: let the 48 h expiry act without waiting for a message."""
        try:
            self._tsunami_events(self.tsunami.tick(now))
        except Exception:  # noqa: BLE001 - the timer must never raise
            _LOGGER.warning("tsunami tick failed", exc_info=True)

    def _tsunami_events(self, events: list[dict[str, Any]]) -> None:
        """Fire what the tracker decided, persist its memory, refresh the sensors."""
        for event in events:
            event_type = event.pop("type")
            self._fire(event_type, event)
        if self.tsunami.dirty:
            self.tsunami.dirty = False
            self._save()
        self._on_quake()

    def _safe_handle(self, msg: Any, now: datetime, backfill: bool) -> None:
        """One malformed message must never end the socket task."""
        try:
            self.handle(msg, now, backfill=backfill)
        except Exception:  # noqa: BLE001 - third-party payloads, any shape
            _LOGGER.warning("P2P message skipped (unexpected shape): %.200r", msg, exc_info=True)

    async def _backfill_quakes(self) -> None:
        """Replay the quake / EEW history (the one request the socket loop waits for)."""
        try:
            items = await self._api.get_json(URL_P2P_HISTORY, HISTORY_PARAMS)
        except JmaError as err:
            _LOGGER.debug("P2P history unavailable: %s", err)
        else:
            now = dt_util.now()
            for msg in reversed(items if isinstance(items, list) else []):
                self._safe_handle(msg, now, backfill=True)

    async def _backfill(self) -> bool:
        """Both replays one after the other; each fails on its own. Returns whether the tsunami list
        answered (with a list) on THIS call. run() does NOT use this: there the list is asked beside
        the socket loop (`_tsunami_retry`), so a slow list can never hold up a live EEW frame."""
        await self._backfill_quakes()
        return await self._replay_tsunami_list()

    async def _replay_tsunami_list(self) -> bool:
        """Ask for the tsunami list and replay it. Only a LIST answer counts (an empty one too);
        returns True for one."""
        try:
            bulletins = await self._api.get_json(
                URL_P2P_TSUNAMI, [("limit", str(TSUNAMI_BACKFILL_LIMIT))])
        except JmaError as err:
            _LOGGER.debug("P2P tsunami list unavailable: %s", err)
            return False
        if not isinstance(bulletins, list):
            _LOGGER.debug("P2P tsunami list: not a list, ignored")
            return False
        try:
            self._tsunami_events(self.tsunami.replay(bulletins, dt_util.now()))
        except Exception:  # noqa: BLE001 - third-party payloads, any shape
            _LOGGER.warning("P2P tsunami replay skipped (unexpected shape)", exc_info=True)
        return True  # it was a list; asking again would return the same shape

    async def _tsunami_retry(self) -> None:
        """Ask for the tsunami list beside the socket loop; while it has not answered on THIS
        connection, ask again every minute."""
        try:
            answered = await self._replay_tsunami_list()  # the first ask runs BESIDE the socket loop
            while not answered:
                await self._retry_sleep(TSUNAMI_RETRY_S)
                answered = await self._replay_tsunami_list()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a retry must never take the socket down
            _LOGGER.warning("P2P tsunami retry stopped", exc_info=True)

    async def run(self) -> None:
        """Connect forever. Backoff 1 -> 60 s, reset only by a connection that lasted 60 s, so
        a server that accepts and closes at once cannot make the client hammer it."""
        backoff = 1
        while True:
            connected_at: datetime | None = None
            try:
                async with self._api.session.ws_connect(
                    URL_P2P_WS, headers=HEADERS, heartbeat=30
                ) as ws:
                    self.connected = True
                    connected_at = dt_util.utcnow()
                    self._on_quake()  # the sensor's `connected` attribute follows the socket
                    # History AFTER the socket is open: anything published while we connected is
                    # then in one or the other; id dedupe absorbs the overlap.
                    # Only the history is waited for. The tsunami list runs as a task:
                    # frames that arrive meanwhile are handled at once, the tracker dedupes.
                    await self._backfill_quakes()
                    retry = asyncio.ensure_future(self._tsunami_retry())
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self.last_message = dt_util.utcnow()
                                try:
                                    payload = json.loads(msg.data)
                                except ValueError:
                                    continue
                                self._safe_handle(payload, dt_util.now(), backfill=False)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                    finally:
                        if retry is not None:
                            retry.cancel()
            except asyncio.CancelledError:
                raise
            except (TimeoutError, aiohttp.ClientError, OSError) as err:
                _LOGGER.debug("P2P socket: %s", err)
            except Exception:  # noqa: BLE001 - the quake socket must never stop for good
                _LOGGER.exception("P2P socket: unexpected error, reconnecting")
            finally:
                now = dt_util.utcnow()
                lasted = connected_at is not None and (now - connected_at).total_seconds() >= 60
                if lasted:
                    backoff = 1
                    self.down_since = now  # a fresh outage starts now
                elif self.down_since is None:
                    self.down_since = now  # otherwise the outage that was already running continues
                if self.connected:
                    self.connected = False
                    self._on_quake()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
