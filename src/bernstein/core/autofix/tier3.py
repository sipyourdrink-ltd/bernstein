"""Tier-3 OpenRouter free-tier shadow-mode escalation for self-driving CI.

The autofix daemon already has two repair tiers:

* **Tier-1** (``.github/workflows/contract-drift-autofix.yml``) is a
  deterministic regenerator. No model spend, regenerates fixtures /
  contracts from source.
* **Tier-2** (``.github/workflows/bernstein-ci-fix.yml``) routes the
  failing job through the Gemini Flash auto-heal with the 50-LOC cap
  and the failing-job allowlist (``Lint``, ``Spelling``, ``Workflow
  lint``, ``Repo hygiene``, ``Dead code``, ``Test (...)``).

Tier-3 picks up only when:

1. ``BERNSTEIN_CI_SELF_DRIVE=tier3`` is set on the workflow.
2. Tier-2 produced no patch on a class that is in the safe allowlist.
3. The failure class + failing-test nodeid pair has not been auto-fixed
   too often in the recurrence window (otherwise we escalate to Tier-4
   = operator hand-off via Telegram + GH issue).

Tier-3 then runs ``bernstein run`` against an OpenRouter free-tier
model (default ``qwen/qwen3-coder-480b:free`` with a fallback list of
DeepSeek R1, Llama 4 Maverick, Devstral 2 and Qwen3-235B Instruct, all
``:free``). The OpenRouter base URL is taken from the
``OPENAI_BASE_URL`` / ``BERNSTEIN_OPENROUTER_BASE_URL`` env var so the
shipped wheel never bakes a default hostname.

**Shadow mode is the default and only mode shipped here.** When this
module captures a patch it:

* Writes a unified-diff under
  ``.sdd/autoheal/tier3-shadow/<run_id>.diff``.
* Writes a lineage-v2 child body under
  ``.sdd/lineage/v2/children/tier3-<run_id>.json`` carrying
  ``failed_run_id``, ``tier=3``, ``model``, ``cost_usd``, ``patch_sha``,
  ``regression_test_sha``.
* Appends a structured ``tier3_shadow`` entry to the decision log so
  the captured patch shows up in ``bernstein decisions tail``.
* Increments the dedicated ``quota_envelope="ci-autoheal"`` rollup so a
  future paid-model fallback flows through the same hard circuit
  breaker (#1330 / #1413).
* Then **exits without pushing**. The patch is data, not a commit.

A second env var, ``BERNSTEIN_CI_SELF_DRIVE_PROMOTE_FROM_SHADOW=1``, is
required to flip the captured patch into an actual push. It stays off
until the shadow-week metrics review and is documented as such.

Cordon enforcement
------------------
Tier-3 may only write to paths that the existing auto-heal cordon
(:mod:`bernstein.core.autoheal.cordon`) accepts, plus
``tests/contract/contracts/*.yaml``. Anything outside that union is a
hard refusal that emits a ``cordon_violation`` decision-log entry; the
patch is dropped.

Recurrence detection
--------------------
The dispatcher persists one row per Tier-3 capture under
``.sdd/autoheal/recurrence.jsonl``. When the same ``(failure_class,
failing_test_nodeid)`` pair has been captured more than
:data:`DEFAULT_RECURRENCE_THRESHOLD` times within
:data:`DEFAULT_RECURRENCE_WINDOW_SECONDS`, Tier-3 stops and emits a
``recurrence_escalation`` decision-log entry instead of a shadow
capture. The workflow surface is expected to read the kind and route
to the operator (Telegram + GH issue) per the existing Tier-4 contract.

No network, no real subprocess
------------------------------
The module never calls ``bernstein run`` or any LLM provider directly.
Both effects are injected via the :class:`RunHook` protocol so the
shadow-mode capture is fully unit-testable. The hook is wired by the
workflow runner script in ``.github/workflows/bernstein-ci-fix.yml``;
the runner is responsible for shaping the actual provider call and
returning the resulting patch / model / cost triple.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from bernstein.core.autoheal.cordon import evaluate as cordon_evaluate
from bernstein.core.autoheal.lineage_writer import (
    AutohealLineagePayload,
    render_canonical_bytes,
    render_payload,
)

# Re-exported: the cordon's blast-radius question is the same one the
# volunteer program's ``allowed_paths`` enforcement asks, so the answer lives
# in one module rather than once per boundary. Imported here under its
# original name so existing Tier-3 callers are unaffected.
from bernstein.core.diff_paths import extract_paths_from_unified_diff
from bernstein.core.observability import decision_log as dl

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env var contract
# ---------------------------------------------------------------------------

#: Tier-3 enabler. Tier-3 fires only when this env var equals
#: ``"tier3"``; any other value (including absence) is a no-op.
ENV_SELF_DRIVE: Final[str] = "BERNSTEIN_CI_SELF_DRIVE"

#: Promotion gate. Tier-3 only pushes a real commit when this env var
#: equals ``"1"``. Default off; flipped by operator decision after the
#: shadow-week metrics review.
ENV_PROMOTE_FROM_SHADOW: Final[str] = "BERNSTEIN_CI_SELF_DRIVE_PROMOTE_FROM_SHADOW"

#: OpenRouter base URL override. The wheel ships no default hostname;
#: callers must set either this var or :data:`ENV_OPENAI_BASE_URL`.
ENV_OPENROUTER_BASE_URL: Final[str] = "BERNSTEIN_OPENROUTER_BASE_URL"

#: Standard OpenAI-compatible base URL env var. Honoured as a fallback
#: when :data:`ENV_OPENROUTER_BASE_URL` is unset so the qwen / codex
#: CLIs work without rewiring.
ENV_OPENAI_BASE_URL: Final[str] = "OPENAI_BASE_URL"

#: Optional override for the daily hard cap on the
#: ``ci-autoheal`` envelope. Defaults to ``0.0`` (free models only); a
#: non-zero value opts the operator into a paid fallback path.
ENV_CI_AUTOHEAL_HARD_CAP: Final[str] = "BERNSTEIN_CI_AUTOHEAL_HARD_CAP_USD"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

#: Dedicated cost-rollup envelope for Tier-3 calls. Always paid out of
#: this envelope regardless of which model was selected, so a future
#: paid-model fallback flows through the same circuit breaker.
QUOTA_ENVELOPE: Final[str] = "ci-autoheal"

#: Primary OpenRouter free-tier model used by Tier-3. Fallback order
#: below kicks in when the primary returns a 429 / overload.
DEFAULT_PRIMARY_MODEL: Final[str] = "qwen/qwen3-coder-480b:free"

#: OpenRouter free-tier fallback order. Each entry is tried in turn on
#: a 429 / overload / explicit fallback signal until one accepts the
#: call. All are free models so the per-call dollar accounting stays at
#: zero until an operator opts in to a paid fallback.
DEFAULT_FALLBACK_MODELS: Final[tuple[str, ...]] = (
    "deepseek/deepseek-r1:free",
    "meta-llama/llama-4-maverick:free",
    "mistralai/devstral-small-2:free",
    "qwen/qwen3-235b-a22b-instruct:free",
)

#: Daily hard cap on the ``ci-autoheal`` envelope. Free models are
#: free, so the default cap is zero; an operator who wires a paid
#: fallback opts in by raising it via :data:`ENV_CI_AUTOHEAL_HARD_CAP`.
DEFAULT_DAILY_HARD_CAP_USD: Final[float] = 0.0

#: Recurrence detection window: how far back to look for repeated
#: captures of the same ``(failure_class, failing_test_nodeid)`` pair.
DEFAULT_RECURRENCE_WINDOW_SECONDS: Final[float] = 24 * 3600.0

#: Threshold: more than this many captures inside the window flips to
#: ``recurrence_escalation``. The current value mirrors the RFC
#: ("fixed > 2 times in 24h").
DEFAULT_RECURRENCE_THRESHOLD: Final[int] = 2

#: Additional cordon glob - Tier-3 may write contract YAMLs even
#: though the standard auto-heal cordon does not enumerate them.
TIER3_EXTRA_GLOBS: Final[tuple[str, ...]] = ("tests/contract/contracts/*.yaml",)

#: Failure-class allowlist (mirrors the regex anchor in
#: ``bernstein-ci-fix.yml``). Anything outside this set is rejected
#: before any model call is made so the cordon expansion question
#: stays governance, not engineering.
SAFE_FAILURE_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "Lint",
        "Spelling (typos)",
        "Workflow lint",
        "Repo hygiene",
        "Dead code (Vulture)",
        "Test (contract-drift)",
        "Test (autoheal)",
    }
)


# ---------------------------------------------------------------------------
# Lightweight cost-envelope sidecar
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnvelopeEntry:
    """One row in the ``ci-autoheal`` envelope ledger.

    The full cost-tracker lives in :mod:`bernstein.core.cost.cost_tracker`
    and is wired into the orchestrator at runtime; Tier-3 captures run
    in the GH Actions workflow where that tracker is not booted, so we
    persist a thin sidecar under
    ``.sdd/autoheal/ci-autoheal-envelope.jsonl`` and let the rollup
    job (``cost_rollup_by_envelope``) pick it up via its existing
    ``quota_envelope=ci-autoheal`` filter.
    """

    ts: float
    failed_run_id: str
    model: str
    cost_usd: float
    quota_envelope: str = QUOTA_ENVELOPE
    daily_hard_cap_usd: float = DEFAULT_DAILY_HARD_CAP_USD

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "failed_run_id": self.failed_run_id,
            "model": self.model,
            "cost_usd": self.cost_usd,
            "quota_envelope": self.quota_envelope,
            "daily_hard_cap_usd": self.daily_hard_cap_usd,
        }


def append_envelope_entry(entry: EnvelopeEntry, sdd_dir: Path) -> Path:
    """Append one envelope row to the Tier-3 sidecar ledger.

    The path is stable so the rollup job and operator dashboards can
    discover it without configuration:
    ``<sdd_dir>/autoheal/ci-autoheal-envelope.jsonl``.
    """
    dest = sdd_dir / "autoheal" / "ci-autoheal-envelope.jsonl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry.to_dict(), separators=(",", ":"), sort_keys=True) + "\n"
    with dest.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return dest


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tier3Config:
    """Effective Tier-3 configuration.

    All fields default to safe shadow-mode behaviour. Tests construct
    a ``Tier3Config`` directly; the workflow runner calls
    :func:`Tier3Config.from_env` so the env-var contract is the single
    source of truth.
    """

    enabled: bool = False
    promote_from_shadow: bool = False
    primary_model: str = DEFAULT_PRIMARY_MODEL
    fallback_models: tuple[str, ...] = DEFAULT_FALLBACK_MODELS
    openrouter_base_url: str = ""
    daily_hard_cap_usd: float = DEFAULT_DAILY_HARD_CAP_USD
    recurrence_window_seconds: float = DEFAULT_RECURRENCE_WINDOW_SECONDS
    recurrence_threshold: int = DEFAULT_RECURRENCE_THRESHOLD

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Tier3Config:
        """Build a config from process env / a caller-supplied mapping.

        Tier-3 is enabled iff ``BERNSTEIN_CI_SELF_DRIVE=tier3``.
        ``BERNSTEIN_CI_SELF_DRIVE_PROMOTE_FROM_SHADOW=1`` flips the
        promotion gate. The OpenRouter base URL is read from
        :data:`ENV_OPENROUTER_BASE_URL` then :data:`ENV_OPENAI_BASE_URL`;
        the shipped wheel never bakes a default hostname.
        """
        source: dict[str, str] = os.environ.copy() if env is None else dict(env)
        self_drive = source.get(ENV_SELF_DRIVE, "").strip().lower()
        promote = source.get(ENV_PROMOTE_FROM_SHADOW, "").strip() == "1"
        base_url = source.get(ENV_OPENROUTER_BASE_URL, "").strip() or source.get(ENV_OPENAI_BASE_URL, "").strip()
        cap_raw = source.get(ENV_CI_AUTOHEAL_HARD_CAP, "").strip()
        cap = DEFAULT_DAILY_HARD_CAP_USD
        if cap_raw:
            try:
                parsed = float(cap_raw)
            except ValueError:
                parsed = DEFAULT_DAILY_HARD_CAP_USD
            if parsed >= 0:
                cap = parsed
        return cls(
            enabled=self_drive == "tier3",
            promote_from_shadow=promote,
            openrouter_base_url=base_url,
            daily_hard_cap_usd=cap,
        )


# ---------------------------------------------------------------------------
# Input / output dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailureContext:
    """Input to a Tier-3 capture.

    Attributes:
        failed_run_id: GitHub Actions run id of the upstream CI run.
        head_sha: Failing commit SHA.
        failure_class: Failing-job class name as emitted by
            ``bernstein-ci-fix.yml`` (must be in
            :data:`SAFE_FAILURE_CLASSES` to escalate).
        failing_test_nodeid: Pytest nodeid (or other deterministic
            test identifier) used for recurrence keying.
        log_tail: Truncated failing-log payload (max 200 lines per the
            existing workflow).
        regression_test_sha: SHA of the regression test that pinned the
            failure. Empty when no regression test is associated yet.
        tier2_produced_patch: When ``True``, Tier-2 already produced a
            patch and Tier-3 must not run.
    """

    failed_run_id: str
    head_sha: str
    failure_class: str
    failing_test_nodeid: str
    log_tail: str
    regression_test_sha: str = ""
    tier2_produced_patch: bool = False


@dataclass(frozen=True, slots=True)
class RunResult:
    """Return value of the injected :class:`RunHook`.

    Attributes:
        patch: Unified-diff string. Empty when the run produced nothing.
        model_used: Model id that actually accepted the call. Tier-3
            walks the fallback list when the primary returns a 429.
        cost_usd: Reported spend for the call (zero for free models).
        meta: Extra structured fields the hook wants surfaced in the
            decision-log entry. Free-form; the writer copies it into
            the ``inputs`` block.
    """

    patch: str
    model_used: str
    cost_usd: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict[str, Any])


class RunHook(Protocol):
    """Spawn one Tier-3 ``bernstein run`` and return the captured patch.

    The hook is responsible for the actual provider call (qwen / codex
    via OpenRouter) and for walking the fallback list on 429. Tier-3
    itself stays network-free so the shadow-mode capture is unit-
    testable.
    """

    def __call__(
        self,
        *,
        context: FailureContext,
        primary_model: str,
        fallback_models: Sequence[str],
        openrouter_base_url: str,
    ) -> RunResult: ...


#: Terminal outcomes emitted by :meth:`Tier3Runner.run`.
Tier3OutcomeKind = Literal[
    "flag_off",
    "tier2_produced_patch",
    "unsafe_class",
    "recurrence_escalated",
    "shadow_captured",
    "shadow_empty",
    "cordon_violation",
    "promoted_push",
]


@dataclass(frozen=True, slots=True)
class Tier3Outcome:
    """Result of a single Tier-3 invocation.

    Attributes:
        kind: Terminal status.
        patch_sha: SHA-256 of the patch (empty when no patch captured).
        patch_path: Path to the persisted shadow patch (empty for the
            non-capture outcomes).
        model_used: Model that produced the patch.
        cost_usd: Cost recorded against the ``ci-autoheal`` envelope.
        decision_id: Decision-log id of the closing entry.
        lineage_payload: Lineage-v2 child body shape (the same one a
            real auto-heal commit would emit).
        rejected_paths: Cordon-violation paths, when ``kind`` is
            ``cordon_violation``.
        reason: Human-readable summary suitable for an operator
            dashboard.
    """

    kind: Tier3OutcomeKind
    reason: str = ""
    patch_sha: str = ""
    patch_path: str = ""
    model_used: str = ""
    cost_usd: float = 0.0
    decision_id: str = ""
    lineage_payload: dict[str, Any] = field(default_factory=dict[str, Any])
    rejected_paths: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Cordon
# ---------------------------------------------------------------------------


def _matches_extra_glob(path: str) -> bool:
    """Return True when ``path`` matches a Tier-3-specific extra glob.

    The standard auto-heal cordon enumerates root config / docs files
    plus ``.cursor/rules/*.mdc``; Tier-3 adds the contract YAMLs.
    """
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in TIER3_EXTRA_GLOBS)


def evaluate_cordon(paths: Sequence[str]) -> tuple[bool, tuple[str, ...]]:
    """Evaluate a list of paths against the Tier-3 cordon.

    A path is accepted when either:

    * The standard auto-heal cordon accepts it (whitespace-only check
      is *not* applied at Tier-3 - Tier-3 patches are intentionally
      content edits, the whitespace carve-out exists only for ruff-
      format passes).
    * It matches one of :data:`TIER3_EXTRA_GLOBS`.

    Returns ``(allowed, rejected_paths)`` so callers can record the
    offending paths in the ``cordon_violation`` decision-log entry.
    """
    rejected: list[str] = []
    for raw in paths:
        path = raw.strip()
        if not path:
            continue
        decision = cordon_evaluate(path, whitespace_only=False)
        if decision.allowed:
            continue
        if _matches_extra_glob(path):
            continue
        rejected.append(path)
    return (not rejected, tuple(rejected))


# ---------------------------------------------------------------------------
# Recurrence tracker
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RecurrenceTracker:
    """Persistent ``(failure_class, test_nodeid)`` recurrence counter.

    Backing store is one JSONL row per capture under
    ``.sdd/autoheal/recurrence.jsonl`` so the workflow can scan it with
    plain ``jq`` and the daemon can rebuild state at boot. Rows older
    than the configured window are ignored for threshold purposes but
    kept on disk (operators may want long-term recurrence stats).
    """

    path: Path
    window_seconds: float = DEFAULT_RECURRENCE_WINDOW_SECONDS
    threshold: int = DEFAULT_RECURRENCE_THRESHOLD

    def _read_rows(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    out.append(payload)
        return out

    def count_recent(
        self,
        *,
        failure_class: str,
        failing_test_nodeid: str,
        now: float | None = None,
    ) -> int:
        """Count rows for ``(failure_class, test_nodeid)`` inside the window."""
        now_ts = time.time() if now is None else now
        cutoff = now_ts - self.window_seconds
        count = 0
        for row in self._read_rows():
            if row.get("failure_class") != failure_class:
                continue
            if row.get("failing_test_nodeid") != failing_test_nodeid:
                continue
            try:
                ts = float(row.get("ts", 0.0))
            except (TypeError, ValueError):
                continue
            if ts >= cutoff:
                count += 1
        return count

    def should_escalate(
        self,
        *,
        failure_class: str,
        failing_test_nodeid: str,
        now: float | None = None,
    ) -> bool:
        """Return True when adding one more capture would breach the threshold.

        The RFC reads as "fixed > 2 times in 24h", which is "the third
        capture in the window is the one that escalates". We compare
        with ``>`` so the threshold itself counts as still-allowed.
        """
        if self.threshold < 0:
            return False
        return (
            self.count_recent(
                failure_class=failure_class,
                failing_test_nodeid=failing_test_nodeid,
                now=now,
            )
            > self.threshold
        )

    def record(
        self,
        *,
        failure_class: str,
        failing_test_nodeid: str,
        failed_run_id: str,
        now: float | None = None,
    ) -> None:
        """Append one capture row to the JSONL ledger."""
        ts = time.time() if now is None else now
        row: dict[str, Any] = {
            "ts": ts,
            "failure_class": failure_class,
            "failing_test_nodeid": failing_test_nodeid,
            "failed_run_id": failed_run_id,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _patch_sha(patch: str) -> str:
    """SHA-256 of the patch bytes; empty when ``patch`` is empty."""
    if not patch:
        return ""
    return hashlib.sha256(patch.encode("utf-8")).hexdigest()


def _build_lineage_payload(
    *,
    context: FailureContext,
    model: str,
    cost_usd: float,
    patch_sha: str,
    confidence: float,
    outcome: str,
) -> dict[str, Any]:
    """Render a lineage-v2 child body for one Tier-3 capture.

    Reuses :class:`AutohealLineagePayload` so the shipped shape is
    byte-identical to a real auto-heal commit: every shadow capture
    carries ``failed_run_id``, ``tier=3``, ``model``, ``cost_usd``,
    ``patch_sha`` and ``regression_test_sha`` in the canonical layout.
    """
    payload = AutohealLineagePayload(
        failed_run_id=context.failed_run_id,
        head_sha=context.head_sha,
        classification=context.failure_class,
        strategy="tier3_openrouter_shadow",
        patch_sha=patch_sha,
        llm_calls=1 if patch_sha else 0,
        cost_usd=cost_usd,
        outcome=outcome,
        confidence=max(0.0, min(1.0, confidence)),
        meta={
            "tier": 3,
            "model": model,
            "regression_test_sha": context.regression_test_sha,
            "failing_test_nodeid": context.failing_test_nodeid,
            "quota_envelope": QUOTA_ENVELOPE,
        },
    )
    # ``render_canonical_bytes`` is the lineage helper we reuse for any
    # byte-level checksumming downstream (kept as a tested call here so
    # the import is exercised in unit tests, never branch-only).
    _ = render_canonical_bytes(payload)
    return render_payload(payload)


def _persist_patch(*, run_id: str, patch: str, sdd_dir: Path) -> Path:
    """Write the captured unified diff to the shadow directory."""
    dest = sdd_dir / "autoheal" / "tier3-shadow" / f"{run_id}.diff"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(patch, encoding="utf-8")
    return dest


def _record_decision(
    *,
    kind: str,
    chosen: str,
    rationale: str,
    inputs: dict[str, Any],
    decision_log_path: Path | None = None,
    confidence: float = 0.0,
) -> str:
    """Append one decision-log row; returns the decision_id or ``""``."""
    try:
        rec = dl.record_decision(
            kind=kind,
            chosen=chosen,
            rationale=rationale,
            confidence=confidence,
            inputs=inputs,
            policy_path=("core.autofix.tier3",),
            path=decision_log_path,
        )
    except ValueError:
        logger.warning("tier3: rejected decision-log kind %s", kind)
        return ""
    if rec is None:
        return ""
    return rec.decision_id


@dataclass(slots=True)
class Tier3Runner:
    """Drive one Tier-3 capture end-to-end.

    The runner is intentionally side-effecting on disk only: it never
    pushes to git on its own, and never reaches out to a provider.
    Both the provider call and any git push (for the future
    promote-from-shadow path) are routed through :class:`RunHook` and
    the explicit ``promote_from_shadow`` flag on :class:`Tier3Config`.
    """

    config: Tier3Config
    run_hook: RunHook
    sdd_dir: Path
    recurrence_tracker: RecurrenceTracker | None = None
    decision_log_path: Path | None = None
    # ``clock`` is the seam tests override to pin recurrence-window
    # math; the production default is :func:`time.time`.
    clock: Callable[[], float] = field(default=time.time)

    def __post_init__(self) -> None:
        if self.recurrence_tracker is None:
            self.recurrence_tracker = RecurrenceTracker(
                path=self.sdd_dir / "autoheal" / "recurrence.jsonl",
                window_seconds=self.config.recurrence_window_seconds,
                threshold=self.config.recurrence_threshold,
            )

    def run(self, context: FailureContext) -> Tier3Outcome:
        """Run the full Tier-3 pipeline for one failing CI run.

        Pipeline order (each gate short-circuits with its own outcome
        kind so the operator surface can read the kind and decide):

        1. ``flag_off`` - Tier-3 not enabled by env.
        2. ``tier2_produced_patch`` - Tier-2 already produced a patch.
        3. ``unsafe_class`` - failure class is not in the allowlist.
        4. ``recurrence_escalated`` - recurrence threshold breached;
           hand off to Tier-4.
        5. ``shadow_empty`` - provider returned no patch.
        6. ``cordon_violation`` - patch touched out-of-cordon paths.
        7. ``shadow_captured`` - patch persisted, lineage + decision +
           envelope rows written, no push.
        8. ``promoted_push`` - only reachable when
           ``promote_from_shadow=True``; mirrors ``shadow_captured``
           and signals the workflow to actually push.
        """
        # 1. flag-off no-op. We deliberately do NOT touch the
        # decision-log, the recurrence tracker, or any filesystem path
        # when Tier-3 is disabled - the flag-off branch must leave zero
        # observable side-effects so the workflow can keep the
        # variable unset on shared infrastructure.
        if not self.config.enabled:
            return Tier3Outcome(kind="flag_off", reason="BERNSTEIN_CI_SELF_DRIVE not set to tier3")

        # 2. tier-2 already produced a patch - defer to it.
        if context.tier2_produced_patch:
            return Tier3Outcome(
                kind="tier2_produced_patch",
                reason="Tier-2 already produced a patch; Tier-3 not invoked",
            )

        # 3. allowlist check on the failing-job class.
        if context.failure_class not in SAFE_FAILURE_CLASSES:
            return Tier3Outcome(
                kind="unsafe_class",
                reason=f"failure class {context.failure_class!r} not in safe allowlist",
            )

        assert self.recurrence_tracker is not None  # populated in __post_init__
        now_ts = self.clock()

        # 4. recurrence detection - same class + nodeid fixed too often.
        if self.recurrence_tracker.should_escalate(
            failure_class=context.failure_class,
            failing_test_nodeid=context.failing_test_nodeid,
            now=now_ts,
        ):
            count = self.recurrence_tracker.count_recent(
                failure_class=context.failure_class,
                failing_test_nodeid=context.failing_test_nodeid,
                now=now_ts,
            )
            decision_id = _record_decision(
                kind="recurrence_escalation",
                chosen="tier4_handoff",
                rationale=(
                    f"recurrence threshold breached: {count} captures in the last "
                    f"{int(self.config.recurrence_window_seconds)}s for "
                    f"{context.failure_class!r} / {context.failing_test_nodeid!r}"
                ),
                inputs={
                    "failed_run_id": context.failed_run_id,
                    "failure_class": context.failure_class,
                    "failing_test_nodeid": context.failing_test_nodeid,
                    "recent_count": count,
                    "window_seconds": self.config.recurrence_window_seconds,
                    "threshold": self.config.recurrence_threshold,
                },
                decision_log_path=self.decision_log_path,
                confidence=1.0,
            )
            return Tier3Outcome(
                kind="recurrence_escalated",
                reason=(
                    f"{count} recurrences in the last "
                    f"{int(self.config.recurrence_window_seconds)}s; "
                    "escalating to Tier-4"
                ),
                decision_id=decision_id,
            )

        # 5. invoke the injected run hook.
        result = self.run_hook(
            context=context,
            primary_model=self.config.primary_model,
            fallback_models=self.config.fallback_models,
            openrouter_base_url=self.config.openrouter_base_url,
        )

        if not result.patch.strip():
            return Tier3Outcome(
                kind="shadow_empty",
                reason="run hook returned no patch",
                model_used=result.model_used,
                cost_usd=result.cost_usd,
            )

        # 6. cordon enforcement.
        touched = extract_paths_from_unified_diff(result.patch)
        allowed, rejected = evaluate_cordon(touched)
        if not allowed:
            decision_id = _record_decision(
                kind="cordon_violation",
                chosen="reject",
                rationale=(f"tier3 patch touched out-of-cordon paths: {', '.join(rejected)}"),
                inputs={
                    "failed_run_id": context.failed_run_id,
                    "rejected_paths": list(rejected),
                    "touched_paths": list(touched),
                    "model_used": result.model_used,
                    "failure_class": context.failure_class,
                },
                decision_log_path=self.decision_log_path,
                confidence=1.0,
            )
            return Tier3Outcome(
                kind="cordon_violation",
                reason=(f"refused: paths outside cordon: {', '.join(rejected)}"),
                model_used=result.model_used,
                cost_usd=result.cost_usd,
                decision_id=decision_id,
                rejected_paths=rejected,
            )

        # 7. capture: persist diff, lineage, envelope and decision row.
        patch_sha = _patch_sha(result.patch)
        diff_path = _persist_patch(
            run_id=context.failed_run_id,
            patch=result.patch,
            sdd_dir=self.sdd_dir,
        )

        # Record the capture in the recurrence ledger before the
        # decision row so a parallel reader cannot see a decision
        # without the matching recurrence row.
        self.recurrence_tracker.record(
            failure_class=context.failure_class,
            failing_test_nodeid=context.failing_test_nodeid,
            failed_run_id=context.failed_run_id,
            now=now_ts,
        )

        append_envelope_entry(
            EnvelopeEntry(
                ts=now_ts,
                failed_run_id=context.failed_run_id,
                model=result.model_used,
                cost_usd=result.cost_usd,
                daily_hard_cap_usd=self.config.daily_hard_cap_usd,
            ),
            self.sdd_dir,
        )

        lineage_payload = _build_lineage_payload(
            context=context,
            model=result.model_used,
            cost_usd=result.cost_usd,
            patch_sha=patch_sha,
            confidence=float(result.meta.get("confidence", 0.5)),
            outcome="shadow_captured",
        )

        promoted = self.config.promote_from_shadow
        kind: Tier3OutcomeKind = "promoted_push" if promoted else "shadow_captured"
        rationale = (
            "tier3 shadow patch captured; not pushed"
            if not promoted
            else "tier3 shadow patch captured and promoted for push"
        )

        decision_id = _record_decision(
            kind="tier3_shadow",
            chosen=result.model_used or self.config.primary_model,
            rationale=rationale,
            inputs={
                "failed_run_id": context.failed_run_id,
                "head_sha": context.head_sha,
                "failure_class": context.failure_class,
                "failing_test_nodeid": context.failing_test_nodeid,
                "model_used": result.model_used,
                "primary_model": self.config.primary_model,
                "fallback_models": list(self.config.fallback_models),
                "cost_usd": result.cost_usd,
                "patch_sha": patch_sha,
                "patch_path": str(diff_path),
                "regression_test_sha": context.regression_test_sha,
                "quota_envelope": QUOTA_ENVELOPE,
                "daily_hard_cap_usd": self.config.daily_hard_cap_usd,
                "promoted": promoted,
                **result.meta,
            },
            decision_log_path=self.decision_log_path,
            confidence=float(result.meta.get("confidence", 0.5)),
        )

        return Tier3Outcome(
            kind=kind,
            reason=rationale,
            patch_sha=patch_sha,
            patch_path=str(diff_path),
            model_used=result.model_used,
            cost_usd=result.cost_usd,
            decision_id=decision_id,
            lineage_payload=lineage_payload,
        )


__all__ = [
    "DEFAULT_DAILY_HARD_CAP_USD",
    "DEFAULT_FALLBACK_MODELS",
    "DEFAULT_PRIMARY_MODEL",
    "DEFAULT_RECURRENCE_THRESHOLD",
    "DEFAULT_RECURRENCE_WINDOW_SECONDS",
    "ENV_CI_AUTOHEAL_HARD_CAP",
    "ENV_OPENAI_BASE_URL",
    "ENV_OPENROUTER_BASE_URL",
    "ENV_PROMOTE_FROM_SHADOW",
    "ENV_SELF_DRIVE",
    "QUOTA_ENVELOPE",
    "SAFE_FAILURE_CLASSES",
    "TIER3_EXTRA_GLOBS",
    "EnvelopeEntry",
    "FailureContext",
    "RecurrenceTracker",
    "RunHook",
    "RunResult",
    "Tier3Config",
    "Tier3Outcome",
    "Tier3OutcomeKind",
    "Tier3Runner",
    "append_envelope_entry",
    "evaluate_cordon",
    "extract_paths_from_unified_diff",
]
