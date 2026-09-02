"""Staged-write / atomic-promote guarantees for the SOC 2 evidence pack.

``generate_audit_pack`` is a multi-stage producer: it resolves several
independent evidence sources, renders a markdown checklist, builds a JSON
manifest, and then publishes *two* files under the evidence directory.
Consumers of that directory (auditors, CI artefact collectors, GRC sync
jobs) must never observe a generation caught mid-write: what they read is
either a complete pack or the previous complete pack.

These tests pin that guarantee at the filesystem layer - real files, real
directories, a real fault injected between the stages of one run.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bernstein.core.security import audit_pack
from bernstein.core.security.audit_pack import generate_audit_pack

_FIXED_NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
_PERIOD = "2026-Q1"


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    """A project root carrying one real evidence artefact.

    The pack resolves every declared source against this root; sources
    without an artefact resolve to ``PENDING``, which is a valid pack.
    One real file is enough to exercise a non-trivial rendering.
    """
    root = tmp_path / "project"
    root.mkdir()
    (root / "CODE_OF_CONDUCT.md").write_text("# code of conduct\n", encoding="utf-8")
    return root


def _mutate_evidence(workdir: Path, body: str) -> None:
    """Change a resolved evidence artefact so a completed pack would differ."""
    (workdir / "CODE_OF_CONDUCT.md").write_text(body, encoding="utf-8")


@pytest.fixture()
def published(tmp_path: Path) -> Path:
    """The published output directory consumers read from."""
    out = tmp_path / "published"
    out.mkdir()
    return out


def _md_path(published: Path) -> Path:
    return published / f"soc2-evidence-{_PERIOD}.md"


def _manifest_path(published: Path) -> Path:
    return published / f"soc2-evidence-{_PERIOD}.json"


def _run(workdir: Path, published: Path) -> None:
    generate_audit_pack(
        workdir=workdir,
        output_dir=published,
        period_label=_PERIOD,
        now=_FIXED_NOW,
        write=True,
    )


def _break_manifest_serialisation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the run at the manifest-serialisation stage.

    The markdown body is fully rendered (and, on an unfixed tree, already
    written to the published path) before the manifest is serialised, so
    this is a fault *between* the two output stages of a single run.
    """

    def exploding_dumps(obj: Any, **kwargs: Any) -> str:
        if isinstance(obj, dict) and obj.get("report_type") == "soc2_evidence_pack":
            msg = "injected failure while serialising the evidence manifest"
            raise RuntimeError(msg)
        return json.dumps(obj, **kwargs)

    shim = SimpleNamespace(
        dumps=exploding_dumps,
        loads=json.loads,
        JSONDecodeError=json.JSONDecodeError,
    )
    monkeypatch.setattr(audit_pack, "json", shim)


def test_killed_run_between_stages_leaves_published_path_unchanged(
    workdir: Path,
    published: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that dies between stages must publish nothing at all."""
    _break_manifest_serialisation(monkeypatch)

    with pytest.raises(RuntimeError, match="injected failure"):
        _run(workdir, published)

    assert not _md_path(published).exists(), "the markdown was published even though the run never completed"
    assert not _manifest_path(published).exists()
    assert list(published.iterdir()) == [], f"published directory not clean: {list(published.iterdir())}"


def test_failed_run_is_a_clean_noop_for_consumers(
    workdir: Path,
    published: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed re-generation leaves the previous complete pack intact."""
    _run(workdir, published)
    before_md = _md_path(published).read_bytes()
    before_manifest = _manifest_path(published).read_bytes()

    # Change the evidence so a completed second run would differ.
    _mutate_evidence(workdir, "# code of conduct v2\n")
    _break_manifest_serialisation(monkeypatch)
    with pytest.raises(RuntimeError, match="injected failure"):
        _run(workdir, published)

    assert _md_path(published).read_bytes() == before_md
    assert _manifest_path(published).read_bytes() == before_manifest


def test_resumed_run_produces_byte_identical_artefact_to_uninterrupted_run(
    workdir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running after an interruption reproduces the uninterrupted pack."""
    clean_dir = tmp_path / "clean"
    clean_dir.mkdir()
    _run(workdir, clean_dir)
    expected_md = _md_path(clean_dir).read_bytes()
    expected_manifest = _manifest_path(clean_dir).read_bytes()

    resumed_dir = tmp_path / "resumed"
    resumed_dir.mkdir()
    with monkeypatch.context() as interrupted:
        _break_manifest_serialisation(interrupted)
        with pytest.raises(RuntimeError, match="injected failure"):
            _run(workdir, resumed_dir)

    _run(workdir, resumed_dir)

    assert _md_path(resumed_dir).read_bytes() == expected_md
    assert _manifest_path(resumed_dir).read_bytes() == expected_manifest


@pytest.mark.skipif(os.name == "nt", reason="st_ino is not a stable identity on Windows")
def test_publish_replaces_the_file_by_rename_so_readers_never_see_truncation(
    workdir: Path,
    published: Path,
) -> None:
    """Publishing swaps a directory entry instead of truncating in place."""
    _run(workdir, published)
    first_md_ino = _md_path(published).stat().st_ino
    first_manifest_ino = _manifest_path(published).stat().st_ino

    _mutate_evidence(workdir, "# code of conduct v2\n")
    _run(workdir, published)

    assert _md_path(published).stat().st_ino != first_md_ino, (
        "markdown was rewritten in place - a reader can observe a truncated file"
    )
    assert _manifest_path(published).stat().st_ino != first_manifest_ino


def test_successful_run_leaves_no_staging_residue_in_published_directory(
    workdir: Path,
    published: Path,
) -> None:
    """Staging is an implementation detail: consumers see only the pack."""
    _run(workdir, published)

    assert sorted(p.name for p in published.iterdir()) == [
        f"soc2-evidence-{_PERIOD}.json",
        f"soc2-evidence-{_PERIOD}.md",
    ]
    manifest = json.loads(_manifest_path(published).read_text(encoding="utf-8"))
    assert manifest["report_type"] == "soc2_evidence_pack"
    # The published markdown is the exact rendered body, not a partial flush.
    rendered = generate_audit_pack(
        workdir=workdir,
        period_label=_PERIOD,
        now=_FIXED_NOW,
        write=False,
    ).markdown
    assert hashlib.sha256(_md_path(published).read_bytes()).hexdigest() == (
        hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not meaningful on Windows")
def test_published_pack_is_readable_the_way_a_direct_write_left_it(
    workdir: Path,
    published: Path,
) -> None:
    """Staging must not change who can read the published evidence pack."""
    _run(workdir, published)

    assert _md_path(published).stat().st_mode & 0o777 == 0o644
    assert _manifest_path(published).stat().st_mode & 0o777 == 0o644
