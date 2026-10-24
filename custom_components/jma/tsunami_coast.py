"""Which 津波予報区 (tsunami forecast region) an entry's tsunami sensor follows. No Home Assistant imports.

The table in `tsunami_coast_data.py` is JMA's own list of the coastal municipalities of each region, by the class20
code of the area list (the `area` the config flow stores); regenerate it with `scripts/build_tsunami_coast.py`.
"""

from __future__ import annotations

from .const import TSUNAMI_COAST_AREA
from .tsunami_coast_data import TSUNAMI_COAST


def home_regions(area: str, class10: str) -> tuple[str, ...]:
    """The regions whose grade is the grade of the place: those JMA lists for its municipality (one, or two for a
    municipality on two coasts), else, for the Tokyo office, the region of its class10 area (what every Tokyo
    entry has had since the sensor was added: JMA's list names six of the 23 wards, and an inland ward keeps
    the Tokyo Bay grade). () for any other municipality: it is not on a coast the list names."""
    if regions := TSUNAMI_COAST.get(area):
        return regions
    legacy = TSUNAMI_COAST_AREA.get(class10)
    return (legacy,) if legacy else ()
