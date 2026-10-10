"""scripts/set_owner.sh, run on a copy of the repository tree in a temporary folder.

The tests hold whether the repository has the placeholder or an owner has already been set: the copy is
first put back into the placeholder state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHANGES = {"custom_components/jma/manifest.json", "README.md"}
NOTE = (
    "<!-- owner-placeholder-note:start -->\n> placeholder note\n<!-- owner-placeholder-note:end -->\n\n"
)
ACCOUNT = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"


def _placeholder_state(tree: Path) -> None:
    """Put the copy into the state before `set_owner.sh` was run: OWNER everywhere, the note in the README."""
    manifest = tree / "custom_components/jma/manifest.json"
    text = manifest.read_text(encoding="utf-8")
    text = re.sub(rf"github\.com/{ACCOUNT}/ha-jma", "github.com/OWNER/ha-jma", text)
    text = re.sub(rf'"@{ACCOUNT}"', '"@OWNER"', text)
    manifest.write_text(text, encoding="utf-8")
    readme = tree / "README.md"
    text = re.sub(rf"github\.com/{ACCOUNT}/ha-jma", "github.com/OWNER/ha-jma", readme.read_text(encoding="utf-8"))
    if "owner-placeholder-note:start" not in text:
        first, _, rest = text.partition("\n\n")
        text = first + "\n\n" + NOTE + rest
    readme.write_text(text, encoding="utf-8")


def _copy(tmp_path: Path) -> Path:
    dest = tmp_path / "tree"
    shutil.copytree(
        ROOT, dest, ignore=shutil.ignore_patterns("__pycache__", ".git", ".pytest_cache", "fixtures")
    )
    _placeholder_state(dest)
    return dest


def _run(tree: Path, *args: str, tmpdir: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    return subprocess.run(
        ["bash", str(tree / "scripts" / "set_owner.sh"), *args], capture_output=True, text=True, check=False, env=env
    )


def _hashes(tree: Path) -> dict[str, str]:
    return {
        str(p.relative_to(tree)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(tree.rglob("*"))
        if p.is_file()
    }


def test_the_placeholder_is_replaced_in_exactly_the_named_places(tmp_path: Path) -> None:
    tree = _copy(tmp_path)
    (tree / "notes.txt").write_text("OWNER of a COOWNER and an OWNERSHIP, github.com/OWNERS/x\n", encoding="utf-8")
    before = _hashes(tree)
    result = _run(tree, "acme-weather")
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.split("changed: ")[1:]) == {f + "\n" for f in CHANGES}

    manifest = json.loads((tree / "custom_components/jma/manifest.json").read_text(encoding="utf-8"))
    assert manifest["documentation"] == "https://github.com/acme-weather/ha-jma"
    assert manifest["issue_tracker"] == "https://github.com/acme-weather/ha-jma/issues"
    assert manifest["codeowners"] == ["@acme-weather"]
    readme = (tree / "README.md").read_text(encoding="utf-8")
    assert "github.com/acme-weather/ha-jma" in readme and "github.com/OWNER/ha-jma" not in readme
    assert "owner-placeholder-note" not in readme and "placeholder note" not in readme
    assert not readme.startswith("\n") and "\n\n\n" not in readme.split("##")[0]

    after = _hashes(tree)
    assert {name for name in after if after[name] != before.get(name)} == CHANGES
    assert (tree / "notes.txt").read_text(encoding="utf-8").startswith("OWNER of a COOWNER")


def test_running_it_twice_changes_nothing_the_second_time(tmp_path: Path) -> None:
    tree = _copy(tmp_path)
    assert _run(tree, "acme").returncode == 0
    after_first = _hashes(tree)
    again = _run(tree, "other")
    assert again.returncode == 0 and again.stdout == ""
    assert _hashes(tree) == after_first


@pytest.mark.parametrize(
    "bad",
    ["", " ", "a b", "a/b", "a\\b", "-x", "x-", "a--b", "$(id)", "x;y", "a" * 40, "OWNER", "ä", "ok\nname", "ok\n", "\nok"],
)
def test_an_empty_or_unsafe_name_is_refused_and_nothing_changes(tmp_path: Path, bad: str) -> None:
    tree = _copy(tmp_path)
    before = _hashes(tree)
    result = _run(tree, bad)
    assert result.returncode != 0
    assert _hashes(tree) == before


def test_no_argument_and_two_arguments_are_refused(tmp_path: Path) -> None:
    tree = _copy(tmp_path)
    before = _hashes(tree)
    assert _run(tree).returncode != 0
    assert _run(tree, "a", "b").returncode != 0
    assert _hashes(tree) == before


def test_a_failed_replacement_leaves_no_temporary_file(tmp_path: Path) -> None:
    tree = _copy(tmp_path)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    readme = tree / "README.md"
    readme.chmod(0o444)  # the replacement of the README cannot be written back
    try:
        if os.access(readme, os.W_OK):
            pytest.skip("files are writable despite the mode (running as a privileged user)")
        result = _run(tree, "acme", tmpdir=scratch)
        assert result.returncode != 0
        assert list(scratch.iterdir()) == []
    finally:
        readme.chmod(0o644)
