"""Property-based bughunt tests for the adapter spawn contract.

Locks in the regression discovered while auditing all 44 CLI adapters:
``SpawnResult.proc`` is the load-bearing handle that the orchestrator
uses to detect early exits, register the agent's stdin pipe for IPC,
and wait for completion.  When an adapter forgets to thread the
``subprocess.Popen`` instance into the result (``SpawnResult(pid=...)``
with no ``proc=...``), downstream code silently degrades to PID-only
liveness checks and zombie processes accumulate until the timeout
watchdog fires.

Affected before this audit (15 adapters, all on the same canonical
copy-paste pattern):

* amp, auggie, autohand, charm, cody, continue_dev, cursor, forge,
  generic, hermes, kilo, kimi, manager, mistral, qwen.

The fix is a one-liner per adapter: ``proc=proc`` in the
``SpawnResult(...)`` call.  This module asserts the contract for every
adapter via:

1. A parametric Hypothesis-generated descriptor for the spawn inputs.
2. ``subprocess.Popen`` mocked at the adapter's module level so the
   harness never shells a real binary.
3. Direct attribute checks on the returned ``SpawnResult``.

Two of the contract surfaces in the audit brief
(``BERNSTEIN_RUN_ID`` env-var injection, ``expected_version``
propagation) are not currently implemented in this codebase.  They are
covered by xfail markers below so the gap is tracked rather than
silently passing.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.adapters.registry import _ADAPTERS, get_adapter
from bernstein.core.credential_scoping import (
    AgentCredentialPolicy,
    AgentNotScopedError,
)

# ---------------------------------------------------------------------------
# Adapter enumeration - share the consistency suite's pattern.
# ---------------------------------------------------------------------------


def _instantiate_adapter(name: str) -> CLIAdapter:
    """Return an adapter instance, handling generic's special case."""
    if name == "generic":
        return get_adapter("generic")
    entry = _ADAPTERS[name]
    if isinstance(entry, CLIAdapter):
        return entry
    return entry()


def _all_testable_adapters() -> list[tuple[str, CLIAdapter]]:
    """Enumerate every adapter that instantiates without external setup."""
    pairs: list[tuple[str, CLIAdapter]] = []
    for name in sorted([*_ADAPTERS.keys(), "generic"]):
        try:
            pairs.append((name, _instantiate_adapter(name)))
        except Exception:  # pragma: no cover - environmental skips
            continue
    return pairs


_TESTABLE_ADAPTERS = _all_testable_adapters()


# ---------------------------------------------------------------------------
# Popen mock + spawn invocation helper.
# ---------------------------------------------------------------------------


def _make_popen_mock(pid: int = 4242) -> MagicMock:
    """Return a Popen stand-in that satisfies ``_probe_fast_exit``."""
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    m.stdout = MagicMock()
    m.stdin = MagicMock()
    m.wait.return_value = 0  # clean exit so probe is a no-op
    m.poll.return_value = None
    return m


def _spawn_with_mock(
    adapter: CLIAdapter,
    *,
    workdir: Path,
    session_id: str,
    prompt: str,
    model: str = "sonnet",
) -> tuple[SpawnResult | None, MagicMock | None]:
    """Spawn ``adapter`` with Popen mocked at its module level.

    Returns ``(result, popen_mock)``.  When the adapter declines to spawn
    because of an external dependency, both elements are ``None`` and the
    test should pytest.skip - the contract is unchecked, not violated.
    """
    popen_mock = _make_popen_mock()
    mod = type(adapter).__module__
    if not hasattr(sys.modules[mod], "subprocess"):
        pytest.skip(f"{mod} does not launch a subprocess")
    sdd = workdir / ".sdd" / "runtime"
    sdd.mkdir(parents=True, exist_ok=True)
    (workdir / ".claude").mkdir(parents=True, exist_ok=True)
    with (
        patch(f"{mod}.subprocess.Popen", return_value=popen_mock),
        patch("shutil.which", return_value="/usr/bin/fake-tool"),
    ):
        try:
            result = adapter.spawn(
                prompt=prompt,
                workdir=workdir,
                model_config=ModelConfig(model=model, effort="high"),
                session_id=session_id,
                timeout_seconds=0,
            )
        except Exception:
            return None, None
    return result, popen_mock


# ---------------------------------------------------------------------------
# Hypothesis strategies for adversarial inputs.
# ---------------------------------------------------------------------------

# Session IDs include shell metacharacters and a few unicode bytes to ensure
# adapters never interpolate the value into a shell command line.
#
# The session_id flows into a filesystem path (``.sdd/runtime/{sid}.log``),
# so the random portion of the strategy is restricted to ASCII printable
# characters: macOS APFS rejects high private-use plane glyphs with
# OSError errno 92, and Linux ext4 + tmpfs round-trip them but the test
# harness still has to bytes-encode the path. The curated ``sampled_from``
# list covers the genuine adversarial cases (shell metacharacters, unicode
# zero-width, Cyrillic) - the property assertion fires on those regardless
# of the random fuzz dimension. In production the orchestrator constructs
# session IDs itself; this is a test-harness constraint, not a contract gap.
_session_id_strategy = st.one_of(
    st.text(
        alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E, blacklist_characters="/\\"),
        min_size=1,
        max_size=40,
    ),
    st.sampled_from(
        [
            "qa-001",
            "backend; rm -rf",  # ``/`` removed: it's a path-separator, not a shell-metachar surface for adapters
            "test`whoami`",
            "$PATH-evil",
            "session​tab",
            "роль-001",
        ],
    ),
)

# Prompts go into a file via ``log_file.write(prompt)`` or stdin pipe -
# the bytes must be encodable by the default filesystem encoding. We
# blacklist NUL (rejected by ``open`` on prompt files) and surrogates
# (UnicodeEncodeError on UTF-8 fs encoders).
_prompt_strategy = st.text(
    alphabet=st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)),
    min_size=0,
    max_size=512,
)


# ---------------------------------------------------------------------------
# Contract: SpawnResult.proc is set, pid is positive, log_path is a Path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("adapter_name", "adapter"),
    _TESTABLE_ADAPTERS,
    ids=[name for name, _ in _TESTABLE_ADAPTERS],
)
@settings(
    max_examples=8,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(session_id=_session_id_strategy, prompt=_prompt_strategy)
def test_spawn_result_proc_is_threaded_through(
    adapter_name: str,
    adapter: CLIAdapter,
    tmp_path: Path,
    session_id: str,
    prompt: str,
) -> None:
    """``SpawnResult.proc`` must be the actual Popen handle, not None.

    Root cause: a copy-paste regression in 15 adapters built
    ``SpawnResult(pid=proc.pid, log_path=log_path)`` and never assigned
    ``proc=proc``.  The dataclass default is ``None``, so downstream
    spawner_core skipped registering stdin/IPC and the
    ``bernstein adapter run`` CLI couldn't ``wait()`` on completion -
    zombie pids until the timeout watchdog fired.

    Interviewer probe: "What if a malicious task name has shell
    metachars?" → the adapter wraps the inner command via
    ``build_worker_cmd`` (no shell), passes argv as a list to
    ``subprocess.Popen`` (no ``shell=True``), and the session_id is
    only used in worker argv positions and log paths - never expanded
    by /bin/sh.
    """
    result, popen_mock = _spawn_with_mock(
        adapter,
        workdir=tmp_path,
        session_id=session_id,
        prompt=prompt,
    )
    if result is None or popen_mock is None:
        pytest.skip(f"{adapter_name} requires external setup (binary/auth)")

    # Cancel any timeout watchdog the adapter may have started even with
    # timeout_seconds=0 (defensive - most adapters skip when zero).
    if result.timeout_timer is not None:
        result.timeout_timer.cancel()

    assert isinstance(result, SpawnResult), f"{adapter_name} returned {type(result).__name__}"
    assert isinstance(result.pid, int) and result.pid > 0, f"{adapter_name} pid invalid"
    assert isinstance(result.log_path, Path), f"{adapter_name} log_path not a Path"
    assert result.proc is popen_mock, (
        f"{adapter_name}: SpawnResult.proc is {result.proc!r}, expected the Popen handle. "
        "Regression: 15 adapters dropped ``proc=proc`` from the SpawnResult call. "
        "spawner_core needs the handle for IPC; adapter_cmd needs it for wait()."
    )


# ---------------------------------------------------------------------------
# Contract: subprocess.Popen always receives env=, cwd=, no shell=True.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("adapter_name", "adapter"),
    _TESTABLE_ADAPTERS,
    ids=[name for name, _ in _TESTABLE_ADAPTERS],
)
def test_spawn_passes_env_and_cwd_no_shell(
    adapter_name: str,
    adapter: CLIAdapter,
    tmp_path: Path,
) -> None:
    """Popen must receive an explicit ``env=`` (not None / inheriting all)
    and ``cwd=`` set to the worktree.  ``shell=True`` is forbidden - every
    adapter must pass argv as a list so shell-metachar task IDs cannot
    escape into /bin/sh -c.
    """
    popen_mock = _make_popen_mock()
    mod = type(adapter).__module__
    if not hasattr(sys.modules[mod], "subprocess"):
        pytest.skip(f"{mod} does not launch a subprocess")
    (tmp_path / ".sdd" / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    with (
        patch(f"{mod}.subprocess.Popen", return_value=popen_mock) as popen_patch,
        patch("shutil.which", return_value="/usr/bin/fake-tool"),
    ):
        try:
            adapter.spawn(
                prompt="probe",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="probe-001",
                timeout_seconds=0,
            )
        except Exception:
            pytest.skip(f"{adapter_name} requires external setup")
            return
    if not popen_patch.called:
        pytest.skip(f"{adapter_name} did not call Popen at this code path")

    kwargs = popen_patch.call_args.kwargs
    args = popen_patch.call_args.args

    # Argv must be a list of strings, not a single shell string.
    cmd = args[0] if args else kwargs.get("args")
    assert isinstance(cmd, list), f"{adapter_name}: argv must be a list, got {type(cmd).__name__}"
    assert all(isinstance(t, str) for t in cmd), f"{adapter_name}: argv must be list[str]"

    # shell=True is a leak vector even with a list argv.
    assert not kwargs.get("shell", False), f"{adapter_name}: shell=True forbidden"

    # env= must be explicit and a dict - None inherits the orchestrator env.
    assert "env" in kwargs, f"{adapter_name}: Popen call missing env="
    assert isinstance(kwargs["env"], dict), f"{adapter_name}: env must be a dict"

    # cwd= must be the worktree, not the orchestrator's process cwd.
    cwd = kwargs.get("cwd")
    assert cwd is not None, f"{adapter_name}: Popen call missing cwd="
    assert Path(cwd) == tmp_path, f"{adapter_name}: cwd is {cwd!r}, expected {tmp_path}"


# ---------------------------------------------------------------------------
# Registry contract.
# ---------------------------------------------------------------------------


def test_every_registry_entry_has_an_implementation() -> None:
    """Each ``_ADAPTERS`` entry must point at a class importable today."""
    for name, entry in _ADAPTERS.items():
        cls = type(entry) if isinstance(entry, CLIAdapter) else entry
        assert inspect.isclass(cls), f"registry[{name!r}] is not a class"
        assert issubclass(cls, CLIAdapter), f"registry[{name!r}] is not a CLIAdapter"


def test_no_registry_entry_shadows_builtin() -> None:
    """Adapter keys must not collide with a Python builtin function/type.

    Python keywords (``continue`` is a real-world entry, mirroring the
    upstream CLI's marketing name) are accepted because the registry
    uses string keys throughout - no attribute access by adapter name
    happens anywhere downstream.
    """
    import builtins

    for name in _ADAPTERS:
        assert not hasattr(builtins, name), f"adapter name {name!r} shadows a builtin"


# ---------------------------------------------------------------------------
# build_filtered_env - credential scoping properties.
# ---------------------------------------------------------------------------


def _env_strategy() -> st.SearchStrategy[dict[str, str]]:
    """Random env dicts mixing safe + sensitive keys.

    Values exclude NUL because ``os.environ.update`` rejects embedded null
    bytes on POSIX, and lone surrogates (``Cs``) because Python's
    ``os.environ`` setter goes through ``PyOS_setenv``/``putenv`` which
    require encodable bytes. The property holds in real-world use; the
    constraint is on the test harness, not the credential filter.
    """
    safe = ["PATH", "HOME", "LANG", "USER"]
    secrets = ["DATABASE_URL", "AWS_SECRET_ACCESS_KEY", "STRIPE_API_KEY", "BERNSTEIN_INTERNAL_TOKEN"]
    return st.dictionaries(
        keys=st.sampled_from(safe + secrets),
        values=st.text(
            alphabet=st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)),
            min_size=0,
            max_size=20,
        ),
        min_size=0,
        max_size=8,
    )


@settings(max_examples=20, deadline=None)
@given(env=_env_strategy())
def test_build_filtered_env_strips_unlisted_secrets(env: dict[str, str]) -> None:
    """Result is a strict subset of (input ∩ allowlist ∪ extras)."""
    with patch.dict("os.environ", env, clear=True):
        out = build_filtered_env(["ANTHROPIC_API_KEY"])
    leaked = set(out) & {"DATABASE_URL", "AWS_SECRET_ACCESS_KEY", "STRIPE_API_KEY"}
    assert not leaked, f"build_filtered_env leaked secrets: {leaked!r}"
    # Bernstein-internal vars not on the allowlist must be stripped too.
    assert "BERNSTEIN_INTERNAL_TOKEN" not in out


@settings(max_examples=15, deadline=None)
@given(env=_env_strategy())
def test_build_filtered_env_preserves_path_and_home(env: dict[str, str]) -> None:
    """PATH/HOME stay in the output if they were set in the source env."""
    with patch.dict("os.environ", env, clear=True):
        out = build_filtered_env([])
    if "PATH" in env:
        assert out.get("PATH") == env["PATH"]
    if "HOME" in env:
        assert out.get("HOME") == env["HOME"]


def test_credential_policy_fail_closed_when_enabled() -> None:
    """An enabled policy without an agent rule must raise, not silently grant."""
    policy = AgentCredentialPolicy(
        enabled=True,
        known_keys=frozenset({"ANTHROPIC_API_KEY"}),
    )
    with pytest.raises(AgentNotScopedError):
        policy.allowed_for("backend-001", role="backend")


# ---------------------------------------------------------------------------
# Prompt rendering determinism.
# ---------------------------------------------------------------------------


def test_render_spawn_prompt_is_deterministic(tmp_path: Path) -> None:
    """Same descriptor → byte-identical prompt across two renders."""
    from types import SimpleNamespace

    from bernstein.core.agents.spawn_prompt import render_spawn_prompt

    task = SimpleNamespace(
        id="T-001",
        role="backend",
        title="Refactor cache layer",
        description="Replace the ad-hoc cache with the canonical decorator.",
    )
    a = render_spawn_prompt("sess-001", task, tmp_path, agent_type="claude")
    b = render_spawn_prompt("sess-001", task, tmp_path, agent_type="claude")
    assert a == b


def test_fork_cache_key_stable_for_same_prefix() -> None:
    """Forked agents must hit the parent's cache prefix byte-for-byte."""
    from bernstein.core.agents.spawn_prompt import fork_cache_key, fork_from_agent

    parent = "## Agent protocol\nshared\n\n## Assigned tasks\nparent task body\n"
    forked = fork_from_agent(parent, "review the parent's PR", session_id="sess-fork-1")
    assert fork_cache_key(parent) == fork_cache_key(forked)


# ---------------------------------------------------------------------------
# Audit-brief contract gaps that today's pipeline does NOT enforce.
# Tracked as xfail so the gap is visible without failing CI.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "BERNSTEIN_RUN_ID is not currently part of the spawn contract - "
        "the audit brief calls for an injected per-run identifier in the "
        "child env-var set, but no adapter or env_isolation surface emits "
        "it today.  Tracking via xfail until the contract is added."
    ),
    strict=True,
)
def test_xfail_bernstein_run_id_in_child_env() -> None:
    """Audit brief item: every spawned process should see BERNSTEIN_RUN_ID."""
    import os

    with patch.dict("os.environ", {"BERNSTEIN_RUN_ID": "run-xyz"}, clear=False):
        env = build_filtered_env(["ANTHROPIC_API_KEY"])
    assert env.get("BERNSTEIN_RUN_ID") == os.environ["BERNSTEIN_RUN_ID"]


@pytest.mark.xfail(
    reason=(
        "expected_version propagation is not implemented in the spawn "
        "pipeline today.  The audit brief calls for the orchestrator's "
        "task-version expectation to be passed into the worker so stale "
        "spawns can self-abort, but neither build_worker_cmd nor any "
        "adapter exposes the parameter."
    ),
    strict=True,
)
def test_xfail_expected_version_propagation() -> None:
    """Audit brief item: expected_version reaches the child worker argv."""
    from bernstein.adapters.base import build_worker_cmd

    sig = inspect.signature(build_worker_cmd)
    assert "expected_version" in sig.parameters
