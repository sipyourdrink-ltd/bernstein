"""Probe an installed CLI binary and capture its self-description as evidence.

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

Network-level sandboxing is deliberately out of scope: ``_sandbox_env`` only
sets opt-out environment variables (``CI``, ``NO_COLOR``, ``DO_NOT_TRACK``).
Denying network egress is left to the caller's environment.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.adapters._contract import _run_capture, _sandbox_env

if TYPE_CHECKING:
    from pathlib import Path

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

__all__ = ["ProbeEvidence", "probe_cli"]


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
