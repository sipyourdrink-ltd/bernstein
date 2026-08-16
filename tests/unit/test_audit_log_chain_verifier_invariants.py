"""Chain-verifier invariants that pin the audit_log mutation gate (issue #3998).

``scripts/mutmut_critical.py --only audit_log`` mutates
``src/bernstein/core/security/audit.py`` one line at a time and reruns the
module's test list; a mutation that leaves every test passing is a
"survivor" - a property of the verifier that no test protects. This file
closes the survivor clusters that were untested as of the 2026-08-16 weekly
run: salvage-offset arithmetic (how much of a damaged log is still
trustworthy), canonicality rejection in chain-tail recovery, the re-entrant
append-lock depth guard, and the input-shape guards on stated chain
predecessors.

Each test is named for the property it protects, not for the mutant it
happens to kill, and calls the private helpers directly where that is the
only way to observe the property in isolation (chain recovery, the
append-lock depth counter, and per-failure position/length bookkeeping have
no public accessor). ``test_audit_log_mutation_kill.py`` is left untouched;
this file is additive, following the same one-sweep-per-file convention its
own docstring documents.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.security import audit as audit_module
from bernstein.core.security.audit import (
    _APPEND_DEPTH,  # pyright: ignore[reportPrivateUsage]
    _GENESIS_HMAC,  # pyright: ignore[reportPrivateUsage]
    AUDIT_KEY_ENV,
    CHAIN_ANCHOR_KEY,
    ArchiveResult,
    AuditKeyMissingError,
    AuditLog,
    _audit_dir_key,  # pyright: ignore[reportPrivateUsage]
    _chain_append_lock,  # pyright: ignore[reportPrivateUsage]
    _chain_tail_from_bytes,  # pyright: ignore[reportPrivateUsage]
    _ChainWalkContext,  # pyright: ignore[reportPrivateUsage]
    _compute_hmac,  # pyright: ignore[reportPrivateUsage]
    _empty_str_list,  # pyright: ignore[reportPrivateUsage]
    _inside_append_section,  # pyright: ignore[reportPrivateUsage]
    _local_tail_state,  # pyright: ignore[reportPrivateUsage]
    _path_size,  # pyright: ignore[reportPrivateUsage]
    _stated_predecessor,  # pyright: ignore[reportPrivateUsage]
    _verify_log_bytes,  # pyright: ignore[reportPrivateUsage]
    load_audit_key,
)

# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def audit_dir(tmp_path: Path) -> Path:
    d = tmp_path / "audit"
    d.mkdir()
    return d


def _run_bounded(fn: Any, *, timeout: float = 5.0) -> None:
    """Run `fn()` on a daemon thread and fail fast instead of hanging forever.

    A broken re-entrancy guard can make a nested `_chain_append_lock` entry
    attempt a *real* second `flock(LOCK_EX)` on a fresh fd for the same
    file. flock contention is per open-file-description, not per-thread, so
    that second call blocks the calling thread against the first
    indefinitely - a genuine OS-level deadlock, not a Python-level one
    `pytest.raises` or a plain call can observe. Running `fn` on a daemon
    thread and joining with a bound turns "this mutation deadlocks" into a
    fast, explicit test failure: the thread leaks (harmless - it is daemon
    and the process moves on), but the test itself, and the pytest process
    running it, does not hang.
    """
    outcome: dict[str, BaseException] = {}

    def _target() -> None:
        try:
            fn()
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    assert not thread.is_alive(), (
        f"did not return within {timeout}s - looks like a real flock self-deadlock, not a slow test"
    )
    if "error" in outcome:
        raise outcome["error"]


def _canonical_entry(**overrides: Any) -> dict[str, Any]:
    """A well-formed audit record body, canonical-serialisable as written."""
    base: dict[str, Any] = {
        "timestamp": "2026-01-01T00:00:00.000000Z",
        "event_type": "e",
        "actor": "a",
        "resource_type": "r",
        "resource_id": "i",
        "details": {},
        "prev_hmac": _GENESIS_HMAC,
        "hmac": "0" * 64,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Platform/import guard (line ~39): fcntl must be the real module on POSIX,
# not the Windows-only stub - otherwise the cross-process append lock
# silently becomes a no-op on every non-Windows install.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl is a stub only on win32")
def test_posix_platforms_import_real_fcntl_module() -> None:
    """On non-Windows, audit.py's module-level `fcntl` name is the real stdlib module."""
    assert audit_module.fcntl is not None
    assert hasattr(audit_module.fcntl, "flock"), "audit.fcntl must be the real module, not a stub"


# ---------------------------------------------------------------------------
# load_audit_key path resolution (lines ~223-224): the ternary that picks
# between an explicit key_path and the env/XDG default, and the
# missing-key guard that must raise before any permission check runs.
# ---------------------------------------------------------------------------


def test_load_audit_key_honours_explicit_path_over_env_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit key_path= wins even when the env default also resolves to a real key."""
    explicit = tmp_path / "explicit.key"
    explicit.write_bytes(b"E" * 64)
    explicit.chmod(0o600)
    env_default = tmp_path / "env-default.key"
    env_default.write_bytes(b"D" * 64)
    env_default.chmod(0o600)
    monkeypatch.setenv(AUDIT_KEY_ENV, str(env_default))

    assert load_audit_key(key_path=explicit) == b"E" * 64


def test_load_audit_key_falls_back_to_default_path_when_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """key_path=None resolves through the env/XDG default, not a literal None path."""
    env_default = tmp_path / "env-default.key"
    env_default.write_bytes(b"D" * 64)
    env_default.chmod(0o600)
    monkeypatch.setenv(AUDIT_KEY_ENV, str(env_default))

    assert load_audit_key(key_path=None) == b"D" * 64


def test_load_audit_key_raises_missing_before_permission_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing key file raises AuditKeyMissingError, not a stat/permission crash."""
    missing = tmp_path / "does-not-exist.key"
    with pytest.raises(AuditKeyMissingError):
        load_audit_key(key_path=missing)


# ---------------------------------------------------------------------------
# Dataclass default factory (line ~289): ArchiveResult's list fields must
# default to a genuinely empty list, not one seeded with a stray element.
# ---------------------------------------------------------------------------


def test_empty_str_list_factory_returns_plain_empty_list() -> None:
    """_empty_str_list() (ArchiveResult's default factory) returns [] with no stray element."""
    result = _empty_str_list()
    assert result == []
    assert None not in result
    assert ArchiveResult().archived == []
    assert ArchiveResult().skipped == []


# ---------------------------------------------------------------------------
# Re-entrant append-lock depth guard (lines ~411-469). The guard lets the
# outermost `_chain_append_lock` acquisition own the cross-process flock
# while inner (same-thread) re-entries pass through without re-acquiring -
# re-acquiring flock on a fresh fd for the same file self-deadlocks.
# ---------------------------------------------------------------------------


def test_inside_append_section_false_for_untouched_dir(audit_dir: Path) -> None:
    """A dir this thread has never entered reports no active section (not a stale True)."""
    assert _inside_append_section(audit_dir) is False


def test_inside_append_section_true_during_lock_false_after(audit_dir: Path) -> None:
    """The section is active only strictly between entry and exit."""
    assert _inside_append_section(audit_dir) is False
    with _chain_append_lock(audit_dir):
        assert _inside_append_section(audit_dir) is True
    assert _inside_append_section(audit_dir) is False


def test_append_lock_on_fresh_thread_does_not_crash(audit_dir: Path) -> None:
    """A thread that has never touched _APPEND_DEPTH probes cleanly (no AttributeError)."""
    outcome: dict[str, object] = {}

    def _probe() -> None:
        try:
            outcome["value"] = _inside_append_section(audit_dir)
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=_probe)
    thread.start()
    thread.join(timeout=5)
    assert "error" not in outcome, f"fresh-thread probe raised: {outcome.get('error')!r}"
    assert outcome.get("value") is False


def test_append_lock_creates_missing_nested_audit_dir(tmp_path: Path) -> None:
    """_chain_append_lock creates a multi-level-missing audit dir (parents=True)."""
    nested = tmp_path / "a" / "b" / "c" / "audit"
    assert not nested.exists()
    with _chain_append_lock(nested):
        pass
    assert nested.is_dir()


def test_append_lock_depth_counter_increments_and_decrements_exactly(audit_dir: Path) -> None:
    """Three-level nesting counts 1, 2, 3 on the way in and 2, 1, (absent) on the way out.

    Run on a bounded daemon thread (see _run_bounded): a broken depth guard
    here means a nested entry retakes a real flock and hangs the calling
    thread forever, which must surface as a fast test failure, not a stuck
    subprocess.
    """
    key = _audit_dir_key(audit_dir)

    def _nest() -> None:
        with _chain_append_lock(audit_dir):
            assert _APPEND_DEPTH.depths[key] == 1
            with _chain_append_lock(audit_dir):
                assert _APPEND_DEPTH.depths[key] == 2
                with _chain_append_lock(audit_dir):
                    assert _APPEND_DEPTH.depths[key] == 3
                assert _APPEND_DEPTH.depths[key] == 2
            assert _APPEND_DEPTH.depths[key] == 1
        assert key not in _APPEND_DEPTH.depths, "guard must be fully unwound (key removed) at depth 0"

    _run_bounded(_nest)


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only; audit.fcntl is a stub there")
def test_nested_append_lock_acquires_flock_exactly_once(audit_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Three nested entries take the OS-level flock exactly once (the outermost only).

    A second real acquisition on a fresh fd for the same file would block
    forever against the first (flock is per open-file-description, not
    per-thread), so this is the property that keeps re-entrant append()
    calls from self-deadlocking. Run on a bounded daemon thread (see
    _run_bounded) precisely because a mutant that breaks this property
    causes that exact deadlock for real.
    """
    import fcntl as _fcntl

    calls: list[int] = []
    real_flock = _fcntl.flock

    def _spy(fd: int, operation: int) -> None:
        if operation == _fcntl.LOCK_EX:
            calls.append(fd)
        return real_flock(fd, operation)

    monkeypatch.setattr(_fcntl, "flock", _spy)

    def _nest() -> None:
        with _chain_append_lock(audit_dir):
            with _chain_append_lock(audit_dir):
                with _chain_append_lock(audit_dir):
                    pass

    _run_bounded(_nest)
    assert len(calls) == 1, f"expected exactly one LOCK_EX acquisition for 3-level nesting, got {len(calls)}"


# ---------------------------------------------------------------------------
# _path_size docstring (line ~474): a single-line docstring, which the
# harness's docstring-skip only recognises across multiple physical lines,
# so its prose is a live mutation target. Pinned the same way the existing
# suite already pins two other docstrings.
# ---------------------------------------------------------------------------


def test_path_size_docstring_describes_missing_file_as_negative_one() -> None:
    """_path_size's docstring documents the -1-when-absent contract in words."""
    doc = _path_size.__doc__
    assert doc is not None
    assert "or ``-1``" in doc, f"expected 'or ``-1``' in docstring, got: {doc!r}"
    assert "does not exist" in doc


# ---------------------------------------------------------------------------
# Input-shape guards on the stated chain predecessor (lines ~627-630).
# A record's self-reported CHAIN_ANCHOR_KEY must be trusted only when it is
# an actual non-empty string; anything else is "makes no assertion", not a
# value to hand back to a caller that treats it as a chain digest.
# ---------------------------------------------------------------------------


def test_stated_predecessor_returns_none_for_non_dict_details() -> None:
    """A non-dict 'details' payload yields None, not an AttributeError from .get()."""
    entry = {"event_type": "x", "details": "not-a-dict"}
    assert _stated_predecessor(entry) is None


def test_stated_predecessor_returns_none_for_non_string_anchor() -> None:
    """A non-string anchor value (e.g. a stray int) is refused, not returned as-is."""
    entry = {"event_type": "x", "details": {CHAIN_ANCHOR_KEY: 12345}}
    assert _stated_predecessor(entry) is None


def test_stated_predecessor_returns_none_for_empty_string_anchor() -> None:
    """An empty-string anchor is 'no assertion', not a claimed (empty) predecessor."""
    entry = {"event_type": "x", "details": {CHAIN_ANCHOR_KEY: ""}}
    assert _stated_predecessor(entry) is None


def test_stated_predecessor_returns_the_anchor_when_present_and_valid() -> None:
    """A genuine non-empty string anchor is returned unchanged."""
    entry = {"event_type": "x", "details": {CHAIN_ANCHOR_KEY: "abc123"}}
    assert _stated_predecessor(entry) == "abc123"


# ---------------------------------------------------------------------------
# Salvage-offset arithmetic (lines ~652-789, 923-926). After a tamper is
# found, ok_end (and _local_tail_state's verified_prefix_end) are supposed
# to name the exact byte offset of the last trustworthy record - not one
# byte short, not one byte long. A 2-valid-lines-plus-torn-tail fixture is
# used throughout rather than an all-clean file: at the exact end of a
# clean buffer several of the off-by-one mutants are silently masked by the
# "does this line own a terminator" boundary check, so only a fixture with
# real trailing damage exposes them.
# ---------------------------------------------------------------------------


def test_ok_end_names_the_last_trustworthy_byte(audit_dir: Path) -> None:
    """ok_end stops exactly at the end of the last verifying record's own terminator."""
    log = AuditLog(audit_dir, key=b"k")
    log.log("evt1", "actor", "task", "rid", {"n": 1})
    log.log("evt2", "actor", "task", "rid", {"n": 2})
    path = sorted(audit_dir.glob("*.jsonl"))[0]
    clean = path.read_bytes()
    assert clean.endswith(b"\n"), "test setup: expected a clean, newline-terminated log"

    garbage = b"NOT-JSON-GARBAGE-TAIL"
    tampered = clean + garbage  # no trailing newline: an unterminated torn suffix
    path.write_bytes(tampered)

    ctx = _ChainWalkContext()
    errors: list[str] = []
    _verify_log_bytes(tampered, path.name, _GENESIS_HMAC, b"k", errors, ctx)

    assert ctx.order == [path.name]
    assert ctx.ok_end[path.name] == len(clean), (
        f"expected the trustworthy prefix to end exactly at {len(clean)} "
        f"(before the torn suffix), got {ctx.ok_end[path.name]}"
    )
    assert path.name in ctx.missing_newline
    assert len(ctx.failures) == 1
    finding = ctx.failures[0]
    assert finding.kind == "invalid_json"
    assert finding.offset == len(clean), "the failure must be pinned at the torn suffix's own start"
    assert finding.length == len(garbage)


def test_ok_end_does_not_credit_a_phantom_terminator_byte(audit_dir: Path) -> None:
    """A verifying record with no owned trailing newline is not credited one extra byte.

    The garbage-tail fixture above puts line_end nowhere near len(raw_bytes), so it
    never exercises the '<' boundary itself - only a buffer that ends exactly at a
    valid record's last byte (nothing after it at all, not even a newline) makes
    line_end == len(raw_bytes) and distinguishes '<' from '<='.
    """
    log = AuditLog(audit_dir, key=b"k")
    log.log("evt1", "actor", "task", "rid", {"n": 1})
    path = sorted(audit_dir.glob("*.jsonl"))[0]
    clean = path.read_bytes()
    assert clean.endswith(b"\n"), "test setup: expected a clean, newline-terminated log"
    truncated = clean[:-1]  # drop only the writer's own trailing newline

    ctx = _ChainWalkContext()
    errors: list[str] = []
    _verify_log_bytes(truncated, path.name, _GENESIS_HMAC, b"k", errors, ctx)

    assert ctx.ok_end[path.name] == len(truncated), (
        f"a record with no owned terminator must not be credited past its own last byte "
        f"(expected {len(truncated)}, got {ctx.ok_end[path.name]})"
    )


def test_ok_end_stays_at_zero_when_no_line_ever_verifies() -> None:
    """When nothing in a segment verifies, ok_end reports 0, not a phantom floor."""
    ctx = _ChainWalkContext()
    errors: list[str] = []
    raw = b"NOT-VALID-JSON\n"
    _verify_log_bytes(raw, "2026-01-01.jsonl", _GENESIS_HMAC, b"k", errors, ctx)
    assert ctx.ok_end["2026-01-01.jsonl"] == 0


def test_failure_offset_and_length_for_undecodable_bytes() -> None:
    """A line that is not valid UTF-8 is pinned with its real offset and byte length."""
    ctx = _ChainWalkContext()
    errors: list[str] = []
    bad_line = b"\x80\x81\x82"
    raw = bad_line + b"\n"
    _verify_log_bytes(raw, "seg.jsonl", _GENESIS_HMAC, b"k", errors, ctx)
    assert len(ctx.failures) == 1
    finding = ctx.failures[0]
    assert finding.kind == "undecodable"
    assert finding.offset == 0
    assert finding.length == len(bad_line)


def test_failure_offset_and_length_for_non_object_entry() -> None:
    """A line that parses to a JSON array (not an object) is pinned with offset and length."""
    ctx = _ChainWalkContext()
    errors: list[str] = []
    bad_line = b"[1, 2, 3]"
    raw = bad_line + b"\n"
    _verify_log_bytes(raw, "seg.jsonl", _GENESIS_HMAC, b"k", errors, ctx)
    assert len(ctx.failures) == 1
    finding = ctx.failures[0]
    assert finding.kind == "not_object"
    assert finding.offset == 0
    assert finding.length == len(bad_line)


def test_failure_offset_and_length_for_non_canonical_entry() -> None:
    """A line with valid-but-unsorted-key JSON is pinned with offset and length."""
    ctx = _ChainWalkContext()
    errors: list[str] = []
    bad_line = b'{"b": 1, "a": 2}'
    canonical = json.dumps({"b": 1, "a": 2}, sort_keys=True).encode()
    assert bad_line != canonical, "test setup: line must not already be canonical"
    raw = bad_line + b"\n"
    _verify_log_bytes(raw, "seg.jsonl", _GENESIS_HMAC, b"k", errors, ctx)
    assert len(ctx.failures) == 1
    finding = ctx.failures[0]
    assert finding.kind == "non_canonical"
    assert finding.offset == 0
    assert finding.length == len(bad_line)


def test_failure_offset_and_length_for_invalid_hmac_entry() -> None:
    """A canonical, hmac-bearing line with a forged hmac is pinned with offset and length."""
    ctx = _ChainWalkContext()
    errors: list[str] = []
    entry = _canonical_entry()
    bad_line = json.dumps(entry, sort_keys=True).encode()
    raw = bad_line + b"\n"
    _verify_log_bytes(raw, "seg.jsonl", _GENESIS_HMAC, b"k", errors, ctx)
    assert len(ctx.failures) == 1
    finding = ctx.failures[0]
    assert finding.kind == "invalid_hmac"
    assert finding.offset == 0
    assert finding.length == len(bad_line)


def test_local_tail_state_offset_excludes_torn_suffix(audit_dir: Path) -> None:
    """_local_tail_state's verified_prefix_end excludes a torn suffix, byte for byte."""
    log = AuditLog(audit_dir, key=b"k")
    log.log("evt1", "actor", "task", "rid", {"n": 1})
    last = log.log("evt2", "actor", "task", "rid", {"n": 2})
    path = sorted(audit_dir.glob("*.jsonl"))[0]
    clean = path.read_bytes()

    torn = clean + b"GARBAGE-NOT-JSON"  # no trailing newline
    prefix_end, head_hmac = _local_tail_state(torn, b"k")
    assert prefix_end == len(clean), f"expected the verified prefix to end at {len(clean)}, got {prefix_end}"
    assert head_hmac == last.hmac


# ---------------------------------------------------------------------------
# Canonicality rejection and local-MAC-validity in chain-tail recovery
# (lines ~901, ~905). Both guards decide whether a byte sequence at the end
# of a segment is trustworthy enough to resume the chain from - the same
# contract _verify_log_bytes enforces, applied at recovery time instead of
# verification time.
# ---------------------------------------------------------------------------


def test_chain_tail_recovery_rejects_non_canonical_candidate() -> None:
    """A well-formed, hmac-bearing line with non-sorted key order is not adopted as the tip.

    Re-canonicalising with sort_keys=True and requiring a byte-for-byte
    match is what makes this check meaningful: a line that parses and has
    the right shape but is not the writer's own byte framing must be
    rejected exactly as _verify_log_bytes would reject it, or recovery
    could resume from a tail verify() considers tampered.
    """
    entry = {"hmac": "a" * 64, "event_type": "e", "prev_hmac": _GENESIS_HMAC}
    non_canonical = json.dumps(entry, sort_keys=False).encode()
    canonical = json.dumps(entry, sort_keys=True).encode()
    assert non_canonical != canonical, "test setup: insertion order must differ from sorted order"

    raw = non_canonical + b"\n"
    assert _chain_tail_from_bytes(raw, key=None) is None


def test_chain_tail_recovery_rejects_locally_invalid_hmac_when_key_provided() -> None:
    """A canonical line with a forged hmac is rejected as a recovery candidate when a key is given.

    Crash-torn tails are arbitrary bytes that can accidentally parse as a
    canonical hmac-bearing object; only a genuine key-holder's MAC may be
    adopted as the chain tip.
    """
    entry = _canonical_entry(hmac="0" * 64)  # not a real HMAC produced by `key`
    raw = json.dumps(entry, sort_keys=True).encode() + b"\n"
    assert _chain_tail_from_bytes(raw, key=b"k") is None


def test_local_tail_state_rejects_non_canonical_candidate() -> None:
    """A genuinely correctly-signed record in non-canonical byte order is not adopted.

    _local_tail_state applies the same canonical-bytes check as chain-tail
    recovery, but always with a key (its signature has no keyless mode), so
    the record here is built with a *real* HMAC for `key` - otherwise a
    forged hmac would make the candidate fail on local validity regardless
    of whether the canonical check itself still ran, masking the mutant.
    """
    key = b"k"
    body = {"event_type": "e", "prev_hmac": _GENESIS_HMAC}
    real_hmac = _compute_hmac(key, _GENESIS_HMAC, body)
    entry = {**body, "hmac": real_hmac}
    non_canonical = json.dumps(entry, sort_keys=False).encode()
    canonical = json.dumps(entry, sort_keys=True).encode()
    assert non_canonical != canonical, "test setup: insertion order must differ from sorted order"

    raw = non_canonical + b"\n"
    prefix_end, head_hmac = _local_tail_state(raw, key)
    assert prefix_end == 0
    assert head_hmac is None
