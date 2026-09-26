"""``bernstein adopt --dry-run``: detection, and nothing written (#5435 slice 1).

The property under test is that detection is trustworthy before anything is
allowed to write. So these pin:

* the detection matrix -- each supported agent, present and absent, at both
  evidence tiers -- plus "none detected", which is a clear error that writes
  nothing;
* the two-agent policy: a running process outranks configuration on disk, a
  tie within a tier is refused rather than guessed, and a weaker tier never
  breaks a stronger tier's tie;
* that the dry run writes nothing, including when run without ``--dry-run``;
* that the reported write plan is ``bernstein init``'s own plan, not a copy
  of it that could drift.

Processes and the home directory are injected, so no test depends on which
agents happen to be installed or running on the machine executing the suite.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.commands import adopt_cmd as adopt
from bernstein.cli.run_bootstrap import INIT_TEMPLATES_DIR, PLAN_CREATE, PLAN_EXISTS, init, plan_init_writes

if TYPE_CHECKING:
    from pathlib import Path

_PROCESS = {probe.agent: probe.signal for probe in adopt.DETECTION_PROBES if probe.kind == adopt.KIND_PROCESS}
_HOME = {probe.agent: probe.signal for probe in adopt.DETECTION_PROBES if probe.kind == adopt.KIND_HOME_PATH}


def _touch(home: Path, rel: str) -> None:
    target = home / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()


def _snapshot(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def _detect(home: Path, *processes: adopt.ProcessInfo) -> adopt.Detection:
    return adopt.resolve_agent(adopt.evaluate_probes(home=home, ancestry=processes), requested="auto")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "home"
    directory.mkdir()
    monkeypatch.setattr(adopt, "_home", lambda: directory)
    return directory


@pytest.fixture
def ancestry(monkeypatch: pytest.MonkeyPatch) -> list[adopt.ProcessInfo]:
    chain: list[adopt.ProcessInfo] = []
    monkeypatch.setattr(adopt, "_ancestor_processes", lambda: tuple(chain))
    return chain


@pytest.fixture
def project(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    return directory


def _run(project: Path, *args: str):
    return CliRunner().invoke(adopt.adopt_cmd, ["--dir", str(project), *args])


# ---------------------------------------------------------------------------
# The table is data, and it covers every supported agent
# ---------------------------------------------------------------------------


def test_the_table_has_one_process_and_one_config_probe_per_agent() -> None:
    for kind in (adopt.KIND_PROCESS, adopt.KIND_HOME_PATH):
        agents = sorted(probe.agent for probe in adopt.DETECTION_PROBES if probe.kind == kind)
        assert agents == sorted(adopt.SUPPORTED_AGENTS), kind


def test_every_probe_says_where_its_signal_comes_from() -> None:
    assert all(probe.grounded_in for probe in adopt.DETECTION_PROBES)


# ---------------------------------------------------------------------------
# Detection matrix: five agents x present/absent, at both tiers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("present", [True, False], ids=["present", "absent"])
@pytest.mark.parametrize("agent", adopt.SUPPORTED_AGENTS)
def test_detection_matrix_running_process(agent: str, present: bool, tmp_path: Path) -> None:
    processes = (adopt.ProcessInfo(name=_PROCESS[agent]),) if present else ()

    detection = _detect(tmp_path, *processes)

    if present:
        assert (detection.outcome, detection.selected, detection.tier) == (
            adopt.OUTCOME_SELECTED,
            agent,
            adopt.TIER_SESSION,
        )
    else:
        assert detection.outcome == adopt.OUTCOME_NONE
        assert detection.selected is None


@pytest.mark.parametrize("present", [True, False], ids=["present", "absent"])
@pytest.mark.parametrize("agent", adopt.SUPPORTED_AGENTS)
def test_detection_matrix_configuration(agent: str, present: bool, tmp_path: Path) -> None:
    if present:
        _touch(tmp_path, _HOME[agent])

    detection = _detect(tmp_path)

    if present:
        assert (detection.outcome, detection.selected, detection.tier) == (
            adopt.OUTCOME_SELECTED,
            agent,
            adopt.TIER_CONFIG,
        )
    else:
        assert detection.outcome == adopt.OUTCOME_NONE


def test_none_detected_is_a_clear_error_and_writes_nothing(
    project: Path, home: Path, ancestry: list[adopt.ProcessInfo]
) -> None:
    before = _snapshot(project.parent)

    result = _run(project, "--dry-run")

    assert result.exit_code == adopt.EXIT_NO_AGENT
    assert "No agent detected" in result.output
    assert "--agent" in result.output
    assert _snapshot(project.parent) == before


def test_none_detected_still_reports_every_probe_it_checked(
    project: Path, home: Path, ancestry: list[adopt.ProcessInfo]
) -> None:
    result = _run(project, "--dry-run", "--json")

    assert result.exit_code == adopt.EXIT_NO_AGENT
    report = json.loads(result.output)
    assert report["outcome"] == adopt.OUTCOME_NONE
    assert len(report["evidence"]) == len(adopt.DETECTION_PROBES)
    assert report["would_write"] == []
    assert report["wrote"] is False


# ---------------------------------------------------------------------------
# Two agents detected
# ---------------------------------------------------------------------------


def test_a_running_process_outranks_configuration_on_disk(tmp_path: Path) -> None:
    """The issue's example: a Cursor config directory and a running Codex."""
    _touch(tmp_path, _HOME["cursor"])

    detection = _detect(tmp_path, adopt.ProcessInfo(name="codex"))

    assert (detection.selected, detection.tier) == ("codex", adopt.TIER_SESSION)


def test_two_running_agents_are_refused_not_guessed(
    project: Path, home: Path, ancestry: list[adopt.ProcessInfo]
) -> None:
    ancestry.extend([adopt.ProcessInfo(name="claude"), adopt.ProcessInfo(name="codex")])

    result = _run(project, "--dry-run")

    assert result.exit_code == adopt.EXIT_AMBIGUOUS
    assert "claude" in result.output
    assert "codex" in result.output
    assert _snapshot(project) == []


def test_two_configured_agents_with_neither_running_are_refused(tmp_path: Path) -> None:
    _touch(tmp_path, _HOME["claude"])
    _touch(tmp_path, _HOME["opencode"])

    detection = _detect(tmp_path)

    assert detection.outcome == adopt.OUTCOME_AMBIGUOUS
    assert detection.candidates == ("claude", "opencode")


def test_a_weaker_tier_never_breaks_a_stronger_tier_s_tie(tmp_path: Path) -> None:
    """Configuration for one of two running agents must not pick between them."""
    _touch(tmp_path, _HOME["claude"])

    detection = _detect(tmp_path, adopt.ProcessInfo(name="claude"), adopt.ProcessInfo(name="codex"))

    assert detection.outcome == adopt.OUTCOME_AMBIGUOUS
    assert detection.tier == adopt.TIER_SESSION


# ---------------------------------------------------------------------------
# Matching a process to an agent binary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("process", "agent"),
    [
        (adopt.ProcessInfo(name="claude.exe"), "claude"),
        (adopt.ProcessInfo(name="CODEX.CMD"), "codex"),
        (adopt.ProcessInfo(name="python3", argv=("python3", "/usr/local/bin/aider")), "aider"),
        (adopt.ProcessInfo(name="node", argv=("node", "C:\\Users\\me\\bin\\cursor-agent")), "cursor"),
    ],
    ids=["windows-exe", "windows-cmd-shim", "interpreter-script", "windows-path"],
)
def test_launchers_and_interpreters_still_match(process: adopt.ProcessInfo, agent: str, tmp_path: Path) -> None:
    assert _detect(tmp_path, process).selected == agent


def test_an_interpreter_running_an_unrelated_script_is_not_an_agent(tmp_path: Path) -> None:
    detection = _detect(tmp_path, adopt.ProcessInfo(name="node", argv=("node", "/opt/tool/cli.js")))

    assert detection.outcome == adopt.OUTCOME_NONE


# ---------------------------------------------------------------------------
# Nothing is written
# ---------------------------------------------------------------------------


def test_dry_run_reports_init_s_plan_and_writes_nothing(
    project: Path, home: Path, ancestry: list[adopt.ProcessInfo]
) -> None:
    ancestry.append(adopt.ProcessInfo(name="claude"))

    result = _run(project, "--dry-run", "--json")

    assert result.exit_code == adopt.EXIT_OK, result.output
    report = json.loads(result.output)
    assert report["selected"] == "claude"
    assert report["wrote"] is False
    assert report["would_write"] == [
        {"path": write.path, "action": write.action} for write in plan_init_writes(project.resolve())
    ]
    assert _snapshot(project) == []


def test_without_dry_run_it_refuses_and_writes_nothing(
    project: Path, home: Path, ancestry: list[adopt.ProcessInfo]
) -> None:
    ancestry.append(adopt.ProcessInfo(name="claude"))

    result = _run(project)

    assert result.exit_code == adopt.EXIT_WRITE_NOT_IMPLEMENTED
    assert "--dry-run" in result.output
    assert _snapshot(project) == []


def test_naming_the_agent_skips_detection(project: Path, home: Path, ancestry: list[adopt.ProcessInfo]) -> None:
    result = _run(project, "--dry-run", "--agent", "opencode", "--json")

    assert result.exit_code == adopt.EXIT_OK, result.output
    report = json.loads(result.output)
    assert (report["outcome"], report["selected"]) == (adopt.OUTCOME_EXPLICIT, "opencode")
    assert report["evidence"] == []


def test_an_unknown_agent_is_a_usage_error(project: Path, home: Path, ancestry: list[adopt.ProcessInfo]) -> None:
    result = _run(project, "--dry-run", "--agent", "not-an-agent")

    assert result.exit_code == 2
    assert _snapshot(project) == []


# ---------------------------------------------------------------------------
# The plan is init's plan, not a fork of it
# ---------------------------------------------------------------------------


def test_the_plan_is_init_s_own_plan(tmp_path: Path) -> None:
    """Before init everything is to be created; after init nothing is.

    If the dry run carried its own list of paths, init could change what it
    writes and this plan would keep reporting the old files. Running the real
    init and re-planning is what proves they are the same list.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    assert {write.action for write in plan_init_writes(root)} == {PLAN_CREATE}

    result = CliRunner().invoke(init, ["--dir", str(root)])

    assert result.exit_code == 0, result.output
    assert {write.action for write in plan_init_writes(root)} == {PLAN_EXISTS}


def test_init_writes_no_path_its_plan_does_not_name(tmp_path: Path) -> None:
    """The other direction: a write added to init without a plan row fails here.

    Only the templates entry may have contents of its own, because init copies
    the bundled tree wholesale. Anything else under ``.sdd/`` has to be named,
    so a new file there cannot hide behind its planned parent directory.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    planned = {write.path for write in plan_init_writes(root)}

    result = CliRunner().invoke(init, ["--dir", str(root)])

    assert result.exit_code == 0, result.output
    unplanned = [
        path for path in _snapshot(root) if path not in planned and not path.startswith(f"{INIT_TEMPLATES_DIR}/")
    ]
    assert unplanned == []


def test_adopt_is_registered_on_the_top_level_cli() -> None:
    from bernstein.cli.main import cli

    assert cli.commands["adopt"] is adopt.adopt_cmd
