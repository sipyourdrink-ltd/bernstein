"""CLI tests for ``bernstein activity browser`` (#2523).

``run`` drives a flow -- live, or against a recorded observation tape -- and
anchors the result into a run journal through the same dispatch path a coding
spawn uses. ``verify`` replays a completed run offline from the content store,
recomputing every anchor and re-evaluating every recorded check. These tests pin
the operator-visible contract: exit codes, the JSON surface, and the fact that a
tampered observation is reported at an exact step index.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.activity_cmd import activity_group
from bernstein.core.orchestration.activity_modalities import ContentStore

_FLOW = {
    "flow_id": "checkout-smoke",
    "start_url": "https://shop/",
    "steps": [
        {
            "action": {"kind": "navigate", "target": "https://shop/cart"},
            "checks": [{"id": "landing-ok", "kind": "dom_contains", "operand": "Sign in"}],
        },
        {"action": {"kind": "click", "target": "#checkout"}, "checks": []},
    ],
    "final_checks": [
        {"id": "order-placed", "kind": "dom_contains", "operand": "Order confirmed"},
        {"id": "no-error", "kind": "dom_not_contains", "operand": "Error 500"},
    ],
    "budget": {"max_steps": 16},
}

_RECORDING = {
    "frames": [
        {"url": "https://shop/", "screenshot_b64": "", "dom_b64": ""},
        {"url": "https://shop/cart", "screenshot_b64": "", "dom_b64": ""},
        {"url": "https://shop/done", "screenshot_b64": "", "dom_b64": ""},
    ]
}

_DOMS = (b"<html>Sign in</html>", b"<html>Cart</html>", b"<html>Order confirmed</html>")
_SHOTS = (b"png-0", b"png-1", b"png-2")


def _write_inputs(project: Path, *, doms: tuple[bytes, ...] = _DOMS) -> tuple[Path, Path]:
    """Write a flow document and a recorded tape into *project*."""
    flow_path = project / "flow.json"
    flow_path.write_text(json.dumps(_FLOW), encoding="utf-8")
    frames = [
        {
            "url": frame["url"],
            "screenshot_b64": base64.b64encode(shot).decode("ascii"),
            "dom_b64": base64.b64encode(dom).decode("ascii"),
        }
        for frame, shot, dom in zip(_RECORDING["frames"], _SHOTS, doms, strict=True)
    ]
    recording_path = project / "tape.json"
    recording_path.write_text(json.dumps({"frames": frames}), encoding="utf-8")
    return flow_path, recording_path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project root with an initialised ``.sdd`` directory."""
    (tmp_path / ".sdd").mkdir()
    return tmp_path


def _run_flow(project: Path, *, doms: tuple[bytes, ...] = _DOMS, run_id: str = "run-cli-browser"):
    flow_path, recording_path = _write_inputs(project, doms=doms)
    return CliRunner().invoke(
        activity_group,
        [
            "browser",
            "run",
            "--flow",
            str(flow_path),
            "--recording",
            str(recording_path),
            "--run",
            run_id,
            "--stage",
            "browser-0",
            "--workdir",
            str(project),
            "--json",
        ],
    )


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_run_over_a_recording_completes_and_anchors(project: Path) -> None:
    result = _run_flow(project)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["flow_id"] == "checkout-smoke"
    assert payload["terminal_state"] == "completed"
    assert payload["reason_code"] == "ok"
    assert payload["steps_executed"] == 3
    assert payload["failed_checks"] == []
    assert len(payload["head_anchor"]) == 64
    assert (project / ".sdd" / "runs" / "run-cli-browser" / "journal.jsonl").exists()


def test_run_is_byte_identical_over_the_same_recording(tmp_path: Path) -> None:
    outputs = []
    for name in ("a", "b"):
        project = tmp_path / name
        (project / ".sdd").mkdir(parents=True)
        result = _run_flow(project)
        assert result.exit_code == 0, result.output
        outputs.append(json.loads(result.output))
    assert outputs[0]["head_anchor"] == outputs[1]["head_anchor"]
    assert outputs[0]["artifact_hash"] == outputs[1]["artifact_hash"]
    assert outputs[0]["evidence_set_hash"] == outputs[1]["evidence_set_hash"]


def test_run_exits_two_when_a_check_fails(project: Path) -> None:
    broken = (_DOMS[0], _DOMS[1], b"<html>Error 500</html>")
    result = _run_flow(project, doms=broken)
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert sorted(payload["failed_checks"]) == ["no-error", "order-placed"]
    # A failing check is still a completed activity: the evidence is anchored.
    assert payload["terminal_state"] == "completed"
    assert payload["reason_code"] == "checks_failed"


def test_run_refuses_a_flow_with_an_unknown_action_kind(project: Path) -> None:
    flow_path = project / "bad.json"
    flow_path.write_text(
        json.dumps({"flow_id": "f", "start_url": "https://x/", "steps": [{"action": {"kind": "teleport"}}]}),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        activity_group,
        ["browser", "run", "--flow", str(flow_path), "--run", "r", "--workdir", str(project)],
    )
    assert result.exit_code != 0
    assert "unknown action kind" in result.output


def test_run_refuses_a_flow_with_no_flow_id(project: Path) -> None:
    flow_path = project / "bad.json"
    flow_path.write_text(json.dumps({"start_url": "https://x/", "steps": []}), encoding="utf-8")
    result = CliRunner().invoke(
        activity_group,
        ["browser", "run", "--flow", str(flow_path), "--run", "r", "--workdir", str(project)],
    )
    assert result.exit_code != 0
    assert "flow_id" in result.output


def test_run_refuses_a_recording_that_is_not_base64(project: Path) -> None:
    flow_path, _ = _write_inputs(project)
    tape = project / "bad-tape.json"
    tape.write_text(json.dumps({"frames": [{"url": "u", "screenshot_b64": "!!", "dom_b64": "!!"}]}), encoding="utf-8")
    result = CliRunner().invoke(
        activity_group,
        [
            "browser",
            "run",
            "--flow",
            str(flow_path),
            "--recording",
            str(tape),
            "--run",
            "r",
            "--workdir",
            str(project),
        ],
    )
    assert result.exit_code != 0
    assert "base64" in result.output


def test_run_without_a_recording_refuses_when_the_driver_is_absent(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bernstein.core.orchestration.browser_driver._import_browser_use", lambda: None)
    flow_path, _ = _write_inputs(project)
    result = CliRunner().invoke(
        activity_group,
        ["browser", "run", "--flow", str(flow_path), "--run", "r", "--workdir", str(project), "--json"],
    )
    # A missing driver is a typed refusal that still anchors an activity result.
    assert result.exit_code == 3, result.output
    payload = json.loads(result.output)
    assert payload["terminal_state"] == "refused"
    assert payload["reason_code"] == "driver_unavailable"


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def _verify(project: Path, *, run_id: str = "run-cli-browser", extra: list[str] | None = None):
    return CliRunner().invoke(
        activity_group,
        ["browser", "verify", run_id, "--workdir", str(project), "--json", *(extra or [])],
    )


def test_verify_passes_for_a_completed_run(project: Path) -> None:
    assert _run_flow(project).exit_code == 0
    result = _verify(project)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"]
    verdict = payload["stages"][0]["browser_verdict"]
    assert verdict["head_anchor_ok"]
    assert [s["index"] for s in verdict["steps"]] == [0, 1, 2]
    assert [c["check_id"] for c in verdict["checks"]] == ["landing-ok", "order-placed", "no-error"]


def test_verify_names_the_step_when_an_observation_is_altered(project: Path) -> None:
    assert _run_flow(project).exit_code == 0
    store = ContentStore(project / ".sdd" / "cas")
    tampered = store.put(_SHOTS[1])
    store.force_put(tampered, b"png-1-TAMPERED")

    result = _verify(project)
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert not payload["ok"]
    assert "step 1" in payload["stages"][0]["reason"]


def test_verify_reports_no_browser_activity_for_an_unknown_run(project: Path) -> None:
    result = _verify(project, run_id="run-absent")
    assert result.exit_code == 1


def test_verify_can_target_a_single_stage(project: Path) -> None:
    assert _run_flow(project).exit_code == 0
    assert _verify(project, extra=["--stage", "browser-0"]).exit_code == 0
    assert _verify(project, extra=["--stage", "browser-99"]).exit_code == 1


def test_activity_verify_surfaces_the_browser_verdict(project: Path) -> None:
    assert _run_flow(project).exit_code == 0
    result = CliRunner().invoke(activity_group, ["verify", "run-cli-browser", "--workdir", str(project), "--json"])
    assert result.exit_code == 0, result.output
    stage = json.loads(result.output)["stages"][0]
    assert stage["kind"] == "browser"
    assert stage["browser_verdict"]["ok"]


def test_human_output_lists_steps_and_checks(project: Path) -> None:
    assert _run_flow(project).exit_code == 0
    result = CliRunner().invoke(activity_group, ["browser", "verify", "run-cli-browser", "--workdir", str(project)])
    assert result.exit_code == 0, result.output
    assert "step OK" in result.output
    assert "check OK" in result.output
