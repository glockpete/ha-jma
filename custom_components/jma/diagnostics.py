"""Diagnostics: configuration and the health of each feed.

The place is hidden: coordinates, the area code and names, both station ids and the entry title are
redacted (also where they sit inside an error message). The office code (prefecture level) stays.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import JmaConfigEntry

REDACTED = "**REDACTED**"
TO_REDACT = {
    "latitude", "longitude",
    "area", "area_name", "area_name_ja", "class10",
    "station_primary", "station_secondary",
    "title",
}


def _secrets(entry: JmaConfigEntry) -> list[str]:
    """Strings that must not survive inside free text (error messages carry URLs and values)."""
    cfg = entry.runtime_data.cfg
    values = [cfg.area, cfg.area_name, cfg.class10, cfg.station_primary, cfg.station_secondary, entry.title,
              str(cfg.latitude), str(cfg.longitude)]
    values += [str(v) for k, v in {**entry.data, **entry.options}.items() if k in TO_REDACT]
    # longest first, so a station id inside a longer value is not cut out of it first
    # Japanese area names are often only two or three characters long, so they are redacted from two characters up;
    # digits and ASCII text shorter than four characters would cut up every number, so those stay.
    return sorted({v for v in values if len(v) >= 4 or (len(v) >= 2 and not v.isascii())}, key=len, reverse=True)


# The tile a failed download asked for follows from the coordinates: `.../<zoom>/<x>/<y>.png`.
_TILE_PATH = re.compile(r"/(\d{1,2})/\d+/\d+(\.(?:png|jpg))")


def _scrub(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, REDACTED)
        return _TILE_PATH.sub(lambda m: f"/{m.group(1)}/{REDACTED}{m.group(2)}", value)
    if isinstance(value, dict):
        return {k: _scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, secrets) for v in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: JmaConfigEntry
) -> dict[str, Any]:
    rt = entry.runtime_data
    out = {
        "entry": async_redact_data(
            {"title": entry.title, "data": dict(entry.data), "options": dict(entry.options)}, TO_REDACT),
        "config": async_redact_data(asdict(rt.cfg), TO_REDACT),
        "coordinators": {
            c.name: {
                "last_update_success": c.last_update_success,
                "last_ok": c.last_ok.isoformat() if c.last_ok else None,
                "last_error": c.last_error,
            }
            for c in rt.coordinators
        },
        "warnings_xml_ok": rt.warnings.xml_ok.isoformat() if rt.warnings.xml_ok else None,
        "warnings_xml_error": rt.warnings.xml_error,
        # Each map-layer frame list fails on its own (the layers coordinator stays up).
        "layers_sources": {
            key: {
                "last_ok": s.last_ok.isoformat() if s.last_ok else None,
                "last_error": s.last_error,
            }
            for key, s in rt.layers.sources.items()
        },
        "quake": {
            "connected": rt.quake.connected,
            "down_since": rt.quake.down_since.isoformat() if rt.quake.down_since else None,
            "last_message": rt.quake.last_message.isoformat() if rt.quake.last_message else None,
        },
        "tsunami": {
            "home_area": rt.quake.tsunami.home_area,
            "replayed": rt.quake.tsunami.replayed,
            "current_id": rt.quake.tsunami.current.id if rt.quake.tsunami.current else None,
            "issued": rt.quake.tsunami.current.issued.isoformat()
            if rt.quake.tsunami.current else None,
            "memory": dict(rt.quake.tsunami.memory),
        },
    }
    secrets = _secrets(entry)
    return {key: (value if key in ("entry", "config") else _scrub(value, secrets)) for key, value in out.items()}
