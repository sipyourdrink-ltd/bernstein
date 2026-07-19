"""End-to-end CLI surface for registered recipes (#2546).

Exercises ``bernstein recipes register / show --registered / history
--verify / pause / fire / resume / rollback / plan / apply`` against a
throwaway working directory, proving the verbs are wired and behave per the
acceptance criteria at the command layer.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.recipes_cmd import recipes_group

_MANIFEST = """\
name: nightly-triage
description: "Nightly triage recipe."
version: "1.0.0"
nodes:
  - id: triage
    command: "echo triage"
schedules:
  - kind: cron
    recurrence: "0 9 * * *"
    timezone: America/New_York
    dst_policy: post_transition
"""


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    recipes_dir = tmp_path / ".bernstein" / "recipes"
    recipes_dir.mkdir(parents=True, exist_ok=True)
    (recipes_dir / "nightly-triage.yaml").write_text(_MANIFEST, encoding="utf-8")
    old = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(old)


def _run(args: list[str]) -> object:
    return CliRunner().invoke(recipes_group, args)


def _accepting_task_server(monkeypatch: pytest.MonkeyPatch, accepted: list[dict[str, object]]) -> None:
    """Stand in for a task server that accepts POSTs and returns task ids.

    ``recipes fire`` reports a dispatch only when work was actually accepted,
    so a test that expects a dispatched fire needs a sink that accepts it.

    The stub validates every payload through the real ``TaskCreate`` model -
    the same binding POST /tasks performs - and returns ``None`` on a
    validation error, which is exactly what ``server_post`` does on a 4xx. A
    stub that accepted any payload would prove only that a function was
    called, and would let field, enum, or required-key drift reach production
    with the suite green.
    """

    def _post(path: str, payload: dict[str, object]) -> dict[str, object] | None:
        from pydantic import ValidationError

        from bernstein.core.server.server_models import TaskCreate

        try:
            TaskCreate(**payload)
        except ValidationError:
            # The server would 422 and server_post would swallow it into None.
            return None
        accepted.append({"path": path, "payload": payload})
        return {"id": f"T-{len(accepted):03d}"}

    monkeypatch.setattr("bernstein.cli.helpers.server_post", _post)


def _unreachable_task_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for a task server that is down (``server_post`` returns None)."""
    monkeypatch.setattr("bernstein.cli.helpers.server_post", lambda _path, _payload: None)


class TestRegisterFlow:
    def test_register_then_show_registered(self, workdir: Path) -> None:
        result = _run(["register", "nightly-triage"])
        assert result.exit_code == 0, result.output
        assert "Registered" in result.output
        assert "recipe_hash:" in result.output

        shown = _run(["show", "nightly-triage", "--registered"])
        assert shown.exit_code == 0, shown.output
        assert "active" in shown.output

    def test_history_verify_passes(self, workdir: Path) -> None:
        assert _run(["register", "nightly-triage"]).exit_code == 0
        result = _run(["history", "nightly-triage", "--verify"])
        assert result.exit_code == 0, result.output
        assert "verified" in result.output

    def test_pause_blocks_fire_then_resume(self, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        accepted: list[dict[str, object]] = []
        _accepting_task_server(monkeypatch, accepted)

        assert _run(["register", "nightly-triage"]).exit_code == 0
        assert _run(["pause", "nightly-triage"]).exit_code == 0
        fired = _run(["fire", "nightly-triage", "--at", "1800000000"])
        assert fired.exit_code == 0
        assert "Not fired" in fired.output
        assert accepted == [], "a paused recipe must submit nothing"

        assert _run(["resume", "nightly-triage"]).exit_code == 0
        fired2 = _run(["fire", "nightly-triage", "--at", "1800000000"])
        assert fired2.exit_code == 0, fired2.output
        assert "projection_hash:" in fired2.output
        # Assert against the submission sink, not the CLI's own echo: exactly
        # one task reached the server, and the receipt names it.
        assert len(accepted) == 1, "a dispatched fire must submit exactly one task"
        assert accepted[0]["path"] == "/tasks"
        assert "T-001" in fired2.output

    def test_dispatched_fire_receipt_names_the_submitted_task(
        self,
        workdir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        accepted: list[dict[str, object]] = []
        _accepting_task_server(monkeypatch, accepted)
        assert _run(["register", "nightly-triage"]).exit_code == 0
        assert _run(["fire", "nightly-triage", "--at", "1800000000"]).exit_code == 0

        from bernstein.core.security.audit import load_or_create_audit_key
        from bernstein.core.security.audit_chain import EVENT_RECIPE_FIRE, AuditChainStore

        chain = AuditChainStore(workdir / ".sdd" / "audit", key=load_or_create_audit_key())
        receipts = list(chain.query(event_type=EVENT_RECIPE_FIRE))
        assert len(receipts) == 1
        submitted_ids = receipts[0].details["submitted_ids"]
        assert submitted_ids == ["T-001"]
        # The id in the receipt is the id the sink actually handed back.
        assert len(accepted) == 1

    def test_fire_exits_nonzero_and_writes_no_receipt_when_the_server_is_down(
        self,
        workdir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _unreachable_task_server(monkeypatch)
        assert _run(["register", "nightly-triage"]).exit_code == 0
        fired = _run(["fire", "nightly-triage", "--at", "1800000000"])
        assert fired.exit_code == 2, fired.output
        assert "Not fired" in fired.output
        assert "projection_hash:" not in fired.output

        from bernstein.core.security.audit import load_or_create_audit_key
        from bernstein.core.security.audit_chain import EVENT_RECIPE_FIRE, AuditChainStore

        chain = AuditChainStore(workdir / ".sdd" / "audit", key=load_or_create_audit_key())
        assert list(chain.query(event_type=EVENT_RECIPE_FIRE)) == [], (
            "no receipt may attest a fire the server never accepted"
        )

    def test_failure_whose_message_contains_paused_still_exits_nonzero(
        self,
        workdir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The exit code comes from structured state, not from the reason text.

        A dispatcher error is arbitrary prose. When it happens to contain the
        word "paused", a substring-based guard routes a failed submission down
        the deliberate-no-op branch and reports success to the caller.
        """

        def _post(_path: str, _payload: dict[str, object]) -> dict[str, object]:
            raise RuntimeError("worker queue paused for maintenance")

        monkeypatch.setattr("bernstein.cli.helpers.server_post", _post)
        assert _run(["register", "nightly-triage"]).exit_code == 0
        fired = _run(["fire", "nightly-triage", "--at", "1800000000"])

        assert "paused" in fired.output, "precondition: the failure text contains the word"
        assert fired.exit_code == 2, f"a failed submission must not exit 0: {fired.output}"

    def test_paused_recipe_exits_zero(self, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _accepting_task_server(monkeypatch, [])
        assert _run(["register", "nightly-triage"]).exit_code == 0
        assert _run(["pause", "nightly-triage"]).exit_code == 0
        fired = _run(["fire", "nightly-triage", "--at", "1800000000"])
        assert fired.exit_code == 0, "a deliberately paused recipe is not a failure"
        assert "Not fired" in fired.output

    def test_fire_unregistered_exits_nonzero(self, workdir: Path) -> None:
        result = _run(["fire", "does-not-exist", "--at", "1"])
        assert result.exit_code == 1

    def test_plan_is_reproducible(self, workdir: Path) -> None:
        first = _run(["plan", "nightly-triage"])
        second = _run(["plan", "nightly-triage"])
        assert first.exit_code == 0, first.output
        assert "plan_hash:" in first.output
        # The plan_hash line is stable across runs against the same state.
        first_hash = _extract_plan_hash(first.output)
        second_hash = _extract_plan_hash(second.output)
        assert first_hash == second_hash

    def test_apply_registers_then_no_change(self, workdir: Path) -> None:
        plan = _run(["plan", "nightly-triage"])
        plan_hash = _extract_plan_hash(plan.output)
        applied = _run(["apply", "--plan", plan_hash, "nightly-triage"])
        assert applied.exit_code == 0, applied.output
        assert "Applied" in applied.output
        assert _run(["show", "nightly-triage", "--registered"]).exit_code == 0


def _extract_plan_hash(output: str) -> str:
    for line in output.splitlines():
        if "plan_hash:" in line:
            return line.split("plan_hash:", 1)[1].strip()
    raise AssertionError(f"no plan_hash in output: {output!r}")
