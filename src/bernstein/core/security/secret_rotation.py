"""Rotate a secret that lives on an external target: mint, store, apply.

Operator pain solved
====================

:mod:`bernstein.core.security.secrets_broker` mints and revokes short-lived
tokens against a read-only backend, and
:mod:`bernstein.core.security.key_rotation` rotates Bernstein's *own* outbound
API keys. Neither writes a new credential onto an external target -- a deploy
account, a CI runner, a partner API -- and neither keeps a bounded history of
what the credential used to be. Rotating such a secret by hand is where
operators lock themselves out: the new value reaches the target, the old one is
gone, and nobody can produce either again.

Order of operations
===================

``mint -> store -> apply`` is the only order that cannot strand an operator::

    rotator = SecretRotator(broker=broker, store=store, journal=journal)
    run = rotator.rotate(
        target=target, principal="svc-deploy",
        secret_name="DEPLOY_KEY", task_id="t-42",
    )

* **mint** asks the broker for new material.
* **store** records it in a bounded per-``(target, principal)`` history *before*
  anything on the target changes.
* **apply** writes it to the target, then leaves a dated receipt there.

A failure at apply raises :class:`RotationError` carrying the
:class:`RotationRun` that says how far the run got. Because store already
completed, both the previous version and the un-applied new one stay
retrievable, and the target still holds the credential it had.

``principal``
=============

``principal`` is the identity the credential authenticates *as on the target*
-- a service account, a deploy user, an operator login the target itself
recognises. It is deliberately not a Bernstein ``task_id``: a task id is
per-run, so a ``(target, task_id)`` history would hold exactly one version and
could never answer "what was this credential before". It is also not a
Bernstein agent identity: one agent may rotate credentials for many principals
on the same target.

Receipts
========

Each successful rotation writes a :class:`RotationReceipt` on the target,
carrying the version id, a fingerprint of the new material (never the material)
and the rotation timestamp. :func:`audit_rotation_receipts` reads those
receipts back and reports a single finding,
``secret_rotation_receipt_not_current``, for a principal whose receipt is
missing *or* older than the policy window -- from the operator's chair, "never
rotated" and "stopped being rotated" are the same fact.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets as _secrets
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

from bernstein.core.security.secrets_broker import (
    SecretsBroker,
    register_secret_for_redaction,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "RotationAuditFinding",
    "RotationError",
    "RotationJournal",
    "RotationReceipt",
    "RotationRun",
    "RotationStep",
    "RotationTarget",
    "SecretRotator",
    "SecretVersion",
    "SecretVersionStore",
    "audit_rotation_receipts",
]

_DEFAULT_MAX_VERSIONS = 3
_MIN_MAX_VERSIONS = 2


def _fingerprint(value: str) -> str:
    """Return a short, non-reversible fingerprint of secret material."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _new_version_id(now: float) -> str:
    """Return a sortable, collision-resistant version id."""
    return f"v-{int(now)}-{_secrets.token_hex(4)}"


class RotationError(Exception):
    """Raised when a rotation run fails.

    Attributes:
        run: The :class:`RotationRun` describing how far the run got, or
            ``None`` when the failure happened before a run could be shaped.
    """

    def __init__(self, message: str, *, run: RotationRun | None = None) -> None:
        super().__init__(message)
        self.run = run


# ---------------------------------------------------------------------------
# Version history (the "store" step)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecretVersion:
    """One stored version of a target secret.

    ``value`` is excluded from ``repr`` so a stored version cannot leak the
    credential into a traceback, a log line, or a test failure message.
    """

    target: str
    principal: str
    version_id: str
    created_at: float
    fingerprint: str
    value: str = field(repr=False)


class SecretVersionStore:
    """Bounded, in-process version history keyed by ``(target, principal)``.

    History is bounded so an incident never has to be reconstructed from an
    unbounded pile of versions, and configurable because how many generations
    an operator must be able to fall back to is a site policy. The bound is at
    least two: rotation's whole safety property is that the previous version
    stays retrievable while the new one is applied, and a bound of one would
    evict it.

    History lives in this process. Persisting versions to an external store is
    the backend contract's job, not this module's.
    """

    def __init__(self, *, max_versions: int = _DEFAULT_MAX_VERSIONS) -> None:
        if max_versions < _MIN_MAX_VERSIONS:
            raise RotationError(
                f"max_versions must be at least {_MIN_MAX_VERSIONS} so the previous version "
                f"stays retrievable during a rotation; got {max_versions}"
            )
        self._max_versions = max_versions
        self._lock = threading.Lock()
        self._history: dict[tuple[str, str], deque[SecretVersion]] = {}

    @property
    def max_versions(self) -> int:
        """Return the configured per-``(target, principal)`` history bound."""
        return self._max_versions

    def store(
        self,
        *,
        target: str,
        principal: str,
        value: str,
        version_id: str,
        created_at: float,
    ) -> SecretVersion:
        """Record ``value`` as the newest version for ``(target, principal)``.

        The oldest version is evicted once the history reaches the bound. The
        value is registered with the redaction registry so it cannot survive
        into a persisted transcript.
        """
        if not target:
            raise RotationError("target must not be empty")
        if not principal:
            raise RotationError("principal must not be empty")
        if not version_id:
            raise RotationError("version_id must not be empty")

        version = SecretVersion(
            target=target,
            principal=principal,
            version_id=version_id,
            created_at=created_at,
            fingerprint=_fingerprint(value),
            value=value,
        )
        with self._lock:
            bucket = self._history.get((target, principal))
            if bucket is None:
                bucket = deque[SecretVersion](maxlen=self._max_versions)
                self._history[(target, principal)] = bucket
            bucket.append(version)
        register_secret_for_redaction(value)
        return version

    def versions(self, *, target: str, principal: str) -> list[SecretVersion]:
        """Return the retained versions for ``(target, principal)``, newest first."""
        with self._lock:
            bucket = self._history.get((target, principal))
            return list(reversed(bucket)) if bucket else []

    def latest(self, *, target: str, principal: str) -> SecretVersion | None:
        """Return the newest retained version, or ``None`` when there is none."""
        with self._lock:
            bucket = self._history.get((target, principal))
            return bucket[-1] if bucket else None

    def get(self, *, target: str, principal: str, version_id: str) -> SecretVersion | None:
        """Return a specific retained version, or ``None`` once it is evicted."""
        with self._lock:
            bucket = self._history.get((target, principal))
            if not bucket:
                return None
            for version in bucket:
                if version.version_id == version_id:
                    return version
        return None


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RotationReceipt:
    """The dated record a rotation leaves on the target it rotated.

    Carries the fingerprint of the applied material, never the material, so a
    receipt is safe to read back as a discoverable attribute of the target.
    """

    target: str
    principal: str
    version_id: str
    fingerprint: str
    rotated_at: int

    def to_canonical_bytes(self) -> bytes:
        """Serialise to canonical JSON bytes."""
        return json.dumps(
            {
                "fingerprint": self.fingerprint,
                "principal": self.principal,
                "rotated_at": self.rotated_at,
                "target": self.target,
                "version_id": self.version_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> RotationReceipt:
        """Parse a receipt from its canonical JSON bytes."""
        parsed: object = json.loads(raw)
        if not isinstance(parsed, dict):
            raise RotationError("rotation receipt payload is not an object")
        row = cast("dict[str, Any]", parsed)
        return cls(
            target=str(row["target"]),
            principal=str(row["principal"]),
            version_id=str(row["version_id"]),
            fingerprint=str(row["fingerprint"]),
            rotated_at=int(row["rotated_at"]),
        )

    def age_seconds(self, *, now: float) -> float:
        """Return how long ago this rotation happened, in seconds."""
        return now - float(self.rotated_at)


# ---------------------------------------------------------------------------
# Target contract (the "apply" step)
# ---------------------------------------------------------------------------


class RotationTarget(ABC):
    """A system that holds a secret Bernstein rotates on an operator's behalf.

    Implementations are responsible for the two writes rotation performs on the
    target: installing the new credential and recording the receipt that says
    when it was installed.
    """

    name: str = ""

    @abstractmethod
    def apply(self, *, principal: str, value: str) -> None:
        """Install ``value`` as ``principal``'s credential on this target.

        Raises:
            Exception: Any failure. The rotator turns it into a
                :class:`RotationError` whose run stopped at
                :attr:`RotationStep.STORE`.
        """

    @abstractmethod
    def write_receipt(self, *, principal: str, receipt: RotationReceipt) -> None:
        """Record ``receipt`` on the target as a readable attribute."""

    @abstractmethod
    def read_receipt(self, *, principal: str) -> RotationReceipt | None:
        """Return the receipt this target holds for ``principal``, if any."""


# ---------------------------------------------------------------------------
# Run record + journal
# ---------------------------------------------------------------------------


class RotationStep(StrEnum):
    """The steps a rotation run passes through, in order."""

    MINT = "mint"
    STORE = "store"
    APPLY = "apply"
    RECEIPT = "receipt"


@dataclass(frozen=True)
class RotationRun:
    """What one rotation run did, and how far it got.

    ``step_reached`` names the last step that *completed*: a run that minted
    and stored but failed to apply reports :attr:`RotationStep.STORE`, which is
    also the guarantee that both versions are still in the store.
    """

    target: str
    principal: str
    secret_name: str
    task_id: str
    version_id: str
    token_id: str
    fingerprint: str
    step_reached: RotationStep | None
    ok: bool
    started_at: float
    finished_at: float
    previous_version_id: str = ""
    error: str = ""

    def to_row(self) -> dict[str, Any]:
        """Return a JSON-safe journal row. Never carries secret material."""
        return {
            "error": self.error,
            "finished_at": self.finished_at,
            "fingerprint": self.fingerprint,
            "ok": self.ok,
            "previous_version_id": self.previous_version_id,
            "principal": self.principal,
            "secret_name": self.secret_name,
            "started_at": self.started_at,
            "step_reached": self.step_reached.value if self.step_reached is not None else "",
            "target": self.target,
            "task_id": self.task_id,
            "token_id": self.token_id,
            "version_id": self.version_id,
        }


class RotationJournal:
    """Append-only JSONL journal of rotation runs."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """Return the journal file path."""
        return self._path

    def record(self, run: RotationRun) -> None:
        """Append ``run`` to the journal."""
        line = json.dumps(run.to_row(), ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def rows(self) -> list[dict[str, Any]]:
        """Return every journaled run, oldest first."""
        if not self._path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            parsed: object = json.loads(line)
            if isinstance(parsed, dict):
                rows.append(cast("dict[str, Any]", parsed))
        return rows


# ---------------------------------------------------------------------------
# The rotator
# ---------------------------------------------------------------------------


class SecretRotator:
    """Run ``mint -> store -> apply`` against a :class:`RotationTarget`."""

    def __init__(
        self,
        *,
        broker: SecretsBroker,
        store: SecretVersionStore,
        journal: RotationJournal | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._broker = broker
        self._store = store
        self._journal = journal
        self._clock = clock

    def rotate(
        self,
        *,
        target: RotationTarget,
        principal: str,
        secret_name: str,
        task_id: str,
        ttl_seconds: int | None = None,
    ) -> RotationRun:
        """Rotate ``principal``'s credential on ``target``.

        Args:
            target: The system holding the credential.
            principal: The identity the credential authenticates as on the
                target.
            secret_name: Backing-store name the broker mints against.
            task_id: Bernstein task id that owns the minted token.
            ttl_seconds: Lifetime override for the minted token.

        Returns:
            The completed :class:`RotationRun`.

        Raises:
            RotationError: When any step fails. The exception's ``run``
                attribute names the last completed step; the same run is
                journaled before the error is raised.
        """
        if not principal:
            raise RotationError("principal must not be empty")

        started_at = self._clock()
        version_id = _new_version_id(started_at)
        target_name = target.name or type(target).__name__
        previous = self._store.latest(target=target_name, principal=principal)
        previous_version_id = previous.version_id if previous is not None else ""

        def _fail(step: RotationStep | None, token_id: str, fingerprint: str, exc: BaseException) -> RotationError:
            run = RotationRun(
                target=target_name,
                principal=principal,
                secret_name=secret_name,
                task_id=task_id,
                version_id=version_id,
                token_id=token_id,
                fingerprint=fingerprint,
                step_reached=step,
                ok=False,
                started_at=started_at,
                finished_at=self._clock(),
                previous_version_id=previous_version_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            self._journal_run(run)
            reached = step.value if step is not None else "nothing"
            logger.error(
                "secret rotation failed for %s/%s after %s: %s",
                target_name,
                principal,
                reached,
                run.error,
            )
            return RotationError(
                f"rotation of {principal!r} on {target_name!r} failed after {reached}: {run.error}",
                run=run,
            )

        # 1. mint -- new material, never derived from what the target holds.
        try:
            token = self._broker.mint(
                secret_name=secret_name,
                task_id=task_id,
                ttl_seconds=ttl_seconds,
                version_id=version_id,
            )
        except Exception as exc:
            raise _fail(None, "", "", exc) from exc

        fingerprint = _fingerprint(token.value)

        # 2. store -- history is bounded, and the previous version survives.
        try:
            self._store.store(
                target=target_name,
                principal=principal,
                value=token.value,
                version_id=version_id,
                created_at=started_at,
            )
        except Exception as exc:
            raise _fail(RotationStep.MINT, token.token_id, fingerprint, exc) from exc

        # 3. apply -- the only step that changes the target.
        try:
            target.apply(principal=principal, value=token.value)
        except Exception as exc:
            raise _fail(RotationStep.STORE, token.token_id, fingerprint, exc) from exc

        # 4. receipt -- dated proof on the target that the apply happened.
        receipt = RotationReceipt(
            target=target_name,
            principal=principal,
            version_id=version_id,
            fingerprint=fingerprint,
            rotated_at=int(started_at),
        )
        try:
            target.write_receipt(principal=principal, receipt=receipt)
        except Exception as exc:
            raise _fail(RotationStep.APPLY, token.token_id, fingerprint, exc) from exc

        run = RotationRun(
            target=target_name,
            principal=principal,
            secret_name=secret_name,
            task_id=task_id,
            version_id=version_id,
            token_id=token.token_id,
            fingerprint=fingerprint,
            step_reached=RotationStep.RECEIPT,
            ok=True,
            started_at=started_at,
            finished_at=self._clock(),
            previous_version_id=previous_version_id,
        )
        self._journal_run(run)
        return run

    def _journal_run(self, run: RotationRun) -> None:
        if self._journal is None:
            return
        try:
            self._journal.record(run)
        except OSError as exc:  # pragma: no cover - filesystem failure path
            logger.error("secret rotation journal write failed: %s", exc)


# ---------------------------------------------------------------------------
# Receipt audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RotationAuditFinding:
    """A principal whose rotation receipt is not current.

    Missing and stale are one finding on purpose: from the chair asking "who is
    out of date", a target that was never rotated and one that stopped being
    rotated are the same answer.
    """

    NAME: ClassVar[str] = "secret_rotation_receipt_not_current"

    target: str
    principal: str
    receipt_present: bool
    age_seconds: float | None
    max_age_seconds: int
    finding: str = NAME


def audit_rotation_receipts(
    *,
    target: RotationTarget,
    principals: Sequence[str],
    max_age_seconds: int,
    now: float | None = None,
) -> list[RotationAuditFinding]:
    """Report principals on ``target`` whose rotation receipt is not current.

    Args:
        target: The target to probe.
        principals: The principals expected to be rotated on it.
        max_age_seconds: Policy window; a receipt older than this is stale.
        now: Wall-clock override for tests.

    Returns:
        One finding per principal whose receipt is missing or stale, in the
        order ``principals`` was given.
    """
    if max_age_seconds <= 0:
        raise RotationError("max_age_seconds must be positive")
    current = now if now is not None else time.time()
    target_name = target.name or type(target).__name__
    findings: list[RotationAuditFinding] = []
    for principal in principals:
        receipt = target.read_receipt(principal=principal)
        if receipt is None:
            findings.append(
                RotationAuditFinding(
                    target=target_name,
                    principal=principal,
                    receipt_present=False,
                    age_seconds=None,
                    max_age_seconds=max_age_seconds,
                )
            )
            continue
        age = receipt.age_seconds(now=current)
        if age > max_age_seconds:
            findings.append(
                RotationAuditFinding(
                    target=target_name,
                    principal=principal,
                    receipt_present=True,
                    age_seconds=age,
                    max_age_seconds=max_age_seconds,
                )
            )
    return findings
