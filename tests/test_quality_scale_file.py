"""The quality-scale declaration: custom_components/jma/quality_scale.yaml and manifest `quality_scale`.

hassfest validates this file for core integrations only (script/hassfest/quality_scale.py returns early when
`integration.core` is false), so this test does what it would: every rule listed once, each status one of
done / todo / exempt, an exempt rule with a comment, and every rule the declared tier needs done or exempt.
The rule list is the one in hassfest's quality_scale.py (54 rules, 2026-10-23).
"""

import json
from pathlib import Path

import yaml

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "jma"

BRONZE = [
    "action-setup", "appropriate-polling", "brands", "common-modules", "config-flow",
    "config-flow-test-coverage", "dependency-transparency", "docs-actions", "docs-conditions",
    "docs-high-level-description", "docs-installation-instructions", "docs-removal-instructions",
    "docs-triggers", "entity-event-setup", "entity-unique-id", "has-entity-name", "runtime-data",
    "test-before-configure", "test-before-setup", "unique-config-entry",
]
SILVER = [
    "action-exceptions", "config-entry-unloading", "docs-configuration-parameters",
    "docs-installation-parameters", "entity-unavailable", "integration-owner", "log-when-unavailable",
    "parallel-updates", "reauthentication-flow", "test-coverage",
]
GOLD = [
    "devices", "diagnostics", "discovery", "discovery-update-info", "docs-data-update", "docs-examples",
    "docs-known-limitations", "docs-supported-devices", "docs-supported-functions", "docs-troubleshooting",
    "docs-use-cases", "dynamic-devices", "entity-category", "entity-device-class", "entity-disabled-by-default",
    "entity-translations", "exception-translations", "icon-translations", "reconfiguration-flow",
    "repair-issues", "stale-devices",
]
PLATINUM = ["async-dependency", "inject-websession", "strict-typing"]
TIERS = {"bronze": BRONZE, "silver": SILVER, "gold": GOLD, "platinum": PLATINUM}
ALL_RULES = BRONZE + SILVER + GOLD + PLATINUM


def _rules() -> dict[str, object]:
    with (COMPONENT / "quality_scale.yaml").open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    assert set(doc) == {"rules"}, doc.keys()
    return doc["rules"]


def _status(value: object) -> str:
    if isinstance(value, str):
        assert value in ("done", "todo"), value  # exempt must carry a comment, so it is never a bare word
        return value
    assert isinstance(value, dict) and set(value) <= {"status", "comment"}, value
    status = value["status"]
    assert status in ("done", "todo", "exempt"), value
    if status == "exempt":
        assert isinstance(value.get("comment"), str) and value["comment"].strip(), value
    return status


def test_every_rule_of_the_scale_is_listed_once_and_nothing_else():
    assert len(ALL_RULES) == 54 == len(set(ALL_RULES))
    assert list(_rules()) == ALL_RULES  # same order as hassfest, so a diff against core reads easily


def test_the_manifest_declares_the_custom_tier():
    # Home Assistant gives the tier "custom" to every integration outside core; the rules are still all met below.
    with (COMPONENT / "manifest.json").open(encoding="utf-8") as f:
        assert json.load(f)["quality_scale"] == "custom"


def test_nothing_is_left_as_todo():
    assert [rule for rule, value in _rules().items() if _status(value) == "todo"] == []


def test_the_evidence_the_file_names_is_there():
    """A handful of the `done` lines point at files or flags; keep the pointers honest."""
    assert (COMPONENT / "icons.json").is_file()  # icon-translations
    assert (COMPONENT / "diagnostics.py").is_file()  # diagnostics
    assert (COMPONENT / "brand" / "icon.png").is_file()  # brands
    for platform in ("sensor", "binary_sensor", "weather", "event"):  # parallel-updates
        assert "PARALLEL_UPDATES = 0" in (COMPONENT / f"{platform}.py").read_text(encoding="utf-8")
    with (COMPONENT / "manifest.json").open(encoding="utf-8") as f:
        manifest = json.load(f)
    assert manifest["codeowners"]  # integration-owner
    assert manifest["requirements"] == []  # dependency-transparency, async-dependency
    source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    assert "async_get_clientsession" in source  # inject-websession
    assert "ConfigEntryNotReady" in source  # test-before-setup
