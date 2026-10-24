"""What the repository ships: translations, manifest, hacs.json."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

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
    assert en == ja and len(en) == 82  # the interface texts and the title


def test_every_form_field_has_a_data_description_in_both_languages():
    """HA quality scale `config-flow`: each field of a form says what it is for and where its value comes from."""
    for language in ("en", "ja"):
        translations = _load(COMPONENT / "translations" / f"{language}.json")
        for section in ("config", "options"):
            for step_id, step in translations[section]["step"].items():
                assert set(step.get("data", {})) == set(step.get("data_description", {})), (language, section, step_id)


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
    assert manifest["version"] == "0.8.0" == VERSION
    for key in ("domain", "documentation", "issue_tracker", "codeowners", "name", "version"):
        assert manifest[key], key


def test_manifest_links_have_the_shape_of_a_github_address():
    manifest = _load(COMPONENT / "manifest.json")
    name = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
    assert re.fullmatch(rf"https://github\.com/{name}/ha-jma", manifest["documentation"])
    assert re.fullmatch(rf"https://github\.com/{name}/ha-jma/issues", manifest["issue_tracker"])
    assert all(re.fullmatch(rf"@{name}", owner) for owner in manifest["codeowners"])


def test_user_agent_names_the_product_and_version_only():
    assert USER_AGENT == f"ha-jma/{VERSION}"


def test_hacs_json_holds_name_country_minimum_version_and_the_release_asset():
    hacs = _load(ROOT / "hacs.json")
    assert set(hacs) == {"name", "country", "homeassistant", "zip_release", "filename", "hide_default_branch"}
    assert hacs["country"] == "JP"  # HACS asks for it when a repository serves one country
    # HACS and the Marketplace download the release asset named here and count its downloads; the default
    # branch is not offered, so users only ever get a tested release
    assert hacs["zip_release"] is True
    assert hacs["filename"] == "jma.zip"
    assert hacs["hide_default_branch"] is True


def test_the_release_workflow_attaches_the_asset_hacs_json_names():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    filename = _load(ROOT / "hacs.json")["filename"]
    triggers = workflow[True]  # YAML 1.1 reads the key "on" as true
    assert triggers["release"]["types"] == ["published"]  # every published release gets its asset
    runs = [step["run"] for step in workflow["jobs"]["asset"]["steps"] if "run" in step]
    assert any(f"zip -r ../../{filename} ." in run for run in runs)
    # upload, not create: a manual run with a tag that has no release must fail, not publish one
    upload = next(step for step in workflow["jobs"]["asset"]["steps"] if step.get("name", "").startswith("Upload"))
    assert upload["run"] == f'gh release upload "$TAG" {filename} --clobber'
    # the release is public before the job runs, so a failed job must hide it again
    last = workflow["jobs"]["asset"]["steps"][-1]
    assert last["if"] == "failure() && github.event_name == 'release'"
    assert last["run"] == 'gh release edit "$TAG" --draft=true'


def test_the_name_says_it_is_unofficial_in_every_place_a_user_sees_it():
    manifest = _load(COMPONENT / "manifest.json")
    hacs = _load(ROOT / "hacs.json")
    assert manifest["name"] == hacs["name"] == "JMA Weather (Unofficial)"
    assert _load(COMPONENT / "translations" / "en.json")["title"] == manifest["name"]
    assert "非公式" in _load(COMPONENT / "translations" / "ja.json")["title"]
