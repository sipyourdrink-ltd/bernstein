"""Tests for the per-run scorecard (#5404).

Acceptance criteria under test:

1. ``build_run_scorecard`` is deterministic: same ledger, same bytes.
2. The on-disk artifact round-trips through ``read_scorecard_artifact``.
3. Two writes of the same scorecard produce byte-identical files.
4. ``verify_scorecard`` returns ``ok=True`` on a clean ledger and names
   the diverging field(s) on a tampered ledger.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.runs_cmd import runs_group
from bernstein.core.persistence.run_scorecard import (
    SCORECARD_KIND,
    SCORECARD_VERSION,
    RunScorecard,
    VerifyResult,
    build_run_scorecard,
    canonical_json_bytes,
    read_scorecard_artifact,
    verify_scorecard,
    write_scorecard_artifact,
)
from bernstein.core.persistence.work_ledger import (
    KIND_RUN_CLOSED,
    KIND_RUN_OPEN,
    KIND_TASK_COMPLETED,
    KIND_TASK_FAILED,
    KIND_TASK_SCHEDULED,
    KIND_TASK_STARTED,
    WorkLedger,
    run_ledger_dir,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_pr_run(root: Path, run_id: str = "run-pr") -> WorkLedger:
    """A closed run that opened a PR: tasks complete, wrap-up with branch."""
    ledger = WorkLedger.open(run_ledger_dir(root / ".sdd", run_id))
    ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": run_id, "host": "ci-runner-1", "parent_run_id": ""})
    ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t1")
    ledger.append(kind=KIND_TASK_STARTED, task_id="t1")
    ledger.append(kind=KIND_TASK_COMPLETED, task_id="t1")
    ledger.append(
        kind=KIND_RUN_CLOSED,
        payload={"run_id": run_id, "branch": "fix/scorecard", "pr_number": 7, "commits_over_base": 3},
    )
    ledger.close()
    return ledger


def _seed_gate_run(root: Path, run_id: str = "run-gate") -> None:
    """A run that ended on a gate failure."""
    ledger = WorkLedger.open(run_ledger_dir(root / ".sdd", run_id))
    ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": run_id, "host": "ci-runner-2"})
    ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t1")
    ledger.append(kind=KIND_TASK_STARTED, task_id="t1")
    ledger.append(kind=KIND_TASK_FAILED, task_id="t1")
    ledger.append(
        kind=KIND_RUN_CLOSED,
        payload={"run_id": run_id, "gate_name": "lint", "failing_check": "ruff check ."},
    )
    ledger.close()


def _seed_killed_run(root: Path, run_id: str = "run-killed") -> None:
    """A run with no wrap-up -- a killed-mid-flight shape."""
    ledger = WorkLedger.open(run_ledger_dir(root / ".sdd", run_id))
    ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": run_id})
    ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t1")
    ledger.append(kind=KIND_TASK_STARTED, task_id="t1")
    ledger.close()


def _write_cost_row(
    path: Path,
    *,
    run_id: str,
    cost_usd: float,
    task_id: str = "t1",
    ts: float = 1000.0,
) -> None:
    """Append a single cost-ledger row in the canonical ``SpendLedger`` shape."""
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": ts,
        "ts_iso": "2026-01-01T00:00:00+00:00",
        "run_id": run_id,
        "task_id": task_id,
        "agent_id": "agent-1",
        "role": "backend",
        "feature_label": "",
        "model": "sonnet",
        "input_tokens": 50,
        "output_tokens": 100,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": cost_usd,
        "quota_envelope": "subscription",
        "tags": {},
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=False, separators=(",", ":")))
        fh.write("\n")


@pytest.fixture()
def pr_run(tmp_path: Path) -> tuple[Path, str]:
    """A closed PR run with a wrap-up; cost ledger exists with one row."""
    _seed_pr_run(tmp_path, "run-pr")
    cost = tmp_path / ".sdd" / "cost" / "ledger.jsonl"
    _write_cost_row(cost, run_id="run-pr", cost_usd=0.42)
    _write_cost_row(cost, run_id="other", cost_usd=99.0)
    return tmp_path, "run-pr"


# ---------------------------------------------------------------------------
# Canonical encoding
# ---------------------------------------------------------------------------


class TestCanonicalJson:
    def test_stable_key_order_and_compact(self) -> None:
        assert canonical_json_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'

    def test_non_ascii_escaped(self) -> None:
        # ensure_ascii keeps the bytes identical regardless of locale.
        assert canonical_json_bytes({"k": "é"}) == b'{"k":"\\u00e9"}'

    def test_no_whitespace(self) -> None:
        # No spaces or newlines -- the canonical envelope must be single-line.
        out = canonical_json_bytes({"x": 1, "y": [1, 2]})
        assert b" " not in out
        assert b"\n" not in out


# ---------------------------------------------------------------------------
# Build determinism
# ---------------------------------------------------------------------------


class TestBuildDeterminism:
    def test_byte_identical_across_builds(self, pr_run: tuple[Path, str]) -> None:
        """Acceptance 1: same ledger -> same scorecard bytes."""
        root, run_id = pr_run
        with WorkLedger.open(run_ledger_dir(root / ".sdd", run_id)) as j1:
            score_1 = build_run_scorecard(j1)
        with WorkLedger.open(run_ledger_dir(root / ".sdd", run_id)) as j2:
            score_2 = build_run_scorecard(j2)
        assert score_1.sha256 == score_2.sha256
        assert score_1.to_canonical_json() == score_2.to_canonical_json()

    def test_scorecard_sha_matches_canonical_content(self, pr_run: tuple[Path, str]) -> None:
        root, run_id = pr_run
        with WorkLedger.open(run_ledger_dir(root / ".sdd", run_id)) as journal:
            score = build_run_scorecard(journal)
        # The on-disk envelope: {content, sha256}. The sha256 inside the
        # envelope equals the hash of the content; the envelope is
        # exactly what ``to_canonical_json`` returns.
        envelope = {"content": score.content, "sha256": score.sha256}
        assert score.to_canonical_json() == canonical_json_bytes(envelope)
        assert score.sha256 == hashlib.sha256(canonical_json_bytes(score.content)).hexdigest()

    def test_minimum_fields_present(self, pr_run: tuple[Path, str]) -> None:
        root, run_id = pr_run
        with WorkLedger.open(run_ledger_dir(root / ".sdd", run_id)) as journal:
            score = build_run_scorecard(journal)
        for field in (
            "run_id",
            "branch",
            "outcome",
            "evidence",
            "started_at",
            "ended_at",
            "elapsed_seconds",
            "host",
            "parent_run_id",
            "attempt_count",
            "steps",
            "tasks_total",
            "tasks_completed",
            "tasks_failed",
            "tasks_started",
            "cost_usd",
            "scorecard_version",
            "kind",
            "version",
        ):
            assert field in score.content, f"missing field {field!r} in scorecard content"

    def test_scorecard_kind_and_version(self, pr_run: tuple[Path, str]) -> None:
        root, run_id = pr_run
        with WorkLedger.open(run_ledger_dir(root / ".sdd", run_id)) as journal:
            score = build_run_scorecard(journal)
        assert score.content["kind"] == SCORECARD_KIND
        assert score.content["version"] == SCORECARD_VERSION
        assert score.content["scorecard_version"] == SCORECARD_VERSION

    def test_pr_run_classifies_correctly(self, pr_run: tuple[Path, str]) -> None:
        root, run_id = pr_run
        with WorkLedger.open(run_ledger_dir(root / ".sdd", run_id)) as journal:
            score = build_run_scorecard(journal)
        assert score.content["outcome"] == "pr-opened"
        assert score.content["branch"] == "fix/scorecard"
        assert score.content["host"] == "ci-runner-1"
        assert score.content["tasks_total"] == 1
        assert score.content["tasks_completed"] == 1
        assert score.content["tasks_failed"] == 0
        assert score.content["tasks_started"] == 1
        assert score.content["steps"] == 1
        assert score.content["attempt_count"] == 1
        assert score.content["cost_usd"] == pytest.approx(0.42)
        # Cost rows for *other* runs do not bleed into this run's cost.
        assert score.content["cost_usd"] != pytest.approx(99.0 + 0.42)

    def test_gate_run_classifies_correctly(self, tmp_path: Path) -> None:
        _seed_gate_run(tmp_path, "run-gate")
        with WorkLedger.open(run_ledger_dir(tmp_path / ".sdd", "run-gate")) as journal:
            score = build_run_scorecard(journal)
        assert score.content["outcome"] == "gate-failed"
        assert "lint" in score.content["evidence"]
        assert score.content["tasks_failed"] == 1
        assert score.content["tasks_completed"] == 0

    def test_killed_run_classifies_as_infra_error(self, tmp_path: Path) -> None:
        _seed_killed_run(tmp_path, "run-killed")
        with WorkLedger.open(run_ledger_dir(tmp_path / ".sdd", "run-killed")) as journal:
            score = build_run_scorecard(journal)
        assert score.content["outcome"] == "infra-error"
        # A missing cost ledger contributes 0.0, not a crash.
        assert score.content["cost_usd"] == 0.0

    def test_cost_zero_when_no_ledger(self, tmp_path: Path) -> None:
        _seed_pr_run(tmp_path, "run-pr")
        # No cost ledger file at all -- still builds, cost_usd == 0.0.
        with WorkLedger.open(run_ledger_dir(tmp_path / ".sdd", "run-pr")) as journal:
            score = build_run_scorecard(journal)
        assert score.content["cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestArtifactRoundTrip:
    def test_write_creates_content_addressed_file(self, pr_run: tuple[Path, str]) -> None:
        root, run_id = pr_run
        with WorkLedger.open(run_ledger_dir(root / ".sdd", run_id)) as journal:
            score = build_run_scorecard(journal)
        out = write_scorecard_artifact(root, run_id, score)
        assert out.name == f"{score.sha256}.json"
        # The on-disk path matches the spec: .sdd/runs/<id>/scorecard/<sha>.json
        assert out.parent == root / ".sdd" / "runs" / run_id / "scorecard"
        # The bytes on disk are exactly the canonical envelope.
        assert out.read_bytes() == score.to_canonical_json()

    def test_read_round_trip(self, pr_run: tuple[Path, str]) -> None:
        root, run_id = pr_run
        with WorkLedger.open(run_ledger_dir(root / ".sdd", run_id)) as journal:
            score = build_run_scorecard(journal)
        out = write_scorecard_artifact(root, run_id, score)
        restored = read_scorecard_artifact(out)
        assert isinstance(restored, RunScorecard)
        assert restored.sha256 == score.sha256
        assert restored.content == score.content

    def test_two_writes_produce_byte_identical_files(self, pr_run: tuple[Path, str]) -> None:
        """Acceptance 3: re-writing the same scorecard is idempotent."""
        root, run_id = pr_run
        with WorkLedger.open(run_ledger_dir(root / ".sdd", run_id)) as journal:
            score = build_run_scorecard(journal)
        out_1 = write_scorecard_artifact(root, run_id, score)
        out_2 = write_scorecard_artifact(root, run_id, score)
        assert out_1 == out_2  # same path (content-addressed)
        assert out_1.read_bytes() == out_2.read_bytes()

    def test_read_rejects_tampered_envelope(self, pr_run: tuple[Path, str]) -> None:
        root, run_id = pr_run
        with WorkLedger.open(run_ledger_dir(root / ".sdd", run_id)) as journal:
            score = build_run_scorecard(journal)
        out = write_scorecard_artifact(root, run_id, score)
        # Hand-edit the file: claim a different sha while leaving content
        # alone. ``read_scorecard_artifact`` must reject this rather than
        # silently trusting the on-disk sha.
        envelope = json.loads(out.read_text(encoding="utf-8"))
        envelope["sha256"] = "0" * 64
        out.write_text(json.dumps(envelope), encoding="utf-8")
        with pytest.raises(ValueError, match="sha256 mismatch"):
            read_scorecard_artifact(out)


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


class TestVerify:
    def test_clean_ledger_verifies(self, pr_run: tuple[Path, str]) -> None:
        """Acceptance 4a: a clean ledger verifies ok=True."""
        root, run_id = pr_run
        with WorkLedger.open(run_ledger_dir(root / ".sdd", run_id)) as journal:
            score = build_run_scorecard(journal)
        out = write_scorecard_artifact(root, run_id, score)
        result = verify_scorecard(root, run_id, out)
        assert isinstance(result, VerifyResult)
        assert result.ok is True
        assert result.artifact_sha256 == score.sha256
        assert result.recomputed_sha256 == score.sha256
        assert "match" in result.description.lower() or "ok" in result.description.lower()

    def test_tampered_ledger_fails_with_field_name(self, pr_run: tuple[Path, str]) -> None:
        """Acceptance 4b: flipping a payload byte in the journal fails verify.

        The ``run.closed`` payload carries the branch; flipping a byte
        inside the entry must change the derived scorecard content
        (``branch`` field) and the verify result must name the
        diverging field.
        """
        root, run_id = pr_run
        with WorkLedger.open(run_ledger_dir(root / ".sdd", run_id)) as journal:
            score = build_run_scorecard(journal)
        out = write_scorecard_artifact(root, run_id, score)

        # Tamper: flip the closing entry's branch payload.
        journal_path = run_ledger_dir(root / ".sdd", run_id) / "000000.jsonl"
        raw = journal_path.read_text(encoding="utf-8")
        # Replace the branch string in the closing entry only.
        tampered = raw.replace('"branch":"fix/scorecard"', '"branch":"fix/wrong"', 1)
        assert tampered != raw, "tamper substitution did not modify the ledger"
        journal_path.write_text(tampered, encoding="utf-8")

        result = verify_scorecard(root, run_id, out)
        assert result.ok is False
        assert result.description  # human-readable, non-empty
        # The diverging field is named in the description.
        assert "branch" in result.description
        # The recomputed SHA differs from the on-disk artifact SHA.
        assert result.artifact_sha256 != result.recomputed_sha256
        assert result.artifact_sha256 == score.sha256

    def test_missing_ledger_reports(self, tmp_path: Path) -> None:
        # Build a scorecard artifact from one run, then verify under a
        # different run id whose ledger does not exist.
        _seed_pr_run(tmp_path, "run-a")
        with WorkLedger.open(run_ledger_dir(tmp_path / ".sdd", "run-a")) as journal:
            score = build_run_scorecard(journal)
        out = write_scorecard_artifact(tmp_path, "run-a", score)
        result = verify_scorecard(tmp_path, "run-missing", out)
        assert result.ok is False
        assert "not found" in result.description.lower()

    def test_run_scorecard_is_frozen(self, pr_run: tuple[Path, str]) -> None:
        # A frozen dataclass cannot be reassigned in place; this is a
        # tripwire for accidental mutability regressions.
        root, run_id = pr_run
        with WorkLedger.open(run_ledger_dir(root / ".sdd", run_id)) as journal:
            score = build_run_scorecard(journal)
        with pytest.raises(FrozenInstanceError):
            score.sha256 = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


class TestModuleSurface:
    def test_all_required_symbols_exported(self) -> None:
        import bernstein.core.persistence.run_scorecard as mod

        for name in (
            "RunScorecard",
            "VerifyResult",
            "build_run_scorecard",
            "read_scorecard_artifact",
            "verify_scorecard",
            "write_scorecard_artifact",
        ):
            assert name in mod.__all__, f"missing {name!r} in __all__"
            assert hasattr(mod, name), f"missing {name!r} on module"


# ---------------------------------------------------------------------------
# CLI surface (`bernstein runs scorecard`)
# ---------------------------------------------------------------------------


class TestCliScorecard:
    def test_scorecard_command_is_registered(self) -> None:
        runner = CliRunner()
        result = runner.invoke(runs_group, ["--help"])
        assert result.exit_code == 0
        # The "scorecard" subcommand must appear in the help text; "report"
        # is the only other one and stays alongside it.
        assert "scorecard" in result.output
        assert "report" in result.output

    def test_scorecard_default_writes_artifact(self, pr_run: tuple[Path, str]) -> None:
        root, run_id = pr_run
        runner = CliRunner()
        result = runner.invoke(runs_group, ["scorecard", run_id, "--workdir", str(root)])
        assert result.exit_code == 0, result.output
        assert "wrote" in result.output
        artifact_dir = root / ".sdd" / "runs" / run_id / "scorecard"
        assert artifact_dir.exists()
        artifacts = list(artifact_dir.glob("*.json"))
        assert len(artifacts) == 1
        # The single one-line summary names the outcome.
        assert "outcome=pr-opened" in result.output

    def test_scorecard_json_emits_canonical_content(self, pr_run: tuple[Path, str]) -> None:
        root, run_id = pr_run
        runner = CliRunner()
        result = runner.invoke(runs_group, ["scorecard", run_id, "--workdir", str(root), "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert (
            payload["sha256"]
            == hashlib.sha256(
                json.dumps(payload["content"], sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            ).hexdigest()
        )
        assert payload["content"]["kind"] == "run_scorecard"
        assert payload["content"]["outcome"] == "pr-opened"

    def test_scorecard_verify_ok_on_clean_ledger(self, pr_run: tuple[Path, str]) -> None:
        root, run_id = pr_run
        runner = CliRunner()
        # Build the artifact first via the default mode.
        runner.invoke(runs_group, ["scorecard", run_id, "--workdir", str(root)])
        result = runner.invoke(runs_group, ["scorecard", run_id, "--workdir", str(root), "--verify"])
        assert result.exit_code == 0, result.output
        assert "OK" in result.output
        assert "match" in result.output.lower()

    def test_scorecard_verify_json_on_clean_ledger(self, pr_run: tuple[Path, str]) -> None:
        root, run_id = pr_run
        runner = CliRunner()
        runner.invoke(runs_group, ["scorecard", run_id, "--workdir", str(root)])
        result = runner.invoke(runs_group, ["scorecard", run_id, "--workdir", str(root), "--verify", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["artifact_sha256"] == payload["recomputed_sha256"]

    def test_scorecard_verify_named_field_on_tamper(self, pr_run: tuple[Path, str]) -> None:
        root, run_id = pr_run
        runner = CliRunner()
        # Build the artifact, then tamper the journal's branch line.
        runner.invoke(runs_group, ["scorecard", run_id, "--workdir", str(root)])
        journal_path = run_ledger_dir(root / ".sdd", run_id) / "000000.jsonl"
        raw = journal_path.read_text(encoding="utf-8")
        journal_path.write_text(raw.replace('"branch":"fix/scorecard"', '"branch":"fix/wrong"', 1), encoding="utf-8")
        result = runner.invoke(runs_group, ["scorecard", run_id, "--workdir", str(root), "--verify", "--json"])
        # Exit 1 on mismatch, payload names the diverging field.
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert "branch" in payload["description"]

    def test_scorecard_verify_failure_output_is_only_the_envelope(self, pr_run: tuple[Path, str]) -> None:
        """A failing ``--verify --json`` writes one JSON document and nothing else.

        The mismatch is carried by ``ok`` and ``description`` inside the
        envelope. Any additional diagnostic line -- however human-friendly --
        lands after the closing brace and stops the output parsing as JSON,
        which is the whole point of the ``--json`` mode.
        """
        root, run_id = pr_run
        runner = CliRunner()
        runner.invoke(runs_group, ["scorecard", run_id, "--workdir", str(root)])
        journal_path = run_ledger_dir(root / ".sdd", run_id) / "000000.jsonl"
        raw = journal_path.read_text(encoding="utf-8")
        journal_path.write_text(raw.replace('"branch":"fix/scorecard"', '"branch":"fix/wrong"', 1), encoding="utf-8")

        result = runner.invoke(runs_group, ["scorecard", run_id, "--workdir", str(root), "--verify", "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        # Round-tripping the parsed envelope must account for every byte the
        # command emitted: no trailing "Error: ..." line, no second document.
        assert json.loads(result.output.strip()) == payload
        assert result.output.strip().endswith("}")

    def test_scorecard_verify_failure_reports_once_on_the_human_path(self, pr_run: tuple[Path, str]) -> None:
        """Without ``--json`` the mismatch is named once, by the FAIL line."""
        root, run_id = pr_run
        runner = CliRunner()
        runner.invoke(runs_group, ["scorecard", run_id, "--workdir", str(root)])
        journal_path = run_ledger_dir(root / ".sdd", run_id) / "000000.jsonl"
        raw = journal_path.read_text(encoding="utf-8")
        journal_path.write_text(raw.replace('"branch":"fix/scorecard"', '"branch":"fix/wrong"', 1), encoding="utf-8")

        result = runner.invoke(runs_group, ["scorecard", run_id, "--workdir", str(root), "--verify"])

        assert result.exit_code == 1
        assert "FAIL" in result.output
        assert result.output.count("FAIL") == 1
        assert "Error:" not in result.output

    def test_scorecard_missing_ledger_exits_nonzero(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(runs_group, ["scorecard", "no-such-run", "--workdir", str(tmp_path)])
        assert result.exit_code != 0
        assert "work ledger not found" in result.output

    def test_scorecard_verify_missing_artifact_exits_nonzero(self, pr_run: tuple[Path, str]) -> None:
        root, run_id = pr_run
        runner = CliRunner()
        # No artifact written yet -- --verify must fail loudly.
        result = runner.invoke(runs_group, ["scorecard", run_id, "--workdir", str(root), "--verify"])
        assert result.exit_code != 0
        assert "no scorecard artifact" in result.output.lower()
