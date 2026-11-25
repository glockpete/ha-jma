"""manifest.json's version and const.VERSION are two hand-synced copies (the User-Agent sent to the services
carries const.VERSION), and the README's entity list names every sensor the code creates."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "jma"


def _const_version() -> str:
    m = re.search(r'^VERSION: Final = "([^"]+)"$', (COMPONENT / "const.py").read_text(encoding="utf-8"), re.M)
    assert m, "const.VERSION not found"
    return m.group(1)


def test_manifest_version_equals_const_version():
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == _const_version()


def test_readme_names_every_sensor_the_code_creates():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    code = (COMPONENT / "sensor.py").read_text(encoding="utf-8")
    keys = re.findall(r'super\(\).__init__\(entry, "([a-z_0-9]+)"', code)
    assert keys, "no sensor keys found in sensor.py"
    for key in keys:
        assert f"sensor.jma_<place>_{key}" in readme, key


_COUNT_WORDS = {6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
_FEED_WORDS = {
    "warnings": "warnings",
    "amedas": "AMeDAS",
    "amedas_station": "AMeDAS",
    "hourly": "hourly forecast",
    "nowcast": "rain nowcast",
    "forecast": "daily forecast",
    "quake": "earthquake connection",
    "volcano": "volcano",
}


def test_readme_counts_the_hokkaido_offices_as_the_area_list_does():
    area = json.loads((ROOT / "tests" / "fixtures" / "area.json").read_text(encoding="utf-8"))
    hokkaido = sorted(code for code in area["offices"] if code.startswith("01"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    found = re.search(r"the (\w+) Hokkaido offices read `hokkaido`", readme)
    assert found, "the sentence about the Hokkaido offices is gone"
    assert found.group(1) == _COUNT_WORDS[len(hokkaido)], (found.group(1), len(hokkaido), hokkaido)


def test_troubleshooting_names_every_feed_the_feed_problem_sensor_watches():
    code = (COMPONENT / "binary_sensor.py").read_text(encoding="utf-8")
    keys = set(re.findall(r'out\["([a-z_]+)"\] = ', code))
    assert keys == set(_FEED_WORDS), keys  # a new feed key needs a word here
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("- **Check the feed-problem sensor.**")
    paragraph = " ".join(readme[start : readme.index("\n- **", start + 5)].split())  # one line
    missing = sorted(key for key in keys if _FEED_WORDS[key] not in paragraph)
    assert missing == [], missing
