"""Tests for the ``bernstein replay debug`` CLI dispatch (#2605).

These exercise :func:`bernstein.cli.commands.replay_cmd.replay_debug`
directly (exit codes + on-disk artifacts) and, for the fork-anchoring and
tamper-refusal acceptance criteria, drive the full pieces the command wires
together. The forensic contract:

* a tampered chain is refused before any success artifact and the mismatch
  is localised to the exact seq;
* the debug receipt is offline-verifiable via ``replay verify`` and fails
  when the chain is stripped or corrupted;
* the two-run path-diff artifact is byte-identical across invocations;
* ``--fork-from`` anchors the fork to the parent ``step_hash`` at seq N.
"""

from __future__ import annotations

import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from bernstein.cli.commands.replay_cmd import replay_debug, replay_verify
from bernstein.core.persistence.journal import Journal, JournalReader
from bernstein.core.persistence.lineage_signer import Ed25519FileKeySigner

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _populate(sdd_dir: Path, run: str, n: int = 4) -> str:
    journal_dir = sdd_dir / "runtime" / "journal" / run
    journal = Journal.open(journal_dir)
    head = ""
    for i in range(n):
        entry = journal.append(
            input_hash=f"a{i}",
            model="m1",
            prompt=f"step {i}",
            tool_call={"name": "noop"},
            tool_result={"ok": True, "n": i},
        )
        head = entry.step_hash
    journal.close()
    return head


def _tamper_stored_step_hash(sdd_dir: Path, run: str, seq: int) -> None:
    bucket = sdd_dir / "runtime" / "journal" / run / "000000.jsonl"
    lines = bucket.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[seq])
    row["step_hash"] = "f" * 64
    lines[seq] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    bucket.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _two_runs_diverging_at(sdd_dir: Path, seq: int) -> tuple[str, str]:
    left, right = "run-left", "run-right"
    jl = Journal.open(sdd_dir / "runtime" / "journal" / left)
    jr = Journal.open(sdd_dir / "runtime" / "journal" / right)
    for i in range(4):
        jl.append(input_hash=f"a{i}", model="m1", prompt=f"p{i}", tool_result={"n": i})
        jr.append(input_hash=f"a{i}", model="m1", prompt=f"p{i}", tool_result={"n": i if i != seq else 999})
    jl.close()
    jr.close()
    return left, right


# ---------------------------------------------------------------------------
# AC1 + AC2: tamper refusal before output, single-step localization
# ---------------------------------------------------------------------------


class TestSingleRun:
    def test_clean_chain_emits_verifiable_receipt(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        sdd = tmp_path / ".sdd"
        head = _populate(sdd, "run-1", 4)
        rc = replay_debug(["run-1"], sdd, as_json=True)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["verified"] is True
        assert payload["head_hash"] == head
        receipt_path = Path(payload["receipt"]["path"])
        assert receipt_path.exists()
        # AC6: the debug receipt verifies offline via the replay verify path.
        assert replay_verify(receipt_path, expected_head=head, public_key_path=None) == 0

    def test_tampered_chain_refused_before_output_and_localized(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sdd = tmp_path / ".sdd"
        _populate(sdd, "run-1", 4)
        _tamper_stored_step_hash(sdd, "run-1", seq=2)

        rc = replay_debug(["run-1"], sdd, as_json=True)
        assert rc == 1  # refused, non-zero exit
        payload = json.loads(capsys.readouterr().out)
        assert payload["verified"] is False
        # Localised to exactly seq 2, naming the divergent field.
        assert payload["seq"] == 2
        assert payload["first_divergent_field"] == "step_hash"
        # No success receipt is emitted for a tampered chain.
        assert "receipt" not in payload
        assert not (sdd / "runtime" / "receipts" / "run-1.debug.tar").exists()

    def test_missing_journal_returns_2(self, tmp_path: Path) -> None:
        assert replay_debug(["nope"], tmp_path / ".sdd", as_json=True) == 2


# ---------------------------------------------------------------------------
# AC6: receipt fails verification once the chain is stripped / corrupted
# ---------------------------------------------------------------------------


class TestReceiptForensics:
    def test_receipt_fails_when_chain_stripped(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        sdd = tmp_path / ".sdd"
        _populate(sdd, "run-1", 3)
        rc = replay_debug(["run-1"], sdd, as_json=True)
        assert rc == 0
        receipt_path = Path(json.loads(capsys.readouterr().out)["receipt"]["path"])

        # Strip the journal member: the receipt is meaningless without the chain.
        stripped = tmp_path / "stripped.tar"
        with tarfile.open(receipt_path, "r") as src, tarfile.open(stripped, "w") as dst:
            for member in src.getmembers():
                if member.name.startswith("journal/"):
                    continue
                fileobj = src.extractfile(member)
                dst.addfile(member, fileobj)
        assert replay_verify(stripped, expected_head=None, public_key_path=None) == 1

    def test_receipt_fails_when_chain_corrupted(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        sdd = tmp_path / ".sdd"
        head = _populate(sdd, "run-1", 3)
        rc = replay_debug(["run-1"], sdd, as_json=True)
        assert rc == 0
        receipt_path = Path(json.loads(capsys.readouterr().out)["receipt"]["path"])

        extract = tmp_path / "x"
        with tarfile.open(receipt_path) as tar:
            tar.extractall(extract, filter="data")
        bucket = next(extract.rglob("*.jsonl"))
        lines = bucket.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0])
        row["prompt"] = "EVIL"
        lines[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        bucket.write_text("\n".join(lines) + "\n", encoding="utf-8")
        repacked = tmp_path / "repacked.tar"
        with tarfile.open(repacked, "w") as tar:
            for item in sorted(extract.rglob("*")):
                tar.add(item, arcname=item.relative_to(extract))
        assert replay_verify(repacked, expected_head=head, public_key_path=None) == 1

    def test_sign_passthrough_signs_and_verifies_with_public_key(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        key_path = tmp_path / "sign.key"
        key_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        pub_path = tmp_path / "sign.pub"
        pub_path.write_bytes(
            key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        )

        sdd = tmp_path / ".sdd"
        head = _populate(sdd, "run-1", 2)
        rc = replay_debug(["run-1"], sdd, as_json=True, sign_key_path=key_path)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["receipt"]["signed"] is True
        receipt_path = Path(payload["receipt"]["path"])
        # Signed receipt verifies with the public key.
        assert replay_verify(receipt_path, expected_head=head, public_key_path=pub_path) == 0
        # And is rejected when a signature is present but no verifier is given.
        assert replay_verify(receipt_path, expected_head=head, public_key_path=None) == 1

    def test_load_signer_is_reused_from_lineage_signer(self, tmp_path: Path) -> None:
        # Guard against a divergent signer implementation: debug must sign with
        # the same Ed25519FileKeySigner the export path uses.
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        key_path = tmp_path / "k"
        key_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        signer = Ed25519FileKeySigner.from_path(key_path)
        assert signer.sign(b"x")


# ---------------------------------------------------------------------------
# AC3 + AC4: two-run divergence + byte-identical content-addressed artifact
# ---------------------------------------------------------------------------


class TestTwoRun:
    def test_two_run_divergence_localized(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        sdd = tmp_path / ".sdd"
        left, right = _two_runs_diverging_at(sdd, seq=2)
        rc = replay_debug([left, right], sdd, as_json=True)
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["diverged"] is True
        assert payload["divergence"]["seq"] == 2
        assert "tool_result" in payload["divergence"]["fields_changed"]

    def test_path_diff_artifact_byte_identical_across_invocations(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sdd = tmp_path / ".sdd"
        left, right = _two_runs_diverging_at(sdd, seq=1)

        rc1 = replay_debug([left, right], sdd, as_json=True)
        payload1 = json.loads(capsys.readouterr().out)
        artifact_path = Path(payload1["artifact_path"])
        first_bytes = artifact_path.read_bytes()
        first_hash = payload1["diff_hash"]

        rc2 = replay_debug([left, right], sdd, as_json=True)
        payload2 = json.loads(capsys.readouterr().out)
        second_bytes = Path(payload2["artifact_path"]).read_bytes()

        assert rc1 == rc2 == 1
        assert first_bytes == second_bytes
        assert first_hash == payload2["diff_hash"]

    def test_identical_runs_exit_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        sdd = tmp_path / ".sdd"
        _populate(sdd, "run-a", 3)
        _populate(sdd, "run-b", 3)
        rc = replay_debug(["run-a", "run-b"], sdd, as_json=True)
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["diverged"] is False

    def test_jump_to_failure_positions_output_without_changing_artifact(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sdd = tmp_path / ".sdd"
        left, right = _two_runs_diverging_at(sdd, seq=2)

        replay_debug([left, right], sdd, as_json=True)
        base = json.loads(capsys.readouterr().out)

        replay_debug([left, right], sdd, as_json=True, jump_to_failure=True)
        jumped = json.loads(capsys.readouterr().out)

        assert jumped["jump_to_seq"] == 2
        # The presentation flag must not perturb the content-addressed artifact.
        assert jumped["diff_hash"] == base["diff_hash"]


# ---------------------------------------------------------------------------
# AC5: --fork-from anchors to the parent step_hash at seq N
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _parent_with_journal(repo: Path) -> tuple[str, list[str]]:
    from bernstein.core.orchestration.run_session import RunSession, sessions_dir_for

    sdir = sessions_dir_for(repo)
    sdir.mkdir(parents=True, exist_ok=True)
    session = RunSession.create(goal="build", run_seed=42)
    session.tasks = [{"id": "t-1", "role": "backend", "title": "x", "status": "in_progress"}]
    session.save(sdir)

    journal_dir = repo / ".sdd" / "runtime" / "journal" / session.session_id
    journal = Journal.open(journal_dir)
    heads: list[str] = []
    for i in range(4):
        entry = journal.append(input_hash=f"a{i}", model="m1", prompt=f"s{i}", tool_result={"n": i})
        heads.append(entry.step_hash)
    journal.close()
    return session.session_id, heads


class TestForkFrom:
    def test_fork_from_step_anchors_parent_step_hash(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        run, heads = _parent_with_journal(repo)
        sdd = repo / ".sdd"

        rc = replay_debug([run], sdd, as_json=True, fork_from=2, repo_root=repo)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["fork"]["from_step"] == 2
        # The reproduction anchor is the parent step hash at seq 2.
        assert payload["fork"]["parent_step_hash"] == heads[2]

        # And the seeded fork journal head equals that parent step hash.
        fork_worktree = Path(payload["fork"]["fork_worktree"])
        fork_sid = payload["fork"]["fork_session_id"]
        fork_journal = fork_worktree / ".sdd" / "runtime" / "journal" / fork_sid
        reader = JournalReader(fork_journal)
        entries = list(reader.entries())
        assert [e.seq for e in entries] == [0, 1, 2]
        assert entries[-1].step_hash == heads[2]

    def test_fork_from_out_of_range_fails_fast(self, repo: Path) -> None:
        run, _heads = _parent_with_journal(repo)
        sdd = repo / ".sdd"
        rc = replay_debug([run], sdd, as_json=True, fork_from=99, repo_root=repo)
        assert rc == 1
        # No fork worktree was created.
        worktrees = repo / ".sdd" / "worktrees"
        assert not worktrees.exists() or not any(worktrees.iterdir())


# ---------------------------------------------------------------------------
# End-to-end wiring through the ``bernstein replay`` pseudo-group
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_replay_debug_single_run_via_group(self, tmp_path: Path) -> None:
        from bernstein.cli.advanced_cmd import replay_cmd
        from click.testing import CliRunner

        sdd = tmp_path / ".sdd"
        _populate(sdd, "run-1", 3)
        runner = CliRunner()
        result = runner.invoke(replay_cmd, ["debug", "run-1", "--sdd-dir", str(sdd), "--as-json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["verified"] is True
        assert payload["mode"] == "single-run"

    def test_replay_debug_two_run_divergence_via_group_exits_nonzero(self, tmp_path: Path) -> None:
        from bernstein.cli.advanced_cmd import replay_cmd
        from click.testing import CliRunner

        sdd = tmp_path / ".sdd"
        left, right = _two_runs_diverging_at(sdd, seq=1)
        runner = CliRunner()
        result = runner.invoke(replay_cmd, ["debug", left, right, "--sdd-dir", str(sdd), "--as-json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["divergence"]["seq"] == 1
