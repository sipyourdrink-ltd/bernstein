#!/usr/bin/env python3
"""Nightly real-run canary for end-to-end runtime coverage.

PR CI exercises unit / property / contract tests and an install-smoke that
checks ``--version`` / ``--help``. None of that actually *executes* a real
end-to-end orchestration flow, so integration-level runtime breaks -- a real
subprocess spawn, a real ``git worktree`` round-trip, a real audit-chain
append-then-verify, a real lineage receipt -- stay invisible until a user
hits them. This script closes that gap: it drives a small, representative
set of genuinely-runtime paths against the deterministic stub adapter, with
no LLM API key and no network egress, and turns any failure into a
structured telemetry event on the operator's error sink.

Design
------
* **Deterministic stub only.** The spawn flow uses
  :class:`bernstein.adapters.mock.MockAgentAdapter`, which forks a real
  subprocess running a self-contained python script -- no provider call,
  no key, no cost.
* **Real runtime, not mocks.** Each flow exercises a path the unit suite
  patches out: an actual ``subprocess.Popen`` + reap, an actual
  ``git worktree add`` / ``git worktree remove``, an actual HMAC-chained
  audit append + verify, and an actual Ed25519-signed lineage receipt that
  is re-verified through the same gate an auditor would use.
* **Fail-loud, report-rich.** On any flow exception the runner records a
  telemetry event tagged ``environment=canary`` carrying the flow name,
  exception type, and the failing top frame. The process then exits
  non-zero so the scheduled workflow goes red.
* **No leaks.** Every flow runs inside a temporary directory it owns and
  tears down, and the worktree flow removes its worktree + branch before
  returning, even on failure.

The emitter is the existing observability client
(:func:`bernstein.core.observability.error_capture.capture_exception`); this
script invents no new transport. With no ``BERNSTEIN_TELEMETRY_DSN``
configured the client is a no-op, mirroring the rest of the observability
boundary, so a missing DSN never makes the canary itself fail.

This module is intentionally *not* collected by the main pytest run -- it is
a scheduled job kept out of PR CI so it does not slow merges. Its seams are
unit-tested in ``tests/unit/test_canary_real_run.py`` with stub flows.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

logger = logging.getLogger("bernstein.canary")

#: Telemetry category for every canary event. Becomes ``logger=bernstein.canary``
#: in the error-tracker UI so an operator can filter the canary stream.
CANARY_CATEGORY = "canary"

#: Sentry-protocol ``environment`` tag, marking canary events ``env_canary``;
#: it also keeps canary noise out of the
#: production error stream.
CANARY_ENVIRONMENT = "canary"

#: A short, deterministic goal the spawn flow hands the stub adapter. The mock
#: adapter pattern-matches the prompt to a scripted fix; this string maps to its
#: ``broken_test`` branch, which is a pure no-op on an empty workdir (it logs and
#: exits 0), so the flow stays a clean spawn-and-reap with no real edits.
CANARY_GOAL = "Run the canary smoke task: verify the broken test harness spawns and exits cleanly."

#: Hard cap on how long the stub subprocess may run before we treat the spawn as
#: hung. The mock adapter sleeps ~2s of simulated work; 60s is generous slack.
SPAWN_REAP_TIMEOUT_S = 60


class EmitFn(Protocol):
    """Signature of the failure emitter the runner depends on.

    Decoupled behind a Protocol so the unit tests can inject a recorder and
    so the default (``error_capture.capture_exception``) is the only place
    that touches the real telemetry boundary.
    """

    def __call__(
        self,
        exc: BaseException,
        *,
        flow: str,
        tags: dict[str, str],
        extra: dict[str, Any],
    ) -> None: ...


# ---------------------------------------------------------------------------
# Default emitter -- routes through the existing observability client
# ---------------------------------------------------------------------------


def _default_emit(
    exc: BaseException,
    *,
    flow: str,
    tags: dict[str, str],
    extra: dict[str, Any],
) -> None:
    """Forward a canary failure to the operator-managed error sink.

    Reuses :func:`bernstein.core.observability.error_capture.capture_exception`,
    which fans out to ``sentry-sdk`` (when initialised) and the dependency-free
    side channel. Both are fail-closed and no-op without a DSN, so this never
    raises and never makes a failing canary worse.
    """
    from bernstein.core.observability import error_capture

    with contextlib.suppress(Exception):
        error_capture.capture_exception(
            exc,
            category=CANARY_CATEGORY,
            tags=tags,
            extra=extra,
        )


# ---------------------------------------------------------------------------
# Failure context extraction
# ---------------------------------------------------------------------------


def _top_frame(exc: BaseException) -> str:
    """Return ``file:line`` of the deepest frame in *exc*'s traceback.

    The ingester surfaces this as the "Top in-app frame" in the synthesised
    prompt, so an operator can jump straight to the failing call site.
    Returns ``"<unknown>"`` when no traceback is attached.
    """
    tb = exc.__traceback__
    if tb is None:
        return "<unknown>"
    frames = traceback.extract_tb(tb)
    if not frames:
        return "<unknown>"
    last = frames[-1]
    filename = os.path.basename(last.filename) or last.filename
    return f"{filename}:{last.lineno}"


def _build_event_context(flow: str, exc: BaseException) -> tuple[dict[str, str], dict[str, Any]]:
    """Build the ``(tags, extra)`` payload for a single flow failure.

    Tags carry the cheap, searchable fields the ingester reads
    (``environment``, ``flow``, ``exc_type``, ``top_frame``); extra carries
    the richer context (one-line value, trimmed traceback) for the prompt.
    """
    exc_type = type(exc).__name__
    top = _top_frame(exc)
    tags: dict[str, str] = {
        "environment": CANARY_ENVIRONMENT,
        "flow": flow,
        "exc_type": exc_type,
        "top_frame": top,
    }
    extra: dict[str, Any] = {
        "flow": flow,
        "exc_type": exc_type,
        "exc_value": str(exc),
        "top_frame": top,
    }
    return tags, extra


# ---------------------------------------------------------------------------
# Flow 1: real subprocess spawn via the deterministic stub adapter
# ---------------------------------------------------------------------------


def flow_subprocess_spawn(*, workdir: Path) -> int:
    """Spawn a worker through the stub adapter and reap it -- a real subprocess.

    Drives :class:`bernstein.adapters.mock.MockAgentAdapter`, which forks an
    actual ``subprocess.Popen`` running a self-contained python worker, then
    waits for it to exit. This is the path unit tests mock out wholesale: the
    real ``Popen`` argv assembly, env isolation, log file creation, and the
    ``proc.wait()`` reap.

    Args:
        workdir: A scratch directory the spawned worker treats as its repo
            root. The caller owns its lifecycle.

    Returns:
        The reaped exit code of the worker process (``0`` on success).

    Raises:
        AssertionError: if the worker exits non-zero or writes no log.
        TimeoutError: if the worker does not exit within the reap timeout.
    """
    from bernstein.adapters.mock import MockAgentAdapter

    # Import from the concrete module rather than the ``bernstein.core.models``
    # back-compat alias: the alias is served by a meta_path redirect finder
    # that static analysers cannot follow, so the concrete path keeps this
    # script type-clean when checked outside the package's pyright root.
    from bernstein.core.tasks.models import ModelConfig

    (workdir / ".sdd" / "runtime").mkdir(parents=True, exist_ok=True)

    adapter = MockAgentAdapter()
    session_id = f"canary-{uuid.uuid4().hex[:12]}"
    result = adapter.spawn(
        prompt=CANARY_GOAL,
        workdir=workdir,
        # The stub adapter ignores the model config; supply a valid one so
        # the contract is honoured without selecting any real provider.
        model_config=ModelConfig(model="sonnet", effort="medium"),
        session_id=session_id,
    )

    proc = getattr(result, "proc", None)
    if proc is None:  # pragma: no cover - mock always wires proc
        raise AssertionError("stub adapter returned no live process handle")

    # Cancel the adapter's own timeout watchdog once we own the reap, so a
    # late watchdog cannot kill an unrelated PID after we return.
    timer = getattr(result, "timeout_timer", None)
    try:
        try:
            code = int(proc.wait(timeout=SPAWN_REAP_TIMEOUT_S))
        except subprocess.TimeoutExpired as exc:
            with contextlib.suppress(Exception):
                proc.kill()
            raise TimeoutError(f"stub worker {session_id} did not exit within {SPAWN_REAP_TIMEOUT_S}s") from exc
    finally:
        if timer is not None:
            with contextlib.suppress(Exception):
                timer.cancel()

    if code != 0:
        raise AssertionError(f"stub worker exited {code}, expected 0")
    if not result.log_path.exists():
        raise AssertionError(f"stub worker wrote no log at {result.log_path}")
    logger.info("spawn flow ok: session=%s exit=%d", session_id, code)
    return code


# ---------------------------------------------------------------------------
# Flow 2: real git worktree create + teardown
# ---------------------------------------------------------------------------


def flow_git_worktree(*, repo_root: Path) -> None:
    """Create a real git worktree, confirm it exists, then tear it down.

    Drives :class:`bernstein.core.git.worktree.WorktreeManager`, exercising
    the actual ``git worktree add`` / ``git worktree remove`` plumbing the
    orchestrator uses for per-task isolation -- a path the unit suite stubs.
    The worktree is always removed before returning, including on failure, so
    the canary never leaks a worktree or branch into the repo.

    Args:
        repo_root: An initialised git repository to host the worktree.

    Raises:
        AssertionError: if the worktree directory is not created, or if it is
            still present after cleanup.
    """
    from bernstein.core.git.worktree import WorktreeManager

    # Disable salvage-push: this is a throwaway worktree, and a push attempt
    # against a bare local repo would be noise. Salvage-on-cleanup is harmless
    # but pointless here, so turn it off to keep the teardown fast and quiet.
    manager = WorktreeManager(repo_root, salvage_on_cleanup=False, salvage_push=False)
    session_id = f"canary-{uuid.uuid4().hex[:12]}"
    try:
        created = manager.create(session_id)
        if not created.is_dir():
            raise AssertionError(f"git worktree add did not create {created}")
    finally:
        manager.cleanup(session_id)

    # If create() had raised, the exception would have propagated through the
    # finally and we would not be here; reaching this line means a worktree
    # was created and cleanup() has run, so the leak check is unconditional.
    if created.exists():
        raise AssertionError(f"git worktree {created} survived cleanup -- leak")
    logger.info("worktree flow ok: session=%s", session_id)


# ---------------------------------------------------------------------------
# Flow 3: real audit-chain append + verify, real signed lineage receipt
# ---------------------------------------------------------------------------


def flow_audit_and_lineage(*, state_dir: Path) -> None:
    """Append HMAC-chained audit events + a signed lineage receipt, then verify.

    Two genuinely-runtime verification paths in one flow:

    * **Audit chain.** Append two events to
      :class:`bernstein.core.security.audit_chain.AuditChainStore` (real HMAC
      key creation + chaining) and assert ``verify()`` reports an intact
      chain.
    * **Lineage receipt.** Record a real artefact write through
      :class:`bernstein.core.lineage.recorder.LineageRecorder` (content hash,
      operator HMAC envelope, Ed25519 detached JWS) and re-verify the emitted
      receipt through :func:`bernstein.core.lineage.gate.check` -- the same
      gate an offline auditor runs. A well-formed receipt is the success
      condition; an unverifiable one raises and becomes a telemetry event.

    Args:
        state_dir: A scratch directory the flow owns; audit logs, the lineage
            store, and the agent card all live beneath it.

    Raises:
        AssertionError: if the audit chain or the lineage receipt fails to
            verify.
    """
    import json

    from bernstein.core.lineage.gate import check as lineage_check
    from bernstein.core.lineage.identity import AgentCard, generate_keypair
    from bernstein.core.lineage.recorder import LineageRecorder
    from bernstein.core.lineage.store import LineageStore
    from bernstein.core.security.audit_chain import AuditChainStore

    # -- audit chain ------------------------------------------------------
    audit_dir = state_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    chain = AuditChainStore(audit_dir)
    chain.log(
        event_type="canary.heartbeat",
        actor="canary",
        resource_type="canary",
        resource_id="seq-1",
        details={"step": 1},
    )
    chain.log_with_prev_digest(
        event_type="canary.heartbeat",
        actor="canary",
        resource_type="canary",
        resource_id="seq-2",
        details={"step": 2},
    )
    ok, failures = chain.verify()
    if not ok:
        raise AssertionError(f"audit chain failed to verify: {failures}")

    # -- lineage receipt --------------------------------------------------
    operator_key = b"canary-operator-hmac-key-not-a-secret"
    priv_pem, pub_pem = generate_keypair()
    agent_id = "agent:canary"
    kid = "canary-kid"
    card = AgentCard(agent_id=agent_id, kid=kid, public_key_pem=pub_pem)

    cards_dir = state_dir / "agents"
    card_dir = cards_dir / agent_id
    card_dir.mkdir(parents=True, exist_ok=True)
    (card_dir / "card.json").write_text(
        json.dumps(
            {
                "protocolVersion": card.protocol_version,
                "agent_id": agent_id,
                "kid": kid,
                "public_key_pem": pub_pem,
            }
        ),
        encoding="utf-8",
    )

    store = LineageStore(state_dir / "lineage")
    recorder = LineageRecorder(store, operator_hmac_key=operator_key)
    entry_hash = recorder.record_write(
        artefact_path="canary/output.txt",
        new_content=b"canary artefact bytes",
        agent_id=agent_id,
        agent_card=card,
        private_key_pem=priv_pem,
        tool_call_id="canary-tool-call",
        span_id="canary-span",
    )
    if not entry_hash.startswith("sha256:"):
        raise AssertionError(f"lineage recorder returned malformed hash: {entry_hash!r}")

    result = lineage_check(
        store.log_path,
        cards_dir,
        operator_secret=operator_key,
    )
    if not result.ok:
        raise AssertionError(f"lineage receipt failed to verify: {result.failures}")
    logger.info("audit+lineage flow ok: entry=%s", entry_hash)


# ---------------------------------------------------------------------------
# Flow registry + runner
# ---------------------------------------------------------------------------


def default_flows() -> dict[str, Callable[[], None]]:
    """Return the shipped canary flows as a name -> zero-arg callable map.

    Each flow allocates and tears down its own temp directory (or git repo),
    so the registry callables take no arguments and leave nothing behind. The
    set is deliberately small -- one representative path per runtime surface
    (subprocess, worktree, audit+lineage) -- to keep the canary fast and
    debuggable rather than an exhaustive integration suite.
    """
    return {
        "subprocess_spawn": _spawn_flow_self_contained,
        "git_worktree": _worktree_flow_self_contained,
        "audit_and_lineage": _audit_lineage_flow_self_contained,
    }


def _spawn_flow_self_contained() -> None:
    """Run :func:`flow_subprocess_spawn` inside an owned temp dir."""
    tmp = Path(tempfile.mkdtemp(prefix="bernstein-canary-spawn-"))
    try:
        flow_subprocess_spawn(workdir=tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _worktree_flow_self_contained() -> None:
    """Run :func:`flow_git_worktree` inside an owned, freshly-init'd git repo."""
    tmp = Path(tempfile.mkdtemp(prefix="bernstein-canary-worktree-"))
    try:
        _git_init(tmp)
        flow_git_worktree(repo_root=tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _audit_lineage_flow_self_contained() -> None:
    """Run :func:`flow_audit_and_lineage` inside an owned temp dir."""
    tmp = Path(tempfile.mkdtemp(prefix="bernstein-canary-audit-"))
    try:
        flow_audit_and_lineage(state_dir=tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _git_init(repo: Path) -> None:
    """Initialise a minimal git repo with one commit (worktree precondition)."""
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "canary@example.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Bernstein Canary"], cwd=str(repo), check=True)
    (repo / "README.md").write_text("# canary\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "canary init"], cwd=str(repo), check=True, capture_output=True)


def run_canary(
    *,
    flows: Mapping[str, Callable[[], None]] | None = None,
    emit: EmitFn | None = None,
) -> int:
    """Run every flow; report failures via telemetry; return a process exit code.

    Each flow runs independently: a failure in one does not stop the others,
    so a single canary run surfaces *every* broken surface rather than just
    the first. Every failure produces exactly one telemetry event carrying
    the flow name, exception type, top frame, and ``environment=canary``.

    Args:
        flows: Override the flow registry (used by tests). Defaults to
            :func:`default_flows`.
        emit: Override the failure emitter (used by tests). Defaults to
            :func:`_default_emit`, which routes through the existing
            observability client.

    Returns:
        ``0`` when every flow passed; ``1`` when one or more failed.
    """
    selected = flows if flows is not None else default_flows()
    emitter = emit if emit is not None else _default_emit

    failures = 0
    for name, fn in selected.items():
        try:
            fn()
        except Exception as exc:
            failures += 1
            logger.error("canary flow %r failed: %s", name, exc, exc_info=True)
            tags, extra = _build_event_context(name, exc)
            with contextlib.suppress(Exception):
                emitter(exc, flow=name, tags=tags, extra=extra)
        else:
            logger.info("canary flow %r passed", name)

    if failures:
        logger.error("canary: %d/%d flow(s) failed", failures, len(selected))
        return 1
    logger.info("canary: all %d flow(s) passed", len(selected))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Parses args, configures logging, runs the canary."""
    parser = argparse.ArgumentParser(description="Bernstein nightly real-run canary.")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Emit DEBUG-level logs for each flow.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return run_canary()


if __name__ == "__main__":
    sys.exit(main())
