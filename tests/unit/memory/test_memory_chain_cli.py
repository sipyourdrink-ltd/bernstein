"""Tests for the ``bernstein memory verify / why / forget`` CLI surface (issue #2298).

Exercises the click subcommands end to end against a real lineage spine
and memory chain in a temp workdir, pinning the exit-code contract and
the ``why`` provenance answer.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.memory_cmd import memory_group
from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.memory.chain import MemoryChain, MemoryScope

_KEY = b"k" * 32


def _seed(workdir: Path, *, claim: str = "prefers dark mode") -> str:
    """Write a spine entry and a memory record; return the memory entry hash."""
    spine = LineageSpine(workdir / ".sdd" / "lineage", run_id="run-42", hmac_key=_KEY)
    src = spine.record(
        artifact_path="src/pref.py",
        content=b"c",
        actor="agent:worker",
        step_id="step-7",
        model="claude",
        timestamp=1,
    )
    chain = MemoryChain(workdir / ".sdd" / "memory" / "chain", hmac_key=_KEY)
    entry = chain.write(
        scope=MemoryScope.USER,
        namespace="alex",
        claim=claim,
        actor="agent:worker",
        source_hash=src,
        run_id="run-42",
        step_id="step-7",
        model="claude",
        timestamp=100,
    )
    return entry.entry_hash


def _run(args: list[str], workdir: Path) -> object:
    runner = CliRunner()
    env = {"BERNSTEIN_AUDIT_KEY_PATH": str(workdir / "audit.key")}
    (workdir / "audit.key").write_bytes(_KEY)
    (workdir / "audit.key").chmod(0o600)
    return runner.invoke(memory_group, args, env=env, catch_exceptions=False)


def test_verify_ok_exit_zero(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = _run(
        ["verify", "--scope", "user", "--namespace", "alex", "--workdir", str(tmp_path)],
        tmp_path,
    )
    assert result.exit_code == 0
    assert "OK" in result.output


def test_verify_no_entries_exit_one(tmp_path: Path) -> None:
    (tmp_path / ".sdd").mkdir()
    result = _run(
        ["verify", "--scope", "user", "--namespace", "nobody", "--workdir", str(tmp_path)],
        tmp_path,
    )
    assert result.exit_code == 1
    assert "NO ENTRIES" in result.output


def test_verify_tamper_exit_two(tmp_path: Path) -> None:
    _seed(tmp_path)
    path = tmp_path / ".sdd" / "memory" / "chain" / "user" / "alex.jsonl"
    path.write_text(path.read_text().replace("prefers dark mode", "prefers light mode"))
    result = _run(
        ["verify", "--scope", "user", "--namespace", "alex", "--workdir", str(tmp_path)],
        tmp_path,
    )
    assert result.exit_code == 2
    assert "TAMPER" in result.output


def test_why_prints_run_and_step(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = _run(
        [
            "why",
            "prefers dark mode",
            "--scope",
            "user",
            "--namespace",
            "alex",
            "--workdir",
            str(tmp_path),
        ],
        tmp_path,
    )
    assert result.exit_code == 0
    assert "run-42" in result.output
    assert "step-7" in result.output


def test_why_unknown_fact_exit_one(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = _run(
        [
            "why",
            "never stored",
            "--scope",
            "user",
            "--namespace",
            "alex",
            "--workdir",
            str(tmp_path),
        ],
        tmp_path,
    )
    assert result.exit_code == 1


def test_forget_appends_tombstone_and_stays_verifiable(tmp_path: Path) -> None:
    entry_hash = _seed(tmp_path)
    result = _run(
        [
            "forget",
            entry_hash,
            "--scope",
            "user",
            "--namespace",
            "alex",
            "--workdir",
            str(tmp_path),
        ],
        tmp_path,
    )
    assert result.exit_code == 0
    # Chain still verifies after the tombstone.
    verify = _run(
        ["verify", "--scope", "user", "--namespace", "alex", "--workdir", str(tmp_path)],
        tmp_path,
    )
    assert verify.exit_code == 0
    chain = MemoryChain(tmp_path / ".sdd" / "memory" / "chain", hmac_key=_KEY)
    assert entry_hash in chain.forgotten_hashes(MemoryScope.USER, "alex")


# ---------------------------------------------------------------------------
# ``memory show`` -- the folded current state with per-claim provenance (#2914)
# ---------------------------------------------------------------------------


def test_show_prints_live_claims_with_their_provenance(tmp_path: Path) -> None:
    """``show`` must answer "what does this namespace say, and where did
    each line come from" without the operator reading the JSONL by hand."""
    _seed(tmp_path)
    result = _run(
        ["show", "--scope", "user", "--namespace", "alex", "--workdir", str(tmp_path)],
        tmp_path,
    )
    assert result.exit_code == 0
    assert "prefers dark mode" in result.output
    assert "run-42" in result.output
    assert "step-7" in result.output
    assert "agent:worker" in result.output


def test_show_json_emits_the_canonical_fold_bytes_verbatim(tmp_path: Path) -> None:
    """``--json`` must emit exactly the canonical fold bytes, so a caller
    can hash or diff the projection instead of re-deriving it."""
    _seed(tmp_path)
    result = _run(
        ["show", "--scope", "user", "--namespace", "alex", "--workdir", str(tmp_path), "--json"],
        tmp_path,
    )
    assert result.exit_code == 0
    chain = MemoryChain(tmp_path / ".sdd" / "memory" / "chain", hmac_key=_KEY)
    expected = chain.fold_bytes(MemoryScope.USER, "alex")
    assert result.output.strip().encode("utf-8") == expected


def test_show_omits_a_tombstoned_claim(tmp_path: Path) -> None:
    """A forgotten claim must leave the current state even though its
    record stays in the chain."""
    entry_hash = _seed(tmp_path)
    forget = _run(
        ["forget", entry_hash, "--scope", "user", "--namespace", "alex", "--workdir", str(tmp_path)],
        tmp_path,
    )
    assert forget.exit_code == 0
    result = _run(
        ["show", "--scope", "user", "--namespace", "alex", "--workdir", str(tmp_path)],
        tmp_path,
    )
    assert "prefers dark mode" not in result.output
    assert result.exit_code == 1


def test_show_on_an_empty_namespace_exits_one(tmp_path: Path) -> None:
    """Nothing live is exit 1, matching ``verify``'s NO ENTRIES contract."""
    (tmp_path / ".sdd").mkdir()
    result = _run(
        ["show", "--scope", "user", "--namespace", "nobody", "--workdir", str(tmp_path)],
        tmp_path,
    )
    assert result.exit_code == 1
