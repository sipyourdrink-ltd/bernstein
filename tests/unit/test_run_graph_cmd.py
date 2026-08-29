"""``bernstein worktrees graph`` - what an operator can trust it to say (#3761).

Each test is named for a property, because the failure that matters here is
not "the command crashed" but "the command said something reassuring about a
fan-out it could not actually check".

The fixture builds real git repositories for the branches rather than
injecting a head resolver: the command resolves heads through git in
production, and a fan-out whose heads are stubbed would not exercise the path
that actually runs.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.run_graph_cmd import resolve_receipt_path, run_graph_cmd
from bernstein.core.lineage.run_graph import (
    build_run_graph,
    build_run_graph_receipt,
    verify_run_graph_receipt,
)
from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.security.agent_card_signer import generate_ed25519_keypair

TIMESTAMP = 1_700_000_000
SESSIONS = ("sess-alpha", "sess-beta")
RUN_IDS = {session: f"run-{session.split('-')[1]}" for session in SESSIONS}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(cwd),
        },
    )


@pytest.fixture
def keypair() -> tuple[bytes, bytes]:
    private, public = generate_ed25519_keypair()
    return private, public


@pytest.fixture
def hmac_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """The key the command itself will resolve, not one chosen here.

    The command opens spines with the audit key, so a fixture that sealed
    them with some other key would have every branch fail for a reason that
    has nothing to do with what these tests are about -- and one of them would
    still pass, because a test asserting "this branch failed" cannot tell a
    tampered branch from an unreadable one.
    """
    from bernstein.core.security.audit import load_or_create_audit_key

    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    return load_or_create_audit_key()


@pytest.fixture
def sealed(tmp_path: Path, keypair: tuple[bytes, bytes], hmac_key: bytes) -> tuple[Path, Path, Path]:
    """A workdir with two branches, their spines, and one sealed receipt.

    Returns ``(workdir, receipt_path, public_key_path)``.
    """
    private_key_pem, public_key_pem = keypair
    workdir = tmp_path / "repo"
    worktrees = workdir / ".sdd" / "runtime" / "worktrees"
    worktrees.mkdir(parents=True)
    lineage_root = workdir / ".sdd" / "lineage"

    for session in SESSIONS:
        branch = worktrees / session
        branch.mkdir()
        _git(branch, "init", "-q", "-b", "main")
        (branch / "out.txt").write_text(session, encoding="utf-8")
        _git(branch, "add", "out.txt")
        _git(branch, "commit", "-q", "-m", f"work in {session}")
        spine = LineageSpine(lineage_root, run_id=RUN_IDS[session], hmac_key=hmac_key)
        spine.record(
            artifact_path=f"out/{session}.txt",
            content=session.encode(),
            actor="tester",
            step_id="step-1",
            model="test-model",
            timestamp=TIMESTAMP,
        )

    graph = build_run_graph(workdir, run_ids=RUN_IDS, lineage_root=lineage_root, hmac_key=hmac_key)
    receipt = build_run_graph_receipt(
        graph=graph,
        workdir=workdir,
        lineage_root=lineage_root,
        hmac_key=hmac_key,
        timestamp=TIMESTAMP,
        private_key_pem=private_key_pem,
        chain=None,
    )
    receipt_path = workdir / ".sdd" / "run-graph" / f"{receipt.receipt_hash}.json"
    public_key_path = tmp_path / "public.pem"
    public_key_path.write_bytes(public_key_pem)
    return workdir, receipt_path, public_key_path


def _tamper(workdir: Path, session: str) -> None:
    """Edit a row inside one branch's spine, leaving its stored head alone.

    This is the edit a node-hash comparison cannot see: the hash carries the
    spine's stored head, and rewriting a row does not rewrite that head.
    """
    lineage_root = workdir / ".sdd" / "lineage"
    journal = next((lineage_root / RUN_IDS[session]).rglob("*.jsonl"))
    raw = journal.read_bytes()
    assert b'"actor":"tester"' in raw, "fixture no longer carries the field this test edits"
    journal.write_bytes(raw.replace(b'"actor":"tester"', b'"actor":"testeR"'))


def _run(args: list[str]) -> tuple[int, str]:
    result = CliRunner().invoke(run_graph_cmd, args, catch_exceptions=False)
    return result.exit_code, result.output


def _run_id_args() -> list[str]:
    return [arg for session, run_id in RUN_IDS.items() for arg in ("--run-id", f"{session}={run_id}")]


# ---------------------------------------------------------------------------
# What --json emits
# ---------------------------------------------------------------------------


def test_the_json_output_is_a_receipt_that_still_verifies(
    sealed: tuple[Path, Path, Path], keypair: tuple[bytes, bytes], hmac_key: bytes, tmp_path: Path
) -> None:
    """--json emits the signed artifact, not a re-encoding of it.

    Round-tripping it through verification is the check that matters: a
    command that pretty-printed or reordered the JSON would still look right
    and would no longer carry a signature anyone could check.
    """
    workdir, receipt_path, _ = sealed
    _public = keypair[1]

    code, output = _run([receipt_path.stem, "--json", "--workdir", str(workdir)])
    assert code == 0

    round_tripped = tmp_path / "round-tripped.json"
    round_tripped.write_text(output.strip(), encoding="utf-8")
    result = verify_run_graph_receipt(
        receipt_path=round_tripped,
        repo_root=workdir,
        run_ids=RUN_IDS,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=hmac_key,
        public_key_pem=_public,
    )
    assert result.ok, result.reason
    assert result.status == "verified"


# ---------------------------------------------------------------------------
# What the default rendering says
# ---------------------------------------------------------------------------


def test_a_tampered_branch_is_named_in_the_default_rendering(sealed: tuple[Path, Path, Path]) -> None:
    """The offending branch is named, and the command exits non-zero.

    "Something in this fan-out is wrong" is not actionable; the session id is.
    """
    workdir, receipt_path, _ = sealed
    _tamper(workdir, "sess-beta")

    code, output = _run([receipt_path.stem, "--workdir", str(workdir), *_run_id_args()])
    assert code == 1
    assert "sess-beta" in output
    assert "FAILED" in output


def test_an_untampered_fan_out_renders_every_branch_as_ok(sealed: tuple[Path, Path, Path]) -> None:
    """The healthy case has to be quiet, or the failing case says nothing."""
    workdir, receipt_path, _ = sealed

    code, output = _run([receipt_path.stem, "--workdir", str(workdir), *_run_id_args()])
    assert code == 0
    assert "FAILED" not in output
    for session in SESSIONS:
        assert session in output


def test_a_branch_with_no_run_id_is_unresolved_rather_than_failing(sealed: tuple[Path, Path, Path]) -> None:
    """A missing input is not evidence against the branch.

    Called failing, it would put a red line next to a branch nobody has
    checked -- and an operator who learns that red lines are often noise stops
    reading the one that is not.
    """
    workdir, receipt_path, _ = sealed

    code, output = _run([receipt_path.stem, "--workdir", str(workdir), "--run-id", "sess-alpha=run-alpha"])
    assert code == 0
    assert "unresolved" in output
    assert "sess-beta" in output
    assert "FAILED" not in output


# ---------------------------------------------------------------------------
# Naming a fan-out
# ---------------------------------------------------------------------------


def test_a_unique_prefix_names_the_fan_out(sealed: tuple[Path, Path, Path]) -> None:
    """A full sha256 is not something an operator retypes."""
    workdir, receipt_path, _ = sealed
    prefix = receipt_path.stem.removeprefix("sha256:")[:8]

    code, output = _run([prefix, "--json", "--workdir", str(workdir)])
    assert code == 0
    assert json.loads(output.strip())["receipt_hash"] == receipt_path.stem


def test_an_ambiguous_prefix_lists_its_candidates_instead_of_choosing(sealed: tuple[Path, Path, Path]) -> None:
    """Picking the first match would render a different fan-out silently."""
    _workdir, receipt_path, _ = sealed
    twin = receipt_path.with_name(f"{receipt_path.stem}0.json")
    twin.write_text(receipt_path.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(Exception, match="matches 2 fan-outs"):
        resolve_receipt_path(receipt_path.parent, receipt_path.stem.removeprefix("sha256:")[:8])


def test_an_unknown_id_says_so_rather_than_rendering_nothing(sealed: tuple[Path, Path, Path]) -> None:
    """An empty render reads as an empty fan-out, which is a different fact."""
    _workdir, receipt_path, _ = sealed

    with pytest.raises(Exception, match="no sealed fan-out matches"):
        resolve_receipt_path(receipt_path.parent, "deadbeef")


# ---------------------------------------------------------------------------
# --verify
# ---------------------------------------------------------------------------


def test_verify_confirms_a_receipt_against_the_tree_it_sealed(sealed: tuple[Path, Path, Path]) -> None:
    workdir, receipt_path, public_key_path = sealed

    code, output = _run(
        [
            receipt_path.stem,
            "--verify",
            "--public-key",
            str(public_key_path),
            "--workdir",
            str(workdir),
            *_run_id_args(),
        ]
    )
    assert code == 0
    assert "verified" in output


def test_verify_without_a_public_key_refuses_rather_than_reporting_a_bad_signature(
    sealed: tuple[Path, Path, Path],
) -> None:
    """No key means the signature was not checked, which is not the same as a
    signature that failed -- and the second reads as tampering."""
    workdir, receipt_path, _ = sealed

    with pytest.raises(Exception, match="needs --public-key"):
        CliRunner().invoke(
            run_graph_cmd,
            [receipt_path.stem, "--verify", "--workdir", str(workdir), *_run_id_args()],
            catch_exceptions=False,
            standalone_mode=False,
        )
