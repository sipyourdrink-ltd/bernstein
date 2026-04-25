"""Bernstein autofix daemon — auto-repair CI failures on Bernstein PRs.

The autofix package watches a configured set of GitHub repositories for
failed checks on pull requests opened by a Bernstein session, claims
ownership via the ``bernstein-session-id`` trailer added by
``bernstein pr``, classifies each failure into a routing bucket, and
dispatches a fresh deterministic Bernstein run with a goal scoped to
the failing logs.

The package is intentionally split across several modules so each
concern can be unit-tested in isolation:

* :mod:`bernstein.core.autofix.config` — typed reader for the
  ``~/.config/bernstein/autofix.toml`` configuration file.
* :mod:`bernstein.core.autofix.classifier` — pure-function classifier
  that maps a failing-log blob to ``flaky``, ``config`` or ``security``
  and chooses a bandit arm (``sonnet`` / ``haiku`` / ``opus``).
* :mod:`bernstein.core.autofix.gh_logs` — wraps ``gh run view
  --log-failed`` and applies the configured byte budget.
* :mod:`bernstein.core.autofix.ownership` — reads PR metadata,
  validates the ``bernstein-session-id`` trailer, and enforces the
  ``bernstein-autofix`` label gate.
* :mod:`bernstein.core.autofix.dispatcher` — orchestrates a single
  attempt: cost-cap check, classifier lookup, audit-chain open, goal
  synthesis, dispatch, audit-chain close.
* :mod:`bernstein.core.autofix.metrics` — Prometheus counters that
  surface attempts and spend per repo.
* :mod:`bernstein.core.autofix.daemon` — process supervisor that
  exposes ``start``, ``stop``, ``status`` and ``attach`` semantics.
"""

from __future__ import annotations

from bernstein.core.autofix.classifier import (
    Classification,
    classify_failure,
)
from bernstein.core.autofix.config import (
    AutofixConfig,
    RepoConfig,
    load_config,
)
from bernstein.core.autofix.dispatcher import (
    AttemptOutcome,
    AttemptRecord,
    Dispatcher,
)
from bernstein.core.autofix.gh_logs import (
    LogExtraction,
    extract_failed_log,
)
from bernstein.core.autofix.metrics import (
    autofix_attempts_total,
    autofix_cost_usd_total,
)
from bernstein.core.autofix.ownership import (
    OwnershipDecision,
    PullRequestMetadata,
    decide_ownership,
)

__all__ = [
    "AttemptOutcome",
    "AttemptRecord",
    "AutofixConfig",
    "Classification",
    "Dispatcher",
    "LogExtraction",
    "OwnershipDecision",
    "PullRequestMetadata",
    "RepoConfig",
    "autofix_attempts_total",
    "autofix_cost_usd_total",
    "classify_failure",
    "decide_ownership",
    "extract_failed_log",
    "load_config",
]
