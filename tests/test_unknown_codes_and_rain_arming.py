"""Regression tests: a warning code the integration does not know is not a level-0 "active warning",
and RainArming must not count an unreadable series as dry."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.jma.parse_nowcast import RainArming, rain_series
from custom_components.jma.parse_warnings import diff_warnings, parse_area_warnings

from .conftest import AREA, mock_jma, setup
from .helpers import clone, load_json

JST = timezone(timedelta(hours=9))
BASE = "20260926040000"


def _r8(kinds_by_type: dict[str, list[dict]]) -> list:
    records = clone(load_json("warning_r8_130000.json"))
    for record in records:
        if record["dataTypeCode"] in kinds_by_type:
            for item in record["warning"]["class20Items"]:
                if item["areaCode"] == AREA:
                    item["kinds"] = kinds_by_type[record["dataTypeCode"]]
    return records


# ---------- an unknown code is not a level-0 "active warning" ----------

def test_unknown_code_is_not_an_active_warning():
    aw = parse_area_warnings(_r8({"VPWW55": [{"code": "99", "status": "発表"}]}), AREA)
    assert aw.warnings == []
    assert aw.level == 0
    assert aw.unknown_codes == [{"code": "99", "status": "発表", "data_type": "VPWW55"}]


def test_unknown_code_beside_a_known_one_leaves_the_known_one_alone():
    aw = parse_area_warnings(
        _r8({"VPWW55": [{"code": "99", "status": "発表"}, {"code": "43", "status": "発表"}]}), AREA)
    assert [(w.code, w.level) for w in aw.warnings] == [("43", 4)]
    assert aw.level == 4
    assert [u["code"] for u in aw.unknown_codes] == ["99"]


def test_unknown_code_that_is_cleared_is_not_reported_as_unknown():
    aw = parse_area_warnings(_r8({"VPWW55": [{"code": "99", "status": "解除"}]}), AREA)
    assert aw.warnings == [] and aw.unknown_codes == []


def test_unknown_code_makes_no_change_to_announce():
    aw = parse_area_warnings(_r8({"VPWW55": [{"code": "99", "status": "発表"}]}), AREA)
    assert diff_warnings([], aw.warnings) == []


def test_unknown_code_is_still_logged_once(caplog):
    from custom_components.jma import parse_warnings

    parse_warnings._logged_unknown.discard("code:98")
    with caplog.at_level(logging.WARNING, logger=parse_warnings.__name__):
        for _ in range(3):
            parse_area_warnings(_r8({"VPWW55": [{"code": "98", "status": "発表"}]}), AREA)
    lines = [r.getMessage() for r in caplog.records if "unknown code '98'" in r.getMessage()]
    assert len(lines) == 1
    assert "level 0" not in lines[0]


async def test_unknown_code_reaches_the_sensor_as_an_attribute_not_a_warning_or_an_event(
    tokyo: HomeAssistant, aioclient_mock, entry, freezer
) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    event_before = hass.states.get("event.jma_chiyoda_alerts").state

    aioclient_mock.clear_requests()
    mock_jma(aioclient_mock, warnings=_r8({"VPWW55": [{"code": "99", "status": "発表"}]}))
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    level = hass.states.get("sensor.jma_chiyoda_warning_level")
    assert level.state == "0"
    assert level.attributes["warnings"] == []
    assert level.attributes["unknown_codes"] == [{"code": "99", "status": "発表", "data_type": "VPWW55"}]
    assert hass.states.get("event.jma_chiyoda_alerts").state == event_before  # no warning_issued


# ---------- RainArming must not count an unreadable series as dry ----------

def _series(bands: list[int | None]):
    frames = [(BASE, bands[0])] + [
        (f"2026092604{m:02d}00" if m < 60 else "20260926050000", b)
        for m, b in zip(range(5, 65, 5), bands[1:], strict=False)
    ]
    return rain_series(BASE, frames)


def test_all_none_bands_do_not_re_arm():
    arm = RainArming()
    t = datetime(2026, 9, 26, 13, 0, tzinfo=JST)
    coming = _series([0, 0, 0, 3] + [3] * 9)
    assert arm.update(coming, t) is True  # episode starts, disarmed
    unreadable = _series([None] * 13)  # every tile failed for 2 hours
    for minutes in range(5, 125, 5):
        assert arm.update(unreadable, t + timedelta(minutes=minutes)) is False
    assert arm.armed is False
    # tiles come back with a gap in the same rain: no second "arriving" push
    assert arm.update(coming, t + timedelta(minutes=125)) is False


def test_an_unreadable_poll_breaks_the_dry_stretch():
    arm = RainArming()
    t = datetime(2026, 9, 26, 13, 0, tzinfo=JST)
    coming = _series([0, 0, 0, 3] + [3] * 9)
    dry = _series([0] * 13)
    unreadable = _series([None] * 13)
    assert arm.update(coming, t) is True
    assert arm.update(dry, t + timedelta(minutes=10)) is False
    assert arm.update(unreadable, t + timedelta(minutes=40)) is False  # what happened in here is unknown
    assert arm.update(dry, t + timedelta(minutes=45)) is False
    assert arm.update(dry, t + timedelta(minutes=80)) is False  # 35 known-dry minutes since the gap
    assert arm.armed is False
    assert arm.update(dry, t + timedelta(minutes=106)) is False  # 61 known-dry minutes: re-armed now
    assert arm.armed is True
    assert arm.update(coming, t + timedelta(minutes=110)) is True


def test_partly_readable_series_still_counts_as_dry_when_nothing_shows_rain():
    arm = RainArming()
    t = datetime(2026, 9, 26, 13, 0, tzinfo=JST)
    coming = _series([0, 0, 0, 3] + [3] * 9)
    assert arm.update(coming, t) is True
    some = _series([0, 0, None, None] + [0] * 9)  # two forecast tiles missing, the rest dry
    assert arm.update(some, t + timedelta(minutes=10)) is False
    assert arm.update(some, t + timedelta(minutes=71)) is False
    assert arm.armed is True
