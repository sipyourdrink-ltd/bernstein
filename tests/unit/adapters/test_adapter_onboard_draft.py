"""Tests for the draft capability profile step (issue #3763).

Covers the three assertions that must fail FIRST because no drafting
function exists yet:

1. Draft from a fixture evidence file whose captured help text contains
   ``--model <name>`` produces a draft whose
   :class:`~bernstein.adapters.capability_profile.InvocationSpec`-shaped
   model flag is ``--model``, with the evidence byte range recorded
   alongside it.
2. Draft from evidence that does NOT contain a flag the operator expected
   (simulated by a fixture with deliberately incomplete probe capture)
   refuses and names that exact field in the refusal - asserted by
   matching the refusal message against the field name, not just
   checking that drafting failed.
3. A drafted profile full argv (binary + subcommands + flags) is
   reconstructable from evidence-backed fields alone, with no field
   sourced from a hard-coded default the evidence did not confirm.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "probe"


def _read_evidence(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_fixture_help_text(fixture_name: str) -> str:
    """Load the --help output from a probe fixture."""
    fixture_path = FIXTURES / f"{fixture_name}.py"
    assert fixture_path.exists(), f"Missing fixture: {fixture_path}"
    # Run the fixture to capture its --help output
    import subprocess

    result = subprocess.run(
        [sys.executable, str(fixture_path), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"Fixture {fixture_name} --help failed: {result.stderr}"
    return result.stdout


def _write_evidence_file(out_dir: Path, binary: str, output: str, command: str) -> Path:
    """Write a synthetic evidence file shaped like probe_cli output."""
    import hashlib

    record = {
        "binary": binary,
        "command": command,
        "exit_code": 0,
        "output": output,
    }
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sha = hashlib.sha256(payload).hexdigest()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{sha}.json"
    path.write_bytes(payload)
    return path


# ---------------------------------------------------------------------------
# Assertion 1: draft from evidence containing --model produces InvocationSpec with model_flag
# ---------------------------------------------------------------------------


def test_draft_from_model_help_produces_invocation_spec_with_model_flag(tmp_path: Path) -> None:
    """Draft from fixture evidence whose --help contains --model <name> yields InvocationSpec.model_flag == --model.

    The evidence byte range (offset / length within the captured help text)
    is recorded alongside the InvocationSpec so the draft is traceable.
    """
    # Import the drafting helper (will fail until implemented)
    from bernstein.adapters.draft import draft_from_evidence

    help_text = _load_fixture_help_text("probe_with_model_help")
    evidence_path = _write_evidence_file(tmp_path, "probe-with-model", help_text, "probe-with-model --help")

    draft = draft_from_evidence(evidence_path)

    assert draft.invocation.model_flag == "--model", (
        f"Expected InvocationSpec.model_flag == '--model', got {draft.invocation.model_flag!r}"
    )
    # The evidence byte range should be recorded (either on the draft or the spec)
    assert hasattr(draft, "evidence_byte_range") or hasattr(draft.invocation, "evidence_byte_range"), (
        "Draft must record the evidence byte range alongside the InvocationSpec"
    )


# ---------------------------------------------------------------------------
# Assertion 2: draft from evidence missing expected flag refuses and names the field
# ---------------------------------------------------------------------------


def test_draft_from_missing_model_help_refuses_named_field(tmp_path: Path) -> None:
    """Draft from fixture evidence whose --help omits --model refuses and names --model in the refusal.

    The test asserts that the refusal message contains the exact field
    name (``--model``), not just that an exception was raised.
    """
    from bernstein.adapters.draft import draft_from_evidence

    help_text = _load_fixture_help_text("probe_missing_model")
    evidence_path = _write_evidence_file(tmp_path, "probe-missing-model", help_text, "probe-missing-model --help")

    with pytest.raises(Exception) as exc_info:
        draft_from_evidence(evidence_path, required_fields={"model_flag"})

    refusal_message = str(exc_info.value)
    assert "--model" in refusal_message, (
        f"Refusal message must name the missing field '--model', got: {refusal_message!r}"
    )


# ---------------------------------------------------------------------------
# Assertion 3: full argv is reconstructable from evidence-backed fields alone
# ---------------------------------------------------------------------------


def test_draft_argv_reconstructable_from_evidence_backed_fields(tmp_path: Path) -> None:
    """A drafted profile's full argv is reconstructable from evidence-backed fields alone.

    No field in the argv may be sourced from a hard-coded default that the
    evidence did not confirm. Every token must trace back to the probe
    evidence.
    """
    from bernstein.adapters.draft import draft_from_evidence

    help_text = _load_fixture_help_text("probe_with_model_help")
    evidence_path = _write_evidence_file(tmp_path, "probe-with-model", help_text, "probe-with-model --help")

    draft = draft_from_evidence(evidence_path)

    # Build argv from the drafted InvocationSpec
    argv = draft.invocation.build_argv(prompt="test prompt", model="test-model")

    # Every flag in argv must be confirmed by the evidence
    # (i.e., present in the InvocationSpec fields, not guessed)
    confirmed_flags = set(draft.invocation.declared_flags())
    argv_flags = [token for token in argv if token.startswith("-")]

    for flag in argv_flags:
        assert flag in confirmed_flags or flag == "test-model", (
            f"Flag {flag!r} in argv was not confirmed by evidence-backed fields: {confirmed_flags}"
        )

    # Binary must come from evidence, not a default
    assert draft.invocation.binary == "probe-with-model", (
        f"Binary must come from evidence, got {draft.invocation.binary!r}"
    )
