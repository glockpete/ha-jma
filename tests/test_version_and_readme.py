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
