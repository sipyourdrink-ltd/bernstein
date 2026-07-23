"""Telemetry-grounded autofix for the Bernstein autofix daemon.

This module extends the existing autofix daemon with telemetry-driven
triggers. When an error or alert fires from a configured upstream
(any Sentry-protocol error tracker, GitHub Actions failure, Datadog,
Loki, custom JSONL logs), an adapter normalises the webhook payload into a typed
:class:`TelemetryEvent`. A small grounding retriever pulls a window of
recent log lines around the event fingerprint plus a short snapshot
of recent commits. The grounded prompt is handed to the existing
autofix dispatch ladder so the daemon can open a PR with the same
audit / cost-cap primitives it already uses for CI failures.

Scope: MVP.

* Sentry-protocol adapter is wired end-to-end.
* GitHub Actions failure adapter is wired end-to-end.
* Datadog, Loki, and custom JSONL adapters are stubbed - the protocol
  is in place and the dispatcher will record ``stubbed`` outcomes so
  follow-up PRs can land production wiring without rewriting the
  core. The webhook receivers accept their payloads but route them
  through the same dispatch path that drops them into the audit log
  for later replay.
* Grounding retrieval ships a single retriever: recent JSONL log
  lines (``RecentJsonlLogRetriever``). Trace and commit retrievers are
  follow-up tickets.

The module is intentionally side-effect free at import time; the
webhook routes (``src/bernstein/core/routes/telemetry_webhooks.py``)
own the FastAPI surface and inject a configured dispatcher.

The whole subsystem is feature-flagged off until the operator opts in
per-source via ``bernstein.yaml: autofix.telemetry_sources``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source identifiers
# ---------------------------------------------------------------------------

#: One of the supported telemetry sources. ``sentry`` covers Sentry SaaS
#: and any Sentry-compatible self-hosted backend - their webhook payloads
#: share the issue-alert shape this MVP parses.
TelemetrySourceId = Literal[
    "sentry",
    "gha_failure",
    "datadog",
    "loki",
    "custom_jsonl",
]


#: Terminal outcome strings emitted by :func:`dispatch_telemetry_event`.
TelemetryDispatchOutcome = Literal[
    "dispatched",  # event accepted, dispatch hook invoked, result captured.
    "stubbed",  # adapter parsed the event but full wiring deferred.
    "skipped",  # source disabled or event filtered out.
    "cost_capped",  # per-event cost cap refused the dispatch.
    "errored",  # dispatch hook raised; caught for audit hygiene.
]


# ---------------------------------------------------------------------------
# Default per-event cost cap
# ---------------------------------------------------------------------------

#: Default per-event USD cost cap. Mirrors the ladder's Rung-1 cap so
#: a runaway telemetry source cannot burn more than one Rung-1
#: equivalent per event without operator opt-in.
DEFAULT_PER_EVENT_COST_CAP_USD: Final[float] = 0.20


# ---------------------------------------------------------------------------
# TelemetryEvent and config dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TelemetryEvent:
    """Normalised telemetry event the dispatcher consumes.

    Each adapter is responsible for filling whichever fields it can
    extract from the upstream payload. Anything truly source-specific
    survives in ``raw`` so a downstream agent can read it on demand.

    Attributes:
        source: One of :data:`TelemetrySourceId`. Identifies which
            adapter produced this event so the dispatcher can route to
            the right cost-cap and the audit trail can replay.
        fingerprint: Stable identifier the operator can group on
            (e.g. Sentry issue id, GHA run id, log signature). The
            grounding retriever uses this as a query key.
        title: Short human-readable summary suitable for a goal line.
        message: Longer error message / first stack frame; capped at
            roughly 4 KiB by :func:`build_grounded_goal`.
        timestamp: Event time as Unix seconds. Defaults to "now" so
            callers do not have to plumb a clock through.
        repo: ``owner/name`` slug when the event has a natural git
            home. Empty string when the source is repo-agnostic.
        environment: Deployment environment (``"prod"``, ``"staging"``,
            ...). Useful for cost-cap routing.
        url: Permalink back to the upstream event (Sentry issue URL,
            GitHub run URL, ...).
        raw: Verbatim source payload, attached to the audit event and
            available to grounding retrievers as needed.
    """

    source: TelemetrySourceId
    fingerprint: str
    title: str = ""
    message: str = ""
    timestamp: float = field(default_factory=time.time)
    repo: str = ""
    environment: str = ""
    url: str = ""
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TelemetrySourceConfig:
    """Per-source operator configuration loaded from ``bernstein.yaml``.

    Attributes:
        source: Source identifier.
        enabled: When ``False`` the receiver still accepts the
            webhook (so the upstream does not see a 404) but the
            dispatcher records ``skipped`` and does not spawn anything.
        endpoint: Public endpoint path, e.g. ``/webhooks/telemetry/sentry/``.
            Operator-visible; the router uses this as its mount path.
        secret_env: Environment variable name holding the HMAC secret
            shared with the upstream. Empty string disables signature
            verification (test-only).
        fingerprint_path: Dotted path into the raw payload used by
            adapters that need to override the default fingerprint
            extraction (e.g. ``"event.issue_id"``). Empty string keeps
            the adapter's default extractor.
        cost_cap_usd: Per-event cost cap. Defaults to
            :data:`DEFAULT_PER_EVENT_COST_CAP_USD`.
    """

    source: TelemetrySourceId
    enabled: bool = False
    endpoint: str = ""
    secret_env: str = ""
    fingerprint_path: str = ""
    cost_cap_usd: float = DEFAULT_PER_EVENT_COST_CAP_USD


@dataclass(frozen=True)
class TelemetrySettings:
    """Effective top-level telemetry-grounded autofix settings.

    Attributes:
        sources: Tuple of per-source configurations in declaration
            order. The daemon iterates this list on every webhook to
            find the matching source.
    """

    sources: tuple[TelemetrySourceConfig, ...] = field(default_factory=tuple)

    def for_source(self, source: TelemetrySourceId) -> TelemetrySourceConfig | None:
        """Return the configured entry for ``source`` or ``None``."""
        for entry in self.sources:
            if entry.source == source:
                return entry
        return None


def load_telemetry_settings(yaml_path: Path | None = None) -> TelemetrySettings:
    """Load ``autofix.telemetry_sources`` from ``bernstein.yaml``.

    The loader is lenient: missing file, missing block, malformed
    types all fall back to the empty (all-disabled) default.

    Args:
        yaml_path: Optional explicit path. Defaults to
            ``./bernstein.yaml`` from the current working directory.

    Returns:
        Populated :class:`TelemetrySettings`.
    """
    target = yaml_path if yaml_path is not None else Path.cwd() / "bernstein.yaml"
    if not target.exists():
        return TelemetrySettings()
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - PyYAML is a runtime dep
        return TelemetrySettings()
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.exception("telemetry_grounded: failed to parse %s", target)
        return TelemetrySettings()
    if not isinstance(raw, dict):
        return TelemetrySettings()
    autofix_block = raw.get("autofix")
    if not isinstance(autofix_block, dict):
        return TelemetrySettings()
    sources_raw = autofix_block.get("telemetry_sources")
    if not isinstance(sources_raw, list):
        return TelemetrySettings()

    parsed: list[TelemetrySourceConfig] = []
    for entry in sources_raw:
        if not isinstance(entry, dict):
            continue
        source_raw = str(entry.get("source", "")).strip()
        if source_raw not in {"sentry", "gha_failure", "datadog", "loki", "custom_jsonl"}:
            continue
        try:
            cap = float(entry.get("cost_cap_usd", DEFAULT_PER_EVENT_COST_CAP_USD))
        except (TypeError, ValueError):
            cap = DEFAULT_PER_EVENT_COST_CAP_USD
        if cap < 0:
            cap = 0.0
        parsed.append(
            TelemetrySourceConfig(
                source=source_raw,  # type: ignore[arg-type]
                enabled=bool(entry.get("enabled", False)),
                endpoint=str(entry.get("endpoint", "")).strip(),
                secret_env=str(entry.get("secret_env", "")).strip(),
                fingerprint_path=str(entry.get("fingerprint_path", "")).strip(),
                cost_cap_usd=cap,
            )
        )
    return TelemetrySettings(sources=tuple(parsed))


# ---------------------------------------------------------------------------
# TelemetrySource protocol + adapters
# ---------------------------------------------------------------------------


class TelemetrySource(Protocol):
    """Protocol every telemetry adapter implements.

    Adapters are pure functions in spirit - they take a raw webhook
    payload and produce a :class:`TelemetryEvent`. They never reach
    out to the network or touch disk; that is the dispatcher's job.
    """

    source: TelemetrySourceId

    def parse(self, payload: dict[str, object]) -> TelemetryEvent:
        """Normalise the raw payload into a :class:`TelemetryEvent`."""
        ...


def _coerce_str(value: object, default: str = "") -> str:
    """Return ``value`` as a string or ``default`` for non-string types.

    A small helper so adapters do not have to repeat ``isinstance``
    checks for every optional field.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return default


def _dig(payload: dict[str, object], dotted_path: str) -> object | None:
    """Walk a dotted path through nested dicts; return ``None`` on miss."""
    if not dotted_path:
        return None
    cursor: object = payload
    for segment in dotted_path.split("."):
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(segment)
        if cursor is None:
            return None
    return cursor


class SentrySource:
    """Adapter for the Sentry-protocol issue-alert webhook.

    Sentry SaaS and any Sentry-compatible self-hosted backend emit the
    same envelope:

    .. code-block:: json

        {
          "action": "created",
          "data": {
            "issue": {
              "id": "1234",
              "title": "ZeroDivisionError: ...",
              "culprit": "module.func in src/x.py",
              "shortId": "PROJ-1A",
              "permalink": "https://sentry.io/.../issues/1234/",
              "metadata": {"value": "..."}
            }
          },
          "installation": {"uuid": "..."},
          "project_slug": "backend"
        }

    We coerce ``data.issue.id`` (or the operator-supplied
    ``fingerprint_path``) into the fingerprint so repeated alerts for
    the same issue map to the same dispatched goal.
    """

    source: TelemetrySourceId = "sentry"

    def __init__(self, fingerprint_path: str = "") -> None:
        self._fingerprint_path = fingerprint_path

    def parse(self, payload: dict[str, object]) -> TelemetryEvent:
        issue_block = payload.get("data")
        issue: dict[str, object] = {}
        if isinstance(issue_block, dict):
            inner = issue_block.get("issue")
            if isinstance(inner, dict):
                issue = inner  # type: ignore[assignment]

        fingerprint: str = ""
        if self._fingerprint_path:
            dug = _dig(payload, self._fingerprint_path)
            fingerprint = _coerce_str(dug)
        if not fingerprint:
            fingerprint = _coerce_str(issue.get("id"))
        if not fingerprint:
            # Hash the title as a last-resort grouping key so audit
            # consumers never see an empty fingerprint.
            fingerprint = "sentry:" + hashlib.sha256(_coerce_str(issue.get("title")).encode("utf-8")).hexdigest()[:16]

        title = _coerce_str(issue.get("title")) or _coerce_str(issue.get("shortId"))
        culprit = _coerce_str(issue.get("culprit"))

        metadata = issue.get("metadata")
        value = ""
        if isinstance(metadata, dict):
            value = _coerce_str(metadata.get("value"))
        message_parts = [part for part in (title, culprit, value) if part]
        message = "\n".join(message_parts)

        environment = _coerce_str(payload.get("project_slug"))

        return TelemetryEvent(
            source=self.source,
            fingerprint=fingerprint,
            title=title or "sentry alert",
            message=message,
            repo="",
            environment=environment,
            url=_coerce_str(issue.get("permalink")) or _coerce_str(issue.get("url")),
            raw=payload,
        )


class GhaFailureSource:
    """Adapter for the GitHub Actions ``workflow_run`` failure webhook.

    The receiver is the same as the existing CI-failure path but the
    telemetry-grounded flow uses the normalised :class:`TelemetryEvent`
    shape so a single grounded goal is built no matter which upstream
    fired. The adapter expects the GitHub ``workflow_run`` envelope:

    .. code-block:: json

        {
          "action": "completed",
          "workflow_run": {
            "id": 123,
            "conclusion": "failure",
            "head_sha": "abc...",
            "head_branch": "feat/x",
            "name": "ci",
            "html_url": "https://github.com/.../actions/runs/123"
          },
          "repository": {"full_name": "owner/name"}
        }

    Non-failure conclusions produce a "skip" fingerprint of ``""`` so
    the dispatcher records ``skipped`` without spawning a run.
    """

    source: TelemetrySourceId = "gha_failure"

    def parse(self, payload: dict[str, object]) -> TelemetryEvent:
        run_block = payload.get("workflow_run")
        run: dict[str, object] = run_block if isinstance(run_block, dict) else {}
        repo_block = payload.get("repository")
        repo_full_name = ""
        if isinstance(repo_block, dict):
            repo_full_name = _coerce_str(repo_block.get("full_name"))

        conclusion = _coerce_str(run.get("conclusion")).lower()
        if conclusion and conclusion not in {"failure", "timed_out", "action_required"}:
            # The receiver still accepts the request so GitHub keeps
            # delivering, but the dispatcher will skip it.
            return TelemetryEvent(
                source=self.source,
                fingerprint="",
                title=f"workflow_run conclusion={conclusion!r}",
                repo=repo_full_name,
                raw=payload,
            )

        run_id = _coerce_str(run.get("id"))
        fingerprint = f"{repo_full_name}#run-{run_id}" if run_id else ""
        title = _coerce_str(run.get("name")) or "github actions failure"
        head_sha = _coerce_str(run.get("head_sha"))
        head_branch = _coerce_str(run.get("head_branch"))
        message_parts = [
            f"workflow: {title}",
            f"branch: {head_branch}" if head_branch else "",
            f"head_sha: {head_sha}" if head_sha else "",
        ]
        message = "\n".join(part for part in message_parts if part)

        return TelemetryEvent(
            source=self.source,
            fingerprint=fingerprint,
            title=title,
            message=message,
            repo=repo_full_name,
            environment="ci",
            url=_coerce_str(run.get("html_url")),
            raw=payload,
        )


class StubSource:
    """Adapter used for the follow-up sources.

    The Datadog / Loki / custom-JSONL receivers all share the same
    fallback shape in MVP: they normalise the payload's top-level
    ``fingerprint``, ``message``, and ``url`` (when present) and
    forward to the dispatcher, which records ``stubbed`` so the
    operator can audit-replay the payload without a real spawn.
    """

    def __init__(self, source: TelemetrySourceId) -> None:
        self.source: TelemetrySourceId = source

    def parse(self, payload: dict[str, object]) -> TelemetryEvent:
        return TelemetryEvent(
            source=self.source,
            fingerprint=_coerce_str(payload.get("fingerprint")),
            title=_coerce_str(payload.get("title")) or f"{self.source} stub",
            message=_coerce_str(payload.get("message")),
            repo=_coerce_str(payload.get("repo")),
            environment=_coerce_str(payload.get("environment")),
            url=_coerce_str(payload.get("url")),
            raw=payload,
        )


def build_default_sources(settings: TelemetrySettings | None = None) -> dict[TelemetrySourceId, TelemetrySource]:
    """Return the built-in adapter map.

    The returned mapping is keyed by source id so the webhook router
    can look up the adapter that matches the incoming endpoint with a
    constant-time dispatch.

    Args:
        settings: Optional settings. When supplied, the Sentry adapter
            honours the operator-supplied ``fingerprint_path``.
    """
    sentry_path = ""
    if settings is not None:
        sentry_cfg = settings.for_source("sentry")
        if sentry_cfg is not None:
            sentry_path = sentry_cfg.fingerprint_path

    return {
        "sentry": SentrySource(fingerprint_path=sentry_path),
        "gha_failure": GhaFailureSource(),
        "datadog": StubSource("datadog"),
        "loki": StubSource("loki"),
        "custom_jsonl": StubSource("custom_jsonl"),
    }


# ---------------------------------------------------------------------------
# Webhook signature verification (shared across sources)
# ---------------------------------------------------------------------------


def verify_webhook_signature(
    *,
    body: bytes,
    signature_header: str,
    secret: str,
    algorithm: str = "sha256",
) -> bool:
    """Constant-time HMAC verification for telemetry webhooks.

    Sentry-protocol trackers and Datadog all sign requests with an
    HMAC-<digest> hex string in a custom header. The header may or
    may not be prefixed with ``"sha256="``; both forms are accepted.

    Args:
        body: Raw request body.
        signature_header: Value of the upstream's signature header.
        secret: Shared secret loaded from ``secret_env``.
        algorithm: Hash algorithm; defaults to ``"sha256"``.

    Returns:
        ``True`` when the signature matches. Empty secret or empty
        header always returns ``False`` - fail-closed.
    """
    if not secret or not signature_header:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, algorithm).hexdigest()
    received = signature_header.strip()
    if received.lower().startswith(f"{algorithm}="):
        received = received.split("=", 1)[1]
    return hmac.compare_digest(expected, received)


# ---------------------------------------------------------------------------
# Grounding retriever
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GroundingContext:
    """The grounded context handed to the dispatch goal.

    Attributes:
        log_excerpts: Recent log lines around the event fingerprint.
            Each entry is a single line; the order is most-recent-last
            so it reads top-to-bottom when concatenated.
        commits: Recent git commits as ``(sha, subject)`` tuples. Empty
            in MVP - the commit retriever is a follow-up.
        related_tests: Names of recent test failures linked to the
            fingerprint. Empty in MVP - the test retriever is a
            follow-up.
        retriever_id: Identifier of the retriever that produced this
            context. Recorded in the audit trail for replay.
    """

    log_excerpts: tuple[str, ...] = field(default_factory=tuple)
    commits: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    related_tests: tuple[str, ...] = field(default_factory=tuple)
    retriever_id: str = ""


class GroundingRetriever(Protocol):
    """Protocol every grounding retriever implements.

    The retriever is invoked synchronously inside the dispatch loop so
    implementations must be fast and bounded - this MVP caps the
    returned log lines at ``DEFAULT_LOG_LINES`` (50).
    """

    retriever_id: str

    def retrieve(self, event: TelemetryEvent) -> GroundingContext:
        """Return grounding for ``event`` or an empty context on miss."""
        ...


#: Default number of log lines a retriever should return. Matches the
#: rough size of a pytest traceback and stays under the autofix log
#: byte budget.
DEFAULT_LOG_LINES: Final[int] = 50


class RecentJsonlLogRetriever:
    """Tail a JSONL log file and return lines matching the fingerprint.

    The retriever scans the tail of a JSONL log (one event per line)
    and returns the most-recent ``max_lines`` entries whose serialised
    form contains the event fingerprint. This is intentionally simple
    so it works against any JSONL log format - Bernstein's own traces,
    a custom ``app.jsonl``, a Loki tail, ...

    Args:
        log_path: Path to the JSONL log file. The retriever returns an
            empty :class:`GroundingContext` when the file does not
            exist or is unreadable.
        max_lines: Cap on returned lines. Defaults to
            :data:`DEFAULT_LOG_LINES`.
        scan_bytes: Maximum bytes to read from the tail. Larger files
            are read from the end. Defaults to 256 KiB which fits ~1k
            log lines on a typical workload.
    """

    retriever_id: str = "recent_jsonl_log"

    def __init__(
        self,
        log_path: Path,
        *,
        max_lines: int = DEFAULT_LOG_LINES,
        scan_bytes: int = 256 * 1024,
    ) -> None:
        self._log_path = log_path
        self._max_lines = max(1, max_lines)
        self._scan_bytes = max(1024, scan_bytes)

    def retrieve(self, event: TelemetryEvent) -> GroundingContext:
        if not event.fingerprint:
            return GroundingContext(retriever_id=self.retriever_id)
        try:
            data = self._read_tail()
        except OSError:
            logger.debug("retriever: cannot read %s", self._log_path)
            return GroundingContext(retriever_id=self.retriever_id)
        if not data:
            return GroundingContext(retriever_id=self.retriever_id)
        matches = _scan_jsonl_for_fingerprint(
            data,
            fingerprint=event.fingerprint,
            max_lines=self._max_lines,
        )
        return GroundingContext(
            log_excerpts=tuple(matches),
            retriever_id=self.retriever_id,
        )

    def _read_tail(self) -> bytes:
        if not self._log_path.exists():
            return b""
        size = self._log_path.stat().st_size
        if size <= self._scan_bytes:
            return self._log_path.read_bytes()
        with self._log_path.open("rb") as fh:
            fh.seek(size - self._scan_bytes)
            # Drop the leading partial line so JSONL parsing is clean.
            _ = fh.readline()
            return fh.read()


def _scan_jsonl_for_fingerprint(
    data: bytes,
    *,
    fingerprint: str,
    max_lines: int,
) -> list[str]:
    """Return the last ``max_lines`` JSONL entries mentioning ``fingerprint``.

    The match is a simple substring scan on the raw line bytes so it
    works against any JSONL shape. Lines that are not valid UTF-8 are
    skipped.
    """
    needle = fingerprint.encode("utf-8")
    matches: list[str] = []
    for raw_line in data.splitlines():
        if needle not in raw_line:
            continue
        try:
            text = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            continue
        matches.append(text)
    if len(matches) <= max_lines:
        return matches
    return matches[-max_lines:]


# ---------------------------------------------------------------------------
# Grounded goal builder
# ---------------------------------------------------------------------------


#: Hard cap on the goal-body size handed to the spawned agent. The
#: autofix dispatcher accepts large goals but huge prompts inflate
#: model spend; 8 KiB matches the existing log byte budget headroom.
MAX_GOAL_BODY_BYTES: Final[int] = 8 * 1024


def build_grounded_goal(event: TelemetryEvent, context: GroundingContext) -> str:
    """Format ``event`` + ``context`` into a deterministic agent goal.

    The output is deterministic given identical inputs so audit
    replay reproduces the goal byte-for-byte.

    Args:
        event: The normalised telemetry event.
        context: The grounding context returned by the retriever.

    Returns:
        Multi-line goal string suitable for ``bernstein run``.
    """
    log_block = "\n".join(context.log_excerpts) if context.log_excerpts else "(no log lines captured)"
    if len(log_block.encode("utf-8")) > MAX_GOAL_BODY_BYTES:
        log_block = log_block.encode("utf-8")[:MAX_GOAL_BODY_BYTES].decode("utf-8", errors="ignore")

    commits_block = (
        "\n".join(f"{sha[:12]} {subject}" for sha, subject in context.commits)
        if context.commits
        else "(no commit context)"
    )
    related_block = ", ".join(context.related_tests) if context.related_tests else "(none)"

    title = event.title or f"{event.source} event"
    url_line = f"\nUpstream: {event.url}\n" if event.url else "\n"

    return (
        f"fix({event.source}): repair telemetry-grounded issue {event.fingerprint}\n"
        f"\n"
        f"Title: {title}\n"
        f"Environment: {event.environment or '(unspecified)'}\n"
        f"Source: {event.source} (retriever={context.retriever_id or 'none'})"
        f"{url_line}"
        f"\n"
        f"Message:\n```\n{event.message or '(no message)'}\n```\n"
        f"\n"
        f"Recent log lines around fingerprint:\n```\n{log_block}\n```\n"
        f"\n"
        f"Recent commits:\n```\n{commits_block}\n```\n"
        f"\n"
        f"Related test failures: {related_block}\n"
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class GroundedDispatchHook(Protocol):
    """Spawn a Bernstein run for a grounded telemetry event.

    Tests inject a stub. Production wiring lives in the autofix
    daemon, which forwards to the same ``bernstein run`` machinery the
    CI-failure dispatcher uses.
    """

    def __call__(
        self,
        *,
        goal: str,
        event: TelemetryEvent,
        context: GroundingContext,
        cost_cap_usd: float,
    ) -> GroundedDispatchResult: ...


@dataclass(frozen=True)
class GroundedDispatchResult:
    """Result returned by the dispatch hook.

    Attributes:
        success: ``True`` when the spawn produced a commit or PR.
        commit_sha: Optional commit SHA of the produced fix.
        cost_usd: USD spend reported by the cost tracker.
        message: Human-readable summary stored in the audit trail.
    """

    success: bool
    commit_sha: str = ""
    cost_usd: float = 0.0
    message: str = ""


@dataclass(frozen=True)
class TelemetryDispatchRecord:
    """Audit-trail data captured by :func:`dispatch_telemetry_event`.

    Attributes:
        outcome: Terminal status; one of :data:`TelemetryDispatchOutcome`.
        source: Source id that produced the event.
        fingerprint: Event fingerprint.
        retriever_id: Retriever id that produced the grounding.
        cost_usd: USD spend reported by the dispatch hook.
        commit_sha: Optional commit SHA produced by the dispatch.
        reason: Human-readable explanation suitable for an audit
            trailer / CLI status line.
        log_lines: How many grounding log lines were captured.
    """

    outcome: TelemetryDispatchOutcome
    source: TelemetrySourceId
    fingerprint: str
    retriever_id: str = ""
    cost_usd: float = 0.0
    commit_sha: str = ""
    reason: str = ""
    log_lines: int = 0


class AuditEmitter(Protocol):
    """Minimal subset of :class:`AuditLog` the dispatcher needs."""

    def log(
        self,
        event_type: str,
        actor: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, object] | None = None,
    ) -> object: ...


_STUBBED_SOURCES: Final[frozenset[TelemetrySourceId]] = frozenset(
    {"datadog", "loki", "custom_jsonl"},
)


def dispatch_telemetry_event(
    event: TelemetryEvent,
    *,
    settings: TelemetrySettings,
    retriever: GroundingRetriever,
    dispatch_hook: GroundedDispatchHook,
    audit: AuditEmitter | None = None,
) -> TelemetryDispatchRecord:
    """Drive a single telemetry-grounded autofix attempt.

    The function is side-effect free except for the audit-log emit and
    the dispatch hook invocation. It is the single integration point
    the webhook receivers and the daemon both call.

    Args:
        event: The normalised telemetry event.
        settings: Effective top-level settings.
        retriever: Grounding retriever to consult for context.
        dispatch_hook: Spawn callable. Tests inject a recorder.
        audit: Optional :class:`AuditEmitter`. When provided every
            terminal outcome emits an audit event.

    Returns:
        A :class:`TelemetryDispatchRecord` capturing the terminal
        outcome and the audit identifiers.
    """
    source_cfg = settings.for_source(event.source)
    if source_cfg is None or not source_cfg.enabled:
        record = TelemetryDispatchRecord(
            outcome="skipped",
            source=event.source,
            fingerprint=event.fingerprint,
            reason=(f"source {event.source!r} is not enabled in autofix.telemetry_sources"),
        )
        _emit_audit(audit, event=event, record=record)
        return record

    if not event.fingerprint:
        record = TelemetryDispatchRecord(
            outcome="skipped",
            source=event.source,
            fingerprint="",
            reason="event has no fingerprint; nothing to ground on",
        )
        _emit_audit(audit, event=event, record=record)
        return record

    try:
        context = retriever.retrieve(event)
    except Exception:  # pragma: no cover - guarded for daemon hygiene
        logger.exception(
            "telemetry_grounded: retriever raised for source=%s fp=%s",
            event.source,
            event.fingerprint,
        )
        context = GroundingContext(retriever_id=retriever.retriever_id)

    # The follow-up sources record a deferred outcome before any
    # dispatch so the operator sees the would-be escalation in the
    # audit trail without spending model budget.
    if event.source in _STUBBED_SOURCES:
        record = TelemetryDispatchRecord(
            outcome="stubbed",
            source=event.source,
            fingerprint=event.fingerprint,
            retriever_id=context.retriever_id,
            reason=(f"{event.source} adapter parsed event; full dispatch wiring deferred to a follow-up PR."),
            log_lines=len(context.log_excerpts),
        )
        _emit_audit(audit, event=event, record=record)
        return record

    if source_cfg.cost_cap_usd <= 0:
        # Zero cap means the operator wired the source but does not
        # want to spend; we record a cost-cap outcome without firing.
        record = TelemetryDispatchRecord(
            outcome="cost_capped",
            source=event.source,
            fingerprint=event.fingerprint,
            retriever_id=context.retriever_id,
            cost_usd=0.0,
            reason="per-event cost cap is zero; refusing to spawn.",
            log_lines=len(context.log_excerpts),
        )
        _emit_audit(audit, event=event, record=record)
        return record

    goal = build_grounded_goal(event, context)

    try:
        result = dispatch_hook(
            goal=goal,
            event=event,
            context=context,
            cost_cap_usd=source_cfg.cost_cap_usd,
        )
    except Exception as exc:
        logger.exception(
            "telemetry_grounded: dispatch hook raised for source=%s fp=%s",
            event.source,
            event.fingerprint,
        )
        record = TelemetryDispatchRecord(
            outcome="errored",
            source=event.source,
            fingerprint=event.fingerprint,
            retriever_id=context.retriever_id,
            reason=f"dispatch hook raised: {exc}",
            log_lines=len(context.log_excerpts),
        )
        _emit_audit(audit, event=event, record=record)
        return record

    if source_cfg.cost_cap_usd > 0 and result.cost_usd > source_cfg.cost_cap_usd:
        record = TelemetryDispatchRecord(
            outcome="cost_capped",
            source=event.source,
            fingerprint=event.fingerprint,
            retriever_id=context.retriever_id,
            cost_usd=result.cost_usd,
            commit_sha=result.commit_sha,
            reason=(
                f"cost ${result.cost_usd:.2f} exceeded cap ${source_cfg.cost_cap_usd:.2f};"
                " attempt accepted but flagged."
            ),
            log_lines=len(context.log_excerpts),
        )
        _emit_audit(audit, event=event, record=record)
        return record

    record = TelemetryDispatchRecord(
        outcome="dispatched",
        source=event.source,
        fingerprint=event.fingerprint,
        retriever_id=context.retriever_id,
        cost_usd=result.cost_usd,
        commit_sha=result.commit_sha,
        reason=result.message or ("dispatch succeeded" if result.success else "dispatch returned no commit"),
        log_lines=len(context.log_excerpts),
    )
    _emit_audit(audit, event=event, record=record)
    return record


def _emit_audit(
    audit: AuditEmitter | None,
    *,
    event: TelemetryEvent,
    record: TelemetryDispatchRecord,
) -> None:
    """Best-effort audit-trail emit; swallows exceptions for daemon hygiene."""
    if audit is None:
        return
    try:
        audit.log(
            event_type="autofix.telemetry.dispatch",
            actor="autofix-telemetry",
            resource_type="telemetry_event",
            resource_id=f"{record.source}:{record.fingerprint}" if record.fingerprint else record.source,
            details={
                "producer": "autofix-telemetry",
                "outcome": record.outcome,
                "source": record.source,
                "fingerprint": record.fingerprint,
                "retriever_id": record.retriever_id,
                "cost_usd": round(record.cost_usd, 6),
                "commit_sha": record.commit_sha,
                "reason": record.reason,
                "log_lines": record.log_lines,
                "event_url": event.url,
                "event_repo": event.repo,
                "event_environment": event.environment,
            },
        )
    except Exception:  # pragma: no cover - guarded for daemon hygiene
        logger.exception(
            "telemetry_grounded: audit emission failed for source=%s fp=%s",
            event.source,
            event.fingerprint,
        )


# ---------------------------------------------------------------------------
# Utilities for the FastAPI receiver
# ---------------------------------------------------------------------------


def parse_json_payload(body: bytes) -> dict[str, object]:
    """Parse a JSON body into a dict; raise ``ValueError`` on bad input.

    The webhook receivers all accept JSON. A non-dict top-level (array,
    bare string) is treated as an error so adapter code never has to
    type-check the input.
    """
    try:
        decoded = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"webhook body is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ValueError("webhook body must be a JSON object at the top level")
    return decoded


def iter_enabled_sources(settings: TelemetrySettings) -> Iterable[TelemetrySourceConfig]:
    """Yield enabled source configs in declaration order.

    Convenience for callers (CLI / dashboards) that want to render the
    operator-visible telemetry inventory.
    """
    return (cfg for cfg in settings.sources if cfg.enabled)


__all__ = [
    "DEFAULT_LOG_LINES",
    "DEFAULT_PER_EVENT_COST_CAP_USD",
    "MAX_GOAL_BODY_BYTES",
    "AuditEmitter",
    "GhaFailureSource",
    "GroundedDispatchHook",
    "GroundedDispatchResult",
    "GroundingContext",
    "GroundingRetriever",
    "RecentJsonlLogRetriever",
    "SentrySource",
    "StubSource",
    "TelemetryDispatchOutcome",
    "TelemetryDispatchRecord",
    "TelemetryEvent",
    "TelemetrySettings",
    "TelemetrySource",
    "TelemetrySourceConfig",
    "TelemetrySourceId",
    "build_default_sources",
    "build_grounded_goal",
    "dispatch_telemetry_event",
    "iter_enabled_sources",
    "load_telemetry_settings",
    "parse_json_payload",
    "verify_webhook_signature",
]
