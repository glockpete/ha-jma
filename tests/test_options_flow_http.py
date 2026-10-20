"""The options flow as the Configure dialog uses it: over HTTP, where Home Assistant must turn the
schema into form fields. Regression: on Home Assistant 2026.9 the form answered HTTP 500 (`unable to
serialize schema: Match(...)`) while every test that called the flow in Python passed."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .conftest import mock_jma, setup

FLOW = "/api/config/config_entries/options/flow"


async def _client(hass: HomeAssistant, aioclient_mock, entry, hass_client):
    mock_jma(aioclient_mock)
    await setup(hass, entry)
    assert await async_setup_component(hass, "config", {})
    await hass.async_block_till_done()
    return await hass_client()


async def test_the_options_form_is_served_over_http(tokyo: HomeAssistant, aioclient_mock, entry, hass_client) -> None:
    client = await _client(tokyo, aioclient_mock, entry, hass_client)
    response = await client.post(FLOW, json={"handler": entry.entry_id})
    assert response.status == 200, await response.text()
    form = await response.json()
    assert form["type"] == "form" and form["step_id"] == "init"
    fields = {f["name"]: f for f in form["data_schema"]}
    assert set(fields) == {"station_primary", "station_secondary", "debug_simulate"}
    assert fields["station_primary"]["default"] == entry.data["station_primary"]
    assert fields["debug_simulate"]["default"] is False


async def test_the_options_form_saves_over_http_and_registers_simulate(tokyo: HomeAssistant, aioclient_mock, entry,
                                                                        hass_client) -> None:
    hass = tokyo
    client = await _client(hass, aioclient_mock, entry, hass_client)
    assert not hass.services.has_service("jma", "simulate")
    flow = await (await client.post(FLOW, json={"handler": entry.entry_id})).json()
    response = await client.post(f"{FLOW}/{flow['flow_id']}", json={
        "station_primary": entry.data["station_primary"], "station_secondary": entry.data["station_secondary"],
        "debug_simulate": True})
    assert response.status == 200, await response.text()
    assert (await response.json())["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["debug_simulate"] is True
    assert hass.services.has_service("jma", "simulate")  # the entry reloaded with the option on


async def test_a_station_id_that_is_not_five_digits_is_refused_with_a_message(tokyo: HomeAssistant, aioclient_mock,
                                                                              entry, hass_client) -> None:
    client = await _client(tokyo, aioclient_mock, entry, hass_client)
    flow = await (await client.post(FLOW, json={"handler": entry.entry_id})).json()
    response = await client.post(f"{FLOW}/{flow['flow_id']}", json={
        "station_primary": "4407", "station_secondary": " 44132 ", "debug_simulate": False})
    assert response.status == 200, await response.text()
    again = await response.json()
    assert again["type"] == "form" and again["errors"] == {"station_primary": "invalid_station"}
    fields = {f["name"]: f for f in again["data_schema"]}
    assert fields["station_primary"]["default"] == "4407" and fields["station_secondary"]["default"] == "44132"
    assert dict(entry.options) == {}  # nothing was saved
