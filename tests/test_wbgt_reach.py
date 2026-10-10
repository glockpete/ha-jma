"""The heat index (WBGT): created only for offices whose CSV file name is known (the offices in the table: Tokyo's 130000)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jma.const import DOMAIN, WBGT_PREF

from .conftest import ENTRY_DATA, mock_jma, setup

OSAKA = {  # an office whose WBGT file name is not known to the integration
    "office": "270000", "area": "2714000", "area_name": "堺市", "class10": "270000",
    "station_primary": "62016", "station_secondary": "62016",  # the station the synthetic point file answers for
}


def test_the_known_offices_are_listed_not_invented() -> None:
    assert WBGT_PREF == {"130000": "tokyo"}


async def test_known_office_gets_the_wbgt_sensor(tokyo: HomeAssistant, aioclient_mock, entry) -> None:
    mock_jma(aioclient_mock)
    await setup(tokyo, entry)
    registry = er.async_get(tokyo)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{ENTRY_DATA['area']}_wbgt") is not None


async def test_other_office_creates_no_wbgt_sensor(tokyo: HomeAssistant, aioclient_mock) -> None:
    mock_jma(aioclient_mock)
    other = MockConfigEntry(domain=DOMAIN, title="JMA Test", unique_id=OSAKA["area"], data=OSAKA)
    await setup(tokyo, other)
    registry = er.async_get(tokyo)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{OSAKA['area']}_wbgt") is None
    # the other sensors of the entry are there
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{OSAKA['area']}_warning_level") is not None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{OSAKA['area']}_typhoon") is not None
