"""Forecast parser against the Tokyo forecast of the fixture."""

from __future__ import annotations

from datetime import date

import pytest

from custom_components.jma.errors import JmaParseError
from custom_components.jma.parse_forecast import (
    HA_CONDITIONS,
    condition_for,
    icon_for,
    name_for,
    parse_forecast,
    parse_overview,
)
from custom_components.jma.telops import TELOPS

from .helpers import clone, load_json

CLASS10 = "130010"
STATION = "44132"


def test_every_jma_code_maps_to_a_real_condition():
    for code in TELOPS:
        for night in (False, True):
            cond = condition_for(code, night)
            assert cond in HA_CONDITIONS, code
            assert cond != "exceptional", code


@pytest.mark.parametrize(
    ("code", "night", "expected"),
    [
        ("100", False, "sunny"),
        ("100", True, "clear-night"),
        ("101", False, "partlycloudy"),
        ("200", False, "cloudy"),
        ("201", False, "partlycloudy"),  # 曇時々晴, English label says "MOSTLY CLOUDY"
        ("209", False, "fog"),  # 霧
        ("328", False, "pouring"),  # 雨一時強く降る
        ("300", False, "rainy"),
        ("306", False, "pouring"),  # 大雨 HEAVYRAIN
        ("308", False, "pouring"),  # RAINSTORM
        ("350", False, "lightning-rainy"),
        ("123", False, "sunny"),  # 晴山沿い雷雨: clear in the plains
        ("304", False, "snowy-rainy"),  # RAINORSNOW
        ("400", False, "snowy"),
        ("407", False, "snowy"),  # 暴風雪 HEAVYSNOWSTORM stays snowy, not pouring
        ("999", False, "exceptional"),
        (None, False, "exceptional"),
    ],
)
def test_condition_rules(code, night, expected):
    assert condition_for(code, night) == expected


def test_icons_and_names():
    assert icon_for("100") == "100.svg"
    assert icon_for("100", True) == "500.svg"
    assert icon_for("nope") is None
    assert name_for("300") == ("雨", "Rain")


def test_fixture_days_temps_and_pops():
    fc = parse_forecast(load_json("forecast_130000.json"), CLASS10, STATION)
    assert fc.issue_hour == "11"
    assert [d.date for d in fc.days][:3] == [date(2026, 9, 26), date(2026, 9, 27), date(2026, 9, 28)]
    assert len(fc.days) == 8  # today + 7 weekly days (weekly starts tomorrow)
    today, tomorrow, day3 = fc.days[:3]
    # JMA's page: 11:00 issue -> today max = temps[0], tomorrow min/max = temps[2]/temps[3]
    assert (today.temp_max, today.temp_min) == (20, None)
    assert (tomorrow.temp_min, tomorrow.temp_max) == (18, 26)
    # pops ['70','50','20','10','0','10']: today = first 2, tomorrow = last 4
    assert today.pop == 70
    assert tomorrow.pop == 20
    assert today.code == "300" and tomorrow.code == "200"
    assert today.text_ja == "雨"
    # day 3 comes from short-term code + weekly pop/temps/reliability
    assert day3.code == "200"
    # the 28th is weekly index 1: pops '40', reliabilities '', tempsMin '20', tempsMax '26'
    assert (day3.pop, day3.temp_min, day3.temp_max, day3.reliability) == (40, 20, 26, None)
    assert fc.days[3].reliability == "B"  # the 29th, weekly index 2
    assert fc.days[-1].date == date(2026, 10, 3)
    assert [p for _, p in fc.pops_6h] == [70, 50, 20, 10, 0, 10]


def test_17_hour_issue_has_no_today_temps():
    data = clone(load_json("forecast_130000.json"))
    data[0]["reportDatetime"] = "2026-09-26T17:00:00+09:00"
    for area in data[0]["timeSeries"][2]["areas"]:
        area["temps"] = ["18", "26"]
    data[0]["timeSeries"][2]["timeDefines"] = [
        "2026-09-27T00:00:00+09:00", "2026-09-27T09:00:00+09:00"]
    fc = parse_forecast(data, CLASS10, STATION)
    assert fc.days[0].temp_max is None and fc.days[0].temp_min is None
    assert (fc.days[1].temp_min, fc.days[1].temp_max) == (18, 26)


def test_html_body_is_a_parse_error():
    with pytest.raises(JmaParseError):
        parse_forecast("<html>maintenance</html>", CLASS10, STATION)
    with pytest.raises(JmaParseError):
        parse_forecast([], CLASS10, STATION)


def test_unknown_class10_falls_back_to_first_area():
    fc = parse_forecast(load_json("forecast_130000.json"), "999999", "00000")
    assert fc.days[0].code == "300"


def test_overview():
    text, when = parse_overview(load_json("overview_130000.json"))
    assert text.startswith("前線が華中から")
    assert when.isoformat() == "2026-09-26T10:37:00+09:00"
    with pytest.raises(JmaParseError):
        parse_overview({})
