"""Tests for the ``bernstein audit`` command dark paths.

Complements ``test_audit_archive_cmd`` / ``test_audit_slice`` by covering the
read/verify/seal/query surface:

  * ``audit show``        - empty states + populated render + --limit
  * ``audit verify-hmac`` - pass (real chain) + fail (tampered chain)
  * ``audit verify``      - both, --hmac-only, --merkle-only, missing dir
  * ``audit seal``        - Merkle root computation + missing dir
  * ``audit query``       - filter by event-type / actor, no-match, missing dir
  * ``audit capabilities``- capability matrix render
  * pure helpers          - ``_parse_filename_date`` / ``_sha256_of_file``
  * help surfaces         - subcommand + flag presence

The seeded fixtures use the public canonical-HMAC math so the chain verifies
against the same tmp key the command resolves - no monkeypatching of internal
verify logic.
"""

from __future__ import annotations

import hashlib
import json
import stat
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.audit_cmd import (
    _parse_filename_date,
    _sha256_of_file,
    audit_group,
)
from bernstein.core.security.audit import AUDIT_KEY_ENV

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run inside an isolated cwd with a pinned tmp HMAC key.

    ``AUDIT_DIR`` in the command module is the relative ``Path(".sdd/audit")``;
    chdir-ing into ``tmp_path`` makes every ``.is_dir()`` resolve there, so the
    real audit dir under the operator's home is never touched.
    """
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"a" * 64)
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv(AUDIT_KEY_ENV, str(key_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _seed_chain(audit_dir: Path, day: str, rows: list[tuple[str, str]]) -> None:
    """Seed ``<audit_dir>/<day>.jsonl`` with a valid HMAC chain.

    ``rows`` is a list of ``(event_type, actor)`` tuples.
    """
    from bernstein.core.security.audit import _compute_hmac, load_or_create_audit_key

    key = load_or_create_audit_key()
    prev = "0" * 64
    audit_dir.mkdir(parents=True, exist_ok=True)
    with (audit_dir / f"{day}.jsonl").open("a", encoding="utf-8", newline="") as fh:
        for i, (event_type, actor) in enumerate(rows):
            entry = {
                "timestamp": f"{day}T00:00:{i:02d}.000000Z",
                "event_type": event_type,
                "actor": actor,
                "resource_type": "task",
                "resource_id": f"id-{i}",
                "details": {"i": i},
                "prev_hmac": prev,
            }
            h = _compute_hmac(key, prev, entry)
            entry["hmac"] = h
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
            prev = h


def _seed_corrupt(audit_dir: Path, day: str, count: int) -> None:
    """Seed a chain whose HMACs were written under a different key."""
    from bernstein.core.security.audit import _compute_hmac

    wrong_key = b"z" * 64
    prev = "0" * 64
    audit_dir.mkdir(parents=True, exist_ok=True)
    with (audit_dir / f"{day}.jsonl").open("a", encoding="utf-8", newline="") as fh:
        for i in range(count):
            entry = {
                "timestamp": f"{day}T00:00:{i:02d}.000000Z",
                "event_type": "x",
                "actor": "a",
                "resource_type": "task",
                "resource_id": f"id-{i}",
                "details": {},
                "prev_hmac": prev,
            }
            h = _compute_hmac(wrong_key, prev, entry)
            entry["hmac"] = h
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
            prev = h


# ---------------------------------------------------------------------------
# audit show
# ---------------------------------------------------------------------------


def test_show_no_audit_dir() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(audit_group, ["show"])
    assert result.exit_code == 0, result.output
    assert "No audit log found" in result.output


def test_show_empty_audit_dir(isolated_audit: Path) -> None:
    (isolated_audit / ".sdd" / "audit").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(audit_group, ["show"])
    assert result.exit_code == 0, result.output
    assert "no log files" in result.output.lower()


def test_show_renders_events_and_limit(isolated_audit: Path) -> None:
    _seed_chain(
        isolated_audit / ".sdd" / "audit",
        "2026-05-14",
        [("task.created", "alice"), ("agent.spawned", "bob"), ("task.done", "carol")],
    )
    runner = CliRunner()
    result = runner.invoke(audit_group, ["show", "--limit", "2"])
    assert result.exit_code == 0, result.output
    assert "task.created" in result.output
    # --limit 2 caps the count line.
    assert "Showing 2 event" in result.output


# ---------------------------------------------------------------------------
# audit verify-hmac
# ---------------------------------------------------------------------------


def test_verify_hmac_missing_dir_exits_one() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(audit_group, ["verify-hmac"])
    assert result.exit_code == 1, result.output
    assert "not found" in result.output.lower()


def test_verify_hmac_clean_chain_passes(isolated_audit: Path) -> None:
    _seed_chain(isolated_audit / ".sdd" / "audit", "2026-05-14", [("task.created", "alice")])
    runner = CliRunner()
    result = runner.invoke(audit_group, ["verify-hmac"])
    assert result.exit_code == 0, result.output
    assert "Passed" in result.output


def test_verify_hmac_tampered_chain_fails(isolated_audit: Path) -> None:
    _seed_corrupt(isolated_audit / ".sdd" / "audit", "2026-05-14", count=2)
    runner = CliRunner()
    result = runner.invoke(audit_group, ["verify-hmac"])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output


# ---------------------------------------------------------------------------
# audit verify (combined / scoped)
# ---------------------------------------------------------------------------


def test_verify_missing_dir_exits_one() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(audit_group, ["verify"])
    assert result.exit_code == 1, result.output
    assert "not found" in result.output.lower()


def test_verify_hmac_only_passes_on_clean_chain(isolated_audit: Path) -> None:
    _seed_chain(isolated_audit / ".sdd" / "audit", "2026-05-14", [("task.created", "alice")])
    runner = CliRunner()
    result = runner.invoke(audit_group, ["verify", "--hmac-only"])
    assert result.exit_code == 0, result.output
    assert "HMAC Chain Verification Passed" in result.output


def test_verify_hmac_only_fails_on_tampered_chain(isolated_audit: Path) -> None:
    _seed_corrupt(isolated_audit / ".sdd" / "audit", "2026-05-14", count=2)
    runner = CliRunner()
    result = runner.invoke(audit_group, ["verify", "--hmac-only"])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output


# ---------------------------------------------------------------------------
# audit seal
# ---------------------------------------------------------------------------


def test_seal_missing_dir_exits_one() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(audit_group, ["seal"])
    assert result.exit_code == 1, result.output
    assert "not found" in result.output.lower()


def test_seal_computes_root_hash(isolated_audit: Path) -> None:
    _seed_chain(isolated_audit / ".sdd" / "audit", "2026-05-14", [("task.created", "alice")])
    runner = CliRunner()
    result = runner.invoke(audit_group, ["seal"])
    assert result.exit_code == 0, result.output
    assert "Root hash" in result.output
    # The seal file is written under the merkle dir.
    merkle_dir = isolated_audit / ".sdd" / "audit" / "merkle"
    assert merkle_dir.is_dir()
    assert any(merkle_dir.iterdir())


def test_seal_then_verify_merkle_passes(isolated_audit: Path) -> None:
    """A freshly-sealed chain verifies clean under the combined verify."""
    _seed_chain(isolated_audit / ".sdd" / "audit", "2026-05-14", [("task.created", "alice")])
    runner = CliRunner()
    seal_result = runner.invoke(audit_group, ["seal"])
    assert seal_result.exit_code == 0, seal_result.output
    verify_result = runner.invoke(audit_group, ["verify"])
    assert verify_result.exit_code == 0, verify_result.output
    assert "Merkle Verification Passed" in verify_result.output


def test_seal_refuses_broken_chain(isolated_audit: Path) -> None:
    """`audit seal` aborts on a broken HMAC chain so a tamper can't be sealed over."""
    _seed_corrupt(isolated_audit / ".sdd" / "audit", "2026-05-14", count=2)
    runner = CliRunner()
    result = runner.invoke(audit_group, ["seal"])
    assert result.exit_code == 1, result.output
    assert "chain is broken" in result.output.lower()
    # No seal must have been written.
    merkle_dir = isolated_audit / ".sdd" / "audit" / "merkle"
    assert not merkle_dir.exists() or not any(merkle_dir.glob("seal-*.json"))


def test_seal_allow_broken_chain_overrides(isolated_audit: Path) -> None:
    """--allow-broken-chain seals a known-corrupted log for forensic capture."""
    _seed_corrupt(isolated_audit / ".sdd" / "audit", "2026-05-14", count=2)
    runner = CliRunner()
    result = runner.invoke(audit_group, ["seal", "--allow-broken-chain"])
    assert result.exit_code == 0, result.output
    assert "Root hash" in result.output
    merkle_dir = isolated_audit / ".sdd" / "audit" / "merkle"
    assert any(merkle_dir.glob("seal-*.json"))


# ---------------------------------------------------------------------------
# audit query
# ---------------------------------------------------------------------------


def test_query_missing_dir_exits_one() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(audit_group, ["query"])
    assert result.exit_code == 1, result.output
    assert "not found" in result.output.lower()


def test_query_filters_by_event_type(isolated_audit: Path) -> None:
    _seed_chain(
        isolated_audit / ".sdd" / "audit",
        "2026-05-14",
        [("task.created", "alice"), ("agent.spawned", "bob"), ("task.created", "alice")],
    )
    runner = CliRunner()
    result = runner.invoke(audit_group, ["query", "--event-type", "task.created"])
    assert result.exit_code == 0, result.output
    assert "alice" in result.output
    # The agent.spawned actor "bob" should be filtered out.
    assert "bob" not in result.output


def test_query_filters_by_actor(isolated_audit: Path) -> None:
    _seed_chain(
        isolated_audit / ".sdd" / "audit",
        "2026-05-14",
        [("task.created", "alice"), ("agent.spawned", "bob")],
    )
    runner = CliRunner()
    result = runner.invoke(audit_group, ["query", "--actor", "bob"])
    assert result.exit_code == 0, result.output
    assert "agent.spawned" in result.output
    assert "task.created" not in result.output


def test_query_no_match_reports_yellow(isolated_audit: Path) -> None:
    _seed_chain(isolated_audit / ".sdd" / "audit", "2026-05-14", [("task.created", "alice")])
    runner = CliRunner()
    result = runner.invoke(audit_group, ["query", "--actor", "nobody-here"])
    assert result.exit_code == 0, result.output
    assert "No matching audit events found" in result.output


# ---------------------------------------------------------------------------
# audit capabilities
# ---------------------------------------------------------------------------


def test_capabilities_renders_matrix(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(audit_group, ["capabilities", "--workdir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Tool Capability Matrix" in result.output
    assert "tool(s) declared" in result.output


def test_capabilities_no_violations_on_empty_runtime(tmp_path: Path) -> None:
    """With no recorded spawn manifests there are no trifecta violations."""
    runner = CliRunner()
    result = runner.invoke(audit_group, ["capabilities", "--workdir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "No lethal-trifecta violations" in result.output


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_parse_filename_date_valid() -> None:
    assert _parse_filename_date("2026-05-14.jsonl") == date(2026, 5, 14)


def test_parse_filename_date_invalid_returns_none() -> None:
    assert _parse_filename_date("not-a-date.jsonl") is None
    # Out-of-range month/day is also rejected.
    assert _parse_filename_date("2026-13-99.jsonl") is None


def test_sha256_of_file_matches_hashlib(tmp_path: Path) -> None:
    payload = b"audit chain bytes"
    f = tmp_path / "blob.bin"
    f.write_bytes(payload)
    assert _sha256_of_file(f) == hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# help surfaces
# ---------------------------------------------------------------------------


def test_audit_group_help_lists_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(audit_group, ["--help"])
    assert result.exit_code == 0, result.output
    for sub in ("show", "seal", "verify", "verify-hmac", "export", "query", "slice", "archive"):
        assert sub in result.output, f"missing subcommand {sub} in audit --help"


def test_audit_verify_help_lists_flags() -> None:
    runner = CliRunner()
    result = runner.invoke(audit_group, ["verify", "--help"])
    assert result.exit_code == 0, result.output
    assert "--merkle-only" in result.output
    assert "--hmac-only" in result.output
