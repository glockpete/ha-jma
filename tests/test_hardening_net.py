"""Hardening, net batch: hostile feeds, hostile HTTP bodies, the P2P socket loop and its log.

Each test asserts the FIXED behaviour of one reproduced defect (N1..N8).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

import custom_components.jma.api as api_module
from custom_components.jma.api import JmaApi
from custom_components.jma.const import EEW_FUTURE_TOLERANCE_S, HTTP_MAX_BODY_BYTES, URL_P2P_HISTORY
from custom_components.jma.errors import JmaError
from custom_components.jma.parse_quake import ALERTED_MAX, EewTracker, parse_551
from custom_components.jma.parse_tsunami import TsunamiTracker, parse_552
from custom_components.jma.quake_ws import LOG_INTERVAL_S, QuakeClient

from .conftest import QUAKE_PLACE
from .helpers import FIXTURES, clone, load_json
from .test_tsunami import HOME, NS, REAL_RUN, _eew_frame, _FrameSocket, _warning_bulletin, _yield_loop

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 17, 0, 12, tzinfo=JST)
INF, NAN = float("inf"), float("nan")


def _client(**kw) -> tuple[QuakeClient, list[tuple[str, dict]]]:
    fired: list[tuple[str, dict]] = []
    return QuakeClient(kw.pop("api", None), lambda t, d: fired.append((t, d)), lambda: None, QUAKE_PLACE, **kw), fired


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


# ---------------------------------------------------------------- N1: a 552 with no known grade

def test_n1_a_frame_with_only_unknown_grades_is_ignored_not_read_as_all_clear(caplog):
    warn = _warning_bulletin()
    tracker = TsunamiTracker(HOME, {})
    now = parse_552(warn).issued + timedelta(minutes=5)
    [first] = tracker.feed(warn, now)
    assert first["type"] == "tsunami" and first["home_grade"] == "warning"
    unknown = clone(warn)
    unknown["id"], unknown["issue"]["time"] = "unknown-1", "2025/07/30 20:50:00"
    for area in unknown["areas"]:
        area["grade"] = "Unknown"
    with caplog.at_level(logging.DEBUG, logger="custom_components.jma.parse_tsunami"):
        assert parse_552(unknown) is None
        assert tracker.feed(unknown, now) == []
    assert any("no known grade" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)
    assert tracker.current.id == "warn-1" and tracker.memory["active"] is True
    assert tracker.memory["home"] == "warning"
    # the picture is still the warning, and a real lowering afterwards is still announced
    assert tracker.picture(now).home == "warning"
    watch = clone(warn)
    watch["id"], watch["issue"]["time"] = "watch-2", "2025/07/30 21:00:00"
    for area in watch["areas"]:
        area["grade"] = "Watch"
    [lowered] = tracker.feed(watch, now + timedelta(minutes=15))
    assert lowered["type"] == "tsunami" and lowered["home_grade"] == "watch"


def test_n1_a_missing_or_foreign_grade_counts_as_not_known_too():
    for grade in (None, "", "Advisory", 3):
        msg = _warning_bulletin()
        for area in msg["areas"]:
            area["grade"] = grade
        assert parse_552(msg) is None


def test_n1_one_known_grade_among_unknown_ones_still_makes_a_bulletin():
    msg = _warning_bulletin()
    msg["areas"][0]["grade"] = "Unknown"
    bulletin = parse_552(msg)
    assert bulletin is not None and any(a.rank for a in bulletin.areas)


def test_n1_a_real_cancel_frame_still_cancels():
    warn = _warning_bulletin()
    tracker = TsunamiTracker(HOME, {})
    now = parse_552(warn).issued + timedelta(minutes=5)
    tracker.feed(warn, now)
    cancel = clone(load_json("p2p_552_cancelled.json"))
    cancel["id"], cancel["issue"]["time"] = "cancel-1", "2025/07/30 21:30:00"
    [event] = tracker.feed(cancel, now + timedelta(minutes=50))
    assert event["type"] == "tsunami_cancelled" and event["cancelled"] is True


# ---------------------------------------------------------------- N2: a 551 from the future

def _551(qid: str, origin: str, scale: int = 30, points: list | None = None) -> dict:
    return {"code": 551, "id": qid, "issue": {"type": "DetailScale", "time": "2026/09/17 00:10:00"},
            "earthquake": {"time": origin, "maxScale": scale, "domesticTsunami": "None",
                           "hypocenter": {"name": "茨城県南部", "magnitude": 4.6, "depth": 50}},
            "points": [{"pref": "東京都", "addr": "東京千代田区大手町", "isArea": False, "scale": scale}]
            if points is None else points}


@pytest.mark.parametrize("backfill", [False, True])
def test_n2_a_future_551_does_not_pin_last_quake(backfill, caplog):
    c, fired = _client()
    c.handle(_551("bad", "2099/01/01 00:00:00"), NOW, backfill=backfill)
    assert c.last_quake is None and fired == []
    c.handle(_551("real", "2026/09/17 00:07:00"), NOW, backfill=backfill)
    assert c.last_quake.id == "real"
    assert [t for t, _ in fired] == ["earthquake"]
    assert any("2099" in r.getMessage() for r in _warnings(caplog))


def test_n2_the_tolerance_is_the_one_of_the_eew_guard():
    c, fired = _client()
    edge = NOW + timedelta(seconds=EEW_FUTURE_TOLERANCE_S)
    c.handle(_551("edge", edge.strftime("%Y/%m/%d %H:%M:%S")), NOW)
    assert c.last_quake is not None and c.last_quake.id == "edge"
    c2, fired2 = _client()
    over = NOW + timedelta(seconds=EEW_FUTURE_TOLERANCE_S + 60)
    c2.handle(_551("over", over.strftime("%Y/%m/%d %H:%M:%S")), NOW)
    assert c2.last_quake is None and fired2 == []


def test_n2_a_dropped_future_551_is_not_remembered_so_it_counts_when_the_clock_is_right():
    c, fired = _client()
    later = NOW + timedelta(hours=1)
    msg = _551("clock", later.strftime("%Y/%m/%d %H:%M:%S"))
    c.handle(msg, NOW)
    assert c.last_quake is None
    c.handle(msg, later + timedelta(minutes=1))
    assert c.last_quake.id == "clock" and [t for t, _ in fired] == ["earthquake"]


# ---------------------------------------------------------------- N3: one bad point of a 551

_OSAKA = {"pref": "大阪府", "addr": "大阪市"}
JUNK_POINTS = [
    pytest.param(None, id="null"),
    pytest.param("x", id="str"),
    pytest.param(5, id="int"),
    pytest.param([], id="list"),
    pytest.param({**_OSAKA, "scale": NAN}, id="nan"),
    pytest.param({**_OSAKA, "scale": INF}, id="inf"),
    pytest.param({**_OSAKA, "scale": -INF}, id="-inf"),
    pytest.param({**_OSAKA, "scale": None}, id="scale-null"),
    pytest.param({**_OSAKA, "scale": "50"}, id="scale-str"),
    pytest.param({**_OSAKA, "scale": True}, id="scale-bool"),
    pytest.param({"pref": "大阪府", "scale": 50}, id="no-addr"),
    pytest.param({"pref": "東京都", "scale": NAN}, id="tokyo-nan-no-addr"),
    pytest.param({"pref": "東京都", "addr": "東京千代田区大手町", "scale": INF}, id="tokyo-inf"),
    pytest.param({"pref": "東京都", "addr": "東京千代田区大手町", "scale": -INF}, id="tokyo--inf"),
]


@pytest.mark.parametrize("junk", JUNK_POINTS)
def test_n3_one_bad_point_does_not_drop_the_quake(junk, caplog):
    c, fired = _client()
    good = {"pref": "東京都", "addr": "東京千代田区大手町", "isArea": False, "scale": 40}
    msg = _551("q1", "2026/09/17 00:07:00", scale=50, points=[junk, good])
    c._safe_handle(msg, NOW, backfill=False)
    assert [t for t, _ in fired] == ["earthquake"]
    assert fired[0][1]["pref_max_scale"] == 40 and fired[0][1]["local_scale"] == 40
    assert c.last_quake.pref_max_scale == 40
    assert not _warnings(caplog)


def test_n3_max_scale_comes_from_the_finite_numbers_only():
    msg = _551("q2", "2026/09/17 00:07:00", points=[
        {"pref": "東京都", "addr": "千代田区", "scale": INF},
        {"pref": "東京都", "addr": "千代田区", "scale": 45.0},
        {"pref": "東京都", "addr": "港区", "scale": NAN},
        {"pref": "東京都", "addr": "港区", "scale": 30},
    ])
    quake = parse_551(msg, QUAKE_PLACE)
    assert quake.pref_max_scale == 45 and isinstance(quake.pref_max_scale, int)


@pytest.mark.parametrize("points", [5, "abc", {"a": 1}, [None, NAN], [{"pref": "東京都", "scale": NAN}]])
def test_n3_no_valid_point_left_is_a_quake_without_a_scale_as_before(points):
    quake = parse_551(_551("q3", "2026/09/17 00:07:00", points=points), QUAKE_PLACE)
    assert quake is not None and quake.pref_max_scale is None and quake.area_max_scale is None


# ---------------------------------------------------------------- N4: a non-finite EEW scale

def _556(scale_from, scale_to, event_id="e1") -> dict:
    return {"code": 556, "test": False, "cancelled": False,
            "issue": {"time": "2026/10/03 11:59:50", "eventId": event_id, "serial": "1"},
            "earthquake": {"originTime": "2026/10/03 11:59:40", "hypocenter": {"name": "東京湾"}},
            "areas": [{"pref": "東京", "name": "東京都２３区", "scaleFrom": scale_from, "scaleTo": scale_to}]}


@pytest.mark.parametrize("bad", [INF, -INF, NAN], ids=["inf", "-inf", "nan"])
def test_n4_a_non_finite_scale_to_is_an_unknown_scale_and_the_event_goes_out(bad, caplog):
    c, fired = _client()
    c._safe_handle(_556(55, bad), datetime(2026, 10, 3, 12, 0, tzinfo=JST), backfill=False)
    [(kind, data)] = fired
    assert kind == "eew" and data["predicted_scale"] == 55 and data["predicted_shindo"] == "6-"
    assert data["scale_from"] == 55 and data["scale_to"] is None
    assert not _warnings(caplog)


@pytest.mark.parametrize("bad", [INF, NAN], ids=["inf", "nan"])
def test_n4_a_non_finite_scale_from_is_an_unknown_scale_and_the_event_goes_out(bad):
    c, fired = _client()
    c._safe_handle(_556(bad, 50), datetime(2026, 10, 3, 12, 0, tzinfo=JST), backfill=False)
    [(kind, data)] = fired
    assert kind == "eew" and data["predicted_scale"] == 50
    assert data["scale_from"] is None and data["scale_to"] == 50


def test_n4_both_scales_non_finite_still_announce_the_warning_with_no_scale():
    c, fired = _client()
    c._safe_handle(_556(NAN, INF), datetime(2026, 10, 3, 12, 0, tzinfo=JST), backfill=False)
    [(kind, data)] = fired
    assert kind == "eew" and data["predicted_scale"] == 0
    assert data["scale_from"] is None and data["scale_to"] is None


def test_n4_a_finite_float_scale_is_kept_as_sent():
    c, fired = _client()
    c._safe_handle(_556(45.0, 50.0), datetime(2026, 10, 3, 12, 0, tzinfo=JST), backfill=False)
    assert fired[0][1]["scale_from"] == 45.0 and fired[0][1]["predicted_scale"] == 50
    assert all(math.isfinite(fired[0][1][k]) for k in ("scale_from", "scale_to"))


# ---------------------------------------------------------------- N5: JmaApi against hostile bodies

async def test_n5a_deeply_nested_json_is_a_jma_error_not_a_recursion_error(hass, aioclient_mock):
    aioclient_mock.get(URL_P2P_HISTORY, text="[" * 200000)
    with pytest.raises(JmaError):
        await JmaApi(async_get_clientsession(hass)).get_json(URL_P2P_HISTORY)


def test_n5b_the_cap_is_far_above_the_largest_file_the_tests_know():
    largest = max(p.stat().st_size for p in Path(FIXTURES).iterdir() if p.is_file())
    assert HTTP_MAX_BODY_BYTES >= 4 * largest
    assert HTTP_MAX_BODY_BYTES <= 64 * 1024 * 1024


async def test_n5b_a_declared_length_over_the_cap_is_refused(hass, aioclient_mock, monkeypatch):
    monkeypatch.setattr(api_module, "HTTP_MAX_BODY_BYTES", 1000)
    aioclient_mock.get("https://example.test/a", text="x", headers={"Content-Length": "1001"})
    with pytest.raises(JmaError, match="limit"):
        await JmaApi(async_get_clientsession(hass)).get_bytes("https://example.test/a")


async def test_n5b_a_body_over_the_cap_is_refused_whatever_the_header_says(hass, aioclient_mock, monkeypatch):
    monkeypatch.setattr(api_module, "HTTP_MAX_BODY_BYTES", 1000)
    api = JmaApi(async_get_clientsession(hass))
    aioclient_mock.get("https://example.test/b", text="x" * 1001)
    for call in (api.get_bytes, api.get_text, api.get_json):
        with pytest.raises(JmaError, match="limit"):
            await call("https://example.test/b")


async def test_n5b_a_body_at_the_cap_and_a_bad_length_header_are_fine(hass, aioclient_mock, monkeypatch):
    monkeypatch.setattr(api_module, "HTTP_MAX_BODY_BYTES", 1000)
    api = JmaApi(async_get_clientsession(hass))
    aioclient_mock.get("https://example.test/c", text="x" * 1000, headers={"Content-Length": "1000"})
    assert await api.get_bytes("https://example.test/c") == b"x" * 1000
    aioclient_mock.clear_requests()
    aioclient_mock.get("https://example.test/c", text='{"a": 1}', headers={"Content-Length": "twelve"})
    assert await api.get_json("https://example.test/c") == {"a": 1}


async def test_n5d_a_redirect_is_a_jma_error_and_is_not_followed(hass, aioclient_mock):
    aioclient_mock.get("https://example.test/r", status=302, headers={"Location": "https://other.test/x"})
    api = JmaApi(async_get_clientsession(hass))
    for call in (api.get_bytes, api.get_text, api.get_json):
        with pytest.raises(JmaError, match="redirect"):
            await call("https://example.test/r")
    assert [str(c[1]) for c in aioclient_mock.mock_calls] == ["https://example.test/r"] * 3


async def test_n5_the_request_is_made_without_following_redirects(hass):
    seen: dict = {}

    class Response:
        status = 200
        headers: dict = {}
        content = NS(iter_chunked=lambda n: _chunks([b"[]"]))

        async def read(self):
            return b"[]"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    session = NS(get=lambda url, **kw: seen.update(kw) or Response())
    assert await JmaApi(session).get_json("https://example.test/") == []
    assert seen["allow_redirects"] is False


async def _chunks(parts):
    for part in parts:
        yield part


@contextlib.asynccontextmanager
async def _server(routes: dict):
    app = web.Application()
    for path, handler in routes.items():
        app.router.add_get(path, handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            yield JmaApi(session), f"http://127.0.0.1:{server.port}"
    finally:
        await server.close()


@pytest.mark.usefixtures("socket_enabled")
async def test_n5b_a_streamed_body_with_no_length_is_cut_at_the_cap(monkeypatch):
    monkeypatch.setattr(api_module, "HTTP_MAX_BODY_BYTES", 100_000)
    sent = {"bytes": 0}

    async def endless(request):
        resp = web.StreamResponse()
        await resp.prepare(request)
        while True:  # the client must hang up: the server never stops by itself
            await resp.write(b"\0" * 65536)
            sent["bytes"] += 65536

    async with _server({"/endless": endless}) as (api, base):
        with pytest.raises(JmaError, match="limit"):
            await asyncio.wait_for(api.get_bytes(base + "/endless"), 10)
    assert sent["bytes"] < 10_000_000


@pytest.mark.usefixtures("socket_enabled")
async def test_n5b_a_declared_length_over_the_cap_is_refused_before_the_body_is_read(monkeypatch):
    monkeypatch.setattr(api_module, "HTTP_MAX_BODY_BYTES", 1000)

    async def liar(request):
        resp = web.StreamResponse(headers={"Content-Length": "5000"})
        await resp.prepare(request)
        await resp.write(b"x" * 10)
        await asyncio.sleep(30)  # never sends the rest
        return resp

    async with _server({"/liar": liar}) as (api, base):
        with pytest.raises(JmaError, match="limit"):
            await asyncio.wait_for(api.get_bytes(base + "/liar"), 5)


@pytest.mark.usefixtures("socket_enabled")
async def test_n5c_a_body_dripped_one_byte_at_a_time_ends_with_the_total_timeout(monkeypatch):
    monkeypatch.setattr(api_module, "TIMEOUT", aiohttp.ClientTimeout(total=0.4))

    async def drip(request):
        resp = web.StreamResponse()
        await resp.prepare(request)
        while True:
            await resp.write(b"x")
            await asyncio.sleep(0.05)

    async with _server({"/drip": drip}) as (api, base):
        with pytest.raises(JmaError):
            await asyncio.wait_for(api.get_bytes(base + "/drip"), 10)


@pytest.mark.usefixtures("socket_enabled")
async def test_n5d_a_real_redirect_is_refused_and_the_target_is_never_asked():
    hits: list[str] = []

    async def redirect(request):
        raise web.HTTPFound("/other.json")

    async def other(request):
        hits.append(str(request.url))
        return web.json_response({"served_by": "elsewhere"})

    async with _server({"/redirect": redirect, "/other.json": other}) as (api, base):
        with pytest.raises(JmaError, match="redirect"):
            await api.get_json(base + "/redirect")
    assert hits == []


@pytest.mark.usefixtures("socket_enabled")
async def test_n5_a_normal_json_answer_still_works():
    async def good(request):
        return web.json_response({"a": [1, 2, 3]})

    async with _server({"/good": good}) as (api, base):
        assert await api.get_json(base + "/good") == {"a": [1, 2, 3]}
        assert await api.get_bytes(base + "/missing") is None  # 404 stays "None" for get_bytes


# ---------------------------------------------------------------- N6: the history never stands in front of the socket

def _connection(frames, closed, get_json):
    sockets = []

    def ws_connect(*a, **k):
        sockets.append(_FrameSocket(frames, closed))
        return sockets[-1]

    fired: list[str] = []
    qc = QuakeClient(NS(session=NS(ws_connect=ws_connect), get_json=get_json),
                     lambda t, d: fired.append(t), lambda: None, place=QUAKE_PLACE, tsunami_area=HOME)
    return qc, fired, sockets


async def _stop(task, *events):
    for event in events:
        event.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_n6_an_error_in_the_history_does_not_tear_the_connection_down(freezer, caplog):
    freezer.move_to("2026-09-26T04:12:00+00:00")
    closed = asyncio.Event()

    async def get_json(url, params=None):
        if url == URL_P2P_HISTORY:
            raise RecursionError("maximum recursion depth exceeded")
        return []

    qc, fired, sockets = _connection([_eew_frame(dt_util.now())], closed, get_json)
    task = asyncio.ensure_future(REAL_RUN(qc))
    try:
        await _yield_loop(200)
        assert len(sockets) == 1 and qc.connected is True  # not reconnecting
        assert fired == ["eew"]
        assert sum("history" in r.getMessage() for r in _warnings(caplog)) == 1
    finally:
        await _stop(task, closed)


async def test_n6_a_live_frame_is_handled_while_the_history_is_still_out(freezer):
    freezer.move_to("2026-09-26T04:12:00+00:00")
    closed, history_answered = asyncio.Event(), asyncio.Event()

    async def get_json(url, params=None):
        if url == URL_P2P_HISTORY:
            await history_answered.wait()
        return []

    qc, fired, _ = _connection([_eew_frame(dt_util.now())], closed, get_json)
    task = asyncio.ensure_future(REAL_RUN(qc))
    try:
        await _yield_loop(200)
        assert fired == ["eew"] and qc.connected is True
    finally:
        await _stop(task, closed, history_answered)


async def test_n6_the_history_is_still_replayed_when_it_answers_late(freezer):
    freezer.move_to("2026-09-26T04:12:00+00:00")
    closed, history_answered = asyncio.Event(), asyncio.Event()
    stamp = dt_util.now().astimezone(JST).strftime("%Y/%m/%d %H:%M:%S")
    old_quake = _551("late", stamp, scale=50)

    async def get_json(url, params=None):
        if url == URL_P2P_HISTORY:
            await history_answered.wait()
            return [old_quake]
        return []

    qc, fired, _ = _connection([], closed, get_json)
    task = asyncio.ensure_future(REAL_RUN(qc))
    try:
        await _yield_loop(100)
        assert fired == []
        history_answered.set()
        await _yield_loop(100)
        assert qc.last_quake is not None and qc.last_quake.id == "late"
    finally:
        await _stop(task, closed, history_answered)


async def test_n6_the_history_task_ends_when_the_client_stops(freezer):
    freezer.move_to("2026-09-26T04:12:00+00:00")
    closed = asyncio.Event()
    state = {"cancelled": 0}

    async def get_json(url, params=None):
        if url == URL_P2P_HISTORY:
            try:
                await asyncio.Event().wait()  # never answers
            except asyncio.CancelledError:
                state["cancelled"] += 1
                raise
        return []

    qc, _, _ = _connection([], closed, get_json)
    task = asyncio.ensure_future(REAL_RUN(qc))
    try:
        await _yield_loop(100)
        assert state["cancelled"] == 0
        await _stop(task, closed)
        await _yield_loop(20)
        assert state["cancelled"] == 1
    finally:
        closed.set()
        task.cancel()


async def test_n6_a_new_connection_ends_the_history_still_out_from_the_last_one(freezer):
    freezer.move_to("2026-09-26T04:12:00+00:00")
    first_closed, second_closed = asyncio.Event(), asyncio.Event()
    state = {"asked": 0, "cancelled": 0}

    async def get_json(url, params=None):
        if url == URL_P2P_HISTORY:
            state["asked"] += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["cancelled"] += 1
                raise
        return []

    sockets = [_FrameSocket([], first_closed), _FrameSocket([], second_closed)]
    qc = QuakeClient(NS(session=NS(ws_connect=lambda *a, **k: sockets.pop(0) if len(sockets) > 1 else sockets[0]),
                        get_json=get_json), lambda t, d: None, lambda: None, place=QUAKE_PLACE, tsunami_area=HOME)
    real_sleep = asyncio.sleep

    async def quick_backoff(seconds, *a, **k):
        await real_sleep(0)

    with patch("custom_components.jma.quake_ws.asyncio.sleep", new=quick_backoff):
        task = asyncio.ensure_future(REAL_RUN(qc))
        try:
            await _yield_loop(50)
            assert state == {"asked": 1, "cancelled": 0} and qc.connected is True
            first_closed.set()  # the connection drops; its history is not answered
            await _yield_loop(100)
            assert state == {"asked": 2, "cancelled": 1} and qc.connected is True
        finally:
            await _stop(task, first_closed, second_closed)


# ---------------------------------------------------------------- N7: a flood of bad frames is not a flood of log lines

_BAD_FRAME = {"code": 551, "earthquake": "x"}  # AttributeError in the parser


def test_n7_five_thousand_bad_frames_give_one_warning(caplog):
    c, _ = _client()
    with caplog.at_level(logging.DEBUG, logger="custom_components.jma.quake_ws"):
        for _ in range(5000):
            c._safe_handle(_BAD_FRAME, NOW, backfill=False)
    assert len(_warnings(caplog)) == 1
    assert sum(r.levelno == logging.DEBUG for r in caplog.records) == 4999


def test_n7_the_next_warning_after_the_interval_counts_what_was_held_back(caplog):
    c, _ = _client()
    clock = {"t": 1000.0}
    c._monotonic = lambda: clock["t"]
    for _ in range(5000):
        c._safe_handle(_BAD_FRAME, NOW, backfill=False)
    clock["t"] += LOG_INTERVAL_S - 1
    c._safe_handle(_BAD_FRAME, NOW, backfill=False)
    assert len(_warnings(caplog)) == 1
    clock["t"] += 2
    c._safe_handle(_BAD_FRAME, NOW, backfill=False)
    warned = _warnings(caplog)
    assert len(warned) == 2
    assert "and 5,000 more" in warned[1].getMessage() and "more" not in warned[0].getMessage()
    clock["t"] += LOG_INTERVAL_S
    c._safe_handle(_BAD_FRAME, NOW, backfill=False)
    assert len(_warnings(caplog)) == 3 and "more" not in _warnings(caplog)[2].getMessage()


def test_n7_each_kind_of_bad_frame_has_its_own_warning(caplog):
    c, _ = _client()
    odd = {"code": 556, "issue": {"eventId": "e", "time": "2026/09/17 00:11:50"}, "areas": 5}  # TypeError
    for _ in range(50):
        c._safe_handle(_BAD_FRAME, NOW, backfill=False)
        c._safe_handle(odd, NOW, backfill=False)
    messages = [r.getMessage() for r in _warnings(caplog)]
    assert len(messages) == 2 and any("AttributeError" in m for m in messages)
    assert any("TypeError" in m for m in messages)


def test_n7_a_future_551_flood_is_one_warning(caplog):
    c, _ = _client()
    for i in range(1000):
        c.handle(_551(f"f{i}", "2099/01/01 00:00:00"), NOW)
    assert len(_warnings(caplog)) == 1


# ---------------------------------------------------------------- N8: the EEW memory is bounded

def _eew(event_id: str, now: datetime, scale_to: int = 50, serial: str = "1") -> dict:
    stamp = (now - timedelta(seconds=5)).astimezone(JST).strftime("%Y/%m/%d %H:%M:%S")
    return {"code": 556, "issue": {"eventId": event_id, "time": stamp, "serial": serial},
            "areas": [{"pref": "東京", "name": "東京都23区", "scaleFrom": 40, "scaleTo": scale_to}]}


def test_n8_a_hundred_thousand_event_ids_leave_a_bounded_memory():
    tracker = EewTracker("東京都")
    now = datetime(2026, 10, 3, 12, 0, tzinfo=JST)
    fired = sum(len(tracker.feed(_eew(f"E{i}", now), now)) for i in range(100_000))
    assert fired == 100_000
    assert len(tracker._alerted) == ALERTED_MAX == 256


def test_n8_the_oldest_is_the_one_dropped():
    tracker = EewTracker("東京都")
    now = datetime(2026, 10, 3, 12, 0, tzinfo=JST)
    for i in range(ALERTED_MAX + 10):
        tracker.feed(_eew(f"E{i}", now), now)
    assert "E0" not in tracker._alerted and "E9" not in tracker._alerted
    assert "E10" in tracker._alerted and f"E{ALERTED_MAX + 9}" in tracker._alerted


def test_n8_entries_older_than_six_hours_are_pruned_on_the_next_feed():
    tracker = EewTracker("東京都")
    t0 = datetime(2026, 10, 3, 12, 0, tzinfo=JST)
    tracker.feed(_eew("old", t0), t0)
    tracker.feed(_eew("mid", t0 + timedelta(hours=5)), t0 + timedelta(hours=5))
    assert set(tracker._alerted) == {"old", "mid"}
    later = t0 + timedelta(hours=6, seconds=1)
    tracker.feed(_eew("new", later), later)
    assert set(tracker._alerted) == {"mid", "new"}


def test_n8_a_repeated_event_inside_the_window_is_not_alerted_again():
    tracker = EewTracker("東京都")
    t0 = datetime(2026, 10, 3, 12, 0, tzinfo=JST)
    assert len(tracker.feed(_eew("same", t0), t0)) == 1
    for i in range(200):  # a busy minute: other events come in between
        tracker.feed(_eew(f"other{i}", t0), t0)
    soon = t0 + timedelta(seconds=30)
    assert tracker.feed(_eew("same", soon), soon) == []
    higher = tracker.feed(_eew("same", soon, scale_to=60, serial="2"), soon)  # a rising forecast still goes out
    assert [e["type"] for e in higher] == ["eew"] and higher[0]["update"] is True
    assert tracker.feed(_eew("same", soon, scale_to=60, serial="3"), soon) == []


def test_n8_a_cancel_still_drops_the_event_and_is_reported_once():
    tracker = EewTracker("東京都")
    t0 = datetime(2026, 10, 3, 12, 0, tzinfo=JST)
    tracker.feed(_eew("gone", t0), t0)
    cancel = {**_eew("gone", t0), "cancelled": True}
    assert [e["type"] for e in tracker.feed(cancel, t0)] == ["eew_cancelled"]
    assert "gone" not in tracker._alerted and tracker.feed(cancel, t0) == []
