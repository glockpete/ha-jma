"""A live early warning is not dropped for a wrong host clock or a missing issue time.

The age window (120 s) weeds out replays after a reconnect; a slow host clock makes a live message
look future-dated, and a live message without issue.time has an unknown age. Neither may be dropped
silently: a message further ahead of the clock than the tolerance is dropped with one WARNING per event id.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.jma import const
from custom_components.jma.parse_quake import EewTracker, QuakePlace
from custom_components.jma.quake_ws import QuakeClient

from .conftest import QUAKE_PLACE

JST = timezone(timedelta(hours=9))
ISSUED = datetime(2026, 9, 26, 12, 17, 20, tzinfo=JST)  # issue.time of the frames below
LOGGER = "custom_components.jma.parse_quake"


def _eew(serial: int = 1, scale_from: int = 45, scale_to: int = 50, **kw) -> dict:
    msg = {
        "code": 556, "test": False, "cancelled": False,
        "issue": {"time": "2026/09/26 12:17:20", "eventId": "20260926121700", "serial": str(serial)},
        "earthquake": {"originTime": "2026/09/26 12:17:00",
                       "hypocenter": {"name": "東京湾", "magnitude": 6.1}},
        "areas": [
            {"pref": "東京", "name": "東京都２３区", "scaleFrom": scale_from, "scaleTo": scale_to,
             "arrivalTime": "2026/09/26 12:17:40"},
        ],
    }
    msg.update(kw)
    return msg


def _no_time(serial: int = 1, scale_from: int = 45, scale_to: int = 50, **kw) -> dict:
    msg = _eew(serial, scale_from, scale_to, **kw)
    del msg["issue"]["time"]
    return msg


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


def test_constant_is_generous_like_the_tsunami_tracker() -> None:
    assert const.EEW_FUTURE_TOLERANCE_S == 600 and const.EEW_BACKFILL_MAX_AGE_S == 120


# ---------- a slow host clock: the message looks future-dated ----------

@pytest.mark.parametrize("slow_s", [60, 9 * 60])
def test_live_frame_fires_when_the_host_clock_is_slow(slow_s: int) -> None:
    events = EewTracker(QUAKE_PLACE.pref).feed(_eew(), ISSUED - timedelta(seconds=slow_s))
    assert [e["type"] for e in events] == ["eew"]


def test_live_frame_is_dropped_with_one_warning_when_the_clock_is_eleven_minutes_slow(caplog) -> None:
    t = EewTracker(QUAKE_PLACE.pref)
    now = ISSUED - timedelta(minutes=11)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert t.feed(_eew(1), now) == []
        assert t.feed(_eew(2), now) == []
        assert t.feed(_eew(3, 50, 55), now) == []
    warned = _warnings(caplog)
    assert len(warned) == 1
    assert "20260926121700" in warned[0].getMessage() and "clock" in warned[0].getMessage()


def test_a_second_event_id_is_warned_about_again(caplog) -> None:
    t = EewTracker(QUAKE_PLACE.pref)
    other = _eew()
    other["issue"]["eventId"] = "20260926121800"
    now = ISSUED - timedelta(minutes=11)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        t.feed(_eew(), now)
        t.feed(other, now)
    assert len(_warnings(caplog)) == 2


def test_a_future_dated_frame_is_not_remembered_so_it_fires_once_the_clock_is_right() -> None:
    t = EewTracker(QUAKE_PLACE.pref)
    assert t.feed(_eew(), ISSUED - timedelta(minutes=11)) == []
    assert [e["type"] for e in t.feed(_eew(2), ISSUED + timedelta(seconds=10))] == ["eew"]


def test_the_warned_ids_are_bounded() -> None:
    t = EewTracker(QUAKE_PLACE.pref)
    now = ISSUED - timedelta(minutes=11)
    for i in range(500):
        msg = _eew()
        msg["issue"]["eventId"] = f"e{i}"
        t.feed(msg, now)
    assert len(t._warned_future) <= 200


# ---------- the 120 s upper limit holds for live and backfill ----------

@pytest.mark.parametrize("backfill", [False, True])
def test_age_window_is_120_seconds(backfill: bool, caplog) -> None:
    assert [e["type"] for e in EewTracker(QUAKE_PLACE.pref).feed(
        _eew(), ISSUED + timedelta(seconds=119), backfill=backfill)] == ["eew"]
    assert [e["type"] for e in EewTracker(QUAKE_PLACE.pref).feed(
        _eew(), ISSUED + timedelta(seconds=120), backfill=backfill)] == ["eew"]
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert EewTracker(QUAKE_PLACE.pref).feed(_eew(), ISSUED + timedelta(seconds=121), backfill=backfill) == []
    assert not _warnings(caplog)  # an over-age drop is a DEBUG line, not a warning
    assert any(r.levelno == logging.DEBUG for r in caplog.records if r.name == LOGGER)


def test_a_fast_clock_beyond_the_window_is_dropped_live(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert EewTracker(QUAKE_PLACE.pref).feed(_eew(), ISSUED + timedelta(minutes=5)) == []
    assert not _warnings(caplog)


# ---------- a missing or unreadable issue.time ----------

def test_live_frame_without_issue_time_fires_and_a_repeat_does_not() -> None:
    t = EewTracker(QUAKE_PLACE.pref)
    first = t.feed(_no_time(1), ISSUED)
    assert [e["type"] for e in first] == ["eew"]
    assert t.feed(_no_time(1), ISSUED) == []
    assert t.feed(_no_time(2), ISSUED) == []  # same scale: no new alarm
    rising = t.feed(_no_time(3, 50, 55), ISSUED)
    assert rising[0]["predicted_scale"] == 55 and rising[0]["update"] is True


def test_live_frame_with_an_unreadable_issue_time_fires() -> None:
    msg = _eew()
    msg["issue"]["time"] = "not a time"
    assert [e["type"] for e in EewTracker(QUAKE_PLACE.pref).feed(msg, ISSUED)] == ["eew"]


def test_backfill_without_a_usable_issue_time_is_dropped() -> None:
    unreadable = _eew()
    unreadable["issue"]["time"] = "not a time"
    for msg in (_no_time(), unreadable):
        assert EewTracker(QUAKE_PLACE.pref).feed(msg, ISSUED, backfill=True) == []


# ---------- cancellation follows the same rules ----------

def test_cancellation_with_a_skewed_clock_emits_eew_cancelled() -> None:
    t = EewTracker(QUAKE_PLACE.pref)
    now = ISSUED - timedelta(minutes=9)
    assert [e["type"] for e in t.feed(_eew(1), now)] == ["eew"]
    assert [e["type"] for e in t.feed(_eew(2, cancelled=True), now)] == ["eew_cancelled"]
    assert t.feed(_eew(3, 50, 55), now) == []  # cancelled ids stay quiet


def test_live_cancellation_without_issue_time_emits_eew_cancelled() -> None:
    t = EewTracker(QUAKE_PLACE.pref)
    assert [e["type"] for e in t.feed(_no_time(1), ISSUED)] == ["eew"]
    assert [e["type"] for e in t.feed(_no_time(2, cancelled=True), ISSUED)] == ["eew_cancelled"]
    assert t.feed(_no_time(3, 50, 55), ISSUED) == []


def test_a_future_dated_cancellation_beyond_tolerance_is_dropped_with_a_warning(caplog) -> None:
    t = EewTracker(QUAKE_PLACE.pref)
    assert t.feed(_eew(1), ISSUED) != []
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert t.feed(_eew(2, cancelled=True), ISSUED - timedelta(minutes=11)) == []
    assert len(_warnings(caplog)) == 1


def test_a_replay_after_a_cancel_on_a_slow_clock_stays_quiet() -> None:
    """The host clock is 5 min slow: the alert and its cancel arrive live, then a reconnect replays both from
    the history 3 min later. The cancel must still be remembered (it was forgotten after 120 s)."""
    t = EewTracker(QUAKE_PLACE.pref)
    live = ISSUED - timedelta(minutes=5)
    assert [e["type"] for e in t.feed(_eew(1), live)] == ["eew"]
    cancel = _eew(2, cancelled=True)
    assert [e["type"] for e in t.feed(cancel, live + timedelta(seconds=10))] == ["eew_cancelled"]
    replay_at = live + timedelta(minutes=3)
    assert t.feed(_eew(1), replay_at, backfill=True) == []
    assert t.feed(cancel, replay_at, backfill=True) == []


# ---------- through the client: a live frame, then the same frame as backfill ----------

class _Rec:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def fire(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


def _client() -> tuple[QuakeClient, _Rec]:
    rec = _Rec()
    client = QuakeClient(hass=None, api=None, fire=rec.fire, on_quake=lambda: None,  # type: ignore[arg-type]
                         place=QuakePlace(QUAKE_PLACE.pref, None))
    return client, rec


def test_the_client_fires_a_live_frame_on_a_slow_clock_and_drops_the_backfill_without_time() -> None:
    client, rec = _client()
    client.handle(_no_time(), ISSUED - timedelta(minutes=9), backfill=False)
    assert [t for t, _ in rec.events] == ["eew"]
    client, rec = _client()
    client.handle(_no_time(), ISSUED, backfill=True)
    assert rec.events == []
