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


# ---------------------------------------------------------------------------
# draft_from_probe: wires a real probe_cli() run into draft_from_evidence()
# (remaining work on issue #3763 - draft_from_evidence had no caller).
# ---------------------------------------------------------------------------

RECORDABLE_FIXTURE = FIXTURES / "probe_recordable.py"


def test_draft_from_probe_wires_a_real_probe_into_drafting(tmp_path: Path) -> None:
    """draft_from_probe() runs a real probe subprocess and drafts from its --help capture.

    Uses the already-executable ``probe_recordable.py`` fixture directly (no
    fixture-file rewrite): a real subprocess is spawned, real evidence files
    land on disk under ``evidence_dir``, and the draft built from them is
    identical in shape to one built from a hand-written evidence file.
    """
    from bernstein.adapters.onboarding import draft_from_probe

    evidence_dir = tmp_path / "evidence"
    draft = draft_from_probe(str(RECORDABLE_FIXTURE), evidence_dir)

    assert draft.invocation.binary == str(RECORDABLE_FIXTURE)
    assert draft.invocation.model_flag == "--model"
    assert draft.invocation.prompt_flag == "--prompt"
    assert draft.evidence_byte_range is not None

    # The probe genuinely ran: real evidence files exist on disk, one per
    # probed command (--version, --help, and the completion patterns).
    evidence_files = list(evidence_dir.glob("*.json"))
    assert len(evidence_files) >= 2, f"expected real probe evidence on disk, found {evidence_files}"


def test_draft_from_probe_refuses_naming_the_missing_field_from_a_real_probe(tmp_path: Path) -> None:
    """draft_from_probe() forwards required_fields and refuses on a real probe with no --model.

    ``probe_ok.py`` is a real, directly-executable fixture whose --help text
    carries no model flag; asking drafting to require one must refuse and
    name it, not silently return a profile with model_flag=None.
    """
    from bernstein.adapters.onboarding import draft_from_probe

    binary = str(FIXTURES / "probe_ok.py")
    evidence_dir = tmp_path / "evidence"

    with pytest.raises(Exception) as exc_info:
        draft_from_probe(binary, evidence_dir, required_fields={"model_flag"})

    assert "--model" in str(exc_info.value)


# ---------------------------------------------------------------------------
# draft_document / write_draft_yaml / read_draft_document: the draft is
# serialized as plain YAML (candidate profile + contract), and provenance
# survives a real round trip through disk (remaining work on issue #3763).
# ---------------------------------------------------------------------------


def test_draft_document_only_carries_evidence_backed_fields(tmp_path: Path) -> None:
    """draft_document() emits invocation + contract + provenance with nothing invented.

    The contract's required_flags must equal the invocation's own
    declared_flags() (no extra, no missing), and required_subcommands must
    stay empty because drafting never populates subcommands from evidence.
    """
    from bernstein.adapters.draft import draft_document, draft_from_evidence

    help_text = _load_fixture_help_text("probe_with_model_help")
    evidence_path = _write_evidence_file(tmp_path, "probe-with-model", help_text, "probe-with-model --help")
    draft = draft_from_evidence(evidence_path)

    document = draft_document(draft)

    assert set(document) == {"invocation", "contract", "provenance"}
    assert document["invocation"]["model_flag"] == "--model"
    assert document["invocation"]["prompt_flag"] == "--prompt"
    assert document["contract"]["binary"] == draft.invocation.binary
    assert document["contract"]["required_flags"] == list(draft.invocation.declared_flags())
    assert document["contract"]["required_subcommands"] == [], (
        "drafting never resolves subcommands from evidence; the contract preview must not invent any"
    )
    start, end = draft.evidence_byte_range
    assert document["provenance"]["model_flag"] == {"start": start, "end": end}


def test_write_draft_yaml_round_trips_through_a_real_file_preserving_provenance(tmp_path: Path) -> None:
    """write_draft_yaml() + read_draft_document() round-trip through a real file, losslessly.

    Checked at the layer where the property actually lives: a real file on
    disk, read back and compared, plus a plain-text check that the file is
    YAML (not some other serialization) as the issue's own wording asks for.
    """
    from bernstein.adapters.draft import draft_document, draft_from_evidence, read_draft_document, write_draft_yaml

    help_text = _load_fixture_help_text("probe_with_model_help")
    evidence_path = _write_evidence_file(tmp_path, "probe-with-model", help_text, "probe-with-model --help")
    draft = draft_from_evidence(evidence_path)

    target = tmp_path / "drafts" / "probe-with-model.yaml"
    returned = write_draft_yaml(draft, target)

    assert returned == target
    assert target.is_file(), "write_draft_yaml must create parent dirs and the file itself"

    raw_text = target.read_text(encoding="utf-8")
    assert "model_flag: --model" in raw_text, f"expected plain-YAML key: value lines, got:\n{raw_text}"

    loaded = read_draft_document(target)
    assert loaded == draft_document(draft), "round trip through disk must be lossless"
    # The property in the issue's title: per-field provenance survives the
    # write, not just the in-memory object.
    start, end = draft.evidence_byte_range
    assert loaded["provenance"]["model_flag"] == {"start": start, "end": end}


# ---------------------------------------------------------------------------
# load_profile_from_draft: consume persisted drafts back into Draft objects
# (task 23955c80b223 - the consumer side of the YAML persistence flow).
# ---------------------------------------------------------------------------


def test_load_profile_from_draft_reads_yaml_and_returns_validated_draft(tmp_path: Path) -> None:
    """load_profile_from_draft(path) reads a draft YAML and returns a validated Draft.

    The returned Draft must have a valid InvocationSpec and preserve the
    evidence_byte_range from the provenance section.
    """
    from bernstein.adapters.draft import (
        draft_from_evidence,
        load_profile_from_draft,
        write_draft_yaml,
    )

    help_text = _load_fixture_help_text("probe_with_model_help")
    evidence_path = _write_evidence_file(tmp_path, "probe-with-model", help_text, "probe-with-model --help")
    original_draft = draft_from_evidence(evidence_path)

    target = tmp_path / "drafts" / "probe-with-model.yaml"
    write_draft_yaml(original_draft, target)

    loaded = load_profile_from_draft(target)

    assert loaded.invocation.binary == "probe-with-model"
    assert loaded.invocation.model_flag == "--model"
    assert loaded.invocation.prompt_flag == "--prompt"
    assert loaded.evidence_byte_range == original_draft.evidence_byte_range


def test_load_profile_from_draft_preserves_provenance(tmp_path: Path) -> None:
    """Per-field provenance (evidence_byte_range) survives the round trip."""
    from bernstein.adapters.draft import (
        draft_from_evidence,
        load_profile_from_draft,
        write_draft_yaml,
    )

    help_text = _load_fixture_help_text("probe_with_model_help")
    evidence_path = _write_evidence_file(tmp_path, "probe-with-model", help_text, "probe-with-model --help")
    original_draft = draft_from_evidence(evidence_path)

    target = tmp_path / "drafts" / "probe-with-model.yaml"
    write_draft_yaml(original_draft, target)

    loaded = load_profile_from_draft(target)

    assert loaded.evidence_byte_range is not None
    start, end = loaded.evidence_byte_range
    assert start >= 0
    assert end > start


def test_load_profile_from_draft_rejects_missing_invocation_section(tmp_path: Path) -> None:
    """Function validates required sections are present before returning."""
    from bernstein.adapters.draft import load_profile_from_draft

    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("provenance: {}\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_profile_from_draft(bad_yaml)

    assert "invocation" in str(exc_info.value)


def test_load_profile_from_draft_rejects_missing_provenance_section(tmp_path: Path) -> None:
    """Function validates required sections are present before returning."""
    from bernstein.adapters.draft import load_profile_from_draft

    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text(
        "invocation:\n  binary: test\n  subcommands: []\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_profile_from_draft(bad_yaml)

    assert "provenance" in str(exc_info.value)


def test_load_profile_from_draft_rejects_missing_binary(tmp_path: Path) -> None:
    """Function validates required invocation fields are present."""
    from bernstein.adapters.draft import load_profile_from_draft

    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text(
        "invocation:\n  subcommands: []\nprovenance: {}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_profile_from_draft(bad_yaml)

    assert "binary" in str(exc_info.value)
