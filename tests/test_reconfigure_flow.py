"""HA quality scale `reconfiguration-flow`: the Reconfigure menu item of an entry picks the two AMeDAS stations again
from the agency's current tables and Home Assistant's home, refreshes the area names the entry was made with, and
keeps the area (the unique id), the title and the debug option."""

from __future__ import annotations

import json
from pathlib import Path

from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jma.const import DOMAIN

from .conftest import AREA, ENTRY_DATA, OTHER_STATION, TITLE, mock_jma, setup

TRANSLATIONS = Path(__file__).parent.parent / "custom_components" / "jma" / "translations"


async def _start(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id})


def _entry_with_typed_stations() -> MockConfigEntry:
    """An entry whose user typed another secondary station under Configure and turned the debug option on."""
    return MockConfigEntry(domain=DOMAIN, title=TITLE, unique_id=AREA, data=ENTRY_DATA,
                           options={"station_secondary": OTHER_STATION, "debug_simulate": True})


async def test_reconfigure_shows_the_stations_picked_now_and_updates_the_entry_in_place(
        tokyo: HomeAssistant, aioclient_mock, caplog) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    entry = _entry_with_typed_stations()
    await setup(hass, entry)
    before = entry.runtime_data
    assert before.cfg.station_secondary == OTHER_STATION

    result = await _start(hass, entry)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reconfigure"
    assert result.get("data_schema") is None  # a confirm form: no field
    placeholders = result["description_placeholders"]
    assert placeholders["area"] == "千代田区 (1310100)"
    assert placeholders["primary"] == "東京 44132 (1.9 km)"
    assert placeholders["secondary"] == "東京 44132 (1.9 km)"
    assert entry.options["station_secondary"] == OTHER_STATION  # showing the form changes nothing

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert entries == [entry]  # the same entry, no second one
    assert dict(entry.options) == {"debug_simulate": True}  # the typed station is gone, the debug option stays
    assert entry.data == ENTRY_DATA
    assert entry.title == TITLE and entry.unique_id == AREA
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not before  # the entry reloaded
    assert entry.runtime_data.cfg.station_secondary == "44132" and entry.runtime_data.debug_simulate is True
    assert "update listener" not in caplog.text  # HA's usage report for update_reload_and_abort + a listener


async def test_reconfigure_refreshes_the_names_and_the_stations_from_the_current_tables_and_keeps_the_title(
        tokyo: HomeAssistant, aioclient_mock) -> None:
    hass = tokyo
    stale = {**ENTRY_DATA, "area_name": "old", "area_name_ja": "old", "class10": "000000",
             "station_primary": "62016", "station_secondary": "62016"}
    entry = MockConfigEntry(domain=DOMAIN, title="JMA old", unique_id=AREA, data=stale)
    entry.add_to_hass(hass)
    mock_jma(aioclient_mock)
    result = await hass.config_entries.flow.async_configure((await _start(hass, entry))["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert entry.data == ENTRY_DATA  # office and area as they were, everything else from the tables
    assert entry.title == "JMA old"  # the title is the user's: Reconfigure does not write it
    assert entry.state is ConfigEntryState.NOT_LOADED  # an entry that was not set up is not set up by this


async def test_reconfigure_keeps_a_title_the_user_chose(tokyo: HomeAssistant, aioclient_mock) -> None:
    """A rename done in the Home Assistant UI is not overwritten by the automatic 'JMA <English name>'."""
    hass = tokyo
    entry = MockConfigEntry(domain=DOMAIN, title="Weather at home", unique_id=AREA, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    mock_jma(aioclient_mock)
    result = await hass.config_entries.flow.async_configure((await _start(hass, entry))["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert entry.title == "Weather at home"


async def test_an_entry_made_before_0_6_gains_its_japanese_area_name(tokyo: HomeAssistant, aioclient_mock) -> None:
    hass = tokyo
    old = {key: value for key, value in ENTRY_DATA.items() if key != "area_name_ja"}
    entry = MockConfigEntry(domain=DOMAIN, title=TITLE, unique_id=AREA, data=old)
    entry.add_to_hass(hass)
    mock_jma(aioclient_mock)
    assert "area_name_ja" not in entry.data
    result = await hass.config_entries.flow.async_configure((await _start(hass, entry))["flow_id"], {})
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["area_name_ja"] == "千代田区"
    assert hass.config_entries.async_entries(DOMAIN) == [entry]


async def test_tables_that_cannot_be_fetched_abort_without_touching_the_entry(
        tokyo: HomeAssistant, aioclient_mock) -> None:
    hass = tokyo
    entry = _entry_with_typed_stations()
    entry.add_to_hass(hass)
    mock_jma(aioclient_mock, area_ok=False)  # the area list answers HTTP 503
    result = await _start(hass, entry)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "cannot_connect"
    assert dict(entry.options) == {"station_secondary": OTHER_STATION, "debug_simulate": True}
    assert entry.data == ENTRY_DATA


async def test_tables_that_fail_between_the_form_and_the_submit_abort_without_touching_the_entry(
        tokyo: HomeAssistant, aioclient_mock) -> None:
    hass = tokyo
    entry = _entry_with_typed_stations()
    entry.add_to_hass(hass)
    mock_jma(aioclient_mock)
    form = await _start(hass, entry)
    assert form["type"] is FlowResultType.FORM
    aioclient_mock.clear_requests()
    mock_jma(aioclient_mock, area_ok=False)  # the submit fetches the tables again
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "cannot_connect"
    assert dict(entry.options) == {"station_secondary": OTHER_STATION, "debug_simulate": True}


async def test_an_entry_whose_unique_id_is_not_its_area_is_not_reconfigured(
        tokyo: HomeAssistant, aioclient_mock) -> None:
    hass = tokyo
    entry = MockConfigEntry(domain=DOMAIN, title=TITLE, unique_id="2710000", data=ENTRY_DATA,
                            options={"station_secondary": OTHER_STATION})
    entry.add_to_hass(hass)
    mock_jma(aioclient_mock)
    result = await hass.config_entries.flow.async_configure((await _start(hass, entry))["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "unique_id_mismatch"
    assert dict(entry.options) == {"station_secondary": OTHER_STATION}


def test_the_reconfigure_texts_exist_in_both_languages_with_the_placeholders() -> None:
    for language in ("en", "ja"):
        config = json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))["config"]
        step = config["step"]["reconfigure"]
        assert step["title"]
        for placeholder in ("{area}", "{primary}", "{secondary}"):
            assert placeholder in step["description"], (language, placeholder)
        assert config["abort"]["reconfigure_successful"] and config["abort"]["unique_id_mismatch"]


def test_the_reconfigure_success_text_does_not_promise_a_reload() -> None:
    """The entry reloads only when it is set up and its settings changed: an entry that is not loaded, or whose
    stations came out the same, does not, so the text says what was done and nothing about what follows."""
    texts = {language: json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))["config"]["abort"]
             for language in ("en", "ja")}
    assert texts["en"]["reconfigure_successful"] == "The stations and names were updated."
    assert texts["ja"]["reconfigure_successful"] == "観測所と名称を更新しました。"
    assert "再読み込み" not in texts["ja"]["reconfigure_successful"]
