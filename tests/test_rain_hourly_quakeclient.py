"""RainArming episodes, Open-Meteo JMA MSM rows, QuakeClient routing and replay limits."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from custom_components.jma.errors import JmaParseError
from custom_components.jma.parse_hourly import parse_hourly, wmo_condition
from custom_components.jma.parse_nowcast import RainArming, rain_series
from custom_components.jma.quake_ws import QuakeClient

from .conftest import QUAKE_PLACE
from .helpers import clone, load_json

JST = timezone(timedelta(hours=9))
BASE = "20260926040000"

# Taken at import time, before the autouse `no_quake_socket` fixture (tests/conftest.py) patches
# QuakeClient.run to a no-op for every other test in the suite. test_socket_opens_before_history_and
# _survives_odd_errors below restores this real implementation for its own run.
_REAL_RUN = QuakeClient.run


def _series(bands: list[int]):
    frames = [(BASE, bands[0])] + [
        (f"2026092604{m:02d}00" if m < 60 else "20260926050000", b)
        for m, b in zip(range(5, 65, 5), bands[1:], strict=False)
    ]
    return rain_series(BASE, frames)


def test_rain_arming_once_per_episode():
    arm = RainArming()
    t = datetime(2026, 9, 26, 13, 0, tzinfo=JST)
    coming = _series([0, 0, 0, 3] + [3] * 9)  # rain in 15 min
    assert arm.update(coming, t) is True
    assert arm.update(coming, t + timedelta(minutes=5)) is False  # same episode
    raining = _series([3] * 13)
    assert arm.update(raining, t + timedelta(minutes=15)) is False
    dry = _series([0] * 13)
    assert arm.update(dry, t + timedelta(minutes=30)) is False
    assert arm.update(coming, t + timedelta(minutes=60)) is False  # only 30 dry minutes
    assert arm.update(dry, t + timedelta(minutes=65)) is False
    assert arm.update(dry, t + timedelta(minutes=126)) is False  # 61 dry minutes: re-armed
    assert arm.update(coming, t + timedelta(minutes=130)) is True


def test_rain_arming_ignores_far_or_light_rain():
    arm = RainArming()
    t = datetime(2026, 9, 26, 13, 0, tzinfo=JST)
    far = _series([0] * 8 + [4] * 5)  # first rain at +40
    assert arm.update(far, t) is False
    drizzle = _series([0, 1, 1, 1] + [0] * 9)  # band 1 is under 1 mm/h
    assert arm.update(drizzle, t) is False


def test_hourly_rows_from_sample():
    rows = parse_hourly(load_json("openmeteo_msm.json"))
    assert rows[0]["datetime"] == "2026-09-26T00:00:00+09:00"
    assert rows[0]["native_temperature"] == 19.2
    assert rows[0]["condition"] == "partlycloudy"  # WMO 2
    assert "precipitation_probability" not in rows[0]  # null for every hour in JMA MSM
    assert rows[2]["condition"] == "rainy"  # WMO 53 drizzle
    assert len(rows) == 72


def test_wmo_mapping():
    assert wmo_condition(0, False) == "clear-night"
    assert wmo_condition(82, True) == "pouring"
    assert wmo_condition(95, True) == "lightning-rainy"
    assert wmo_condition(None, True) is None
    with pytest.raises(JmaParseError):
        parse_hourly({"error": True, "reason": "bad model"})


@pytest.mark.parametrize(("code", "condition"), [
    (45, "fog"), (48, "fog"),                                                   # fog, depositing rime fog
    (71, "snowy"), (75, "snowy"), (77, "snowy"), (85, "snowy"), (86, "snowy"),  # snow fall, grains, showers
    (4, None), (100, None),                                                     # not in the WMO table
])
def test_wmo_mapping_fog_snow_and_codes_outside_the_table(code, condition):
    assert wmo_condition(code, True) == condition
    assert wmo_condition(code, False) == condition


def test_hourly_row_with_a_weather_code_outside_the_table_has_no_condition():
    data = clone(load_json("openmeteo_msm.json"))
    data["hourly"]["weather_code"][1] = 4  # hour 0 stays code 2
    rows = parse_hourly(data)
    assert len(rows) == 72
    assert "condition" not in rows[1] and rows[1]["native_temperature"] == 18.5  # the other figures stay
    assert rows[0]["condition"] == "partlycloudy"


def test_hourly_rows_without_a_temperature_are_dropped():
    """Beyond the model's horizon the feed answers null; those hours leave no row."""
    data = clone(load_json("openmeteo_msm.json"))
    data["hourly"]["temperature_2m"][-6:] = [None] * 6  # the last six hours of the 72
    rows = parse_hourly(data)
    assert len(rows) == 66
    assert rows[-1]["datetime"] == "2026-09-28T17:00:00+09:00"


class _Rec:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.quakes = 0

    def fire(self, t: str, d: dict) -> None:
        self.events.append((t, d))

    def on_quake(self) -> None:
        self.quakes += 1


def _q(qid: str, when: str, local: int) -> dict:
    return {"code": 551, "id": qid, "issue": {"type": "DetailScale"},
            "earthquake": {"time": when, "maxScale": local, "domesticTsunami": "None",
                           "hypocenter": {"name": "東京湾", "magnitude": 4.5, "depth": 30}},
            "points": [{"pref": "東京都", "addr": "千代田区丸の内", "isArea": False, "scale": local}]}


def test_quake_client_routes_and_limits_replay():
    rec = _Rec()
    qc = QuakeClient(api=None, fire=rec.fire, on_quake=rec.on_quake, place=QUAKE_PLACE)  # type: ignore[arg-type]
    now = datetime(2026, 9, 26, 13, 0, tzinfo=JST)
    qc.handle(_q("old", "2026/09/26 12:00:00", 40), now, backfill=True)  # 60 min old
    assert rec.events == []  # recorded, not alerted
    assert qc.last_quake is not None and qc.last_quake.id == "old" and rec.quakes == 1
    qc.handle(_q("new", "2026/09/26 12:58:00", 30), now, backfill=True)
    assert [t for t, _ in rec.events] == ["earthquake"]
    data = rec.events[0][1]
    assert data["pref_shindo"] == "3" and data["local_shindo"] == "3"
    qc.handle({"code": 556, "test": False, "issue": {"eventId": "e1", "serial": "1",
               "time": "2026/09/26 12:59:50"},
               "earthquake": {"hypocenter": {"name": "東京湾"}},
               "areas": [{"name": "東京都２３区", "scaleFrom": 45, "scaleTo": 50}]}, now)
    assert rec.events[-1][0] == "eew" and rec.events[-1][1]["predicted_shindo"] == "5+"
    qc.handle({"code": 552, "time": "x"}, now)  # no id / issue time: ignored, never raises
    assert not hasattr(qc, "last_tsunami") and qc.tsunami.current is None
    assert [t for t, _ in rec.events] == ["earthquake", "eew"]


def test_quake_client_fires_no_earthquake_without_a_point_of_the_prefecture():
    """p2p_551_sample_a.json (Kumamoto etc.) carries no point of the entry's prefecture at all:
    last_quake keeps tracking the newest one for the sensor, but nothing in the prefecture reported, so
    no push. backfill=False so the age guard cannot be the reason nothing fires -- only the rule can."""
    rec = _Rec()
    qc = QuakeClient(api=None, fire=rec.fire, on_quake=rec.on_quake, place=QUAKE_PLACE)  # type: ignore[arg-type]
    now = datetime(2026, 9, 26, 13, 0, tzinfo=JST)
    for msg in load_json("p2p_551_sample_a.json"):
        qc.handle(msg, now, backfill=False)
    assert [t for t, _ in rec.events if t == "earthquake"] == []
    assert qc.last_quake is not None


class _WS:
    def __init__(self, order):
        self.order = order

    async def __aenter__(self):
        self.order.append("connect")
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


async def test_socket_opens_before_history_and_survives_odd_errors(hass, monkeypatch) -> None:
    """The socket must open before history is replayed (aiohttp buffers what arrives
    meanwhile, the trackers dedupe the overlap), and an unexpected (non-network) error from
    ws_connect must not kill the reconnect loop for good.

    Restores the real QuakeClient.run for this test only: tests/conftest.py's autouse
    no_quake_socket fixture patches QuakeClient.run to a no-op for every other test in the suite
    (QuakeClient.handle is driven directly there); this is the one test that exercises run()
    itself, so it must undo that patch."""
    monkeypatch.setattr(QuakeClient, "run", _REAL_RUN)
    order: list[str] = []
    attempts = {"n": 0}

    def ws_connect(*_a, **_k):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("odd")  # not a network error
        return _WS(order)

    async def get_json(url, params=None):  # the tsunami list, asked beside the socket loop
        return []

    client = QuakeClient(NS(session=NS(ws_connect=ws_connect), get_json=get_json), lambda *a: None, lambda: None,
                         QUAKE_PLACE)

    async def backfill() -> None:
        order.append("history")

    client._backfill_quakes = backfill
    task = asyncio.create_task(client.run())
    await asyncio.sleep(1.5)  # backoff 1 s after the odd error
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert order[:2] == ["connect", "history"]


def test_earthquake_events_say_what_changed_for_the_real_ibaraki_sequence():
    """The real 2026-08-23 茨城県南部 M5.9 reports (p2p_551_sample_b.json) are ONE quake.
    Each event carries `change`: the first report is "new", a higher prefecture maximum is "scale", and
    the report that only adds the local figure and the epicentre is "detail" (an update of a
    notification, not another alarm)."""
    rec = _Rec()
    qc = QuakeClient(api=None, fire=rec.fire, on_quake=rec.on_quake, place=QUAKE_PLACE)  # type: ignore[arg-type]
    now = datetime(2026, 8, 23, 2, 10, tzinfo=JST)
    for msg in sorted(load_json("p2p_551_sample_b.json"), key=lambda m: m["time"]):
        qc.handle(msg, now, backfill=False)
    quakes = [(d["change"], d["pref_shindo"], bool(d["hypocenter"])) for kind, d in rec.events if kind == "earthquake"]
    assert quakes == [("new", "4", False), ("scale", "5-", False), ("detail", "5-", True)]
