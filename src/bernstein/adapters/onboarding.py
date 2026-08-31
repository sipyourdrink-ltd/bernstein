"""Probe CLI evidence, record a supervised golden transcript, and replay held-out invocations.

Issue #3762: no probe step turns an installed CLI's own output into evidence
Bernstein can admit from. This module closes that gap: it runs the binary's
``--version``, ``--help``, and a set of common shell-completion introspection
invocations, and writes each captured result to a content-addressed evidence
file (the SHA-256 of the record's canonical JSON bytes is the filename).

Design invariants:

* **Never raises.** A missing binary, non-zero exit, or timeout is recorded
  as evidence rather than surfaced as an exception, mirroring
  :func:`bernstein.adapters._contract._run_capture`'s 127/not-found handling.
* **Content-addressed.** Two runs that observed the same upstream surface
  produce byte-identical evidence files; a mutated file no longer hashes to
  its recorded identity.
* **Deterministic.** The sandboxed environment strips color and telemetry
  opt-outs, so the captured output is stable across runs.

The module also records a supervised smoke invocation as a golden transcript
and runs a small deterministic held-out set against the same binary. Those
operations deliberately reuse the bounded ``_run_capture``/
``_sandbox_env`` boundary; they do not claim to provide an OS network
sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from bernstein.adapters._contract import _run_capture, _sandbox_env
from bernstein.adapters.capability_profile import AdapterCapabilityProfile, InvocationSpec
from bernstein.adapters.conformance import GoldenTranscript, StepResult, TranscriptResult, TranscriptStep
from bernstein.adapters.draft import Draft, draft_from_evidence

#: Per-command timeout for probe invocations.
_PROBE_TIMEOUT_SECONDS = 30

#: Shell-completion introspection patterns, tried in order. Each is run and
#: recorded regardless of success; a CLI that supports none of them still
#: yields evidence naming the failures.
_COMPLETION_PATTERNS: tuple[tuple[str, ...], ...] = (
    ("completion", "bash"),
    ("--generate-completions", "bash"),
    ("shell-completion", "bash"),
)

__all__ = [
    "HeldOutInvocation",
    "ProbeEvidence",
    "derive_held_out_invocations",
    "draft_from_probe",
    "probe_cli",
    "record_golden_transcript",
    "replay_held_out_invocations",
]


@dataclass(frozen=True)
class ProbeEvidence:
    """One content-addressed evidence file for a single probe command."""

    command: str
    exit_code: int
    path: Path
    sha256: str


def _canonical_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_evidence(out_dir: Path, record: dict[str, Any]) -> ProbeEvidence:
    """Write ``record`` under its content-addressed name and return its handle."""
    payload = _canonical_bytes(record)
    sha = _sha256_hex(payload)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{sha}.json"
    path.write_bytes(payload)
    return ProbeEvidence(
        command=record["command"],
        exit_code=record["exit_code"],
        path=path,
        sha256=sha,
    )


def probe_cli(binary: str, out_dir: Path) -> list[ProbeEvidence]:
    """Probe an installed CLI binary, capturing its self-description as evidence.

    Runs ``<binary> --version``, ``<binary> --help``, and a set of common
    shell-completion introspection invocations, writing each captured result
    to a content-addressed evidence file under ``out_dir``. Never raises: a
    missing binary, non-zero exit, or timeout is recorded as evidence rather
    than surfaced as an exception.

    Args:
        binary: The CLI binary name to probe (resolved via ``PATH``).
        out_dir: Directory to write evidence files into (created if absent).

    Returns:
        One :class:`ProbeEvidence` per probe command, in probe order.
    """
    commands: list[list[str]] = [[binary, "--version"], [binary, "--help"]]
    commands.extend([binary, *pattern] for pattern in _COMPLETION_PATTERNS)

    evidence: list[ProbeEvidence] = []
    for cmd in commands:
        rc, output = _run_capture(cmd, timeout=_PROBE_TIMEOUT_SECONDS, env=_sandbox_env())
        record = {
            "binary": binary,
            "command": " ".join(cmd),
            "exit_code": rc,
            "output": output,
        }
        evidence.append(_write_evidence(out_dir, record))
    return evidence


def _help_evidence(evidence: Sequence[ProbeEvidence]) -> ProbeEvidence:
    """Select the ``--help`` capture out of one probe run's evidence list.

    :func:`probe_cli` always emits evidence in the fixed order it declares
    its commands, but callers should not depend on that order; this walks
    the list and matches on the recorded command string instead.

    Raises:
        ValueError: No evidence entry recorded a ``--help`` invocation.
    """
    for item in evidence:
        if item.command.endswith(" --help"):
            return item
    raise ValueError("probe evidence has no --help capture to draft from")


def draft_from_probe(
    binary: str,
    out_dir: Path,
    *,
    required_fields: set[str] | None = None,
) -> Draft:
    """Probe ``binary`` and draft a capability profile from its ``--help`` capture.

    This is the caller :func:`~bernstein.adapters.draft.draft_from_evidence`
    was missing (issue #3763): it wires a real, freshly run :func:`probe_cli`
    capture into drafting, so a candidate profile can be produced from an
    installed CLI directly rather than only from a hand-written evidence
    fixture in a test. ``out_dir`` receives the raw probe evidence exactly as
    :func:`probe_cli` already writes it, so the drafted profile's provenance
    stays inspectable after this call returns.

    Args:
        binary: The CLI binary name to probe (resolved via ``PATH``).
        out_dir: Directory receiving the probe evidence files.
        required_fields: Forwarded to
            :func:`~bernstein.adapters.draft.draft_from_evidence`; field
            names drafting must resolve from evidence or refuse, by name.

    Returns:
        The :class:`~bernstein.adapters.draft.Draft` built from the probe's
        ``--help`` capture.

    Raises:
        ValueError: Drafting could not resolve a required field (see
            :func:`~bernstein.adapters.draft.draft_from_evidence`).
    """
    evidence = probe_cli(binary, out_dir)
    help_evidence = _help_evidence(evidence)
    return draft_from_evidence(help_evidence.path, required_fields=required_fields)


# ---------------------------------------------------------------------------
# Supervised transcript recording and held-out replay
# ---------------------------------------------------------------------------


_HELD_OUT_CASE_COUNT = 3
_TRANSCRIPT_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_RECORDED_ADAPTER_CLASS = "bernstein.adapters.capability_profile.RecordedProfileAdapter"


@dataclass(frozen=True)
class HeldOutInvocation:
    """One deterministic invocation in an onboarding held-out set.

    ``argv`` is stored as a tuple so callers cannot accidentally mutate the
    exact command that was derived from the profile. ``invocation`` is kept as
    an optional provenance handle for supervised replay; it is not persisted
    in a transcript or included in a result message.
    """

    argv: tuple[str, ...]
    prompt: str
    model: str
    env_passthrough: tuple[str, ...] = ()
    invocation: InvocationSpec | None = None

    def __post_init__(self) -> None:
        """Reject an invocation that cannot be passed to the runner."""
        if not self.argv or any(not isinstance(token, str) or not token for token in self.argv):
            raise ValueError("held-out invocation argv must contain non-empty string tokens")
        if not self.prompt:
            raise ValueError("held-out invocation prompt must be non-empty")
        if not self.model:
            raise ValueError("held-out invocation model must be non-empty")
        if any(not isinstance(key, str) or not key for key in self.env_passthrough):
            raise ValueError("held-out invocation env_passthrough must contain non-empty names")


def _evidence_path(evidence: Path | str | ProbeEvidence) -> Path:
    """Resolve a probe-evidence handle to its JSON path."""
    if isinstance(evidence, ProbeEvidence):
        return evidence.path
    return Path(evidence)


def _read_evidence(evidence: Path | str | ProbeEvidence) -> tuple[dict[str, Any], str]:
    """Read one probe record and return its document plus content hash."""
    path = _evidence_path(evidence)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"could not read probe evidence {path}: {exc}") from exc
    digest = _sha256_hex(payload)
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"probe evidence {path} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("probe evidence must contain a JSON object")
    binary = parsed.get("binary")
    if not isinstance(binary, str) or not binary:
        raise ValueError("probe evidence binary must be a non-empty string")
    return parsed, digest


def _onboarding_env(env_passthrough: Iterable[str] = ()) -> dict[str, str]:
    """Build the normalized environment for an explicit onboarding run."""
    extra = {key: os.environ[key] for key in env_passthrough if key in os.environ}
    return _sandbox_env(extra)


def _validate_timeout(timeout_seconds: int) -> None:
    """Require a finite positive subprocess timeout."""
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        raise ValueError("onboarding subprocess timeout must be a positive integer")


def _validate_transcript_name(name: str) -> str:
    """Return a safe one-component transcript filename stem."""
    if not isinstance(name, str) or not _TRANSCRIPT_SLUG_RE.fullmatch(name):
        raise ValueError("transcript name must be a safe single-component slug")
    return name


def _recorded_ctor_kwargs(profile: AdapterCapabilityProfile) -> dict[str, Any]:
    """Return primitive constructor fields for ``RecordedProfileAdapter``."""
    invocation = profile.invocation
    return {
        "registry_name": profile.name,
        "display_name": profile.display_name,
        "binary": invocation.binary,
        "subcommands": list(invocation.subcommands),
        "model_flag": invocation.model_flag,
        "prompt_flag": invocation.prompt_flag,
        "prompt_positional": invocation.prompt_positional,
        "extra_args": list(invocation.extra_args),
        "env_passthrough": list(invocation.env_passthrough),
    }


def _atomic_write_yaml(path: Path, document: dict[str, Any]) -> None:
    """Write one YAML document through a same-directory atomic replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(document, handle, default_flow_style=False, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        # ``os.replace`` removes the temporary directory entry on success;
        # unlinking only the remaining path keeps failed writes tidy.
        with suppress(FileNotFoundError):
            temporary.unlink()


def record_golden_transcript(
    profile: AdapterCapabilityProfile,
    *,
    name: str,
    smoke_prompt: str,
    smoke_model: str,
    golden_dir: Path,
    timeout_seconds: int = _PROBE_TIMEOUT_SECONDS,
) -> Path:
    """Supervise a known-good invocation and atomically record its transcript.

    The smoke command is built by :meth:`InvocationSpec.build_argv` and is
    persisted only after it exits successfully. Captured output is discarded
    after the exit status is checked; the transcript contains just the
    invocation declaration and the public prompt/model pair.

    Args:
        profile: Capability profile whose invocation is being onboarded.
        name: Safe one-component transcript filename stem.
        smoke_prompt: Public prompt used for the supervised smoke command.
        smoke_model: Public model id used for the supervised smoke command.
        golden_dir: Directory receiving ``<name>.yaml``.
        timeout_seconds: Finite timeout for the smoke process.

    Returns:
        The atomically replaced transcript path.

    Raises:
        ValueError: Invalid name, prompt/model, or timeout.
        RuntimeError: The smoke process did not exit successfully.
        OSError: The destination could not be written atomically.
    """
    if not isinstance(profile, AdapterCapabilityProfile):
        raise TypeError("profile must be an AdapterCapabilityProfile")
    safe_name = _validate_transcript_name(name)
    if not smoke_prompt or not smoke_model:
        raise ValueError("smoke_prompt and smoke_model must be non-empty")
    _validate_timeout(timeout_seconds)

    invocation = profile.invocation
    smoke_argv = invocation.build_argv(prompt=smoke_prompt, model=smoke_model)
    exit_code, _output = _run_capture(
        smoke_argv,
        timeout=timeout_seconds,
        env=_onboarding_env(invocation.env_passthrough),
    )
    if exit_code != 0:
        raise RuntimeError(f"onboarding smoke invocation failed with exit code {exit_code}")

    document: dict[str, Any] = {
        "name": safe_name,
        "adapter_class": _RECORDED_ADAPTER_CLASS,
        "ctor_kwargs": _recorded_ctor_kwargs(profile),
        "steps": [{"prompt": smoke_prompt, "model": smoke_model}],
    }
    # Parse the exact existing transcript shape before persistence. This keeps
    # a malformed in-memory document from becoming an apparently absent file
    # under the loader's intentional malformed-file suppression.
    GoldenTranscript(
        name=safe_name,
        adapter_class=_RECORDED_ADAPTER_CLASS,
        ctor_kwargs=dict(document["ctor_kwargs"]),
        steps=[TranscriptStep(prompt=smoke_prompt, model=smoke_model)],
    )
    target = golden_dir / f"{safe_name}.yaml"
    _atomic_write_yaml(target, document)
    return target


def _argv_tuple(raw: object, field_name: str) -> tuple[str, ...]:
    """Convert one serialized argv to an immutable tuple."""
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise ValueError(f"{field_name} must be a list of strings")
    argv = tuple(raw)
    if not argv or any(not isinstance(token, str) or not token for token in argv):
        raise ValueError(f"{field_name} must be a list of non-empty strings")
    return argv


def _embedded_recorded_argvs(evidence: dict[str, Any]) -> tuple[tuple[str, ...], ...]:
    """Read optional recorded argv fields from an evidence extension."""
    values: list[tuple[str, ...]] = []
    for field_name in ("recorded_argv", "recorded_argvs"):
        raw = evidence.get(field_name)
        if raw is None:
            continue
        if isinstance(raw, Sequence) and not isinstance(raw, str) and all(isinstance(item, str) for item in raw):
            values.append(_argv_tuple(raw, field_name))
            continue
        if not isinstance(raw, Sequence) or isinstance(raw, str):
            raise ValueError(f"{field_name} must be an argv list or list of argv lists")
        values.extend(_argv_tuple(item, field_name) for item in raw)
    return tuple(values)


def derive_held_out_invocations(
    evidence: Path | str | ProbeEvidence,
    invocation: InvocationSpec,
    *,
    smoke_prompt: str,
    smoke_model: str,
    recorded_argvs: Iterable[Sequence[str]] = (),
) -> tuple[HeldOutInvocation, ...]:
    """Derive three unique, deterministic invocations outside recorded argv.

    The evidence bytes, smoke prompt, and smoke model form the seed. Every
    candidate is rebuilt through :meth:`InvocationSpec.build_argv`; only its
    safe prompt payload differs from the known-good smoke input. Existing
    smoke/recorded argv tuples are excluded directly rather than by a lossy
    string comparison.

    Args:
        evidence: Probe evidence path or :class:`ProbeEvidence` handle.
        invocation: Invocation drafted from that evidence.
        smoke_prompt: Public prompt used by the successful smoke recording.
        smoke_model: Public model used by the successful smoke recording.
        recorded_argvs: Additional complete argv tuples already recorded.

    Returns:
        Exactly three immutable held-out cases in stable order.

    Raises:
        ValueError: Evidence is malformed, names a different binary, or does
            not permit three distinct held-out invocations.
    """
    if not isinstance(invocation, InvocationSpec):
        raise TypeError("invocation must be an InvocationSpec")
    if not smoke_prompt or not smoke_model:
        raise ValueError("smoke_prompt and smoke_model must be non-empty")
    evidence_doc, evidence_hash = _read_evidence(evidence)
    if evidence_doc["binary"] != invocation.binary:
        raise ValueError(
            f"probe evidence binary {evidence_doc['binary']!r} does not match invocation binary {invocation.binary!r}"
        )

    recorded: set[tuple[str, ...]] = {
        tuple(invocation.build_argv(prompt=smoke_prompt, model=smoke_model)),
        *_embedded_recorded_argvs(evidence_doc),
    }
    for index, raw_argv in enumerate(recorded_argvs):
        recorded.add(_argv_tuple(raw_argv, f"recorded_argvs[{index}]"))

    seed = _sha256_hex(
        json.dumps(
            {
                "evidence_sha256": evidence_hash,
                "smoke_model": smoke_model,
                "smoke_prompt": smoke_prompt,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    cases: list[HeldOutInvocation] = []
    counter = 0
    while len(cases) < _HELD_OUT_CASE_COUNT and counter < 4096:
        token = _sha256_hex(f"{seed}:{counter}".encode("ascii"))[:24]
        prompt = f"bernstein-held-out-{counter + 1}-{token}"
        argv = tuple(invocation.build_argv(prompt=prompt, model=smoke_model))
        counter += 1
        if argv in recorded or any(case.argv == argv for case in cases):
            continue
        cases.append(
            HeldOutInvocation(
                argv=argv,
                prompt=prompt,
                model=smoke_model,
                env_passthrough=invocation.env_passthrough,
                invocation=invocation,
            )
        )
    if len(cases) != _HELD_OUT_CASE_COUNT:
        raise ValueError("fewer than three unique held-out invocations can be formed")
    return tuple(cases)


def _case_from_raw(raw: HeldOutInvocation | Sequence[str]) -> HeldOutInvocation:
    """Accept a typed case or a raw argv for defensive replay callers."""
    if isinstance(raw, HeldOutInvocation):
        return raw
    argv = _argv_tuple(raw, "held_out_invocation")
    # A raw argv has no independently recoverable prompt/model metadata; it is
    # still executable and is represented with safe placeholders in the typed
    # result object. Normal derivation always supplies the richer case.
    return HeldOutInvocation(argv=argv, prompt="held-out", model="held-out")


def replay_held_out_invocations(
    invocations: Iterable[HeldOutInvocation | Sequence[str]],
    *,
    transcript_name: str = "held-out-invocations",
    adapter_class: str = _RECORDED_ADAPTER_CLASS,
    invocation: InvocationSpec | None = None,
    timeout_seconds: int = _PROBE_TIMEOUT_SECONDS,
) -> TranscriptResult:
    """Execute every held-out argv and return ordinary transcript results.

    A zero exit is the only passing outcome. ``_run_capture`` normalizes
    missing binaries (127), timeouts (124), and execution errors, while its
    captured output remains transient and is never copied into a result
    message.
    """
    _validate_timeout(timeout_seconds)
    cases = tuple(_case_from_raw(item) for item in invocations)
    result = TranscriptResult(transcript_name=transcript_name, adapter_class=adapter_class)
    if not cases:
        result.step_results.append(
            StepResult(step_index=0, passed=False, message="no held-out invocations were supplied")
        )
        return result

    fallback_env = invocation.env_passthrough if invocation is not None else ()
    for index, case in enumerate(cases):
        env_passthrough = case.env_passthrough or fallback_env
        try:
            exit_code, _output = _run_capture(
                list(case.argv),
                timeout=timeout_seconds,
                env=_onboarding_env(env_passthrough),
            )
            passed = exit_code == 0
            message = f"exit_code={exit_code}"
        except (OSError, TypeError, ValueError) as exc:
            passed = False
            message = f"execution error: {type(exc).__name__}"
        result.step_results.append(StepResult(step_index=index, passed=passed, message=message))
    return result
