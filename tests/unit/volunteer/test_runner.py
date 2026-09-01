"""The runner starts real processes, so these tests start real processes.

Every claim this module makes is about something the operating system did: a
clone that ran, a worktree that exists on disk, an environment a child was
handed, a process tree that stopped.  A pipeline tested against mocked
subprocesses proves the mocks were wired up, which is not the claim a donor
lending their laptop is relying on.  So the agent is always a real interpreter
started via ``sys.executable``, the repository is always a real git repository,
and the environment assertions are made against what the child *received* and
wrote down -- never against the dictionary the builder returned.

The ceilings are seconds rather than minutes to keep the file fast.  The code
path is the one a 30-minute task takes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.adapters._contract import AuthBasis
from bernstein.core.volunteer.manifest import load_manifest
from bernstein.core.volunteer.runner import (
    AgentInvocation,
    ClaimedTask,
    DonorLimits,
    RefusalStage,
    TaskDiff,
    TaskRefusal,
    WallClockBudget,
    mock_agent_argv,
    profile_budget,
    repo_url_problem,
    run_claimed_task,
)
from bernstein.core.volunteer.sandbox_profile import build_volunteer_profile, sandbox_env

if TYPE_CHECKING:
    from collections.abc import Sequence

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and shell shims")

#: A canary that has to be findable by substring anywhere it leaks to.
CANARY = "canary-do-not-execute-4032"

#: Variables the C library adds to a child on top of the environment it was
#: given, so an exact-equality assertion would fail on the platform rather than
#: on the code.  Both are synthesized rather than inherited -- a child spawned
#: with ``env={"A": "1"}`` receives them too -- so allowing them does not
#: weaken the claim that nothing crosses from the parent's environment.
_PLATFORM_INJECTED_ENV = frozenset({"LC_CTYPE", "__CF_USER_TEXT_ENCODING"})

#: The fixture project, cut down to the one line the mock agent rewrites.
APP_SOURCE = """from flask import Flask, jsonify

app = Flask(__name__)
ITEMS = ["a", "b", "c"]


@app.route("/items/<int:n>")
def item(n):
    return jsonify({"id": n, "item": ITEMS[n]})  # off-by-one
"""

_GIT_IDENTITY = ["-c", "user.name=fixture", "-c", "user.email=fixture@invalid"]


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A real git repository with one commit, cloned from by every test."""
    repo = tmp_path / "fixture-repo"
    repo.mkdir(parents=True)
    (repo / "app.py").write_text(APP_SOURCE, encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main", "-q"], cwd=repo, check=True)
    subprocess.run(["git", *_GIT_IDENTITY, "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", *_GIT_IDENTITY, "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def _manifest(**overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "version": 1,
        "license": "Apache-2.0",
        "gates": [["true"]],
        "sandbox": "container",
        "max_wall_clock_minutes": 5,
    }
    payload.update(overrides)
    return load_manifest(json.dumps(payload))


def _donor(**overrides: Any) -> DonorLimits:
    defaults: dict[str, Any] = {"available_backends": ("container",), "accepts_plain_container": True}
    defaults.update(overrides)
    return DonorLimits(**defaults)


def _task(repo: Path | str, **overrides: Any) -> ClaimedTask:
    defaults: dict[str, Any] = {
        "repo_url": str(repo),
        "issue_number": 7,
        "issue_title": "off-by-one in the items endpoint",
        "issue_body": "ITEMS[n] should be ITEMS[n - 1]",
    }
    defaults.update(overrides)
    return ClaimedTask(**defaults)


def _run(
    repo: Path | str,
    tmp_path: Path,
    *,
    agent_argv: Any,
    task: ClaimedTask | None = None,
    manifest: Any = None,
    donor: DonorLimits | None = None,
    budget: WallClockBudget | None = None,
    adapter_id: str | None = None,
) -> TaskDiff | TaskRefusal:
    return run_claimed_task(
        task if task is not None else _task(repo),
        manifest if manifest is not None else _manifest(),
        donor=donor if donor is not None else _donor(),
        workspace=tmp_path / "run",
        adapter_id=adapter_id,
        agent_argv=agent_argv,
        sanitize=_passthrough,
        budget=budget,
    )


def _passthrough(text: str) -> str:
    """Stand-in for the issue-text sanitizer this pipeline is handed.

    Deliberately does nothing.  Every property this file asserts has to hold
    for text that was *not* cleaned up on the way in, because the containment
    the runner provides -- a file rather than an argument, an environment built
    from a profile -- is what has to survive hostile input, not the
    normalisation in front of it.
    """
    return text


def _python_agent(body: str) -> Any:
    """A launcher that runs *body* as a real Python program in the worktree."""

    def build(invocation: AgentInvocation) -> Sequence[str]:
        script = invocation.workdir / ".sdd" / "runtime" / "probe-agent.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(body, encoding="utf-8")
        return [sys.executable, str(script)]

    return build


def _recording_launcher(inner: Any) -> tuple[list[AgentInvocation], Any]:
    """Wrap a launcher so a test can assert it was never reached."""
    seen: list[AgentInvocation] = []

    def build(invocation: AgentInvocation) -> Sequence[str]:
        seen.append(invocation)
        return inner(invocation)

    return seen, build


def _worktree_dirs(root: Path) -> list[Path]:
    return [path for path in root.rglob(".sdd/worktrees/*") if path.is_dir()]


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    try:
        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return status.stdout.strip().startswith("Z")


def _assert_process_is_gone(pid: int, *, attempts: int = 40) -> None:
    """Poll rather than assert once: reaping is not instantaneous."""
    for _ in range(attempts):
        if not _process_alive(pid):
            return
        time.sleep(0.05)
    pytest.fail(f"pid {pid} survived the run ceiling; the agent's children outlived the donor's loan")


# --------------------------------------------------------------------------
# The pipeline end to end
# --------------------------------------------------------------------------


def test_the_happy_path_produces_the_agents_patch_against_the_cloned_base(fixture_repo: Path, tmp_path: Path) -> None:
    """Real clone, real worktree, real subprocess, real diff.

    The whole point of the slice: three primitives that were independently
    tested and never called together now compose into one result.
    """
    outcome = _run(fixture_repo, tmp_path, agent_argv=mock_agent_argv(fix="off-by-one"))

    assert isinstance(outcome, TaskDiff), getattr(outcome, "as_record", dict)()
    assert "ITEMS[n - 1]" in outcome.diff
    assert outcome.diff.startswith("diff --git")
    assert outcome.worktree_path.is_dir()
    assert (outcome.worktree_path / "app.py").read_text(encoding="utf-8").count("ITEMS[n - 1]") == 1
    assert len(outcome.base_commit) == 40
    assert outcome.manifest_sha256 == _manifest().digest
    assert outcome.as_record()["outcome"] == "diff"
    assert outcome.as_record()["diff_sha256"] == outcome.diff_sha256


def test_the_prompt_file_and_the_agents_own_scaffolding_stay_out_of_the_patch(
    fixture_repo: Path, tmp_path: Path
) -> None:
    """The patch is the agent's work, not the runner's paperwork.

    The prompt, the agent's program and its log all land inside the worktree
    because that is where the agent can reach them.  A patch that carried them
    would be refused by any maintainer reading it, and would leak the issue
    text into a commit besides.
    """
    outcome = _run(
        fixture_repo,
        tmp_path,
        agent_argv=mock_agent_argv(fix="off-by-one"),
        task=_task(fixture_repo, issue_body=f"reproduce with {CANARY}"),
    )

    assert isinstance(outcome, TaskDiff)
    assert "volunteer-task.md" not in outcome.diff
    assert "mock-agent-" not in outcome.diff
    assert CANARY not in outcome.diff


# --------------------------------------------------------------------------
# Every failure is a record, not an exception
# --------------------------------------------------------------------------


def test_a_sandbox_refusal_arrives_as_a_record_rather_than_an_exception(fixture_repo: Path, tmp_path: Path) -> None:
    """The profile primitive raises; the pipeline must not.

    A caller cannot be expected to guess which of the composed primitives
    throws what.  The refusal carries the primitive's own reason code
    unchanged, so refusals stay comparable across a fleet.
    """
    seen, launcher = _recording_launcher(mock_agent_argv(fix="off-by-one"))

    outcome = _run(
        fixture_repo,
        tmp_path,
        agent_argv=launcher,
        manifest=_manifest(sandbox="microvm"),
        donor=_donor(available_backends=("container",)),
    )

    assert isinstance(outcome, TaskRefusal)
    assert outcome.stage == RefusalStage.SANDBOX_PROFILE
    assert outcome.reason == "microvm_required_but_unavailable"
    assert outcome.as_record()["outcome"] == "refused"
    assert seen == [], "the agent launcher ran despite the host being unable to contain the task"
    assert _worktree_dirs(tmp_path) == []


def test_a_clone_failure_refuses_before_any_worktree_or_agent_exists(tmp_path: Path) -> None:
    """A refusal has to be clean, not partial.

    Nothing downstream of the clone may have happened: no worktree on disk, no
    process started.  A pipeline that half-runs leaves a donor's machine with
    state nobody is going to clean up.
    """
    seen, launcher = _recording_launcher(mock_agent_argv(fix="off-by-one"))

    outcome = _run(tmp_path / "no-such-repository", tmp_path, agent_argv=launcher)

    assert isinstance(outcome, TaskRefusal)
    assert outcome.stage == RefusalStage.CLONE
    assert outcome.reason == "clone_failed"
    assert seen == [], "the agent launcher ran after the clone failed"
    assert _worktree_dirs(tmp_path) == []


def test_an_agent_that_exits_non_zero_is_a_refusal_rather_than_a_patch(fixture_repo: Path, tmp_path: Path) -> None:
    """A failure verdict must not be laundered into a submission.

    The agent here edits a file and *then* fails.  The edit is real and the
    diff would be non-empty, which is exactly why the exit status has to
    decide: a patch from a run that reported failure is a patch nobody
    reviewed.
    """
    outcome = _run(
        fixture_repo,
        tmp_path,
        agent_argv=_python_agent(
            "from pathlib import Path\n"
            "Path('app.py').write_text('# half a fix\\n', encoding='utf-8')\n"
            "raise SystemExit(3)\n"
        ),
    )

    assert isinstance(outcome, TaskRefusal)
    assert outcome.stage == RefusalStage.AGENT
    assert outcome.reason == "agent_failed"
    assert outcome.wall_clock is not None
    assert outcome.wall_clock["killed"] is False, "a failing agent and a killed one must stay distinguishable"
    assert outcome.wall_clock["exit_code"] == 3


# ---------------------------------------------------------------------------
# Provider-terms auth_basis preflight
# ---------------------------------------------------------------------------


def test_auth_basis_subscription_oauth_is_refused_in_volunteer_mode(fixture_repo: Path, tmp_path: Path) -> None:
    """adapter with auth_basis=subscription_oauth is refused in volunteer mode.

    The refusal receipt must name the compliant alternatives (API key or
    local endpoint), not just say "no".
    """
    outcome = _run(
        fixture_repo,
        tmp_path,
        agent_argv=mock_agent_argv(fix="off-by-one"),
        adapter_id="copilot",  # copilot has subscription_oauth
    )
    assert isinstance(outcome, TaskRefusal)
    assert outcome.stage == RefusalStage.AGENT
    assert outcome.reason == "provider_terms_unavailable"
    record = outcome.as_record()
    assert "subscription OAuth" in record["detail"]
    assert "API-key adapter" in record["detail"]
    assert "local endpoint adapter" in record["detail"]


def test_auth_basis_unknown_is_refused_in_volunteer_mode(fixture_repo: Path, tmp_path: Path) -> None:
    """adapter with unknown auth_basis is refused in volunteer mode.

    computer_use carries an unknown auth_basis in its contract; the gate
    must refuse it and name compliant paths.
    """
    outcome = _run(
        fixture_repo,
        tmp_path,
        agent_argv=mock_agent_argv(fix="off-by-one"),
        adapter_id="computer_use",  # computer_use has unknown auth_basis
    )
    assert isinstance(outcome, TaskRefusal)
    assert outcome.stage == RefusalStage.AGENT
    assert outcome.reason == "provider_terms_unavailable"
    record = outcome.as_record()
    assert "unknown" in record["detail"].lower()
    assert "API-key adapter" in record["detail"]
    assert "local endpoint adapter" in record["detail"]


def test_auth_basis_api_key_is_accepted_in_volunteer_mode(fixture_repo: Path, tmp_path: Path) -> None:
    """adapter with auth_basis=api_key is accepted in volunteer mode."""
    outcome = _run(
        fixture_repo,
        tmp_path,
        agent_argv=mock_agent_argv(fix="off-by-one"),
        adapter_id="claude",  # claude has api_key
    )
    assert isinstance(outcome, TaskDiff)
    assert outcome.as_record()["outcome"] == "diff"


def test_auth_basis_local_is_accepted_in_volunteer_mode(
    fixture_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """adapter with auth_basis=local is accepted in volunteer mode.

    No shipped adapter carries a local auth_basis, so a local profile is
    injected via the registry and the runner must accept it the same way
    it accepts api_key.
    """
    from bernstein.adapters.capability_profile import PROFILES, AdapterCapabilityProfile, InvocationSpec

    monkeypatch.setitem(
        PROFILES,
        "local-adapter",
        AdapterCapabilityProfile(
            name="local-adapter",
            display_name="Local Adapter",
            invocation=InvocationSpec(binary="local"),
            auth_basis=AuthBasis.LOCAL,
        ),
    )
    outcome = _run(
        fixture_repo,
        tmp_path,
        agent_argv=mock_agent_argv(fix="off-by-one"),
        adapter_id="local-adapter",
    )
    assert isinstance(outcome, TaskDiff)
    assert outcome.as_record()["outcome"] == "diff"


def test_a_run_that_changed_nothing_is_a_refusal_rather_than_an_empty_patch(fixture_repo: Path, tmp_path: Path) -> None:
    """An empty submission is not a submission."""
    outcome = _run(fixture_repo, tmp_path, agent_argv=_python_agent("pass\n"))

    assert isinstance(outcome, TaskRefusal)
    assert outcome.stage == RefusalStage.DIFF
    assert outcome.reason == "empty_diff"


def test_a_repo_url_that_names_a_transport_helper_is_refused_before_git_runs(tmp_path: Path) -> None:
    """``git clone ext::sh -c ...`` names a command for git to run.

    The repository URL arrives from a claimed task and is exactly as
    trustworthy as the issue text beside it.  What is pinned here is that the
    refusal happens at *this* layer -- the stage and reason say so, and no
    process was started to reach them.

    Current git also refuses ``ext::`` by itself, which is why the marker
    assertion below would hold even with this check deleted, and why the check
    is not written to lean on it: ``protocol.ext.allow`` makes git's refusal a
    setting, and a donor who once turned it on for their own work would be
    handing it to a stranger's URL.
    """
    marker = tmp_path / "helper-ran"
    outcome = _run(f"ext::sh -c 'touch {marker}'", tmp_path, agent_argv=mock_agent_argv(fix="off-by-one"))

    assert isinstance(outcome, TaskRefusal)
    assert outcome.stage == RefusalStage.REPO_URL
    assert outcome.reason == "unsupported_repo_url"
    assert not marker.exists(), "the transport helper's command ran"


@pytest.mark.parametrize(
    ("url", "rejected"),
    [
        ("ext::sh -c whoami", True),
        ("-u./payload", True),
        ("", True),
        ("evil::helper", True),
        ("https://example.test/project.git", False),
        ("ssh://git@example.test/project.git", False),
        ("file:///srv/project", False),
        ("/srv/project", False),
    ],
)
def test_the_repo_url_allowlist_admits_transports_and_refuses_helpers(url: str, rejected: bool) -> None:
    """An allowlist of transports, not a denylist of the ones we thought of."""
    assert (repo_url_problem(url) is not None) is rejected


def test_private_github_repository_is_refused_in_volunteer_mode(
    fixture_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A private GitHub repository is refused during open-source preflight.

    The runner calls urllib.request.urlopen for https://api.github.com/repos/{repo_path}
    to check repository visibility. When the API returns {"private": true}, the task
    must be refused with open_source_preflight_failed and a detail containing "private".
    """
    import urllib.request

    class _FakeResponse:
        status = 200

        def read(self):
            return b'{"private": true, "full_name": "owner/repo"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def mock_urlopen(req, *, timeout: int = 10):
        assert req.full_url.startswith("https://api.github.com/repos/owner/repo")
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    outcome = _run(
        "https://github.com/owner/repo",
        tmp_path,
        agent_argv=mock_agent_argv(fix="off-by-one"),
    )

    assert isinstance(outcome, TaskRefusal)
    assert outcome.stage == RefusalStage.REPO_URL
    assert outcome.reason == "open_source_preflight_failed"
    assert "private" in outcome.detail.lower()


# --------------------------------------------------------------------------
# The wall clock, against real process trees
# --------------------------------------------------------------------------


@pytest.mark.slow
def test_a_hanging_agent_is_killed_at_the_run_ceiling_and_becomes_a_refusal(fixture_repo: Path, tmp_path: Path) -> None:
    """The property the cap exists for, measured rather than asserted.

    Elapsed time has to land near the ceiling, not near the sleep: a test that
    only checked the refusal code would pass against an implementation that
    waited out the full sleep and then reported a timeout.
    """
    started = time.monotonic()

    outcome = _run(
        fixture_repo,
        tmp_path,
        agent_argv=_python_agent("import time; time.sleep(120)\n"),
        budget=WallClockBudget.start(4),
    )
    elapsed = time.monotonic() - started

    assert isinstance(outcome, TaskRefusal)
    assert outcome.stage == RefusalStage.AGENT
    assert outcome.reason == "wall_clock_exceeded"
    assert outcome.wall_clock is not None
    assert outcome.wall_clock["killed"] is True
    assert elapsed < 30, f"the run held the machine for {elapsed:.1f}s against a 4s ceiling"


@POSIX_ONLY
@pytest.mark.slow
def test_the_run_ceiling_kills_the_agents_own_child_processes(fixture_repo: Path, tmp_path: Path) -> None:
    """Killing the agent alone is the failure the cap exists to prevent.

    An agent shells out -- a test runner, a build, a package install.  If the
    cap reaps only the process the runner started, those children keep running
    on a stranger's machine with nothing watching them, which is the original
    problem minus the error message.

    The agent writes its child's pid outside the worktree, then both sleep far
    past the ceiling.  After the kill, that pid must be gone.
    """
    marker = tmp_path / "grandchild.pid"
    outcome = _run(
        fixture_repo,
        tmp_path,
        agent_argv=_python_agent(
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(120)\n"
        ),
        budget=WallClockBudget.start(3),
    )

    assert isinstance(outcome, TaskRefusal)
    assert outcome.reason == "wall_clock_exceeded"
    _assert_process_is_gone(int(marker.read_text()))


@pytest.mark.slow
def test_a_budget_shared_across_phases_is_spent_down_rather_than_refreshed(fixture_repo: Path, tmp_path: Path) -> None:
    """The one-budget-per-run decision, as something a caller can observe.

    A donor lends N seconds of their machine, not N seconds per phase.  The
    caller here holds one budget and drives two phases with it -- the shape the
    step after this one uses to run gates after the agent.  The first phase
    hangs and spends the loan; the second must refuse for want of time rather
    than start with a fresh ceiling.
    """
    shared = WallClockBudget.start(4)

    first = _run(
        fixture_repo,
        tmp_path,
        agent_argv=_python_agent("import time; time.sleep(120)\n"),
        budget=shared,
    )
    second = _run(
        fixture_repo,
        tmp_path / "second",
        agent_argv=_python_agent("pass\n"),
        budget=shared,
    )

    assert isinstance(first, TaskRefusal)
    assert first.reason == "wall_clock_exceeded"
    assert isinstance(second, TaskRefusal)
    assert second.reason == "budget_exhausted", "the second phase was handed a fresh ceiling"


def test_a_caller_supplied_budget_can_only_tighten_the_profile_ceiling() -> None:
    """A containment control a caller can widen is a suggestion.

    The clamp is checked from both directions, because only one of them is
    dangerous and a test that checked the safe one would look identical.
    """
    profile = build_volunteer_profile(
        _manifest(max_wall_clock_minutes=5),
        available_backends=("container",),
        donor_accepts_plain_container=True,
    )
    assert profile.wall_clock_seconds == 300

    generous = profile_budget(profile, WallClockBudget.start(86_400))
    assert generous.remaining_seconds <= 300, "a caller widened the profile's ceiling"

    tighter = profile_budget(profile, WallClockBudget.start(30))
    assert 25 <= tighter.remaining_seconds <= 30

    unbudgeted = profile_budget(profile, None)
    assert unbudgeted.total_seconds == 300


# --------------------------------------------------------------------------
# The environment, observed from inside the spawned process
# --------------------------------------------------------------------------


_ENV_PROBE = """
import json, os, sys
from pathlib import Path

Path({dump!r}).write_text(
    json.dumps({{"argv": sys.argv, "env": dict(os.environ)}}),
    encoding="utf-8",
)
Path("app.py").write_text("# touched by the probe\\n", encoding="utf-8")
"""


def test_the_sandboxed_agent_never_sees_a_variable_set_before_the_run(
    fixture_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canary test, asked of the process instead of the builder.

    Checking what ``sandbox_env`` returns proves the builder is correct.  It
    does not prove that nothing between the builder and the spawn merged
    ``os.environ`` back in, and that merge is precisely the mistake this
    boundary exists to make impossible.  So the assertion is made against the
    environment the child was actually handed and wrote to disk itself.
    """
    monkeypatch.setenv("MY_SECRET_PROBE", f"{CANARY}-env")
    monkeypatch.setenv("ANTHROPIC_API_KEY", f"sk-ant-{CANARY}")
    dump = tmp_path / "agent-env.json"

    outcome = _run(fixture_repo, tmp_path, agent_argv=_python_agent(_ENV_PROBE.format(dump=str(dump))))

    assert isinstance(outcome, TaskDiff)
    observed = json.loads(dump.read_text(encoding="utf-8"))["env"]
    assert "MY_SECRET_PROBE" not in observed
    assert "ANTHROPIC_API_KEY" not in observed
    assert CANARY not in json.dumps(observed)

    profile = build_volunteer_profile(
        _manifest(), available_backends=("container",), donor_accepts_plain_container=True
    )
    derived = sandbox_env(profile)
    assert {key: observed.get(key) for key in derived} == derived, (
        "the spawned environment is not the one the profile derived"
    )
    assert set(observed) - set(derived) <= _PLATFORM_INJECTED_ENV, (
        f"the spawn carries variables the profile did not derive: {sorted(set(observed) - set(derived))}"
    )


def test_issue_text_reaches_the_agent_as_a_file_and_never_as_an_argument(fixture_repo: Path, tmp_path: Path) -> None:
    """Untrusted text is data the agent opens, not a word in a command line.

    Two things are proved together because either alone is misleading: the text
    did arrive (a runner that dropped it would pass a "not in argv" assertion
    trivially), and it arrived only through the file.
    """
    dump = tmp_path / "agent-argv.json"

    outcome = _run(
        fixture_repo,
        tmp_path,
        agent_argv=_python_agent(_ENV_PROBE.format(dump=str(dump))),
        task=_task(
            fixture_repo,
            issue_title=f"title {CANARY}-title",
            issue_body=f"body {CANARY}-body",
        ),
    )

    assert isinstance(outcome, TaskDiff)
    prompt = (outcome.worktree_path / ".sdd" / "runtime" / "volunteer-task.md").read_text(encoding="utf-8")
    assert f"{CANARY}-title" in prompt
    assert f"{CANARY}-body" in prompt

    observed = json.loads(dump.read_text(encoding="utf-8"))
    assert CANARY not in json.dumps(observed["argv"])
    assert CANARY not in json.dumps(observed["env"])


@POSIX_ONLY
def test_the_clone_never_carries_a_host_credential_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same canary one layer earlier, where it is easier to miss.

    The repository URL comes from a claimed task, so the clone is a trusted
    program pointed at an untrusted host.  Handing it the donor's tokens, ssh
    agent or ``~/.gitconfig`` credential helper would send them wherever that
    URL points.  Observed by putting a shim named ``git`` first on the path and
    reading the environment it was actually given.
    """
    monkeypatch.setenv("GITHUB_TOKEN", f"ghp_{CANARY}")
    monkeypatch.setenv("SSH_AUTH_SOCK", f"/tmp/{CANARY}.sock")
    dump = tmp_path / "clone-env.txt"
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(f"#!/bin/sh\n/usr/bin/env > '{dump}'\nexit 1\n", encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim_dir))

    outcome = _run(tmp_path / "some-repo", tmp_path, agent_argv=mock_agent_argv(fix="off-by-one"))

    assert isinstance(outcome, TaskRefusal)
    assert outcome.stage == RefusalStage.CLONE
    observed = dump.read_text(encoding="utf-8")
    assert CANARY not in observed
    assert "GITHUB_TOKEN" not in observed
    assert "SSH_AUTH_SOCK" not in observed
    assert "GIT_TERMINAL_PROMPT=0" in observed, "the clone could still block on a credential prompt"
    home = next(line for line in observed.splitlines() if line.startswith("HOME="))
    assert home != f"HOME={Path.home()}", "the clone reads the donor's ~/.gitconfig and its credential helper"
    assert home.endswith("git-home"), home
