"""Parameterized contract tests - all adapters satisfy CLIAdapter interface.

The adapter list is discovered dynamically from the registry so that newly
registered adapters are automatically exercised by the contract suite.  A
second suite replays recorded golden transcripts (``tests/golden/*.yaml``)
and asserts the actual Popen argv still matches - this catches silent CLI
flag regressions that pure interface checks miss.
"""

from __future__ import annotations

import inspect
import re
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from bernstein.core.models import ModelConfig

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.clm import CLM_ENDPOINT_ENV, CLM_MODEL_ENV, CLM_TOKEN_ENV
from bernstein.adapters.generic import GenericAdapter
from bernstein.adapters.registry import _ADAPTERS, get_adapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_popen_mock(pid: int) -> MagicMock:
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    m.stdout = MagicMock()
    return m


def _popen_path(adapter: CLIAdapter) -> str:
    """Return the module path for patching subprocess.Popen for a given adapter."""
    mod = type(adapter).__module__
    return f"{mod}.subprocess.Popen"


# ---------------------------------------------------------------------------
# Adapter factories - dynamically discovered from the adapter registry.
#
# The mock adapter is excluded because it spawns a real subprocess (it is
# the fixture backing live conformance tests) rather than going through the
# common subprocess.Popen path the contract suite mocks.  The generic
# adapter is instantiated separately because it requires a ``cli_command``
# constructor argument.
#
# Per-adapter spawn inputs for ``test_spawn_returns_spawn_result`` are keyed
# on ``adapter.name()`` below.  Adapters whose spawn() needs credentials,
# runtime config, or a host-local login cache are still in the parametrized
# class; only that one case gets adapter-specific inputs or an environment
# skip (see the spawn test).
# ---------------------------------------------------------------------------

#: Per-adapter ``ModelConfig`` overrides for ``test_spawn_returns_spawn_result``.
_CONTRACT_MODEL_CONFIG: dict[str, ModelConfig] = {
    "clm": ModelConfig(model="clm-7b-instruct", effort="high"),
    # OpenCode resolves ``-m`` as ``provider/model``; the adapter refuses a bare
    # id it cannot qualify from the operator's config, and the contract suite
    # runs without one, so the case hands it an already-qualified id.
    "opencode": ModelConfig(model="anthropic/sonnet", effort="high"),
}

#: Extra keyword arguments forwarded to ``spawn()`` in the contract suite.
_CONTRACT_SPAWN_KWARGS: dict[str, dict[str, Any]] = {
    "PythonRuntime": {
        "mcp_config": {"runtime_module": "bernstein.adapters.python_runtime_runner"},
    },
}

#: Process env patches required before ``spawn()`` reaches ``Popen``.
_CONTRACT_SPAWN_ENV: dict[str, dict[str, str]] = {
    "clm": {
        CLM_ENDPOINT_ENV: "https://clm.internal.example/v1/",
        CLM_TOKEN_ENV: "scoped-jwt-contract-test",
        CLM_MODEL_ENV: "clm-7b-instruct",
    },
}


def _discover_registered_names() -> list[str]:
    """Return sorted adapter names registered in the adapter registry.

    Excludes:
    - ``mock`` - spawns a real subprocess (live conformance fixture); the
      whole class is omitted because every case would hit a real ``Popen``.
    - ``generic`` - constructed with explicit kwargs below; omitted from
      discovery and added as its own factory row.
    """
    return sorted(n for n in _ADAPTERS if n not in {"mock", "generic"})


#: Adapters whose ``prompt`` argument is a structured descriptor rather than
#: free-form instruction text. Only the *input shape* differs - every contract
#: assertion still applies - so the spawn case is handed a prompt the adapter
#: accepts instead of the adapter being dropped from the whole class. Excluding
#: it would take the other twelve cases with it: garak went in by name and the
#: suite lost thirteen collected cases in one commit. Keyed on ``adapter.name()``
#: (the registry key), not on the class name the parametrize id carries.
_CONTRACT_PROMPT: dict[str, str] = {
    # garak's prompt IS the target descriptor (``--target <type>:<name>``); it
    # refuses to spawn without one, which is the documented behaviour proved in
    # tests/unit/test_adapter_garak.py.
    "garak": "openai:gpt-4o",
}


def _make_factory(name: str) -> Any:
    """Build a zero-arg factory that instantiates a registered adapter."""

    def _factory() -> CLIAdapter:
        return get_adapter(name)

    return _factory


_ADAPTER_FACTORIES: list[tuple[str, Any]] = [
    *((type(get_adapter(n)).__name__, _make_factory(n)) for n in _discover_registered_names()),
    ("GenericAdapter", lambda: GenericAdapter(cli_command="test-cli")),
]


# ---------------------------------------------------------------------------
# Contract: all adapters are subclasses of CLIAdapter
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("no_watchdog_threads")
@pytest.mark.parametrize(
    "name,factory",
    _ADAPTER_FACTORIES,
    ids=[f[0] for f in _ADAPTER_FACTORIES],
)
class TestAdapterContract:
    """Every adapter must satisfy the CLIAdapter abstract interface.

    The watchdog-threads fixture is applied class-wide - the contract
    suite parameterizes across every registered adapter, and each
    ``spawn()`` call would otherwise arm a daemon Timer that outlives
    the test and eventually hits the runner's thread limit on CI.
    """

    def test_is_subclass_of_cli_adapter(self, name: str, factory: Any) -> None:
        adapter = factory()
        assert isinstance(adapter, CLIAdapter)

    def test_has_spawn_method(self, name: str, factory: Any) -> None:
        adapter = factory()
        assert hasattr(adapter, "spawn")
        assert callable(adapter.spawn)

    def test_has_name_method(self, name: str, factory: Any) -> None:
        adapter = factory()
        assert hasattr(adapter, "name")
        assert callable(adapter.name)

    def test_name_returns_non_empty_string(self, name: str, factory: Any) -> None:
        adapter = factory()
        result = adapter.name()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_has_is_alive_method(self, name: str, factory: Any) -> None:
        adapter = factory()
        assert hasattr(adapter, "is_alive")
        assert callable(adapter.is_alive)

    def test_has_kill_method(self, name: str, factory: Any) -> None:
        adapter = factory()
        assert hasattr(adapter, "kill")
        assert callable(adapter.kill)

    def test_has_detect_tier_method(self, name: str, factory: Any) -> None:
        adapter = factory()
        assert hasattr(adapter, "detect_tier")
        assert callable(adapter.detect_tier)

    def test_spawn_signature_matches_base(self, name: str, factory: Any) -> None:
        """Every base parameter must be untouched; adapter-specific extras
        must be keyword-only with a default.

        The spawner threads optional capabilities (e.g. ``explicit_max_turns``)
        to adapters by inspecting ``spawn()`` signatures, so adapters may
        extend the base contract - but only in a way that keeps every
        base-shaped call site working unchanged.
        """
        adapter = factory()
        base_sig = inspect.signature(CLIAdapter.spawn)
        adapter_sig = inspect.signature(type(adapter).spawn)
        assert adapter_sig.return_annotation == base_sig.return_annotation, (
            f"{name}.spawn() changed the return annotation"
        )
        for pname, param in base_sig.parameters.items():
            assert pname in adapter_sig.parameters, f"{name}.spawn() is missing base parameter {pname!r}"
            assert adapter_sig.parameters[pname] == param, f"{name}.spawn() altered base parameter {pname!r}"
        for pname, param in adapter_sig.parameters.items():
            if pname in base_sig.parameters:
                continue
            assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
                f"{name}.spawn() extra parameter {pname!r} must be keyword-only"
            )
            assert param.default is not inspect.Parameter.empty, (
                f"{name}.spawn() extra parameter {pname!r} must have a default"
            )

    def test_spawn_returns_spawn_result(self, name: str, factory: Any, tmp_path: Path) -> None:
        adapter = factory()
        module = sys.modules[type(adapter).__module__]
        if not hasattr(module, "subprocess"):
            # This case, and only this case, assumes the adapter reaches its
            # agent through ``subprocess.Popen`` in its own module. An adapter
            # that drives a server over HTTP has nothing to patch here, and the
            # same SpawnResult contract is proved against its real transport in
            # its own suite (skyvern: tests/unit/adapters/test_skyvern_adapter.py).
            # Keyed on the property rather than on a name, and narrowed to one
            # case: excluding such an adapter from the whole class drops the
            # twelve contract cases that do apply to it.
            pytest.skip(f"{name} does not spawn through subprocess.Popen")
        spawn_mod = inspect.getmodule(type(adapter).spawn)
        if spawn_mod is not None:
            if hasattr(spawn_mod, "_has_q_login_cache") and not spawn_mod._has_q_login_cache():
                pytest.skip(
                    "AWS Q Developer login cache not present on this host (run `q login` once before exercising spawn)",
                )
            if hasattr(spawn_mod, "_detect_tool") and spawn_mod._detect_tool() is None:
                pytest.skip("No IaC CLI (terraform or pulumi) on PATH")
        proc_mock = _make_popen_mock(pid=42)
        popen_target = _popen_path(adapter)

        # Claude adapter needs special handling (two Popen calls)
        side = [proc_mock, _make_popen_mock(pid=43)] if "claude" in popen_target else [proc_mock]

        adapter_key = adapter.name()
        model_config = _CONTRACT_MODEL_CONFIG.get(
            adapter_key,
            ModelConfig(model="sonnet", effort="high"),
        )
        spawn_kwargs = _CONTRACT_SPAWN_KWARGS.get(adapter_key, {})
        env_patch = _CONTRACT_SPAWN_ENV.get(adapter_key)
        env_ctx = patch.dict("os.environ", env_patch) if env_patch else nullcontext()

        with patch(popen_target, side_effect=side), env_ctx:
            result = adapter.spawn(
                prompt=_CONTRACT_PROMPT.get(adapter.name(), "test prompt"),
                workdir=tmp_path,
                model_config=model_config,
                session_id="contract-test",
                **spawn_kwargs,
            )
        assert isinstance(result, SpawnResult)
        assert isinstance(result.pid, int)
        assert isinstance(result.log_path, Path)

    def test_is_alive_returns_bool(self, name: str, factory: Any) -> None:
        adapter = factory()
        with patch("bernstein.adapters.base.process_alive", return_value=True):
            result = adapter.is_alive(99999)
        assert isinstance(result, bool)

    def test_kill_does_not_raise(self, name: str, factory: Any) -> None:
        adapter = factory()
        with patch("bernstein.adapters.base.reap_process_group"):
            adapter.kill(999)  # must not raise

    def test_kill_suppresses_oserror(self, name: str, factory: Any) -> None:
        adapter = factory()
        with patch("bernstein.adapters.base.reap_process_group", return_value=False):
            adapter.kill(99999)  # must not raise

    def test_detect_tier_returns_none_or_api_tier_info(self, name: str, factory: Any) -> None:
        adapter = factory()
        result = adapter.detect_tier()
        # Base implementation returns None; subclasses may return ApiTierInfo
        if result is not None:
            from bernstein.core.models import ApiTierInfo

            assert isinstance(result, ApiTierInfo)


# ---------------------------------------------------------------------------
# Collection guard - whole-class adapter exclusions drop 13 cases each (#4935)
# ---------------------------------------------------------------------------

# 680 after restoring five adapters plus the guard test on current upstream main.
_EXPECTED_COLLECTED = 681


def test_module_collected_case_count() -> None:
    """Fail if a registry name is excluded from the whole contract class again."""
    module = Path(__file__)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(module), "--collect-only", "-q"],
        capture_output=True,
        text=True,
        check=True,
        timeout=180,
    )
    combined = proc.stdout + proc.stderr
    match = re.search(r"(\d+) tests collected", combined)
    assert match is not None, combined
    assert int(match.group(1)) == _EXPECTED_COLLECTED, combined


# ---------------------------------------------------------------------------
# Golden transcript replay - catches CLI flag regressions that interface
# checks miss.  Each transcript records the inner CLI argv (after the
# ``bernstein-worker -- `` separator) and the set of credential env keys
# the adapter declared via ``build_filtered_env``.
# ---------------------------------------------------------------------------

_GOLDEN_DIR = Path(__file__).parent.parent / "golden"


def _load_replay_transcripts() -> list[tuple[str, dict[str, Any]]]:
    """Load golden YAMLs that declare ``inner_argv`` (replay-capable)."""
    if not _GOLDEN_DIR.exists():
        return []
    loaded: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(_GOLDEN_DIR.glob("*.yaml")):
        raw_obj: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw_obj, dict):
            continue
        raw: dict[str, Any] = dict(raw_obj)
        if "adapter_class" not in raw or "steps" not in raw:
            continue
        steps_raw = raw.get("steps") or []
        # Only replay transcripts that declare an expected inner_argv.
        if not any(isinstance(s, dict) and "inner_argv" in s for s in steps_raw):
            continue
        loaded.append((path.stem, raw))
    return loaded


def _import_adapter_class(dotted: str) -> type[CLIAdapter]:
    """Import a CLIAdapter subclass by dotted path."""
    import importlib

    module_path, class_name = dotted.rsplit(".", 1)
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    assert inspect.isclass(cls) and issubclass(cls, CLIAdapter)
    return cls


def _split_inner_argv(argv: list[str]) -> list[str]:
    """Return the portion of argv after the bernstein-worker ``--`` separator.

    Every adapter wraps its CLI through ``bernstein.core.orchestration.worker``
    so the first ``--`` marker splits the wrapper flags from the actual CLI
    invocation.  If no ``--`` is found the full argv is returned unchanged.
    """
    for i, tok in enumerate(argv):
        if tok == "--":
            return argv[i + 1 :]
    return argv


def _strip_json_payloads(argv: list[str], json_flags: list[str]) -> list[str]:
    """Return argv with each ``flag <payload>`` pair for the listed flags removed."""
    if not json_flags:
        return argv
    out: list[str] = []
    skip_next = False
    for tok in argv:
        if skip_next:
            skip_next = False
            continue
        if tok in json_flags:
            skip_next = True
            continue
        out.append(tok)
    return out


_GOLDEN_REPLAY_TRANSCRIPTS = _load_replay_transcripts()


@pytest.mark.parametrize(
    "transcript_name,transcript",
    _GOLDEN_REPLAY_TRANSCRIPTS or [("<none>", {})],
    ids=[t[0] for t in _GOLDEN_REPLAY_TRANSCRIPTS] or ["<none>"],
)
class TestGoldenReplay:
    """Replay recorded transcripts; fail if CLI argv or env keys drift."""

    def test_replay_matches_recorded_argv(
        self,
        transcript_name: str,
        transcript: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if transcript_name == "<none>" or not transcript:
            pytest.skip("No replay-capable golden transcripts found")

        adapter_cls = _import_adapter_class(str(transcript["adapter_class"]))
        ctor_kwargs = dict(transcript.get("ctor_kwargs") or {})
        adapter = adapter_cls(**ctor_kwargs)

        # Capture the extra_keys passed to build_filtered_env so we can
        # assert adapter-declared credential env without being sensitive
        # to the outer test environment.  Every adapter under test uses
        # the shared build_filtered_env helper.
        import importlib

        import bernstein.adapters.env_isolation as env_isolation

        captured_extra_keys: list[tuple[str, ...]] = []
        original_build = env_isolation.build_filtered_env

        def _spy(extra_keys: Any = (), **kwargs: Any) -> dict[str, str]:
            keys_tuple = tuple(extra_keys)
            captured_extra_keys.append(keys_tuple)
            return original_build(extra_keys, **kwargs)

        # Patch every module that imported the symbol directly.
        adapter_module = importlib.import_module(adapter_cls.__module__)
        monkeypatch.setattr(env_isolation, "build_filtered_env", _spy)
        if hasattr(adapter_module, "build_filtered_env"):
            monkeypatch.setattr(adapter_module, "build_filtered_env", _spy)

        # Gemini adapter consults `shutil.which` to pick between the
        # `antigravity` and `gemini` binaries (#1740 cascade). On hosts
        # where only one happens to be installed, the recorded golden
        # would drift from the actual argv. Pin the resolver to "neither
        # installed" so non-strict discovery returns the first cascade
        # entry deterministically across local + CI runs. Scoped to the
        # gemini adapter to avoid leaking into other tests' subprocess
        # lookups (shutil is a shared module).
        if adapter_cls.__module__ == "bernstein.adapters.gemini":
            monkeypatch.setattr(
                "bernstein.adapters.gemini.resolve_google_cli_binary",
                lambda **_kw: "antigravity",
            )

        default_role = str(transcript.get("session_role") or "replay")
        for step_idx, step in enumerate(transcript.get("steps", [])):
            prompt = str(step["prompt"])
            model = str(step.get("model", "sonnet"))
            pid = int(step.get("expected_pid") or (1000 + step_idx))
            expected_argv = step.get("inner_argv")
            required_json_flags = list(step.get("required_json_flags") or [])
            expected_env_extras = set(step.get("env_extra_keys") or [])
            role = str(step.get("session_role") or default_role)

            popen_target = f"{adapter_cls.__module__}.subprocess.Popen"
            # Claude spawns a second Popen for the wrapper script; supply
            # two mocks so either shape works without diverging per-adapter.
            mocks = [_make_popen_mock(pid), _make_popen_mock(pid + 1)]

            with patch(popen_target, side_effect=mocks) as popen_mock:
                result = adapter.spawn(
                    prompt=prompt,
                    workdir=tmp_path,
                    model_config=ModelConfig(model=model, effort="low"),
                    session_id=f"{role}-{step_idx}",
                    timeout_seconds=0,  # disable watchdog in tests
                )

            assert isinstance(result, SpawnResult)
            assert popen_mock.call_args_list, f"{transcript_name} step {step_idx}: Popen never called"

            # The first Popen call is always the CLI invocation under test.
            call_args, _ = popen_mock.call_args_list[0]
            actual_argv = list(call_args[0])
            inner = _split_inner_argv(actual_argv)

            if expected_argv is not None:
                expected_list = [str(x) for x in expected_argv]
                # Strip payload args for JSON-valued flags - their content is
                # version-sensitive; the flag's presence is asserted below.
                stripped_inner = _strip_json_payloads(inner, required_json_flags)
                stripped_expected = _strip_json_payloads(expected_list, required_json_flags)
                assert stripped_inner == stripped_expected, (
                    f"{transcript_name} step {step_idx}: inner argv drift\n"
                    f"  expected: {stripped_expected}\n"
                    f"  actual:   {stripped_inner}"
                )

            for flag in required_json_flags:
                assert flag in inner, (
                    f"{transcript_name} step {step_idx}: required JSON flag {flag!r} missing from argv"
                )

            if expected_env_extras:
                seen_extras: set[str] = set()
                for keys_tuple in captured_extra_keys:
                    seen_extras.update(keys_tuple)
                assert expected_env_extras <= seen_extras, (
                    f"{transcript_name} step {step_idx}: env extras regression\n"
                    f"  expected keys: {sorted(expected_env_extras)}\n"
                    f"  seen keys:     {sorted(seen_extras)}"
                )
