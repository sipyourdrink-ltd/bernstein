"""Failure-signal adapters for ``bernstein audit diagnose`` (#2928).

Each adapter turns one operator-facing failure signal into a
:class:`~bernstein.core.replay.diagnose.SignalPredicate` -- a pure function
of on-disk records with no network and no live process:

* ``gate[:RECEIPT_HASH]`` -- the rejecting statistical verdict receipt
  (:mod:`bernstein.eval.gate_receipt`); the fingerprint is the receipt's
  suite / candidate result-set content hashes.
* ``artefact:PATH`` -- the lineage taint projection
  (:mod:`bernstein.core.lineage.provenance`) walked back from the artefact
  tip; the fingerprint is the content hash of every untrusted provenance
  record in the closure, and the content-addressed parent chain from the
  offending record to the tip is attached as evidence.
* ``incident:CASE_ID`` -- a synthesised incident eval case
  (:mod:`bernstein.eval.incident_synthesizer`); the fingerprint is the exact
  recorded failure text carried by the case. When the journal never recorded
  those bytes the diagnosis refuses rather than guessing.
* ``replay`` -- chain-integrity mode: the finding is the first step at which
  ``verify_journal`` reports a break.

The resolved predicate's ``params`` block is embedded verbatim in the
diagnosis receipt, so :func:`predicate_from_params` reconstructs the
identical predicate offline without re-reading the gate / lineage / incident
stores -- re-verification depends only on the receipt and the journal.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.replay.diagnose import (
    SIGNAL_MODE_CHAIN,
    SIGNAL_MODE_CONTENT,
    DiagnoseError,
    SignalPredicate,
)
from bernstein.core.replay.diff import (
    REASON_CODE_BAD_INPUT_CONTENT_HASH,
    REASON_CODE_CHAIN_BREAK,
    REASON_CODE_FIRST_FAILING_TOOL_RESULT,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from bernstein.core.lineage.entry import LineageEntry

#: Signal kinds accepted by :func:`resolve_signal`.
SIGNAL_KIND_GATE = "gate"
SIGNAL_KIND_ARTEFACT = "artefact"
SIGNAL_KIND_INCIDENT = "incident"
SIGNAL_KIND_REPLAY = "replay"

_PREDICATE_IDS: dict[str, str] = {
    SIGNAL_KIND_GATE: "gate/v1",
    SIGNAL_KIND_ARTEFACT: "artefact/v1",
    SIGNAL_KIND_INCIDENT: "incident/v1",
    SIGNAL_KIND_REPLAY: "replay/v1",
}

_DEFAULT_REASON_CODES: dict[str, str] = {
    SIGNAL_KIND_GATE: REASON_CODE_BAD_INPUT_CONTENT_HASH,
    SIGNAL_KIND_ARTEFACT: REASON_CODE_BAD_INPUT_CONTENT_HASH,
    SIGNAL_KIND_INCIDENT: REASON_CODE_FIRST_FAILING_TOOL_RESULT,
    SIGNAL_KIND_REPLAY: REASON_CODE_CHAIN_BREAK,
}

_RECEIPT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

#: Minimum length of an incident fingerprint line. Shorter fragments (a bare
#: word, a truncation residue) would match almost any payload and turn the
#: lookup into noise, so they are dropped deterministically.
_MIN_INCIDENT_NEEDLE_LEN = 12

#: Markers that introduce the recorded failure text inside a synthesised
#: incident case prompt (see ``incident_synthesizer._build_prompt_from_dlq``
#: and ``_build_prompt_from_postmortem``).
_INCIDENT_ERROR_MARKERS = frozenset({"Last error (trimmed):", "Representative error snippets:"})


def _bare_digest(content_hash: str) -> str:
    """Return the hex digest without a ``sha256:`` prefix.

    Journal payloads record content hashes both prefixed and bare; matching
    on the bare digest covers both spellings deterministically.
    """
    return content_hash.split(":", 1)[-1]


def _predicate(kind: str, params: dict[str, Any]) -> SignalPredicate:
    """Assemble a :class:`SignalPredicate` for *kind* from its params block."""
    needles = tuple(str(n) for n in params.get("needles", []))
    lineage_path = tuple(str(h) for h in params.get("lineage_path", []))
    return SignalPredicate(
        predicate_id=_PREDICATE_IDS[kind],
        params=params,
        default_reason_code=_DEFAULT_REASON_CODES[kind],
        needles=needles,
        lineage_path=lineage_path,
        mode=SIGNAL_MODE_CHAIN if kind == SIGNAL_KIND_REPLAY else SIGNAL_MODE_CONTENT,
    )


def predicate_from_params(params: Mapping[str, Any]) -> SignalPredicate:
    """Reconstruct the predicate a receipt embeds, without any store reads.

    The params block is self-contained (kind, needles, anchors), so offline
    re-verification reproduces exactly the predicate that was evaluated --
    including its ``predicate_hash`` -- from the receipt alone.

    Raises:
        DiagnoseError: The params block names an unknown signal kind.
    """
    kind = params.get("kind")
    if not isinstance(kind, str) or kind not in _PREDICATE_IDS:
        raise DiagnoseError(f"diagnosis receipt names an unknown signal kind: {kind!r}")
    # Receipt params are external input at verify time: reject malformed
    # shapes instead of coercing them. A plain string here would otherwise
    # iterate per-character through tuple(str(...) for ...) and silently
    # rewrite the predicate (and therefore its predicate_hash) into
    # something that was never evaluated.
    for list_field in ("needles", "lineage_path"):
        value = params.get(list_field, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise DiagnoseError(
                f"diagnosis receipt params field {list_field!r} must be a list of strings; refusing to rebuild "
                "a predicate from a malformed receipt"
            )
    gate_block = params.get("lineage_gate")
    if gate_block is not None and (
        not isinstance(gate_block, dict) or any(not isinstance(v, bool) for v in gate_block.values())
    ):
        raise DiagnoseError(
            "diagnosis receipt params field 'lineage_gate' must be a mapping of booleans; refusing to rebuild "
            "a predicate from a malformed receipt"
        )
    return _predicate(kind, dict(params))


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------


def _load_gate_receipts(gate_dir: Path) -> list[Any]:
    """Load every parseable verdict receipt under *gate_dir*."""
    from bernstein.eval.gate_receipt import VerdictReceipt

    receipts: list[Any] = []
    if not gate_dir.is_dir():
        return receipts
    for path in sorted(gate_dir.glob("*.json")):
        try:
            receipts.append(VerdictReceipt.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return receipts


def gate_signal(receipt_hash: str | None, *, gate_dir: Path) -> SignalPredicate:
    """Resolve the ``gate`` signal from a sealed verdict receipt.

    Args:
        receipt_hash: Explicit ``sha256:...`` receipt hash, or ``None`` to
            resolve the most recent rejecting receipt deterministically
            (highest ``(timestamp, receipt_hash)`` among
            ``significant_regression`` verdicts).
        gate_dir: The ``.sdd/eval/gate`` receipt store.

    Raises:
        DiagnoseError: No matching receipt exists, or the named receipt hash
            is not canonical.
    """
    from bernstein.eval.significance import Verdict

    receipts = _load_gate_receipts(gate_dir)
    if receipt_hash is not None:
        if not _RECEIPT_HASH_RE.match(receipt_hash):
            raise DiagnoseError(f"gate receipt hash is not a canonical sha256 digest: {receipt_hash!r}")
        matched = [r for r in receipts if r.receipt_hash == receipt_hash]
        if not matched:
            raise DiagnoseError(f"no verdict receipt {receipt_hash} under {gate_dir}")
        receipt = matched[0]
    else:
        rejecting = [r for r in receipts if r.verdict is Verdict.SIGNIFICANT_REGRESSION]
        if not rejecting:
            raise DiagnoseError(
                f"no rejecting verdict receipt under {gate_dir}; name one explicitly with --signal gate:<receipt-hash>"
            )
        receipt = max(rejecting, key=lambda r: (r.timestamp, r.receipt_hash))

    needles = sorted({_bare_digest(receipt.suite_content_hash), _bare_digest(receipt.candidate_result_set_hash)})
    params: dict[str, Any] = {
        "kind": SIGNAL_KIND_GATE,
        "receipt_hash": receipt.receipt_hash,
        "verdict": str(receipt.verdict),
        "needles": needles,
    }
    return _predicate(SIGNAL_KIND_GATE, params)


# ---------------------------------------------------------------------------
# artefact
# ---------------------------------------------------------------------------


def _path_tip_to_offender(
    tip: str,
    offenders: frozenset[str],
    index: Mapping[str, LineageEntry],
) -> tuple[str, ...]:
    """Deterministic parent-edge path from *tip* down to the first offender.

    Depth-first over ``parent_hashes`` in recorded order, so two verifiers
    holding the same log derive the identical path. Returned in
    culprit-to-tip order (the receipt's ``lineage_path`` contract).
    """
    stack: list[tuple[str, tuple[str, ...]]] = [(tip, (tip,))]
    visited: set[str] = set()
    while stack:
        current, path = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        if current in offenders:
            return tuple(reversed(path))
        entry = index.get(current)
        if entry is None:
            continue
        # Reversed push keeps pop() order equal to recorded parent order.
        for parent in reversed(entry.parent_hashes):
            stack.append((parent, (*path, parent)))
    return ()


def artefact_signal(
    artefact_path: str,
    *,
    lineage_log: Path,
    cards_dir: Path,
    operator_secret: bytes | None = None,
) -> SignalPredicate:
    """Resolve the ``artefact:PATH`` signal from the *gated* lineage log.

    The lineage gate runs first -- strict byte-canonical entry parsing,
    detached-signature verification against the agent cards, parent
    anchoring, and (when *operator_secret* is supplied) the per-entry
    operator HMAC -- exactly the checks ``bernstein audit taint`` requires
    before trusting the log. Only a log that passes shapes the predicate:
    an unsigned, malformed, or reparented entry refuses the diagnosis
    instead of being sealed into a signed receipt (bot-ack: 3706042986).
    Whether the HMAC layer was checked is disclosed in the predicate params
    (and therefore in the receipt) as ``lineage_gate.operator_hmac_checked``,
    mirroring how the taint CLI degrades when no operator secret is
    configured.

    Walks the taint projection back from the artefact tip; the offending
    fingerprint is the content hash of every untrusted provenance record in
    the closure.

    Raises:
        DiagnoseError: The lineage gate fails, the log is missing/empty, the
            artefact has no lineage tip, the artefact is not tainted, or the
            closure carries no provenance record to name.
    """
    from bernstein.core.lineage.entry import entry_hash
    from bernstein.core.lineage.gate import check
    from bernstein.core.lineage.provenance import (
        TrustClass,
        is_untrusted,
        load_entries_from_log,
        resolve_artefact_tip,
        taint_for_artefact,
    )

    gate_result = check(lineage_log, cards_dir, operator_secret=operator_secret)
    if not gate_result.ok:
        shown = "; ".join(gate_result.failures[:5])
        raise DiagnoseError(
            f"lineage gate failed for {lineage_log} ({len(gate_result.failures)} failure(s): {shown}); "
            "refusing to shape a diagnosis from an unverified lineage log"
        )

    entries = load_entries_from_log(lineage_log)
    if not entries:
        raise DiagnoseError(f"no lineage entries at {lineage_log}; cannot resolve artefact signal")
    verdict = taint_for_artefact(artefact_path, entries)
    if not verdict.resolved:
        raise DiagnoseError(f"artefact {artefact_path!r} has no lineage tip in {lineage_log}")
    if not verdict.tainted:
        raise DiagnoseError(
            f"artefact {artefact_path!r} is not tainted per its lineage closure "
            f"(effective trust: {verdict.trust.value}); no offending content hash to locate"
        )

    index = {entry_hash(e): e for e in entries}
    offenders = frozenset(h for h, tc in verdict.trust_records if h in index and is_untrusted(TrustClass(tc)))
    if not offenders:
        raise DiagnoseError(
            f"artefact {artefact_path!r} is tainted only because its closure carries no "
            "provenance record (fail-closed default); there is no recorded offending "
            "content hash to locate"
        )

    tip = resolve_artefact_tip(artefact_path, entries)
    lineage_path: tuple[str, ...] = ()
    if tip is not None:
        lineage_path = _path_tip_to_offender(tip, offenders, index)

    needles = sorted({_bare_digest(index[h].content_hash) for h in offenders})
    params: dict[str, Any] = {
        "kind": SIGNAL_KIND_ARTEFACT,
        "artefact_path": artefact_path,
        "tip": tip or "",
        "needles": needles,
        "lineage_path": list(lineage_path),
        # Honest disclosure of the gate mode the predicate was shaped under:
        # signatures/anchoring always verified, operator HMAC only when the
        # secret was configured. Sealed into the receipt via these params.
        "lineage_gate": {"checked": True, "operator_hmac_checked": operator_secret is not None},
    }
    return _predicate(SIGNAL_KIND_ARTEFACT, params)


# ---------------------------------------------------------------------------
# incident
# ---------------------------------------------------------------------------


def _fingerprint_lines(prompt: str) -> tuple[str, ...]:
    """Extract the recorded failure text from a synthesised case prompt.

    Only the lines following an error marker count as fingerprint; leading
    ``- `` bullets and trailing truncation ellipses are stripped, and lines
    shorter than :data:`_MIN_INCIDENT_NEEDLE_LEN` are dropped. Sorted and
    de-duplicated for determinism.
    """
    needles: set[str] = set()
    in_error = False
    for raw_line in prompt.splitlines():
        line = raw_line.strip()
        if line in _INCIDENT_ERROR_MARKERS:
            in_error = True
            continue
        if not in_error:
            continue
        line = line.removeprefix("- ").removesuffix("...").strip()
        if len(line) >= _MIN_INCIDENT_NEEDLE_LEN:
            needles.add(line)
    return tuple(sorted(needles))


def incident_signal(case_id: str, *, cases_dir: Path) -> SignalPredicate:
    """Resolve the ``incident:CASE_ID`` signal from a synthesised eval case.

    The fingerprint is the exact failure text the case recorded. When the
    case is absent or carries no matchable failure text, the resolution
    fails closed -- the adapter never widens the match to prose similarity.

    Raises:
        DiagnoseError: The case id is unsafe, the case file is missing or
            unparseable, or the case carries no matchable fingerprint.
    """
    import yaml

    if not _CASE_ID_RE.match(case_id):
        raise DiagnoseError(f"unsafe incident case id: {case_id!r}")
    case_path = cases_dir / f"{case_id}.yaml"
    if not case_path.is_file():
        raise DiagnoseError(f"no incident eval case {case_id!r} under {cases_dir}")
    try:
        raw = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DiagnoseError(f"cannot read incident case {case_id!r}: {exc}") from exc
    if not isinstance(raw, dict):
        raise DiagnoseError(f"incident case {case_id!r} is not a mapping")
    case = cast("dict[str, Any]", raw)

    needles = _fingerprint_lines(str(case.get("prompt", "")))
    if not needles:
        raise DiagnoseError(
            f"incident case {case_id!r} carries no matchable failure fingerprint "
            f"(no error line of >= {_MIN_INCIDENT_NEEDLE_LEN} characters); refusing to guess"
        )
    params: dict[str, Any] = {
        "kind": SIGNAL_KIND_INCIDENT,
        "case_id": case_id,
        "needles": list(needles),
    }
    return _predicate(SIGNAL_KIND_INCIDENT, params)


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def replay_signal() -> SignalPredicate:
    """Resolve the ``replay`` signal: chain-integrity localisation."""
    return _predicate(SIGNAL_KIND_REPLAY, {"kind": SIGNAL_KIND_REPLAY})


# ---------------------------------------------------------------------------
# spec parsing
# ---------------------------------------------------------------------------


def resolve_signal(
    spec: str,
    *,
    sdd_dir: Path,
    workdir: Path | None = None,
    cases_dir: Path | None = None,
    lineage_cards_dir: Path | None = None,
    lineage_operator_secret: bytes | None = None,
) -> SignalPredicate:
    """Resolve a ``--signal`` spec string into a :class:`SignalPredicate`.

    Grammar::

        gate                    latest rejecting verdict receipt
        gate:sha256:<hex>       explicit verdict receipt
        artefact:<path>         tainted artefact (``artifact:`` accepted)
        incident:<case-id>      synthesised incident eval case
        replay                  chain-integrity localisation

    Args:
        spec: The raw ``--signal`` value.
        sdd_dir: The project ``.sdd`` directory (gate receipts under
            ``<sdd>/eval/gate``, lineage log under ``<sdd>/lineage``).
        workdir: Project root; defaults to ``sdd_dir.parent``. Used for the
            incident case corpus default location.
        cases_dir: Override for the incident case corpus (defaults to
            ``<workdir>/src/bernstein/eval/cases/incidents``).
        lineage_cards_dir: Agent-card directory for the lineage gate the
            artefact signal runs (defaults to ``<sdd>/agents``, matching
            ``bernstein audit taint``).
        lineage_operator_secret: Optional operator HMAC secret; when given
            the lineage gate also verifies each entry's ``operator_hmac``.

    Raises:
        DiagnoseError: The spec is malformed or its backing record is
            missing (fail closed; never a heuristic fallback).
    """
    resolved_workdir = workdir if workdir is not None else sdd_dir.parent
    kind, _, argument = spec.partition(":")
    kind = kind.strip().lower()
    if kind == "artifact":  # common alternate spelling
        kind = SIGNAL_KIND_ARTEFACT

    if kind == SIGNAL_KIND_REPLAY:
        if argument:
            raise DiagnoseError(f"--signal replay takes no argument (got {spec!r})")
        return replay_signal()
    if kind == SIGNAL_KIND_GATE:
        return gate_signal(argument or None, gate_dir=sdd_dir / "eval" / "gate")
    if kind == SIGNAL_KIND_ARTEFACT:
        if not argument:
            raise DiagnoseError("--signal artefact requires a path: artefact:<path>")
        return artefact_signal(
            argument,
            lineage_log=sdd_dir / "lineage" / "log.jsonl",
            cards_dir=lineage_cards_dir if lineage_cards_dir is not None else sdd_dir / "agents",
            operator_secret=lineage_operator_secret,
        )
    if kind == SIGNAL_KIND_INCIDENT:
        if not argument:
            raise DiagnoseError("--signal incident requires a case id: incident:<case-id>")
        resolved_cases = (
            cases_dir
            if cases_dir is not None
            else resolved_workdir / "src" / "bernstein" / "eval" / "cases" / "incidents"
        )
        return incident_signal(argument, cases_dir=resolved_cases)

    raise DiagnoseError(
        f"unknown --signal {spec!r}; expected gate[:RECEIPT_HASH], artefact:<path>, incident:<case-id>, or replay"
    )


__all__ = [
    "SIGNAL_KIND_ARTEFACT",
    "SIGNAL_KIND_GATE",
    "SIGNAL_KIND_INCIDENT",
    "SIGNAL_KIND_REPLAY",
    "artefact_signal",
    "gate_signal",
    "incident_signal",
    "predicate_from_params",
    "replay_signal",
    "resolve_signal",
]
