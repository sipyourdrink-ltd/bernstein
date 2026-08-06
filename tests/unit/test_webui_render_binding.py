"""The published browser renders have to stay bound to the SPA that ships.

Pixels cannot be compared across machines, so the browser half of the render
freshness gate checks a weaker property deliberately: every committed render of
the web UI is bound to a content hash of the SPA bundle in the wheel, and the
gate fails when the bundle moves and the renders do not.

These tests cover the binding's three failure modes - a moved bundle, a render
nothing binds, and a binding that outlives its render - plus the property the
digest rests on, that a rename counts as a change.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "bind_webui_renders.py"


@pytest.fixture
def binder() -> Any:
    """Load scripts/bind_webui_renders.py without executing main()."""
    spec = importlib.util.spec_from_file_location("bind_webui_renders_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_binding_matches_the_bundle_that_ships_today(binder: Any) -> None:
    """The gate itself, run against the repository as committed."""
    assert binder.verify() == []


def test_every_committed_spa_render_is_bound(binder: Any) -> None:
    """An unbound render is one nothing would notice going stale."""
    bound = binder.load_binding()["renders"]
    assert sorted(bound) == binder.committed_renders()


def test_a_moved_bundle_fails_and_names_the_renders_to_recapture(
    binder: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure has to say which screenshots are now suspect.

    "The bundle changed" leaves the reader to work out what to do about it;
    the affected renders are exactly the actionable part.
    """
    moved = tmp_path / "static"
    moved.mkdir()
    (moved / "index.html").write_text("<!doctype html><title>a different build</title>")
    monkeypatch.setattr(binder, "BUNDLE_DIR", moved)

    problems = binder.verify()

    assert problems, "a moved bundle must not pass"
    joined = "\n".join(problems)
    assert "webui-agents-diffs.png" in joined
    assert binder.load_binding()["spa_bundle_sha256"] in joined


def test_a_render_with_no_binding_fails(binder: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding a screenshot must not be a way out of the gate."""
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in binder.committed_renders():
        (assets / name).write_bytes(b"")
    (assets / "webui-brand-new-screen.png").write_bytes(b"")
    monkeypatch.setattr(binder, "ASSET_DIR", assets)

    problems = binder.verify()

    assert any("webui-brand-new-screen.png" in problem for problem in problems), problems


def test_a_binding_that_outlives_its_render_fails(binder: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A deleted screenshot leaves a claim about a file that is not there."""
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in binder.committed_renders()[1:]:
        (assets / name).write_bytes(b"")
    dropped = binder.committed_renders()[0]
    monkeypatch.setattr(binder, "ASSET_DIR", assets)

    problems = binder.verify()

    assert any(dropped in problem for problem in problems), problems


def test_renaming_a_bundle_file_moves_the_digest(binder: Any, tmp_path: Path) -> None:
    """A rename ships a new UI even when every byte is the same.

    Vite emits content-hashed filenames, so a rebuild that changes nothing but
    the asset name is exactly what a UI change looks like from outside.
    """
    first = tmp_path / "one"
    second = tmp_path / "two"
    for directory in (first, second):
        (directory / "assets").mkdir(parents=True)
        (directory / "index.html").write_text("<!doctype html>")
    (first / "assets" / "index-AAAA.js").write_text("console.log(1)")
    (second / "assets" / "index-BBBB.js").write_text("console.log(1)")

    assert binder.bundle_digest(first)[0] != binder.bundle_digest(second)[0]
    # ...and the same tree hashes the same, or the gate would fail at random.
    assert binder.bundle_digest(first) == binder.bundle_digest(first)


def test_the_digest_covers_file_contents_too(binder: Any, tmp_path: Path) -> None:
    """The complement of the rename property: same names, different bytes."""
    first = tmp_path / "one"
    second = tmp_path / "two"
    for directory in (first, second):
        directory.mkdir()
    (first / "index.html").write_text("<!doctype html><title>a</title>")
    (second / "index.html").write_text("<!doctype html><title>b</title>")

    assert binder.bundle_digest(first)[0] != binder.bundle_digest(second)[0]
    assert binder.bundle_digest(first)[0] != hashlib.sha256(b"").hexdigest()


def test_the_documented_update_flag_writes_the_binding(
    binder: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command the docs print has to reach the code that writes the file.

    Asserting the string appears in a markdown page proves the sentence, not
    the behaviour: the flag could be renamed and the page would still read
    correctly while the operator's copy-paste failed.
    """
    written = tmp_path / "webui-renders.json"
    monkeypatch.setattr(binder, "BINDING", written)

    assert binder.main(["--update"]) == 0

    rebound = json.loads(written.read_text(encoding="utf-8"))
    assert rebound["spa_bundle_sha256"] == binder.bundle_digest()[0]
    assert sorted(rebound["renders"]) == binder.committed_renders()
