"""Unit tests for :mod:`bernstein.core.preview.command_discovery`."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.preview.command_discovery import (
    discover_commands,
    list_candidates,
)


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_discover_prefers_package_json_dev_over_start(tmp_path: Path) -> None:
    """``scripts.dev`` wins over ``scripts.start`` in the same package.json."""
    _write(
        tmp_path / "package.json",
        json.dumps({"scripts": {"dev": "vite", "start": "vite preview"}}),
    )
    chosen = discover_commands(tmp_path)
    assert chosen is not None
    assert chosen.source == "package.json:dev"
    assert chosen.command == "npm run dev"


def test_discover_falls_back_to_package_json_start(tmp_path: Path) -> None:
    """Without a ``dev`` script, the ``start`` script wins."""
    _write(tmp_path / "package.json", json.dumps({"scripts": {"start": "next start"}}))
    chosen = discover_commands(tmp_path)
    assert chosen is not None
    assert chosen.source == "package.json:start"


def test_discover_uses_procfile_when_no_package_json(tmp_path: Path) -> None:
    """Procfile is consulted when ``package.json`` is missing."""
    _write(tmp_path / "Procfile", "worker: rake jobs:work\nweb: bundle exec rails server\n")
    chosen = discover_commands(tmp_path)
    assert chosen is not None
    assert chosen.source == "Procfile:web"
    assert chosen.command == "bundle exec rails server"


def test_discover_uses_bernstein_yaml_last(tmp_path: Path) -> None:
    """``bernstein.yaml :: preview.command`` is the explicit fallback."""
    _write(tmp_path / "bernstein.yaml", "preview:\n  command: ./scripts/serve.sh\n")
    chosen = discover_commands(tmp_path)
    assert chosen is not None
    assert chosen.source == "bernstein.yaml"
    assert chosen.command == "./scripts/serve.sh"


def test_discover_precedence_package_json_beats_procfile(tmp_path: Path) -> None:
    """``package.json`` precedes ``Procfile``."""
    _write(tmp_path / "package.json", json.dumps({"scripts": {"dev": "vite"}}))
    _write(tmp_path / "Procfile", "web: bundle exec rails server\n")
    chosen = discover_commands(tmp_path)
    assert chosen is not None
    assert chosen.source == "package.json:dev"


def test_list_candidates_includes_tool_versions_metadata(tmp_path: Path) -> None:
    """``.tool-versions`` rows surface as non-runnable entries."""
    _write(tmp_path / ".tool-versions", "nodejs 20.10.0\npython 3.12.0\n")
    rows = list_candidates(tmp_path)
    sources = [r.source for r in rows]
    assert sources == [".tool-versions", ".tool-versions"]
    assert all(not r.is_runnable() for r in rows)


def test_returns_none_when_nothing_matches(tmp_path: Path) -> None:
    """Empty directories yield no runnable command."""
    assert discover_commands(tmp_path) is None


def test_malformed_package_json_does_not_raise(tmp_path: Path) -> None:
    """A broken JSON file is silently skipped (discovery is non-fatal)."""
    _write(tmp_path / "package.json", "not-json")
    assert discover_commands(tmp_path) is None
