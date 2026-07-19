"""Declarative adapter capability profiles and the profile factory.

Covers issue #2663 (the mechanism) and the profile-built agents landed
through it for issue #2610 (the payload).

The invariants pinned here are the ones that make a profile a safe
substitute for a hand-written adapter module:

* a profile round-trips into an adapter that satisfies the same
  ``CLIAdapter`` contract the conformance suite enforces;
* an underspecified profile is refused with a typed error rather than
  producing a half-built adapter;
* the generic CLI fallback still serves agents that have no profile;
* two builds from the same profile are byte-identical in the argv they
  emit and in their content-addressed profile hash.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters._contract import (
    STRATEGY_MATRIX,
    ContractSpec,
    DangerousModeStrategy,
    EventChannel,
    ResumeStrategy,
)
from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.capability_profile import (
    BOOLEAN_CAPABILITIES,
    PROFILES,
    AdapterCapabilityProfile,
    CapabilityMismatchError,
    InvocationSpec,
    ProfileImplementation,
    ProfileValidationError,
    SandboxTier,
    TaskCapabilityRequirements,
    UnknownProfileError,
    build_adapter_class_from_profile,
    build_adapter_from_profile,
    get_profile,
    profile_built_adapter_classes,
    profile_contract_discrepancies,
    select_profile_for,
    unmet_requirements,
)
from bernstein.adapters.conformance import (
    ConformanceHarness,
    assert_strategies_declared,
    load_golden_transcripts,
)
from bernstein.adapters.registry import get_adapter, iter_adapter_specs
from bernstein.core.agents.multimodal import is_multimodal_capable

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Repo-root-relative golden transcript directory, resolved from this
#: file so the suite does not depend on the working directory.
GOLDEN_DIR = Path(__file__).resolve().parents[2] / "golden"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_profile(**overrides: object) -> AdapterCapabilityProfile:
    """Build a valid profile, overriding individual fields for a case."""
    base: dict[str, object] = {
        "name": "demo_agent",
        "display_name": "Demo Agent",
        "invocation": InvocationSpec(
            binary="demo-agent",
            subcommands=("run",),
            model_flag="--model",
            prompt_flag=None,
            extra_args=("--no-stream",),
        ),
        "implementation": ProfileImplementation.FACTORY,
    }
    base.update(overrides)
    return AdapterCapabilityProfile(**base)  # type: ignore[arg-type]


@pytest.fixture
def spawn_workdir(tmp_path: Path) -> Path:
    """A workdir with the runtime tree an adapter spawn expects."""
    (tmp_path / ".sdd" / "runtime").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _popen_mock(pid: int = 4242) -> MagicMock:
    mock = MagicMock(spec=subprocess.Popen)
    mock.pid = pid
    mock.stdout = MagicMock()
    mock.poll.return_value = None
    mock.wait.return_value = 0
    return mock


def _capture_argv(adapter: CLIAdapter, workdir: Path) -> list[str]:
    """Spawn the adapter against a mocked Popen and return the argv it built."""
    popen = _popen_mock()
    module = type(adapter).__module__
    with patch(f"{module}.subprocess.Popen", return_value=popen) as patched:
        result = adapter.spawn(
            prompt="do the thing",
            workdir=workdir,
            model_config=ModelConfig(model="sonnet", effort="low"),
            session_id="profile-test-001",
            timeout_seconds=0,
        )
    if result.timeout_timer is not None:
        result.timeout_timer.cancel()
    return list(patched.call_args.args[0])


# ---------------------------------------------------------------------------
# Profile schema and content addressing
# ---------------------------------------------------------------------------


class TestProfileContentAddressing:
    """Profiles are content-addressed so drift is detectable as a hash change."""

    def test_profile_hash_is_stable_across_equal_profiles(self) -> None:
        assert _minimal_profile().profile_hash == _minimal_profile().profile_hash

    def test_profile_hash_is_hex_sha256(self) -> None:
        digest = _minimal_profile().profile_hash
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")

    def test_capability_change_changes_the_hash(self) -> None:
        before = _minimal_profile().profile_hash
        after = _minimal_profile(mcp_client=True).profile_hash
        assert before != after

    def test_invocation_change_changes_the_hash(self) -> None:
        before = _minimal_profile().profile_hash
        after = _minimal_profile(
            invocation=InvocationSpec(binary="demo-agent", subcommands=("exec",), model_flag="--model")
        ).profile_hash
        assert before != after

    def test_canonical_dict_is_json_safe_and_sorted(self) -> None:
        canonical = _minimal_profile().to_canonical_dict()
        assert list(canonical) == sorted(canonical)
        assert canonical["sandbox"] == str(SandboxTier.NONE)


# ---------------------------------------------------------------------------
# Typed refusals for underspecified profiles
# ---------------------------------------------------------------------------


class TestProfileValidation:
    """An underspecified profile is refused, never half-built."""

    def test_blank_name_is_refused(self) -> None:
        with pytest.raises(ProfileValidationError, match="name"):
            _minimal_profile(name="")

    def test_blank_binary_is_refused(self) -> None:
        with pytest.raises(ProfileValidationError, match="binary"):
            _minimal_profile(invocation=InvocationSpec(binary=""))

    def test_negative_parallel_workers_is_refused(self) -> None:
        with pytest.raises(ProfileValidationError, match="max_parallel_workers"):
            _minimal_profile(max_parallel_workers=-1)

    def test_factory_profile_without_prompt_delivery_is_refused(self) -> None:
        """A factory-built profile must say how the prompt reaches the CLI."""
        with pytest.raises(ProfileValidationError, match="prompt"):
            _minimal_profile(invocation=InvocationSpec(binary="demo-agent", prompt_flag=None, prompt_positional=False))

    def test_model_flag_declared_without_value_is_refused(self) -> None:
        with pytest.raises(ProfileValidationError, match="model_flag"):
            _minimal_profile(invocation=InvocationSpec(binary="demo-agent", model_flag=""))

    def test_validation_error_is_a_value_error(self) -> None:
        """Callers catching ValueError keep working."""
        assert issubclass(ProfileValidationError, ValueError)

    def test_unknown_profile_lookup_is_typed(self) -> None:
        with pytest.raises(UnknownProfileError, match="no-such-agent"):
            get_profile("no-such-agent")


# ---------------------------------------------------------------------------
# Factory: profile -> working adapter
# ---------------------------------------------------------------------------


class TestProfileFactory:
    """A profile round-trips into an adapter that honours the CLIAdapter contract."""

    def test_build_returns_cli_adapter(self) -> None:
        adapter = build_adapter_from_profile(_minimal_profile())
        assert isinstance(adapter, CLIAdapter)

    def test_adapter_name_comes_from_the_profile(self) -> None:
        adapter = build_adapter_from_profile(_minimal_profile())
        assert adapter.name() == "Demo Agent"

    def test_adapter_carries_registry_name_for_session_namespacing(self) -> None:
        adapter = build_adapter_from_profile(_minimal_profile())
        assert adapter.registry_name == "demo_agent"

    def test_factory_produces_a_class_so_each_lookup_gets_a_fresh_instance(self) -> None:
        """Registering a class (not a shared instance) keeps per-spawn state isolated."""
        cls = build_adapter_class_from_profile(_minimal_profile())
        assert isinstance(cls, type)
        assert cls() is not cls()

    def test_spawn_returns_spawn_result(self, spawn_workdir: Path) -> None:
        adapter = build_adapter_from_profile(_minimal_profile())
        popen = _popen_mock()
        module = type(adapter).__module__
        with patch(f"{module}.subprocess.Popen", return_value=popen):
            result = adapter.spawn(
                prompt="do the thing",
                workdir=spawn_workdir,
                model_config=ModelConfig(model="sonnet", effort="low"),
                session_id="profile-test-001",
                timeout_seconds=0,
            )
        if result.timeout_timer is not None:
            result.timeout_timer.cancel()
        assert isinstance(result, SpawnResult)
        assert result.pid == 4242
        assert isinstance(result.log_path, Path)

    def test_spawn_builds_argv_from_the_invocation_spec(self, spawn_workdir: Path) -> None:
        adapter = build_adapter_from_profile(_minimal_profile())
        argv = _capture_argv(adapter, spawn_workdir)
        assert "demo-agent" in argv
        # subcommand, model flag/value, extra args and the positional prompt
        # all reach the wrapped command line.
        assert "run" in argv
        assert "--model" in argv
        assert "sonnet" in argv
        assert "--no-stream" in argv
        assert "do the thing" in argv

    def test_prompt_flag_is_used_when_declared(self, spawn_workdir: Path) -> None:
        profile = _minimal_profile(
            invocation=InvocationSpec(binary="demo-agent", prompt_flag="--prompt", model_flag=None)
        )
        argv = _capture_argv(build_adapter_from_profile(profile), spawn_workdir)
        assert "--prompt" in argv
        assert argv[argv.index("--prompt") + 1] == "do the thing"

    def test_missing_binary_raises_runtime_error(self, spawn_workdir: Path) -> None:
        adapter = build_adapter_from_profile(_minimal_profile())
        module = type(adapter).__module__
        with (
            patch(f"{module}.subprocess.Popen", side_effect=FileNotFoundError()),
            pytest.raises(RuntimeError, match="demo-agent"),
        ):
            adapter.spawn(
                prompt="do the thing",
                workdir=spawn_workdir,
                model_config=ModelConfig(model="sonnet", effort="low"),
                session_id="profile-test-001",
                timeout_seconds=0,
            )

    def test_declared_strategy_flows_through_the_adapter(self) -> None:
        profile = _minimal_profile(
            resume=ResumeStrategy.FLAG,
            dangerous_mode=DangerousModeStrategy.CLI_FLAG,
            event_channel=EventChannel.STREAM_JSON,
        )
        strategy = build_adapter_from_profile(profile).strategy()
        assert strategy.resume is ResumeStrategy.FLAG
        assert strategy.dangerous_mode is DangerousModeStrategy.CLI_FLAG
        assert strategy.event_channel is EventChannel.STREAM_JSON

    def test_declaration_only_profile_is_not_factory_built(self) -> None:
        """Profiles that document a hand-written module must not be built."""
        profile = _minimal_profile(implementation=ProfileImplementation.MODULE)
        with pytest.raises(ProfileValidationError, match="MODULE"):
            build_adapter_from_profile(profile)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestProfileDeterminism:
    """Two builds from one profile behave identically - replay stays sound."""

    def test_two_builds_emit_identical_argv(self, spawn_workdir: Path) -> None:
        profile = _minimal_profile()
        first = _capture_argv(build_adapter_from_profile(profile), spawn_workdir)
        second = _capture_argv(build_adapter_from_profile(profile), spawn_workdir)
        assert first == second

    def test_two_builds_share_the_profile_hash(self) -> None:
        profile = _minimal_profile()
        first = build_adapter_from_profile(profile)
        second = build_adapter_from_profile(profile)
        assert first.profile_hash == second.profile_hash == profile.profile_hash

    def test_profile_hash_is_reachable_from_a_live_adapter(self) -> None:
        """Dispatch records the hash the adapter presented, so drift is visible."""
        adapter = build_adapter_from_profile(_minimal_profile())
        assert adapter.profile_hash == _minimal_profile().profile_hash


# ---------------------------------------------------------------------------
# Capability-aware selection
# ---------------------------------------------------------------------------


class TestCapabilityAwareSelection:
    """A task routes to a profile that satisfies it, or is refused explicitly."""

    def test_satisfied_requirements_select_the_profile(self) -> None:
        profile = _minimal_profile(mcp_client=True, sandbox=SandboxTier.CONTAINER)
        selected = select_profile_for(
            TaskCapabilityRequirements(mcp_client=True, sandbox=SandboxTier.CONTAINER),
            profiles=(profile,),
        )
        assert selected is profile

    def test_unmet_requirement_refuses_rather_than_falling_back(self) -> None:
        profile = _minimal_profile(mcp_client=False)
        with pytest.raises(CapabilityMismatchError) as excinfo:
            select_profile_for(TaskCapabilityRequirements(mcp_client=True), profiles=(profile,))
        assert "mcp_client" in str(excinfo.value)

    def test_refusal_carries_a_content_addressed_receipt(self) -> None:
        profile = _minimal_profile(vision=False)
        with pytest.raises(CapabilityMismatchError) as excinfo:
            select_profile_for(TaskCapabilityRequirements(vision=True), profiles=(profile,))
        receipt = excinfo.value.receipt
        assert receipt.unmet == ("vision",)
        assert len(receipt.receipt_hash) == 64

    def test_refusal_receipt_is_deterministic(self) -> None:
        profile = _minimal_profile(vision=False)
        requirements = TaskCapabilityRequirements(vision=True)
        hashes = []
        for _ in range(2):
            with pytest.raises(CapabilityMismatchError) as excinfo:
                select_profile_for(requirements, profiles=(profile,))
            hashes.append(excinfo.value.receipt.receipt_hash)
        assert hashes[0] == hashes[1]

    def test_parallel_worker_shortfall_is_refused(self) -> None:
        profile = _minimal_profile(max_parallel_workers=2)
        with pytest.raises(CapabilityMismatchError, match="max_parallel_workers"):
            select_profile_for(TaskCapabilityRequirements(max_parallel_workers=8), profiles=(profile,))

    def test_every_boolean_axis_exists_on_both_sides(self) -> None:
        """An axis a task cannot request would be a silently unenforceable gate."""
        for axis in BOOLEAN_CAPABILITIES:
            assert axis in AdapterCapabilityProfile.__dataclass_fields__
            assert axis in TaskCapabilityRequirements.__dataclass_fields__

    def test_every_boolean_axis_is_actually_enforced(self) -> None:
        """Requiring an axis a profile lacks must report that exact axis."""
        for axis in BOOLEAN_CAPABILITIES:
            profile = _minimal_profile(**{axis: False})
            requirements = TaskCapabilityRequirements(**{axis: True})
            assert unmet_requirements(profile, requirements) == (axis,)

    def test_higher_capability_than_required_still_matches(self) -> None:
        profile = _minimal_profile(max_parallel_workers=16, sandbox=SandboxTier.VM)
        selected = select_profile_for(
            TaskCapabilityRequirements(max_parallel_workers=4, sandbox=SandboxTier.PROCESS),
            profiles=(profile,),
        )
        assert selected is profile


# ---------------------------------------------------------------------------
# Generic fallback is preserved
# ---------------------------------------------------------------------------


class TestGenericFallbackPreserved:
    """Agents with no profile keep working through the generic CLI adapter."""

    def test_generic_adapter_still_resolves(self) -> None:
        adapter = get_adapter("generic")
        assert isinstance(adapter, CLIAdapter)
        assert adapter.name() == "Generic CLI"

    def test_generic_has_no_profile(self) -> None:
        with pytest.raises(UnknownProfileError):
            get_profile("generic")

    def test_generic_spawn_is_unaffected_by_the_profile_layer(self, spawn_workdir: Path) -> None:
        argv = _capture_argv(get_adapter("generic"), spawn_workdir)
        assert "generic-cli" in argv

    def test_unknown_adapter_still_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown adapter"):
            get_adapter("definitely-not-an-agent")


# ---------------------------------------------------------------------------
# Shipped catalogue: the #2610 payload
# ---------------------------------------------------------------------------


def _shipped_profiles() -> Iterator[tuple[str, AdapterCapabilityProfile]]:
    yield from sorted(PROFILES.items())


_SHIPPED = list(_shipped_profiles())


class TestShippedProfiles:
    """Every shipped profile stays consistent with the gates already in place."""

    def test_catalogue_is_not_empty(self) -> None:
        assert PROFILES

    @pytest.mark.parametrize("name,profile", _SHIPPED, ids=[n for n, _ in _SHIPPED])
    def test_profile_name_matches_its_key(self, name: str, profile: AdapterCapabilityProfile) -> None:
        assert profile.name == name

    @pytest.mark.parametrize("name,profile", _SHIPPED, ids=[n for n, _ in _SHIPPED])
    def test_profile_agrees_with_strategy_matrix(self, name: str, profile: AdapterCapabilityProfile) -> None:
        """The declared strategy must match the authoritative matrix row."""
        assert name in STRATEGY_MATRIX, f"{name} has a profile but no STRATEGY_MATRIX row"
        row = STRATEGY_MATRIX[name]
        assert profile.resume is row.resume
        assert profile.dangerous_mode is row.dangerous_mode
        assert profile.event_channel is row.event_channel

    @pytest.mark.parametrize("name,profile", _SHIPPED, ids=[n for n, _ in _SHIPPED])
    def test_profile_declares_no_capability_the_contract_cannot_back(
        self,
        name: str,
        profile: AdapterCapabilityProfile,
    ) -> None:
        """Declared invocation surface must not exceed the pinned contract."""
        spec = ContractSpec.load(name)
        assert profile_contract_discrepancies(profile, spec) == ()

    @pytest.mark.parametrize("name,profile", _SHIPPED, ids=[n for n, _ in _SHIPPED])
    def test_vision_claim_matches_the_multimodal_capability_table(
        self,
        name: str,
        profile: AdapterCapabilityProfile,
    ) -> None:
        """A profile may not claim vision the attachment gate does not grant."""
        if profile.vision:
            assert is_multimodal_capable(name), f"{name} declares vision but is not multimodal-capable"

    @pytest.mark.parametrize("name,profile", _SHIPPED, ids=[n for n, _ in _SHIPPED])
    def test_every_profile_has_a_contract_on_disk(self, name: str, profile: AdapterCapabilityProfile) -> None:
        """No profile ships without the contract that pins its CLI surface."""
        assert ContractSpec.load(name).adapter == name


class TestProfileBuiltAdaptersAreRegistered:
    """Factory-built agents are first-class registry citizens."""

    def test_pydantic_ai_is_profile_built(self) -> None:
        assert "pydantic_ai" in profile_built_adapter_classes()

    def test_pydantic_ai_resolves_through_the_registry(self) -> None:
        adapter = get_adapter("pydantic_ai")
        assert isinstance(adapter, CLIAdapter)
        assert adapter.registry_name == "pydantic_ai"

    def test_pydantic_ai_is_enumerated_by_the_registry(self) -> None:
        assert "pydantic_ai" in {name for name, _ in iter_adapter_specs()}

    def test_profile_built_adapter_spawns_the_declared_binary(self, spawn_workdir: Path) -> None:
        argv = _capture_argv(get_adapter("pydantic_ai"), spawn_workdir)
        assert "clai" in argv
        assert "do the thing" in argv

    def test_declaration_only_profiles_are_not_factory_built(self) -> None:
        """Hand-written modules keep owning their own spawn path."""
        built = profile_built_adapter_classes()
        for name, profile in PROFILES.items():
            if profile.implementation is ProfileImplementation.MODULE:
                assert name not in built


class TestProfileBuiltAdapterClearsConformance:
    """The factory-built adapter faces the unmodified conformance harness."""

    def test_generated_class_is_importable_by_dotted_path(self) -> None:
        """Golden-transcript replay loads adapters by dotted path."""
        module = importlib.import_module("bernstein.adapters.capability_profile")
        assert isinstance(getattr(module, "PydanticAiProfileAdapter", None), type)

    def test_generated_class_identity_is_stable(self) -> None:
        """The registry and a later lookup must agree on the same class."""
        assert profile_built_adapter_classes()["pydantic_ai"] is type(get_adapter("pydantic_ai"))

    def test_golden_transcript_replays_clean(self, tmp_path: Path) -> None:
        transcripts = [
            transcript
            for transcript in load_golden_transcripts(GOLDEN_DIR)
            if transcript.name == "pydantic_ai_adapter_spawn"
        ]
        assert transcripts, "pydantic_ai golden transcript is missing"

        report = ConformanceHarness().run_all(transcripts, workdir=tmp_path)
        assert report.regressions == []
        assert report.passed

    def test_strategy_declaration_gate_accepts_the_profile_built_adapter(self) -> None:
        """The undeclared-strategy gate must pass for the whole registry."""
        assert_strategies_declared()


class TestProfileContractVerification:
    """The canary refuses a profile whose declaration outruns its contract."""

    def test_undeclared_flag_is_reported(self) -> None:
        spec = ContractSpec.load("pydantic_ai")
        overreaching = _minimal_profile(
            name="pydantic_ai",
            invocation=InvocationSpec(
                binary="clai",
                model_flag="-m",
                extra_args=("--totally-invented-flag",),
            ),
        )
        discrepancies = profile_contract_discrepancies(overreaching, spec)
        assert any("--totally-invented-flag" in reason for reason in discrepancies)

    def test_wrong_binary_is_reported(self) -> None:
        spec = ContractSpec.load("pydantic_ai")
        wrong = _minimal_profile(name="pydantic_ai", invocation=InvocationSpec(binary="not-clai", model_flag="-m"))
        assert any("binary" in reason for reason in profile_contract_discrepancies(wrong, spec))

    def test_undeclared_env_passthrough_is_reported(self) -> None:
        """A forwarded secret the contract does not pin is a discrepancy.

        ``env_passthrough`` is the credential surface a profile forwards
        into the isolated environment; the contract's ``secret_env``
        allow-list is the pinned surface. A profile that forwards a secret
        the contract does not carry declares more than was verified, so the
        cross-check must report it the same way it reports an undeclared
        flag, rather than letting the credential surface drift silently.
        """
        spec = ContractSpec.load("pydantic_ai")
        overreaching = _minimal_profile(
            name="pydantic_ai",
            invocation=InvocationSpec(
                binary="clai",
                model_flag="-m",
                env_passthrough=("TOTALLY_INVENTED_SECRET",),
            ),
        )
        discrepancies = profile_contract_discrepancies(overreaching, spec)
        assert any("TOTALLY_INVENTED_SECRET" in reason for reason in discrepancies), discrepancies
