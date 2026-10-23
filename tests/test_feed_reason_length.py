"""binary_sensor.jma_<place>_feed_problem: the reasons are attributes written to the recorder every minute, so an error
text stored by a coordinator (a server's whole HTML page, say) is cut short wherever it goes into one."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.jma.binary_sensor import ERROR_TEXT_MAX, clip_error, feed_conditions
from custom_components.jma.const import HEALTH_AMEDAS_MAX_AGE, HEALTH_HOURLY_MAX_AGE

from .conftest import OTHER_STATION, mock_jma, setup

LONG = "x" * 1000
# A reason is the words around the error (up to about 90 characters, a timestamp among them) and the error itself,
# which is cut to ERROR_TEXT_MAX; 1,000 characters in, at most ~300 out.
LIMIT = ERROR_TEXT_MAX + 100


def _short(reason: str | None) -> bool:
    """The reason holds the error cut to ERROR_TEXT_MAX characters (and not a character more)."""
    return reason is not None and len(reason) <= LIMIT and reason.rsplit(": ", 1)[1] == clip_error(LONG)


def test_clip_error_cuts_at_200_characters_and_says_so() -> None:
    assert clip_error("a" * ERROR_TEXT_MAX) == "a" * ERROR_TEXT_MAX  # exactly 200: whole
    clipped = clip_error("a" * (ERROR_TEXT_MAX + 1))
    assert len(clipped) == ERROR_TEXT_MAX and clipped.endswith("…") and clipped[:-1] == "a" * (ERROR_TEXT_MAX - 1)
    assert clip_error(LONG).endswith("…") and len(clip_error(LONG)) == ERROR_TEXT_MAX


def test_clip_error_leaves_a_short_text_and_a_missing_one_as_the_f_string_gave_them() -> None:
    assert clip_error("HTTP 503") == "HTTP 503"
    assert clip_error(None) == "None"


async def test_every_reason_that_embeds_an_error_text_is_short(
        tokyo: HomeAssistant, aioclient_mock, entry_two_stations) -> None:
    hass = tokyo
    mock_jma(aioclient_mock)
    await setup(hass, entry_two_stations)
    rt = entry_two_stations.runtime_data
    now = dt_util.utcnow()

    # r8 warnings never fetched
    rt.warnings.last_ok, rt.warnings.last_error = None, LONG
    reason = feed_conditions(rt, now)["warnings"]
    assert _short(reason) and "r8 warnings not fetched" in reason

    # the XML cross-check never fetched (r8 itself is fine)
    rt.warnings.last_ok, rt.warnings.xml_ok, rt.warnings.xml_error = now, None, LONG
    rt.warnings.data.stale.clear()
    reason = feed_conditions(rt, now)["warnings"]
    assert _short(reason) and "XML cross-check not fetched" in reason

    # hourly not fetched for longer than the limit
    rt.hourly.last_ok, rt.hourly.last_error = now - 2 * HEALTH_HOURLY_MAX_AGE, LONG
    reason = feed_conditions(rt, now)["hourly"]
    assert _short(reason) and "Open-Meteo hourly not fetched" in reason

    # one AMeDAS station silent while the other answers
    primary, secondary = rt.cfg.station_primary, rt.cfg.station_secondary
    assert secondary == OTHER_STATION
    rt.amedas.station_ok = {primary: now - 2 * HEALTH_AMEDAS_MAX_AGE, secondary: now - timedelta(minutes=1)}
    rt.amedas.station_error = {primary: LONG}
    reason = feed_conditions(rt, now)["amedas_station"]
    assert _short(reason) and "has answered nothing since" in reason
