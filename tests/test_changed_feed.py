"""A changed feed never crashes: each coordinator is given ONE document in which a field its parser
reads is missing or renamed. At start (nothing to keep) the coordinator fails with UpdateFailed; once it
has good data it keeps it. Either way no traceback is logged at error level for the feed.

The one exception to "fails" is stated per feed: the heat index (a CSV that lacks the columns reads as
"no data", a quiet successful poll), the typhoon list and each map-layer list (kept apart).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.jma.const import (
    URL_AMEDAS_POINT,
    URL_FORECAST,
    URL_HIMAWARI_TIMES,
    URL_JAPAN_FORECAST,
    URL_NOWC_TIMES,
    URL_OPEN_METEO,
    URL_RASRF_TIMES,
    URL_RISK_TIMES,
    URL_TYPHOON_TARGET,
    URL_WARNING_TIMELINE,
    URL_WARNINGS,
    URL_WBGT,
)

from .conftest import mock_jma, setup
from .helpers import clone, load_json, load_text


def _rename(obj: Any, old: str, new: str) -> Any:
    """The same document with every key `old` renamed to `new` (at any depth)."""
    if isinstance(obj, dict):
        return {(new if k == old else k): _rename(v, old, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rename(v, old, new) for v in obj]
    return obj


def _drop(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return {k: _drop(v, key) for k, v in obj.items() if k != key}
    if isinstance(obj, list):
        return [_drop(v, key) for v in obj]
    return obj


POINT_URL = URL_AMEDAS_POINT.format(station="44132", ymd="20260926", hh="12")
POINT_URL_2 = URL_AMEDAS_POINT.format(station="62016", ymd="20260926", hh="12")

# (runtime attribute, [(url, kind, broken document)]) -- kind: json | text | bytes
FEEDS: dict[str, tuple[str, list[tuple[str, str, Any]]]] = {
    "forecast": ("forecast", [(URL_FORECAST.format(office="130000"), "json",
                               _rename(load_json("forecast_130000.json"), "timeSeries", "timeSeriesRenamed"))]),
    "hourly": ("hourly", [(URL_OPEN_METEO, "json",
                           _rename(load_json("openmeteo_msm.json"), "hourly", "hourlyRenamed"))]),
    "warnings": ("warnings", [(URL_WARNINGS.format(office="130000"), "json",
                               _rename(load_json("warning_r8_130000.json"), "class20Items", "class20ItemsRenamed"))]),
    "nowcast": ("nowcast", [(URL_NOWC_TIMES.format(n="N1"), "json",
                             _rename(load_json("targetTimes_N1.json"), "basetime", "baseRenamed"))]),
    "warning outlook": ("timeline", [(URL_WARNING_TIMELINE.format(office="130000"), "json",
                                      _rename(load_json("warning_timeline_130000.json"), "timeDefines", "timeDefinesRenamed"))]),
    "japan": ("japan", [(URL_JAPAN_FORECAST, "json",
                         _rename(load_json("forecast_010000.json"), "timeSeries", "timeSeriesRenamed"))]),
}


async def _start(hass, aioclient_mock, entry, broken: list[tuple[str, str, Any]]) -> None:
    for url, kind, doc in broken:  # registered first, so these answer instead of the good sample
        if kind == "json":
            aioclient_mock.get(url, json=doc)
        elif kind == "text":
            aioclient_mock.get(url, text=doc)
        else:
            aioclient_mock.get(url, content=doc)
    mock_jma(aioclient_mock)
    await setup(hass, entry)


def _no_error_traceback(caplog: pytest.LogCaptureFixture) -> None:
    bad = [r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info]
    assert bad == [], [(r.name, r.getMessage()) for r in bad]


@pytest.mark.parametrize("name", list(FEEDS))
async def test_a_changed_feed_fails_cleanly_at_start(tokyo: HomeAssistant, aioclient_mock, entry, caplog, name) -> None:
    attr, broken = FEEDS[name]
    await _start(tokyo, aioclient_mock, entry, broken)
    coordinator = getattr(entry.runtime_data, attr)
    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert coordinator.data is None
    _no_error_traceback(caplog)


@pytest.mark.parametrize("name", list(FEEDS))
async def test_a_changed_feed_keeps_the_last_good_data(tokyo: HomeAssistant, aioclient_mock, entry, caplog, name) -> None:
    attr, broken = FEEDS[name]
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    coordinator = getattr(entry.runtime_data, attr)
    good = coordinator.data
    assert good is not None and coordinator.last_update_success is True
    aioclient_mock.clear_requests()
    await _start_again(aioclient_mock, broken)
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True and coordinator.data is good
    assert coordinator.last_error  # the problem is recorded for feed health
    _no_error_traceback(caplog)


async def _start_again(aioclient_mock, broken) -> None:
    for url, kind, doc in broken:
        aioclient_mock.get(url, json=doc) if kind == "json" else aioclient_mock.get(url, text=doc)
    mock_jma(aioclient_mock)


# ---------- the feeds that do not fail ----------

AMEDAS_BROKEN = [
    # every element of a row renamed (the rows keep their times), and a row that is not an object
    (POINT_URL, "json", {t: {f"{k}Renamed": v for k, v in row.items()}
                         for t, row in load_json("point_62016_12.json").items()}),
    (POINT_URL_2, "json", {"20260926125000": "not a row"}),
]


async def test_changed_amedas_files_give_an_empty_observation_and_a_recorded_reason(
        tokyo: HomeAssistant, aioclient_mock, entry_two_stations, caplog) -> None:
    """AMeDAS today: a station file that parses but holds no usable value is a poll that succeeds with an
    empty observation, and the reason is recorded per station (feed health reads it). It does not raise."""
    entry = entry_two_stations  # two different stations, so each has its own recorded reason
    await _start(tokyo, aioclient_mock, entry, AMEDAS_BROKEN)
    coordinator = entry.runtime_data.amedas
    assert coordinator.last_update_success is True
    assert coordinator.data.observed_at is None and coordinator.data.temperature is None
    assert coordinator.station_error["44132"] and coordinator.station_error["62016"]
    assert coordinator.station_ok == {}
    _no_error_traceback(caplog)


async def test_an_amedas_file_that_is_not_an_object_fails_cleanly(tokyo: HomeAssistant, aioclient_mock, entry, caplog) -> None:
    broken = [(POINT_URL, "json", ["not", "an", "object"]), (POINT_URL_2, "json", ["not", "an", "object"])]
    await _start(tokyo, aioclient_mock, entry, broken)
    coordinator = entry.runtime_data.amedas
    assert coordinator.last_update_success is False and isinstance(coordinator.last_exception, UpdateFailed)
    _no_error_traceback(caplog)


async def test_a_changed_amedas_file_keeps_the_last_good_data_when_neither_answers(
        tokyo: HomeAssistant, aioclient_mock, entry, caplog) -> None:
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    coordinator = entry.runtime_data.amedas
    good = coordinator.data
    assert good.observed_at is not None
    aioclient_mock.clear_requests()
    await _start_again(aioclient_mock, [(POINT_URL, "json", ["x"]), (POINT_URL_2, "json", ["x"])])
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True and coordinator.data is good
    _no_error_traceback(caplog)

async def test_a_heat_index_file_that_lost_its_columns_is_no_data(tokyo: HomeAssistant, aioclient_mock, entry, caplog) -> None:
    """WBGT: a CSV without the expected header reads as "no data" (a quiet, successful poll)."""
    text = load_text("wbgt_yohou_tokyo.csv").replace("Date", "Day", 1)
    lines = text.splitlines()
    lines[0] = ",".join(f"renamed{i}" for i in range(len(lines[0].split(","))))
    await _start(tokyo, aioclient_mock, entry, [(URL_WBGT.format(pref="tokyo"), "text", "\n".join(lines))])
    coordinator = entry.runtime_data.wbgt
    assert coordinator.last_update_success is True and coordinator.data.forecast is None
    _no_error_traceback(caplog)


async def test_a_changed_typhoon_list_fails_cleanly(tokyo: HomeAssistant, aioclient_mock, entry, caplog) -> None:
    broken = _rename(load_json("typhoon_targetTc.json"), "tropicalCyclone", "cycloneRenamed")
    await _start(tokyo, aioclient_mock, entry, [(URL_TYPHOON_TARGET, "json", broken)])
    coordinator = entry.runtime_data.typhoon
    assert coordinator.last_update_success is False and isinstance(coordinator.last_exception, UpdateFailed)
    _no_error_traceback(caplog)


async def test_a_changed_typhoon_forecast_keeps_the_storm_without_details(tokyo: HomeAssistant, aioclient_mock, entry, caplog) -> None:
    from custom_components.jma.const import URL_TYPHOON_FORECAST
    broken = _drop(load_json("typhoon_TC2632_forecast.json"), "advancedHours")
    await _start(tokyo, aioclient_mock, entry, [(URL_TYPHOON_FORECAST.format(tc="TC2632"), "json", broken)])
    coordinator = entry.runtime_data.typhoon
    assert coordinator.last_update_success is True and coordinator.data.count == 1
    assert coordinator.data.storms[0]["incomplete"] is True
    _no_error_traceback(caplog)


async def test_a_typhoon_field_that_changed_its_type_does_not_escape(tokyo: HomeAssistant, aioclient_mock, entry, caplog) -> None:
    """parse_storm read `.get` on a field that is now a string: the AttributeError must not reach Home
    Assistant (a traceback in the log); it is handled where the other parser errors are."""
    from custom_components.jma.const import URL_TYPHOON_SPECS
    broken = clone(load_json("typhoon_TC2632_specs.json"))
    broken[1]["maximumWind"] = "x"
    await _start(tokyo, aioclient_mock, entry, [(URL_TYPHOON_SPECS.format(tc="TC2632"), "json", broken)])
    coordinator = entry.runtime_data.typhoon
    assert coordinator.last_update_success is True and coordinator.data.count == 1
    assert coordinator.data.storms[0]["incomplete"] is True
    _no_error_traceback(caplog)


LAYER_LISTS = {
    "n3": (URL_NOWC_TIMES.format(n="N3"), "basetime"),
    "risk": (URL_RISK_TIMES, "basetime"),
    "rasrf": (URL_RASRF_TIMES, "basetime"),
    "himawari": (URL_HIMAWARI_TIMES, "basetime"),
}
LAYER_FILES = {
    "n3": "targetTimes_N3_b.json", "risk": "risk_targetTimes.json",
    "rasrf": "rasrf_targetTimes.json", "himawari": "himawari_targetTimes_jp.json",
}


def _broken_layer(key: str) -> tuple[str, str, Any]:
    url, field = LAYER_LISTS[key]
    return url, "json", _rename(load_json(LAYER_FILES[key]), field, "baseRenamed")


@pytest.mark.parametrize("key", list(LAYER_LISTS))
async def test_one_changed_map_layer_list_leaves_the_others(tokyo: HomeAssistant, aioclient_mock, entry, caplog, key) -> None:
    await _start(tokyo, aioclient_mock, entry, [_broken_layer(key)])
    coordinator = entry.runtime_data.layers
    assert coordinator.last_update_success is True
    assert coordinator.sources[key].last_error  # recorded, and shown by the diagnostics
    assert [k for k, s in coordinator.sources.items() if k != key and s.last_error] == []
    _no_error_traceback(caplog)


async def test_all_map_layer_lists_changed_fails_cleanly(tokyo: HomeAssistant, aioclient_mock, entry, caplog) -> None:
    await _start(tokyo, aioclient_mock, entry, [_broken_layer(k) for k in LAYER_LISTS])
    coordinator = entry.runtime_data.layers
    assert coordinator.last_update_success is False and isinstance(coordinator.last_exception, UpdateFailed)
    _no_error_traceback(caplog)


def test_every_coordinator_of_the_runtime_has_a_case_here() -> None:
    import dataclasses

    from custom_components.jma import coordinator as coordinator_module
    from custom_components.jma.coordinator import JmaRuntime, _JmaCoordinator
    covered = {attr for attr, _ in FEEDS.values()} | {"amedas", "wbgt", "typhoon", "layers"}
    # The coordinators of the runtime are read from its field types, not listed by hand: a coordinator added
    # to JmaRuntime without a case here fails this test. (The annotations are strings; the one that names a
    # class this module does not import, the quake client, is not a coordinator.)
    runtime_coordinators = {f.name for f in dataclasses.fields(JmaRuntime)
                            if isinstance(getattr(coordinator_module, str(f.type), None), type)
                            and issubclass(getattr(coordinator_module, str(f.type)), _JmaCoordinator)}
    assert len(runtime_coordinators) == 10
    assert covered == runtime_coordinators


@pytest.mark.parametrize("attr", ["forecast", "hourly", "amedas", "warnings", "nowcast", "timeline", "typhoon",
                                  "wbgt", "layers", "japan"])
async def test_an_attribute_error_out_of_a_fetch_is_handled_where_the_others_are(
        tokyo: HomeAssistant, aioclient_mock, entry, caplog, attr) -> None:
    """The coordinator wrapper catches AttributeError (a field that changed its type reaches `.get` on a
    string) like KeyError / TypeError: UpdateFailed at start, the last good data afterwards, no traceback."""
    from unittest.mock import AsyncMock, patch

    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    coordinator = getattr(entry.runtime_data, attr)
    good = coordinator.data
    with patch.object(coordinator, "_fetch", AsyncMock(side_effect=AttributeError("'str' object has no attribute 'get'"))):
        await coordinator.async_refresh()
        if good is not None:  # last good data kept
            assert coordinator.last_update_success is True and coordinator.data is good
        assert coordinator.last_error == "'str' object has no attribute 'get'"
        coordinator.data = None  # nothing to keep: the same error is an UpdateFailed
        await coordinator.async_refresh()
        assert coordinator.last_update_success is False and isinstance(coordinator.last_exception, UpdateFailed)
    _no_error_traceback(caplog)
