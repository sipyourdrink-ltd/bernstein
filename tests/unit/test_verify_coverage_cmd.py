"""Unit tests for the `bernstein verify coverage` subcommand (Issue #5400).

Tests the command line interface for reading merge admission receipts,
recomputing coverage set hashes, and verifying structured coverage sets
(verified, unverified, and skipped paths).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.verify_cmd import verify_cmd
from bernstein.core.quality.merge_receipt import (
    VerificationScope,
    emit_merge_receipt,
    load_or_create_merge_identity,
)


@pytest.fixture(scope="function")
def workdir(tmp_path: Path) -> Path:
    """Create a temporary project root with .sdd directory."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".sdd").mkdir(parents=True)
    (root / ".sdd" / "identity").mkdir(parents=True)
    (root / ".sdd" / "lineage").mkdir(parents=True)
    (root / ".sdd" / "merges" / "receipts").mkdir(parents=True)
    return root


@pytest.fixture(scope="function")
def populated_workdir(workdir: Path) -> Path:
    """Create a working directory with signed merge identity."""
    root = workdir
    private_pem, public_pem = load_or_create_merge_identity(root)
    identity_dir = root / ".sdd" / "identity"
    (identity_dir / "merge-identity-key.pem").write_text(private_pem, encoding="ascii")
    (identity_dir / "merge-identity-public.pem").write_text(public_pem, encoding="ascii")
    return root


def _emit_receipt(root: Path, head_sha: str, merge_base_sha: str = "base_123", **kwargs) -> None:
    """Helper to emit a merge admission receipt."""
    hmac_key = b"x" * 32
    lineage_root = root / ".sdd" / "lineage"
    private_key_pem = (root / ".sdd" / "identity" / "merge-identity-key.pem").read_text(encoding="ascii")
    public_key_pem = (root / ".sdd" / "identity" / "merge-identity-public.pem").read_text(encoding="ascii")

    defaults = dict(
        required_context_ids=("ci/green",),
        blast_radius={
            "score": 0.1,
            "hard_one_way": False,
            "components": [],
            "hits": [],
            "rationale": "clean",
            "files_touched": 2,
            "files": ["src/a.py", "src/b.py"],
        },
        review_verdict="pass",
        ruleset_bytes=b"",
        decision="admit",
        authority="autonomous",
        timestamp=1000,
        change_set=("src/a.py", "src/b.py"),
        scopes=(
            VerificationScope(
                oracle="test",
                checked=("src/a.py", "src/b.py"),
                skipped=(),
            ),
        ),
    )
    defaults.update(kwargs)
    emit_merge_receipt(
        workdir=root,
        lineage_root=lineage_root,
        hmac_key=hmac_key,
        private_key_pem=private_key_pem,
        public_key_pem=public_key_pem,
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
        **defaults,
    )


def test_verify_help_shows_coverage_subcommand() -> None:
    """The verify group help text lists the coverage subcommand."""
    runner = CliRunner()
    result = runner.invoke(verify_cmd, ["--help"])
    assert result.exit_code == 0
    assert "coverage" in result.output
    assert "Inspect merge receipt coverage" in result.output


def test_verify_coverage_help() -> None:
    """The verify coverage help text documents options and exit codes."""
    runner = CliRunner()
    result = runner.invoke(verify_cmd, ["coverage", "--help"])
    assert result.exit_code == 0
    assert "--sha" in result.output
    assert "--workdir" in result.output
    assert "--json" in result.output
    assert "Exit codes:" in result.output


def test_verify_coverage_success_human(populated_workdir: Path) -> None:
    """Running against a valid receipt recomputes hash and exits 0 with table."""
    root = populated_workdir
    head_sha = "sha_success_123"
    _emit_receipt(
        root,
        head_sha,
        change_set=("src/a.py", "src/b.py", "docs/c.md"),
        scopes=(
            VerificationScope(
                oracle="unit-tests",
                checked=("src/a.py",),
                skipped=(("docs/c.md", "docs change"),),
            ),
        ),
        unverified_threshold=1.0,
    )

    runner = CliRunner()
    result = runner.invoke(verify_cmd, ["coverage", head_sha, "-w", str(root)])
    assert result.exit_code == 0, result.output
    assert "Merge Coverage: VERIFIED" in result.output
    assert "src/a.py" in result.output
    assert "docs/c.md (docs change)" in result.output
    assert "src/b.py" in result.output
    assert head_sha in result.output


def test_verify_coverage_success_json(populated_workdir: Path) -> None:
    """Running with --json emits machine-readable JSON object and exits 0."""
    root = populated_workdir
    head_sha = "sha_json_456"
    _emit_receipt(
        root,
        head_sha,
        change_set=("src/a.py", "src/b.py"),
        scopes=(
            VerificationScope(
                oracle="linter",
                checked=("src/a.py", "src/b.py"),
                skipped=(),
            ),
        ),
    )

    runner = CliRunner()
    result = runner.invoke(verify_cmd, ["coverage", head_sha, "-w", str(root), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["status"] == "verified"
    assert data["head_sha"] == head_sha
    assert data["verified"] == ["src/a.py", "src/b.py"]
    assert data["unverified"] == []
    assert data["skipped"] == []
    assert data["coverage_set_hash"].startswith("sha256:")
    assert data["recomputed_hash"] == data["coverage_set_hash"]


def test_verify_coverage_via_sha_option(populated_workdir: Path) -> None:
    """The --sha option is accepted in place of the positional argument."""
    root = populated_workdir
    head_sha = "sha_flag_789"
    _emit_receipt(root, head_sha)

    runner = CliRunner()
    result = runner.invoke(verify_cmd, ["coverage", "--sha", head_sha, "-w", str(root), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["head_sha"] == head_sha


def test_verify_coverage_missing_sha_fails() -> None:
    """Invoking verify coverage with no SHA exits 1."""
    runner = CliRunner()
    result = runner.invoke(verify_cmd, ["coverage"])
    assert result.exit_code == 1
    assert "Commit SHA is required" in result.output

    result_json = runner.invoke(verify_cmd, ["coverage", "--json"])
    assert result_json.exit_code == 1
    data = json.loads(result_json.output)
    assert data["ok"] is False
    assert data["status"] == "missing_sha"


def test_verify_coverage_missing_receipt_fails(populated_workdir: Path) -> None:
    """Invoking verify coverage for a non-existent receipt exits 1."""
    root = populated_workdir
    runner = CliRunner()
    result = runner.invoke(verify_cmd, ["coverage", "nonexistent_sha", "-w", str(root)])
    assert result.exit_code == 1
    assert "NOT FOUND" in result.output
    assert "No merge admission receipt found" in result.output

    result_json = runner.invoke(verify_cmd, ["coverage", "nonexistent_sha", "-w", str(root), "--json"])
    assert result_json.exit_code == 1
    data = json.loads(result_json.output)
    assert data["ok"] is False
    assert data["status"] == "missing"


def test_verify_coverage_v1_receipt_no_coverage_fails(populated_workdir: Path) -> None:
    """A v1 receipt with no coverage sets exits 1 and names the reason."""
    root = populated_workdir
    head_sha = "v1_sha_123"
    safe = hashlib.sha256(head_sha.encode("utf-8")).hexdigest()
    receipt_path = root / ".sdd" / "merges" / "receipts" / f"{safe}.json"

    # Write a v1 receipt without coverage fields
    v1_payload = {
        "v": 1,
        "head_sha": head_sha,
        "merge_base_sha": "base_v1",
        "required_context_ids": ["ci/green"],
        "gate_results_hash": "sha256:abc",
        "ruleset_hash": "sha256:def",
        "review_receipt_id": "",
        "journal_head": "",
        "decision": "admit",
        "authority": "autonomous",
        "timestamp": 1000,
        "signer_public_key_pem": "pub",
        "signature": "sig",
        "journal_entry_hash": "entry",
        "advisory": "",
    }
    receipt_path.write_text(json.dumps(v1_payload), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(verify_cmd, ["coverage", head_sha, "-w", str(root)])
    assert result.exit_code == 1
    assert "NO COVERAGE DATA" in result.output
    assert "schema v1" in result.output

    result_json = runner.invoke(verify_cmd, ["coverage", head_sha, "-w", str(root), "--json"])
    assert result_json.exit_code == 1
    data = json.loads(result_json.output)
    assert data["ok"] is False
    assert data["status"] == "no_coverage_sets"
    assert "schema v1" in data["reason"]


def test_verify_coverage_tamper_mismatch_exits_2(populated_workdir: Path) -> None:
    """A receipt whose coverage_set_hash diverges from its sets exits 2."""
    root = populated_workdir
    head_sha = "tamper_sha_456"
    _emit_receipt(root, head_sha)

    # Tamper with the stored coverage_set_hash
    safe = hashlib.sha256(head_sha.encode("utf-8")).hexdigest()
    receipt_path = root / ".sdd" / "merges" / "receipts" / f"{safe}.json"
    data = json.loads(receipt_path.read_text(encoding="utf-8"))
    data["coverage_set_hash"] = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    receipt_path.write_text(json.dumps(data), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(verify_cmd, ["coverage", head_sha, "-w", str(root)])
    assert result.exit_code == 2
    assert "TAMPER / DIVERGENCE DETECTED" in result.output
    assert "Stored coverage_set_hash" in result.output
    assert "Recomputed coverage_set_hash" in result.output

    result_json = runner.invoke(verify_cmd, ["coverage", head_sha, "-w", str(root), "--json"])
    assert result_json.exit_code == 2
    data_json = json.loads(result_json.output)
    assert data_json["ok"] is False
    assert data_json["status"] == "mismatch"
    assert data_json["expected"] != data_json["recomputed"]
