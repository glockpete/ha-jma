"""One lookup of the AMeDAS station names (parse_amedas.station_names_from_table), used by the setup (weather
attributes, first heat alert poll) and by the heat alert coordinator (later polls)."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.jma import coordinator as coordinator_module
from custom_components.jma import parse_amedas
from custom_components.jma import station_names_from_table as init_helper
from custom_components.jma.const import URL_AMEDAS_TABLE

from .conftest import mock_jma, setup
from .helpers import load_json

TABLE = load_json("amedastable.json")  # 44132 = 東京, 62016 = 能勢


def test_the_pure_function_gives_the_name_of_each_id_in_order() -> None:
    assert parse_amedas.station_names_from_table(TABLE, ("44132", "62016")) == ("東京", "能勢")
    assert parse_amedas.station_names_from_table(TABLE, ("62016", "44132")) == ("能勢", "東京")
    assert parse_amedas.station_names_from_table(TABLE, ("44132", "44132")) == ("東京", "東京")  # one name per id


@pytest.mark.parametrize("row", [None, {}, {"kjName": None}, {"kjName": ""}, {"kjName": 5}, {"enName": "Tokyo"}])
def test_a_missing_or_odd_name_is_none(row) -> None:
    assert parse_amedas.station_names_from_table({"44132": row}, ("44132", "99999")) == (None, None)


@pytest.mark.parametrize("table", [None, ["not", "a", "table"], "text"])
def test_a_table_of_the_wrong_shape_raises_attribute_error_for_the_callers_to_log(table) -> None:
    with pytest.raises(AttributeError):
        parse_amedas.station_names_from_table(table, ("44132",))


def test_setup_and_the_heat_alert_coordinator_use_the_same_function() -> None:
    assert init_helper is parse_amedas.station_names_from_table
    assert coordinator_module.station_names_from_table is parse_amedas.station_names_from_table


async def test_setup_stores_the_names_for_the_weather_attributes(
        tokyo: HomeAssistant, aioclient_mock, entry_two_stations) -> None:
    mock_jma(aioclient_mock)
    await setup(tokyo, entry_two_stations)
    runtime = entry_two_stations.runtime_data
    assert runtime.extra["station_names"] == {"44132": "東京", "62016": "能勢"}
    assert runtime.heat_alert.station_names == ("東京", "能勢")


async def test_the_coordinator_loads_one_name_per_station_id_even_when_both_are_the_same(
        tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    co = entry.runtime_data.heat_alert
    co.station_names = ()
    await co._load_station_names()
    assert co.station_names == ("東京", "東京")
    co.station_names = ()
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL_AMEDAS_TABLE, json={"44132": {"kjName": ""}})
    await co._load_station_names()
    assert co.station_names == ()
