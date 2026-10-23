"""P2P地震情報 WebSocket client: 551 quake info, 556 EEW, 552 tsunami.

Pushes over wss://api.p2pquake.net/v2/ws; on every (re)connect the recent history is replayed
through the same trackers so nothing missed while disconnected is lost, and nothing old is
announced (EEW older than 120 s and quakes older than 10 min are recorded, not alerted).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import aiohttp
from homeassistant.util import dt as dt_util

from .api import HEADERS, JmaApi
from .const import (
    EEW_FUTURE_TOLERANCE_S,
    QUAKE_BACKFILL_MAX_AGE_S,
    SHINDO,
    TSUNAMI_BACKFILL_LIMIT,
    URL_P2P_HISTORY,
    URL_P2P_TSUNAMI,
    URL_P2P_WS,
)
from .errors import JmaError
from .parse_quake import EewTracker, Quake, QuakePlace, QuakeTracker, parse_551
from .parse_tsunami import TsunamiPicture, TsunamiTracker

_LOGGER = logging.getLogger(__name__)
# 552 is NOT here: /v2/history?codes=552 answers [] (live 2026-10-03); see URL_P2P_TSUNAMI.
# While the tsunami list has never answered, ask again this often inside an open connection.
TSUNAMI_RETRY_S = 60
LOG_INTERVAL_S = 300  # one WARNING per kind of bad frame in this time; the rest go to DEBUG, counted
HISTORY_PARAMS = [("codes", "551"), ("codes", "556"), ("limit", "20")]


def quake_event_data(quake: Quake) -> dict[str, Any]:
    data = quake.as_dict()
    data["pref_shindo"] = SHINDO.get(quake.pref_max_scale or 0)
    data["area_shindo"] = SHINDO.get(quake.area_max_scale or 0)
    data["local_shindo"] = SHINDO.get(quake.local_scale or 0)
    data["max_shindo"] = SHINDO.get(quake.max_scale or 0)
    return data


class QuakeClient:
    """Owns the socket, the trackers and the last quake."""

    def __init__(
        self,
        api: JmaApi,
        fire: Callable[[str, dict[str, Any]], None],
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
        # what the sensors were last told about: the bulletin id (an attribute) and the picture (the state)
        self._tsunami_shown: tuple[str | None, TsunamiPicture | None] = (None, None)
        self._quakes = QuakeTracker(place)
        self._eew = EewTracker(place.pref, place.area)
        self.enabled = True  # False when the connection limit of the service is used up by other entries
        self.connected = False
        self.down_since: datetime | None = dt_util.utcnow()
        self.last_message: datetime | None = None
        self.last_quake: Quake | None = None
        self._retry_sleep: Callable[[float], Any] = asyncio.sleep  # injectable for tests
        self._monotonic: Callable[[], float] = time.monotonic  # injectable for tests
        self._log_state: dict[str, tuple[float, int]] = {}  # kind -> (when last warned, how many held back since)
        self._outage_warned = False  # the outage running now has had its one WARNING; the INFO ends it
        self._backfill_task: asyncio.Task[None] | None = None

    def _warn_limited(self, kind: str, msg: str, *args: Any, exc_info: bool = False) -> None:
        """A WARNING for a kind of bad input at most once in LOG_INTERVAL_S; the rest go to DEBUG and are counted
        in the next WARNING, so a flood of bad frames is one line, not one line per frame."""
        now = self._monotonic()
        last, held = self._log_state.get(kind, (None, 0))
        if last is not None and now - last < LOG_INTERVAL_S:
            self._log_state[kind] = (last, held + 1)
            _LOGGER.debug(msg, *args, exc_info=exc_info)
            return
        self._log_state[kind] = (now, 0)
        if held:
            msg += f" (and {held:,} more of this kind since the last warning)"
        _LOGGER.warning(msg, *args, exc_info=exc_info)

    def _log_outage(self, opened: bool, reason: str, *, exc_info: bool = False) -> None:
        """The log of one outage (HA's log-when-unavailable): a WARNING for its first failure, DEBUG for every attempt
        after it, an INFO from the connection that ends it. `opened`: the connection was up before it failed."""
        if self._outage_warned:
            _LOGGER.debug("P2P socket: %s", reason, exc_info=exc_info)
            return
        self._outage_warned = True
        if opened:
            _LOGGER.warning("P2P earthquake connection lost: %s; reconnecting", reason, exc_info=exc_info)
        else:  # nothing was up yet (start-up) or the outage began with a refused connection
            _LOGGER.warning("P2P earthquake connection cannot be opened: %s; retrying", reason, exc_info=exc_info)

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
            if parsed and parsed.time and (parsed.time - now).total_seconds() > EEW_FUTURE_TOLERANCE_S:
                # The same guard as for an EEW: a quake "from the future" would become last_quake and make every
                # real quake after it look older. Not remembered, so it counts when the clock is right again.
                self._warn_limited(
                    "future 551", "P2P 551 %s ignored: origin %s is more than %d min ahead of the clock (%s); "
                    "the clock of this host looks wrong", parsed.id, parsed.time.isoformat(),
                    EEW_FUTURE_TOLERANCE_S // 60, now.isoformat())
                return
            if parsed and parsed.time and (
                self.last_quake is None or self.last_quake.time is None
                or parsed.time >= self.last_quake.time
            ):
                self.last_quake = parsed
                self._on_quake()
            quake = self._quakes.feed(msg)
            # The event follows the entry's scope: its area (23 wards of Tokyo) or, without one, its prefecture.
            if quake and quake.scope_max_scale is not None and not (
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
            self._tsunami_events(self.tsunami.feed(msg, now, backfill=backfill), now)

    def tsunami_tick(self, now: datetime) -> None:
        """Called every few minutes: let the 48 h expiry act without waiting for a message. The sensors are
        written only when the tick fired an event or changed what they show (a bulletin that expired, or a held
        future-dated one taken up)."""
        if not self.enabled:
            return
        try:
            events = self.tsunami.tick(now)
            if events or self._tsunami_view(now) != self._tsunami_shown:
                self._tsunami_events(events, now)
            else:
                self._save_tsunami_memory()
        except Exception:  # noqa: BLE001 - the timer must never raise
            _LOGGER.warning("tsunami tick failed", exc_info=True)

    def _tsunami_events(self, events: list[dict[str, Any]], now: datetime) -> None:
        """Fire what the tracker decided, persist its memory, refresh the sensors."""
        for event in events:
            event_type = event.pop("type")
            self._fire(event_type, event)
        self._save_tsunami_memory()
        self._tsunami_shown = self._tsunami_view(now)
        self._on_quake()

    def _tsunami_view(self, now: datetime) -> tuple[str | None, TsunamiPicture | None]:
        current = self.tsunami.current
        return (current.id if current else None, self.tsunami.picture(now))

    def _save_tsunami_memory(self) -> None:
        if self.tsunami.dirty:
            self.tsunami.dirty = False
            self._save()

    def _safe_handle(self, msg: Any, now: datetime, backfill: bool) -> None:
        """One malformed message must never end the socket task."""
        try:
            self.handle(msg, now, backfill=backfill)
        except Exception as err:  # noqa: BLE001 - third-party payloads, any shape
            self._warn_limited(
                f"bad frame {type(err).__name__}", "P2P message skipped (unexpected shape, %s): %.200r",
                type(err).__name__, msg, exc_info=True)

    async def _backfill_quakes(self) -> None:
        """Replay the quake / EEW history. It runs beside the socket loop (a task of the connection): live frames
        are handled meanwhile, and nothing in here may end that task's connection."""
        try:
            items = await self._api.get_json(URL_P2P_HISTORY, HISTORY_PARAMS)
        except JmaError as err:
            _LOGGER.debug("P2P history unavailable: %s", err)
        except Exception:  # noqa: BLE001 - a replay must never take the socket down
            self._warn_limited("history", "P2P history replay failed", exc_info=True)
        else:
            now = dt_util.now()
            for msg in reversed(items if isinstance(items, list) else []):
                self._safe_handle(msg, now, backfill=True)

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
            now = dt_util.now()
            self._tsunami_events(self.tsunami.replay(bulletins, now), now)
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

    def _start_backfill(self) -> None:
        """The history of this connection, as a task the client owns; one still out from the last connection ends."""
        self._stop_backfill()
        self._backfill_task = asyncio.ensure_future(self._backfill_quakes())

    def _stop_backfill(self) -> None:
        if self._backfill_task is not None:
            self._backfill_task.cancel()
            self._backfill_task = None

    async def run(self) -> None:
        """Connect forever (see _run_connections); when this task is cancelled the history task ends with it."""
        try:
            await self._run_connections()
        finally:
            self._stop_backfill()

    async def _run_connections(self) -> None:
        """Backoff 1 -> 60 s, reset only by a connection that lasted 60 s, so
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
                    if self._outage_warned:
                        self._outage_warned = False
                        _LOGGER.info("P2P earthquake connection is back")
                    self._on_quake()  # the sensor's `connected` attribute follows the socket
                    # History AFTER the socket is open: anything published while we connected is
                    # then in one or the other; id dedupe absorbs the overlap.
                    # Nothing is waited for: the history (the client's task, it ends with the next connection or
                    # the stop) and the tsunami list (cancelled with this connection) run beside the socket loop;
                    # frames that arrive meanwhile are handled at once, the trackers dedupe.
                    self._start_backfill()
                    retry = asyncio.ensure_future(self._tsunami_retry())
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self.last_message = dt_util.utcnow()
                                try:
                                    payload = json.loads(msg.data)
                                except ValueError:
                                    _LOGGER.debug("P2P socket: frame is not JSON, dropped: %.80r", msg.data)
                                    continue
                                self._safe_handle(payload, dt_util.now(), backfill=False)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                    finally:
                        retry.cancel()
            except asyncio.CancelledError:
                raise
            except (TimeoutError, aiohttp.ClientError, OSError) as err:
                self._log_outage(connected_at is not None, str(err) or type(err).__name__)
            except Exception as err:  # noqa: BLE001 - the quake socket must never stop for good
                # The traceback is kept for the first failure of an outage only, not once a minute.
                self._log_outage(connected_at is not None, f"unexpected {type(err).__name__}", exc_info=True)
            else:
                self._log_outage(True, "closed by the server")
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
