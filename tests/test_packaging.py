"""What the repository ships: translations, manifest, hacs.json."""

from __future__ import annotations

import json
import re
from pathlib import Path

from custom_components.jma.const import USER_AGENT, VERSION

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "jma"


def _keys(obj, prefix=""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _keys(value, f"{prefix}{key}.")
    else:
        yield prefix.rstrip(".")


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_english_and_japanese_translations_have_the_same_keys():
    en = set(_keys(_load(COMPONENT / "translations" / "en.json")))
    ja = set(_keys(_load(COMPONENT / "translations" / "ja.json")))
    assert en == ja and len(en) == 40


def test_translation_placeholders_match():
    en = _load(COMPONENT / "translations" / "en.json")["config"]["step"]["confirm"]["description"]
    ja = _load(COMPONENT / "translations" / "ja.json")["config"]["step"]["confirm"]["description"]
    for placeholder in ("{area}", "{primary}", "{secondary}"):
        assert placeholder in en and placeholder in ja


def test_strings_json_is_not_shipped():
    # a custom integration ships translations/en.json, not strings.json
    assert not (COMPONENT / "strings.json").exists()


def test_manifest():
    manifest = _load(COMPONENT / "manifest.json")
    assert manifest["requirements"] == []  # Home Assistant itself requires Pillow
    assert manifest["version"] == "0.1.0" == VERSION
    for key in ("domain", "documentation", "issue_tracker", "codeowners", "name", "version"):
        assert manifest[key], key


def test_manifest_links_use_the_owner_placeholder_or_one_account_name():
    # `scripts/set_owner.sh <name>` replaces OWNER; either form must keep the shape of a GitHub address.
    manifest = _load(COMPONENT / "manifest.json")
    name = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
    assert re.fullmatch(rf"https://github\.com/{name}/ha-jma", manifest["documentation"])
    assert re.fullmatch(rf"https://github\.com/{name}/ha-jma/issues", manifest["issue_tracker"])
    assert all(re.fullmatch(rf"@{name}", owner) for owner in manifest["codeowners"])


def test_user_agent_names_the_product_and_version_only():
    assert USER_AGENT == f"ha-jma/{VERSION}"


def test_hacs_json_holds_name_and_minimum_version_only():
    hacs = _load(ROOT / "hacs.json")
    assert set(hacs) == {"name", "homeassistant"}
