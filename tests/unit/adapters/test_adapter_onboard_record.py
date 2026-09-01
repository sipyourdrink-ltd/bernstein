"""Tests for supervised onboarding transcript recording (issue #3764)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from bernstein.adapters import registry
from bernstein.adapters.admission import _transcripts_for, replay_fingerprint
from bernstein.adapters.capability_profile import (
    AdapterCapabilityProfile,
    InvocationSpec,
    ProfileValidationError,
    RecordedProfileAdapter,
)
from bernstein.adapters.conformance import ConformanceHarness
from bernstein.adapters.onboarding import (
    derive_held_out_invocations,
    record_golden_transcript,
    replay_held_out_invocations,
)

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "probe" / "probe_recordable.py"
SMOKE_PROMPT = "describe the public fixture"
SMOKE_MODEL = "fixture-model"


def _profile(*, extra_args: tuple[str, ...] = ()) -> AdapterCapabilityProfile:
    """Build the fixture profile used by the onboarding tests."""
    return AdapterCapabilityProfile(
        name="recordable-fixture",
        display_name="Recordable Fixture",
        invocation=InvocationSpec(
            binary=str(FIXTURE),
            model_flag="--model",
            prompt_flag="--prompt",
            prompt_positional=False,
            extra_args=extra_args,
        ),
    )


def _write_evidence(path: Path, *, binary: str = str(FIXTURE)) -> Path:
    """Write a synthetic probe record without running the probing step."""
    document = {
        "binary": binary,
        "command": f"{binary} --help",
        "exit_code": 0,
        "output": "--model <name> --prompt <text>",
    }
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    path.write_bytes(payload)
    assert hashlib.sha256(payload).hexdigest()
    return path


def test_record_round_trip_and_replay_stays_process_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful smoke creates a schema-exact transcript that replays mocked."""
    profile = _profile()
    golden_dir = tmp_path / "golden"
    transcript_path = record_golden_transcript(
        profile,
        name="recordable-fixture",
        smoke_prompt=SMOKE_PROMPT,
        smoke_model=SMOKE_MODEL,
        golden_dir=golden_dir,
    )

    document = yaml.safe_load(transcript_path.read_text(encoding="utf-8"))
    assert set(document) == {"name", "adapter_class", "ctor_kwargs", "steps"}
    assert set(document["steps"][0]) == {"prompt", "model"}
    assert document["steps"][0] == {"prompt": SMOKE_PROMPT, "model": SMOKE_MODEL}
    assert document["adapter_class"] == "bernstein.adapters.capability_profile.RecordedProfileAdapter"

    monkeypatch.setitem(registry._ADAPTERS, profile.name, RecordedProfileAdapter)
    loaded = _transcripts_for(profile.name, golden_dir)
    assert len(loaded) == 1
    adapter = RecordedProfileAdapter(**loaded[0].ctor_kwargs)
    assert adapter.profile.invocation.to_canonical_dict() == profile.invocation.to_canonical_dict()

    popen_target = "bernstein.adapters.capability_profile.subprocess.Popen"
    with patch(popen_target) as popen:
        replay_one = ConformanceHarness().replay_transcript(loaded[0], workdir=tmp_path / "replay-one")
        replay_two = ConformanceHarness().replay_transcript(loaded[0], workdir=tmp_path / "replay-two")
    assert replay_one.passed and replay_two.passed
    # ``ConformanceHarness`` installs its own controlled Popen side effect for
    # each replay; the outer patch is only a guard that the target is importable.
    assert popen.call_count == 0

    evidence_path = _write_evidence(tmp_path / "evidence.json")
    held_out = derive_held_out_invocations(
        evidence_path,
        profile.invocation,
        smoke_prompt=SMOKE_PROMPT,
        smoke_model=SMOKE_MODEL,
    )
    held_out_result_one = replay_held_out_invocations(held_out, invocation=profile.invocation)
    held_out_result_two = replay_held_out_invocations(held_out, invocation=profile.invocation)
    assert held_out_result_one.passed and held_out_result_two.passed
    assert replay_fingerprint(
        profile.name,
        contract_hash="contract-hash",
        installed_version="1.0.0",
        results=[replay_one, held_out_result_one],
    ) == replay_fingerprint(
        profile.name,
        contract_hash="contract-hash",
        installed_version="1.0.0",
        results=[replay_two, held_out_result_two],
    )


def test_held_out_derivation_is_deterministic_disjoint_and_complete(tmp_path: Path) -> None:
    """Held-out argv is stable, unique, disjoint, and includes every flag."""
    profile = _profile()
    evidence_path = _write_evidence(tmp_path / "evidence.json")
    first = derive_held_out_invocations(
        evidence_path,
        profile.invocation,
        smoke_prompt=SMOKE_PROMPT,
        smoke_model=SMOKE_MODEL,
        recorded_argvs=(profile.invocation.build_argv(prompt="already recorded", model=SMOKE_MODEL),),
    )
    second = derive_held_out_invocations(
        evidence_path,
        profile.invocation,
        smoke_prompt=SMOKE_PROMPT,
        smoke_model=SMOKE_MODEL,
        recorded_argvs=(profile.invocation.build_argv(prompt="already recorded", model=SMOKE_MODEL),),
    )
    assert first == second
    assert len(first) == 3
    argv = [case.argv for case in first]
    assert len(set(argv)) == 3
    recorded = {tuple(profile.invocation.build_argv(prompt=SMOKE_PROMPT, model=SMOKE_MODEL))}
    assert recorded.isdisjoint(argv)
    for case in first:
        assert set(profile.invocation.declared_flags()).issubset(set(case.argv))


def test_empty_recorded_argv_is_rejected(tmp_path: Path) -> None:
    """Malformed empty records cannot weaken held-out exclusion checks."""
    profile = _profile()
    evidence_path = _write_evidence(tmp_path / "evidence.json")
    with pytest.raises(ValueError, match="non-empty strings"):
        derive_held_out_invocations(
            evidence_path,
            profile.invocation,
            smoke_prompt=SMOKE_PROMPT,
            smoke_model=SMOKE_MODEL,
            recorded_argvs=((),),
        )


def test_recorded_profile_rejects_mapping_tokens() -> None:
    """Mapping-shaped YAML fields cannot silently turn into key tokens."""
    with pytest.raises(ProfileValidationError, match="must be a list of strings"):
        RecordedProfileAdapter(
            registry_name="recordable-fixture",
            display_name="Recordable Fixture",
            binary="probe-recordable",
            subcommands={"unexpected": "value"},
        )


def test_rejected_flag_fails_every_held_out_step(tmp_path: Path) -> None:
    """An advertised-but-rejected option is a failed execution, never a skip."""
    profile = _profile(extra_args=("--rejected-by-fixture",))
    evidence_path = _write_evidence(tmp_path / "evidence.json")
    held_out = derive_held_out_invocations(
        evidence_path,
        profile.invocation,
        smoke_prompt=SMOKE_PROMPT,
        smoke_model=SMOKE_MODEL,
    )
    result = replay_held_out_invocations(held_out, invocation=profile.invocation)
    assert len(result.step_results) == 3
    assert not result.passed
    assert all(not step.passed for step in result.step_results)
    assert all("probe-recordable" not in step.message for step in result.step_results)


def test_failed_smoke_and_evidence_mismatch_do_not_write_transcript(tmp_path: Path) -> None:
    """Failed supervision and mismatched evidence leave no golden file."""
    golden_dir = tmp_path / "golden"
    with pytest.raises(RuntimeError, match="exit code"):
        record_golden_transcript(
            _profile(extra_args=("--rejected-by-fixture",)),
            name="failed-record",
            smoke_prompt=SMOKE_PROMPT,
            smoke_model=SMOKE_MODEL,
            golden_dir=golden_dir,
        )
    assert not list(golden_dir.glob("*.yaml"))

    evidence_path = _write_evidence(tmp_path / "mismatch.json", binary="different-binary")
    with pytest.raises(ValueError, match="does not match"):
        derive_held_out_invocations(
            evidence_path,
            _profile().invocation,
            smoke_prompt=SMOKE_PROMPT,
            smoke_model=SMOKE_MODEL,
        )
    assert not list(golden_dir.glob("*.yaml"))


def test_empty_held_out_replay_is_not_a_vacuous_pass() -> None:
    """A caller cannot turn an empty supervised run into an all([]) pass."""
    result = replay_held_out_invocations(())
    assert result.step_results
    assert not result.passed
