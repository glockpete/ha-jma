"""The device title ("JMA Chuo (Tokyo)") must tell two municipalities with one English name apart: two entries with the
same title get the same device name and entity ids ending in _2. Checked against the agency's full area table
(tests/fixtures/area.json, 1,805 municipalities)."""
from __future__ import annotations

from collections import defaultdict

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.jma.config_flow import _short_name, device_title
from custom_components.jma.const import DOMAIN

from .conftest import AREA, mock_jma
from .helpers import clone, load_json

CHUO_WARD_TOKYO = "1310200"
CHUO_CITY_YAMANASHI = "1921400"


@pytest.fixture(scope="module")
def area_json() -> dict:
    return load_json("area.json")


def test_two_municipalities_called_chuo_get_different_titles(area_json: dict) -> None:
    assert device_title(area_json, CHUO_WARD_TOKYO) == "JMA Chuo (Tokyo)"
    assert device_title(area_json, CHUO_CITY_YAMANASHI) == "JMA Chuo (Yamanashi)"


def test_the_tests_place_keeps_its_plain_title(area_json: dict) -> None:
    # 1310100 shares "Chiyoda" with the town in Gunma, but it is the only city of the two: its title does not change
    assert device_title(area_json, AREA) == "JMA Chiyoda"
    assert device_title(area_json, "1052300") == "JMA Chiyoda (Gunma)"


def test_the_fixture_has_102_shared_names_and_every_title_is_still_unique(area_json: dict) -> None:
    by_short: dict[str, list[str]] = defaultdict(list)
    for code, row in area_json["class20s"].items():
        by_short[_short_name(row["enName"])].append(code)
    shared = {name: codes for name, codes in by_short.items() if len(codes) > 1}
    assert len(area_json["class20s"]) == 1805 and len(shared) == 102
    titles = {code: device_title(area_json, code) for code in area_json["class20s"]}
    assert len(set(titles.values())) == 1805  # no residual collision in the whole table
    # a name that nobody shares is the title it always was
    for name, codes in by_short.items():
        if len(codes) == 1:
            assert titles[codes[0]] == f"JMA {name}"


def test_a_place_alone_in_its_kind_keeps_the_plain_title_and_the_others_get_their_office(area_json: dict) -> None:
    # Toshima: a city in Tokyo, a village in Tokyo's islands (the city's office), a village in the Amami islands
    assert device_title(area_json, "1311600") == "JMA Toshima"
    assert device_title(area_json, "1336200") == "JMA Toshima Village (Tokyo)"
    assert device_title(area_json, "4630400") == "JMA Toshima (Amami)"


def test_the_whole_name_is_used_where_two_places_of_one_office_share_the_short_name(area_json: dict) -> None:
    # Fuchu City (Tokyo), Fuchu City and Fuchu Town (both Hiroshima)
    assert device_title(area_json, "1320600") == "JMA Fuchu (Tokyo)"
    assert device_title(area_json, "3420800") == "JMA Fuchu City (Hiroshima)"
    assert device_title(area_json, "3430200") == "JMA Fuchu Town (Hiroshima)"


def test_a_prefecture_word_and_a_bracketed_note_are_not_repeated_in_the_brackets(area_json: dict) -> None:
    table = clone(area_json)
    table["offices"]["190000"]["enName"] = "Yamanashi Prefecture"
    assert device_title(table, CHUO_CITY_YAMANASHI) == "JMA Chuo (Yamanashi)"
    table["offices"]["190000"]["enName"] = "Yamanashi (Excluding Somewhere)"
    assert device_title(table, CHUO_CITY_YAMANASHI) == "JMA Chuo (Yamanashi)"


def test_a_table_that_cannot_name_the_office_still_gives_two_different_titles(area_json: dict) -> None:
    table = clone(area_json)
    del table["offices"]["190000"]["enName"]
    assert device_title(table, CHUO_CITY_YAMANASHI) == f"JMA Chuo ({CHUO_CITY_YAMANASHI})"
    assert device_title(table, CHUO_WARD_TOKYO) == "JMA Chuo (Tokyo)"


def test_a_row_without_an_english_name_is_titled_by_its_code(area_json: dict) -> None:
    table = clone(area_json)
    del table["class20s"][AREA]["enName"]
    assert device_title(table, AREA) == f"JMA {AREA}"


async def test_the_config_flow_creates_the_entry_with_the_disambiguated_title(
        tokyo: HomeAssistant, aioclient_mock) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"office": "190000"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"area": CHUO_CITY_YAMANASHI})
    assert result["step_id"] == "confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["title"] == "JMA Chuo (Yamanashi)"
