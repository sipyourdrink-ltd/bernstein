"""Flow report model for the browser modality: the chain is the artifact (#2523).

A site check that reports "passed" and leaves a folder of screenshots behind is
unfalsifiable after the fact. Nothing binds the verdict to the pixels, nothing
binds the pixels to the action that produced them, and nothing detects a swapped
file. This module is the shape that fixes that: a browser flow report is not
prose with attachments, it is the Merkle-chained action journal itself.

The chain
---------
Each :class:`BrowserStepRecord` pins the content hashes of the exact screenshot
and DOM bytes the worker saw *before* it acted, folds them into an
``observation_hash``, and folds that plus the action and the prior anchor into
this step's ``anchor`` (reusing the anchor primitive in
:mod:`bernstein.core.agents.computer_use`). The head anchor is the run's
identity. Because each anchor folds in its predecessor, a single altered
observation changes that step's anchor and every anchor after it, so divergence
surfaces at an exact index rather than as a flaky assertion.

The verdict is recomputed, never trusted
----------------------------------------
:class:`BrowserCheckRecord` carries what the worker concluded, but
:func:`verify_browser_flow_report` does not take that on faith: it reattaches the
DOM bytes from the content store by hash and *re-evaluates* the assertion. A
report whose hashes all recompute but whose recorded verdict disagrees with the
bytes fails, naming the check id. That is the property a screenshot folder cannot
have -- the verdict is a deterministic projection of anchored bytes, so a worker
cannot lie about what it saw without breaking a hash.

Everything here is a pure function of bytes. No wall clock, no network, no
ordering that depends on either, so two operators verifying the same report get
byte-identical verdicts.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from bernstein.core.agents.computer_use import (
    GENESIS_ANCHOR,
    Action,
    ActionKind,
    compute_action_anchor,
    compute_observation_hash,
)
from bernstein.core.orchestration.activity import ActivityRejected

if TYPE_CHECKING:
    from bernstein.core.orchestration.activity_modalities import ContentStore

__all__ = [
    "BrowserCheckRecord",
    "BrowserCheckVerdict",
    "BrowserFlowReport",
    "BrowserFlowVerdict",
    "BrowserStepRecord",
    "BrowserStepVerdict",
    "CheckKind",
    "build_step_record",
    "dom_digest_of",
    "evaluate_check",
    "normalise_dom",
    "report_to_canonical_bytes",
    "validate_browser_flow_report",
    "verify_browser_flow_report",
]

#: A ``sha256:``-prefixed content hash as stored in the content store.
_CONTENT_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")

#: A bare 64-char hex anchor.
_ANCHOR = re.compile(r"^[0-9a-f]{64}$")


class CheckKind(StrEnum):
    """The assertion vocabulary a site check may use.

    Deliberately closed and offline-evaluable: every kind is a pure function of
    the anchored bytes, so a verifier re-derives the verdict months later with
    the network disabled. Anything that would need a live page (timing, network
    waterfall, animation) is out by construction -- it could not be replayed.
    """

    DOM_CONTAINS = "dom_contains"
    DOM_NOT_CONTAINS = "dom_not_contains"
    SCREENSHOT_HASH_EQUALS = "screenshot_hash_equals"


def normalise_dom(dom: bytes) -> bytes:
    """Collapse runs of ASCII whitespace so the digest ignores cosmetic noise.

    Hashing raw markup makes the digest change on every reflow, indent, or
    minifier tweak, which would turn every replay into a false divergence.
    Collapsing whitespace is a pure, total function of the input bytes, so the
    normalised form is still fully determined by what the worker saw.

    Args:
        dom: The raw DOM / accessibility snapshot bytes.

    Returns:
        The normalised bytes the digest and every assertion are computed over.
    """
    return b" ".join(dom.split())


def dom_digest_of(dom: bytes) -> str:
    """Return the hex digest of the normalised DOM bytes."""
    return hashlib.sha256(normalise_dom(dom)).hexdigest()


def evaluate_check(
    *,
    kind: CheckKind,
    operand: str,
    dom_bytes: bytes,
    screenshot_content_hash: str,
) -> bool:
    """Evaluate one assertion against anchored bytes.

    Pure and offline: the same inputs always produce the same verdict, which is
    what lets :func:`verify_browser_flow_report` recompute a recorded verdict
    rather than trust it.

    Args:
        kind: The assertion kind.
        operand: The expected substring (DOM kinds) or content hash (screenshot
            kind).
        dom_bytes: The step's raw DOM bytes, as reattached from the store.
        screenshot_content_hash: The step's pinned screenshot content hash.

    Returns:
        Whether the assertion holds.
    """
    if kind is CheckKind.SCREENSHOT_HASH_EQUALS:
        return screenshot_content_hash == operand
    needle = normalise_dom(operand.encode("utf-8"))
    present = needle in normalise_dom(dom_bytes)
    return present if kind is CheckKind.DOM_CONTAINS else not present


@dataclass(frozen=True, slots=True)
class BrowserStepRecord:
    """One anchored step: the observation the worker saw and the action it took.

    The record is the action receipt. ``anchor`` folds in ``prev_anchor``, so the
    step sequence is a single-parent Merkle chain whose head is the run identity.
    Only the *digest* of any typed value is recorded, so a form-filling flow never
    writes a secret into the chain.

    Attributes:
        index: Zero-based position in the flow.
        action_kind: The action verb (an :class:`~bernstein.core.agents.computer_use.ActionKind` value).
        action_target: The action target (URL, selector, element ref).
        action_value_digest: SHA-256 hex digest of any typed value, else empty.
        screenshot_content_hash: ``sha256:`` hash of the pre-action screenshot
            bytes in the content store.
        dom_content_hash: ``sha256:`` hash of the pre-action DOM bytes in the
            content store.
        dom_digest: Hex digest of the *normalised* DOM bytes.
        observation_hash: ``sha256(screenshot_bytes + dom_digest)``.
        prev_anchor: The prior step's anchor (genesis sentinel for step 0).
        anchor: ``sha256(canonical(prev_anchor, observation_hash, action))``.
    """

    index: int
    action_kind: str
    action_target: str
    action_value_digest: str
    screenshot_content_hash: str
    dom_content_hash: str
    dom_digest: str
    observation_hash: str
    prev_anchor: str
    anchor: str

    def action(self) -> Action:
        """Rebuild the canonical action this record anchored."""
        return Action(
            kind=ActionKind(self.action_kind),
            target=self.action_target,
            value_digest=self.action_value_digest,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON projection stored on the report."""
        return {
            "index": self.index,
            "action_kind": self.action_kind,
            "action_target": self.action_target,
            "action_value_digest": self.action_value_digest,
            "screenshot_content_hash": self.screenshot_content_hash,
            "dom_content_hash": self.dom_content_hash,
            "dom_digest": self.dom_digest,
            "observation_hash": self.observation_hash,
            "prev_anchor": self.prev_anchor,
            "anchor": self.anchor,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> BrowserStepRecord:
        """Rebuild a step record from its report projection."""
        return cls(
            index=int(row.get("index", -1)),
            action_kind=str(row.get("action_kind", "")),
            action_target=str(row.get("action_target", "")),
            action_value_digest=str(row.get("action_value_digest", "")),
            screenshot_content_hash=str(row.get("screenshot_content_hash", "")),
            dom_content_hash=str(row.get("dom_content_hash", "")),
            dom_digest=str(row.get("dom_digest", "")),
            observation_hash=str(row.get("observation_hash", "")),
            prev_anchor=str(row.get("prev_anchor", "")),
            anchor=str(row.get("anchor", "")),
        )


def build_step_record(
    *,
    index: int,
    prev_anchor: str,
    action: Action,
    screenshot_content_hash: str,
    dom_content_hash: str,
    screenshot_bytes: bytes,
    dom_bytes: bytes,
) -> BrowserStepRecord:
    """Anchor one step from the bytes the worker observed before it acted.

    Args:
        index: Zero-based position in the flow.
        prev_anchor: The prior step's anchor (genesis sentinel for step 0).
        action: The canonicalised action taken after this observation.
        screenshot_content_hash: Store hash of the screenshot bytes.
        dom_content_hash: Store hash of the DOM bytes.
        screenshot_bytes: The exact screenshot bytes observed.
        dom_bytes: The exact DOM bytes observed.

    Returns:
        The anchored :class:`BrowserStepRecord`.
    """
    dom_digest = dom_digest_of(dom_bytes)
    observation_hash = compute_observation_hash(screenshot_bytes=screenshot_bytes, dom_digest=dom_digest)
    anchor = compute_action_anchor(prev_anchor=prev_anchor, observation_hash=observation_hash, action=action)
    return BrowserStepRecord(
        index=index,
        action_kind=str(action.kind),
        action_target=action.target,
        action_value_digest=action.value_digest,
        screenshot_content_hash=screenshot_content_hash,
        dom_content_hash=dom_content_hash,
        dom_digest=dom_digest,
        observation_hash=observation_hash,
        prev_anchor=prev_anchor,
        anchor=anchor,
    )


@dataclass(frozen=True, slots=True)
class BrowserCheckRecord:
    """One site-check assertion and the verdict the worker recorded for it.

    ``passed`` is a claim, not evidence. Verification re-evaluates the assertion
    against the reattached bytes at ``step_index`` and refuses any disagreement,
    so the field is only ever a cached projection of the anchored observation.

    Attributes:
        check_id: Stable id, unique within the report.
        kind: The assertion kind.
        operand: The expected substring or content hash.
        step_index: The step whose observation the assertion is evaluated against.
        passed: The verdict the worker recorded.
    """

    check_id: str
    kind: CheckKind
    operand: str
    step_index: int
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON projection stored on the report."""
        return {
            "check_id": self.check_id,
            "kind": str(self.kind),
            "operand": self.operand,
            "step_index": self.step_index,
            "passed": self.passed,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> BrowserCheckRecord:
        """Rebuild a check record from its report projection."""
        return cls(
            check_id=str(row.get("check_id", "")),
            kind=CheckKind(str(row.get("kind", CheckKind.DOM_CONTAINS))),
            operand=str(row.get("operand", "")),
            step_index=int(row.get("step_index", -1)),
            passed=bool(row.get("passed", False)),
        )


@dataclass(frozen=True, slots=True)
class BrowserFlowReport:
    """The activity artifact for a browser run: the anchored flow and its checks.

    Its canonical JSON projection is hashed as the dispatched
    :class:`~bernstein.core.orchestration.activity.ActivityResult`'s
    ``artifact_hash`` and stored content-addressed, so an offline verifier
    reattaches and re-verifies the whole run from the run's content store alone.

    Attributes:
        flow_id: Stable identifier for the flow (also the profile isolation key).
        start_url: The URL the flow started from (provenance).
        steps: The anchored steps, in execution order.
        checks: The site-check assertions and their recorded verdicts.
        head_anchor: The final anchor -- the run's identity. Genesis when the run
            recorded no step.
    """

    flow_id: str
    start_url: str
    steps: tuple[BrowserStepRecord, ...] = ()
    checks: tuple[BrowserCheckRecord, ...] = ()
    head_anchor: str = GENESIS_ANCHOR

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON artifact projection (hashed as ``artifact_hash``)."""
        return {
            "flow_id": self.flow_id,
            "start_url": self.start_url,
            "steps": [s.to_dict() for s in self.steps],
            "checks": [c.to_dict() for c in self.checks],
            "head_anchor": self.head_anchor,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> BrowserFlowReport:
        """Rebuild a report from its artifact projection."""
        raw_steps = row.get("steps", [])
        raw_checks = row.get("checks", [])
        return cls(
            flow_id=str(row.get("flow_id", "")),
            start_url=str(row.get("start_url", "")),
            steps=(
                tuple(BrowserStepRecord.from_dict(s) for s in raw_steps if isinstance(s, dict))
                if isinstance(raw_steps, list)
                else ()
            ),
            checks=(
                tuple(BrowserCheckRecord.from_dict(c) for c in raw_checks if isinstance(c, dict))
                if isinstance(raw_checks, list)
                else ()
            ),
            head_anchor=str(row.get("head_anchor", GENESIS_ANCHOR)),
        )


def report_to_canonical_bytes(report: BrowserFlowReport) -> bytes:
    """Return the canonical JSON bytes hashed as the report's ``artifact_hash``.

    Matches the canonicalisation the activity boundary hashes with, so the content
    hash of these bytes equals the anchored ``artifact_hash`` and the report
    reattaches from the store by that hash.
    """
    return json.dumps(report.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str).encode(
        "utf-8"
    )


def validate_browser_flow_report(report: BrowserFlowReport) -> BrowserFlowReport:
    """Refuse a malformed or broken-chain report at the boundary.

    Enforces every structural invariant the replay guarantee rests on, so a
    report that could not be verified later never reaches the journal:

    * ``flow_id`` is non-empty;
    * step indices are contiguous from zero;
    * every step pins ``sha256:``-shaped screenshot and DOM content hashes and a
      well-formed anchor;
    * the anchor chain links (step 0 from genesis, each later step from its
      predecessor's anchor);
    * ``head_anchor`` is the last step's anchor (genesis for an empty flow); and
    * check ids are unique, operands non-empty, and every ``step_index`` points at
      a step that exists.

    Args:
        report: The report to validate.

    Returns:
        The same report, unchanged, when valid.

    Raises:
        ActivityRejected: On the first violated invariant.
    """
    if not report.flow_id.strip():
        raise ActivityRejected("browser flow report has an empty flow_id")

    expected_prev = GENESIS_ANCHOR
    for position, step in enumerate(report.steps):
        if step.index != position:
            raise ActivityRejected(f"browser flow report has a non-contiguous step index: {step.index!r} at {position}")
        for label, value in (
            ("screenshot", step.screenshot_content_hash),
            ("dom", step.dom_content_hash),
        ):
            if not _CONTENT_HASH.match(value):
                raise ActivityRejected(f"step {position} has a malformed {label} content hash: {value!r}")
        if not _ANCHOR.match(step.anchor):
            raise ActivityRejected(f"step {position} has a malformed anchor: {step.anchor!r}")
        if step.prev_anchor != expected_prev:
            raise ActivityRejected(
                f"step {position} has a broken prev_anchor link: "
                f"declared {step.prev_anchor!r}, expected {expected_prev!r}"
            )
        expected_prev = step.anchor

    if report.head_anchor != expected_prev:
        raise ActivityRejected(
            f"browser flow report head_anchor {report.head_anchor!r} is not the final step anchor {expected_prev!r}"
        )

    seen_ids: set[str] = set()
    for check in report.checks:
        if not check.check_id.strip():
            raise ActivityRejected("browser flow report check has an empty check_id")
        if check.check_id in seen_ids:
            raise ActivityRejected(f"browser flow report has a duplicate check_id: {check.check_id!r}")
        seen_ids.add(check.check_id)
        if not check.operand:
            raise ActivityRejected(f"check {check.check_id!r} has an empty operand")
        if not 0 <= check.step_index < len(report.steps):
            raise ActivityRejected(
                f"check {check.check_id!r} has an out-of-range step_index: {check.step_index!r} "
                f"(flow recorded {len(report.steps)} steps)"
            )
    return report


@dataclass(frozen=True, slots=True)
class BrowserStepVerdict:
    """Per-step outcome of :func:`verify_browser_flow_report`.

    Attributes:
        index: The step this verdict covers.
        ok: True only when both observations reattached, both still hash to their
            pinned values, and the recomputed anchor matches the recorded one.
        reason: A short explanation naming the step index when ``ok`` is False.
    """

    index: int
    ok: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON projection surfaced by the CLI."""
        return {"index": self.index, "ok": self.ok, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class BrowserCheckVerdict:
    """Per-check outcome of :func:`verify_browser_flow_report`.

    Attributes:
        check_id: The check this verdict covers.
        ok: True only when the assertion re-evaluated from the reattached bytes
            agrees with the verdict the worker recorded.
        passed: The re-evaluated verdict (what the bytes actually say).
        reason: A short explanation naming the check when ``ok`` is False.
    """

    check_id: str
    ok: bool
    passed: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON projection surfaced by the CLI."""
        return {"check_id": self.check_id, "ok": self.ok, "passed": self.passed, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class BrowserFlowVerdict:
    """Outcome of resolving a whole browser flow report offline.

    Attributes:
        ok: True only when every step and every check verdict is ``ok``.
        steps: Per-step verdicts in flow order.
        checks: Per-check verdicts in report order.
        head_anchor_ok: Whether the recomputed head anchor matches the report.
        reason: The first failure's explanation, else empty.
    """

    ok: bool
    steps: tuple[BrowserStepVerdict, ...] = ()
    checks: tuple[BrowserCheckVerdict, ...] = ()
    head_anchor_ok: bool = False
    reason: str = ""


def _verify_step(
    step: BrowserStepRecord,
    *,
    prev_anchor: str,
    store: ContentStore,
) -> tuple[BrowserStepVerdict, bytes | None]:
    """Reattach one step's bytes and recompute its digest, hash, and anchor."""
    blobs: dict[str, bytes] = {}
    for label, content_hash in (
        ("screenshot", step.screenshot_content_hash),
        ("dom", step.dom_content_hash),
    ):
        try:
            content = store.get(content_hash)
        except KeyError:
            return (
                BrowserStepVerdict(
                    index=step.index,
                    ok=False,
                    reason=f"step {step.index}: {label} bytes missing from store for {content_hash!r}",
                ),
                None,
            )
        recomputed = "sha256:" + hashlib.sha256(content).hexdigest()
        if recomputed != content_hash:
            return (
                BrowserStepVerdict(
                    index=step.index,
                    ok=False,
                    reason=(
                        f"step {step.index}: {label} content hash mismatch "
                        f"(pinned {content_hash!r}, recomputed {recomputed!r})"
                    ),
                ),
                None,
            )
        blobs[label] = content

    dom_bytes = blobs["dom"]
    dom_digest = dom_digest_of(dom_bytes)
    if dom_digest != step.dom_digest:
        return (
            BrowserStepVerdict(
                index=step.index,
                ok=False,
                reason=(
                    f"step {step.index}: dom digest mismatch (pinned {step.dom_digest!r}, recomputed {dom_digest!r})"
                ),
            ),
            None,
        )

    observation_hash = compute_observation_hash(screenshot_bytes=blobs["screenshot"], dom_digest=dom_digest)
    if observation_hash != step.observation_hash:
        return (
            BrowserStepVerdict(
                index=step.index,
                ok=False,
                reason=f"step {step.index}: observation hash mismatch",
            ),
            None,
        )

    try:
        action = step.action()
    except ValueError:
        return (
            BrowserStepVerdict(
                index=step.index,
                ok=False,
                reason=f"step {step.index}: unknown action kind {step.action_kind!r}",
            ),
            None,
        )
    anchor = compute_action_anchor(prev_anchor=prev_anchor, observation_hash=observation_hash, action=action)
    if anchor != step.anchor:
        return (
            BrowserStepVerdict(
                index=step.index,
                ok=False,
                reason=f"step {step.index}: anchor mismatch (recorded {step.anchor!r}, recomputed {anchor!r})",
            ),
            None,
        )
    return BrowserStepVerdict(index=step.index, ok=True), dom_bytes


def verify_browser_flow_report(report: BrowserFlowReport, *, store: ContentStore) -> BrowserFlowVerdict:
    """Resolve a browser flow report offline against the content store.

    Walks the chain from genesis: for each step it reattaches the screenshot and
    DOM bytes by their pinned hashes, re-hashes them to detect tamper, recomputes
    the normalised DOM digest and the observation hash, and recomputes the anchor
    from the *running* predecessor -- so an altered observation or a forged action
    receipt fails naming the exact step index. It then re-evaluates every check
    against the reattached bytes and refuses any disagreement with the verdict the
    worker recorded, naming the check id.

    The whole function touches only the store, so it holds with the network
    disabled, and its output is a pure function of the report and the stored
    bytes, so two verify runs emit identical verdicts.

    Args:
        report: The report to resolve.
        store: The content-addressed store holding the observation bytes.

    Returns:
        A :class:`BrowserFlowVerdict`. ``ok`` requires every step and check to
        resolve.
    """
    step_verdicts: list[BrowserStepVerdict] = []
    dom_by_index: dict[int, bytes] = {}
    prev = GENESIS_ANCHOR
    for step in report.steps:
        if step.prev_anchor != prev:
            step_verdicts.append(
                BrowserStepVerdict(
                    index=step.index,
                    ok=False,
                    reason=(
                        f"step {step.index}: broken chain link "
                        f"(recorded prev_anchor {step.prev_anchor!r}, expected {prev!r})"
                    ),
                )
            )
            break
        verdict, dom_bytes = _verify_step(step, prev_anchor=prev, store=store)
        step_verdicts.append(verdict)
        if not verdict.ok:
            break
        if dom_bytes is not None:
            dom_by_index[step.index] = dom_bytes
        prev = step.anchor

    steps_ok = all(v.ok for v in step_verdicts)
    head_anchor_ok = steps_ok and report.head_anchor == prev

    check_verdicts: list[BrowserCheckVerdict] = []
    for check in report.checks:
        dom_bytes = dom_by_index.get(check.step_index)
        if dom_bytes is None:
            check_verdicts.append(
                BrowserCheckVerdict(
                    check_id=check.check_id,
                    ok=False,
                    passed=False,
                    reason=(
                        f"check {check.check_id!r} rests on step {check.step_index} "
                        "which did not resolve from the store"
                    ),
                )
            )
            continue
        step = report.steps[check.step_index]
        recomputed = evaluate_check(
            kind=check.kind,
            operand=check.operand,
            dom_bytes=dom_bytes,
            screenshot_content_hash=step.screenshot_content_hash,
        )
        if recomputed != check.passed:
            check_verdicts.append(
                BrowserCheckVerdict(
                    check_id=check.check_id,
                    ok=False,
                    passed=recomputed,
                    reason=(
                        f"check {check.check_id!r} recorded passed={check.passed} but the anchored bytes "
                        f"at step {check.step_index} evaluate to {recomputed}"
                    ),
                )
            )
            continue
        check_verdicts.append(BrowserCheckVerdict(check_id=check.check_id, ok=True, passed=recomputed))

    reason = ""
    if not steps_ok:
        reason = next(v.reason for v in step_verdicts if not v.ok)
    elif not head_anchor_ok:
        reason = f"head_anchor mismatch: recorded {report.head_anchor!r}, recomputed {prev!r}"
    else:
        reason = next((v.reason for v in check_verdicts if not v.ok), "")

    return BrowserFlowVerdict(
        ok=steps_ok and head_anchor_ok and all(v.ok for v in check_verdicts),
        steps=tuple(step_verdicts),
        checks=tuple(check_verdicts),
        head_anchor_ok=head_anchor_ok,
        reason=reason,
    )
