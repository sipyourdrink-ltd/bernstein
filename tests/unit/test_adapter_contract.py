"""Parameterized contract tests - all adapters satisfy CLIAdapter interface.

The adapter list is discovered dynamically from the registry so that newly
registered adapters are automatically exercised by the contract suite.  A
second suite replays recorded golden transcripts (``tests/golden/*.yaml``)
and asserts the actual Popen argv still matches - this catches silent CLI
flag regressions that pure interface checks miss.
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from bernstein.core.models import ModelConfig

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.generic import GenericAdapter
from bernstein.adapters.registry import _ADAPTERS, get_adapter

PROJECT_ROOT = Path(__file__).resolve().parents[2]

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


def _sonar_properties() -> dict[str, str]:
    """Return logical key-value entries from sonar-project.properties."""
    properties: dict[str, str] = {}
    pending = ""
    for raw_line in (PROJECT_ROOT / "sonar-project.properties").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1]
            continue
        line = f"{pending}{line}"
        pending = ""
        if "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        properties[key.strip()] = value.strip()
    return properties


# ---------------------------------------------------------------------------
# Adapter factories - dynamically discovered from the adapter registry.
#
# The mock adapter is excluded because it spawns a real subprocess (it is
# the fixture backing live conformance tests) rather than going through the
# common subprocess.Popen path the contract suite mocks.  The generic
# adapter is instantiated separately because it requires a ``cli_command``
# constructor argument.
# ---------------------------------------------------------------------------


def _discover_registered_names() -> list[str]:
    """Return sorted adapter names registered in the adapter registry.

    Excludes:
    - ``mock`` - spawns a real subprocess (live conformance fixture).
    - ``generic`` - constructed with explicit kwargs below.
    - ``iac`` - spawn() raises RuntimeError unless terraform or pulumi is
      on PATH; CI runners (especially macOS) don't have these and the
      contract test mocks Popen but can't mock the binary-availability
      check.  The adapter has its own integration test that skips when
      no tool is installed.
    - ``clm`` - spawn() raises ClmConfigError unless CLM_ENDPOINT/TOKEN/MODEL
      are set; the contract test mocks Popen but can't satisfy the runtime
      config requirement.  Covered by ``test_adapter_clm.py`` and the
      integration suite under ``tests/integration/adapters/``.
    - ``q_dev`` - spawn() raises SpawnError unless a ``q login`` cache
      exists on disk; CI runners don't have one and the contract test
      mocks Popen but can't fake an authenticated AWS Builder ID
      session.  Covered by ``test_adapter_q_dev.py`` which monkeypatches
      ``Path.home`` to stand up a fake cache directory.
    """
    return sorted(n for n in _ADAPTERS if n not in {"mock", "generic", "iac", "clm", "q_dev"})


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
        proc_mock = _make_popen_mock(pid=42)
        popen_target = _popen_path(adapter)

        # Claude adapter needs special handling (two Popen calls)
        side = [proc_mock, _make_popen_mock(pid=43)] if "claude" in popen_target else [proc_mock]

        with patch(popen_target, side_effect=side):
            result = adapter.spawn(
                prompt="test prompt",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="contract-test",
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


def test_sonar_s2638_exclusion_is_scoped_to_adapter_spawn_contracts() -> None:
    """Adapter overrides keep the base signature; the Sonar exclusion stays narrow."""
    properties = _sonar_properties()
    criteria = [item.strip() for item in properties["sonar.issue.ignore.multicriteria"].split(",")]
    matching_resource_keys = [
        properties[f"sonar.issue.ignore.multicriteria.{criterion}.resourceKey"]
        for criterion in criteria
        if properties.get(f"sonar.issue.ignore.multicriteria.{criterion}.ruleKey") == "python:S2638"
    ]

    assert matching_resource_keys == ["src/bernstein/adapters/*.py"]


def test_sonar_s2638_exclusion_matches_top_level_adapters() -> None:
    """The scoped S2638 exclusion must match Sonar's top-level adapter paths."""
    properties = _sonar_properties()
    resource_key = properties["sonar.issue.ignore.multicriteria.e18.resourceKey"]

    assert PurePosixPath("src/bernstein/adapters/aichat.py").match(resource_key)


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
