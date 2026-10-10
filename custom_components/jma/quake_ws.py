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
from .const import QUAKE_BACKFILL_MAX_AGE_S, SHINDO, URL_P2P_HISTORY, URL_P2P_WS
from .errors import JmaError
from .parse_quake import EewTracker, Quake, QuakePlace, QuakeTracker, parse_551

_LOGGER = logging.getLogger(__name__)
HISTORY_PARAMS = [("codes", "551"), ("codes", "552"), ("codes", "556"), ("limit", "20")]


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
    ) -> None:
        self._api = api
        self._fire = fire
        self._on_quake = on_quake
        self.place = place
        self._quakes = QuakeTracker(place)
        self._eew = EewTracker(place.pref)
        self.enabled = True  # False when the connection limit of the service is used up by other entries
        self.connected = False
        self.down_since: datetime | None = dt_util.utcnow()
        self.last_message: datetime | None = None
        self.last_quake: Quake | None = None
        self.last_tsunami: dict[str, Any] | None = None

    def handle(self, msg: Any, now: datetime, backfill: bool = False) -> None:
        """Route one P2P message (pushed or replayed)."""
        if not isinstance(msg, dict):
            return
        code = msg.get("code")
        if code == 551:
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
                self._fire("earthquake", quake_event_data(quake))
        elif code == 556:
            for event in self._eew.feed(msg, now, backfill=backfill):
                event_type = event.pop("type")
                event["predicted_shindo"] = SHINDO.get(event.get("predicted_scale") or 0)
                self._fire(event_type, event)
        elif code == 552:
            self.last_tsunami = {k: msg.get(k) for k in ("time", "cancelled", "areas")}
            _LOGGER.info("P2P tsunami information received (recorded, not alerted)")

    def _safe_handle(self, msg: Any, now: datetime, backfill: bool) -> None:
        """One malformed message must never end the socket task."""
        try:
            self.handle(msg, now, backfill=backfill)
        except Exception:  # noqa: BLE001 - third-party payloads, any shape
            _LOGGER.warning("P2P message skipped (unexpected shape): %.200r", msg)

    async def _backfill(self) -> None:
        try:
            items = await self._api.get_json(URL_P2P_HISTORY, HISTORY_PARAMS)
        except JmaError as err:
            _LOGGER.debug("P2P history unavailable: %s", err)
            return
        now = dt_util.now()
        for msg in reversed(items if isinstance(items, list) else []):
            self._safe_handle(msg, now, backfill=True)

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
                    # History AFTER the socket is open: anything published while connecting is
                    # then in one or the other; id dedupe absorbs the overlap.
                    await self._backfill()
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
