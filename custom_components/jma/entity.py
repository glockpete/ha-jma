"""Shared entity base: one service device per config entry ("JMA <place>")."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import JmaConfigEntry, JmaRuntime


class JmaEntity(Entity):
    """Unique id = <area>_<key>; entity id = <device>_<translated name>."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: JmaConfigEntry, key: str) -> None:
        self.runtime: JmaRuntime = entry.runtime_data
        self._entry_id = entry.entry_id
        cfg = self.runtime.cfg
        self._attr_translation_key = key
        self._attr_unique_id = f"{cfg.area}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cfg.area)},
            name=entry.title,
            manufacturer="Japan Meteorological Agency (気象庁)",
            model=f"{cfg.office} / {cfg.area} {cfg.area_name}",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://www.jma.go.jp/bosai/",
        )
