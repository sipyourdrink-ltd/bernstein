"""Maker-checker and judge-panel gates whose verdict is a signed record.

Issue #2294. Bernstein has phase gates, but a gate decision historically left
no attestable record of what the checker or panel actually saw. This module
makes each gate a deterministic step whose verdict is a *signed adjudication
record* anchored to the run's lineage spine (the journal-anchored audit chain):

    {inputs_hash, rubric_hash, panel_config, per_judge_verdict, final_verdict}

The record's canonical bytes are the artefact the spine hashes, so the record's
``journal_entry_hash`` is the spine entry hash over exactly those bytes -- its
chain-verifiable identity. ``verify_adjudication`` recomputes ``inputs_hash``
from the claimed inputs and confirms the recorded panel saw exactly those
inputs, then confirms the record is still anchored in a spine that itself
verifies.

Independence is enforced structurally: a panel whose judges share
model+temp+prompt (the same ``identity_hash``) is rejected as non-independent
so a panel cannot agree on the same error. Maker-checker is the two-role
special case; N-judge panels aggregate by majority.

A cheap-maker / capable-checker cost mode reuses the existing spend ledger for
per-role cost attribution.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import LineageSpine, content_hash_of

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.cost.spend_ledger import SpendLedger

#: Run id under which panel-independent adjudication spines are keyed when the
#: caller anchors to a shared review spine rather than a per-run journal.
_ADJUDICATION_ACTOR = "adjudication"
_ADJUDICATION_MODEL = ""

#: Repo-relative POSIX subpath the record artefact is anchored under.
_RECORD_SUBPATH = (".sdd", "adjudication", "records")


# ---------------------------------------------------------------------------
# Verdict + judge model
# ---------------------------------------------------------------------------


class Verdict(StrEnum):
    """Terminal gate verdict."""

    PASS = "pass"
    FAIL = "fail"


class PanelMode(StrEnum):
    """Panel coordination mode."""

    #: N independent judges, aggregated by majority (ties fail closed).
    PANEL = "panel"
    #: Two roles: a cheap maker and a capable checker. The checker vetoes.
    MAKER_CHECKER = "maker_checker"


class AdjudicationClass(StrEnum):
    """Independence classification of an adjudication record."""

    #: Panel judges are strictly independent from the producing identity.
    INDEPENDENT = "independent"
    #: Panel contains a judge sharing model/identity with the producing identity.
    WEAK = "weak"
    #: Record carries no producing identity (legacy/unattributed).
    UNATTRIBUTED = "unattributed"
    #: Producing identity is empty or unresolved.
    UNRESOLVED = "unresolved"


def _canonical_json(value: Any) -> bytes:
    """Serialise *value* to canonical JSON bytes (sorted keys, tight separators)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256_of(value: Any) -> str:
    """Return the ``sha256:``-prefixed digest of *value*'s canonical JSON."""
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class ProducerIdentity:
    """Identity of the producer (agent/model/run) whose work is being adjudicated."""

    model: str
    temperature: float = 0.0
    prompt_hash: str = ""
    served_model: str = ""

    def identity_hash(self) -> str:
        """Return identity digest over model + served_model + temp + prompt_hash."""
        return _sha256_of(
            {
                "model": self.model,
                "prompt_hash": self.prompt_hash,
                "served_model": self.served_model,
                "temperature": self.temperature,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "prompt_hash": self.prompt_hash,
        }
        if self.served_model:
            d["served_model"] = self.served_model
        return d


def _coerce_producer(producer: ProducerIdentity | JudgeConfig | dict[str, Any] | str | None) -> ProducerIdentity | None:
    """Coerce various producer representations into a :class:`ProducerIdentity`."""
    if producer is None:
        return None
    if isinstance(producer, ProducerIdentity):
        return producer
    if isinstance(producer, JudgeConfig):
        return ProducerIdentity(
            model=producer.model,
            temperature=producer.temperature,
            prompt_hash=producer.prompt_hash,
        )
    if isinstance(producer, str):
        return ProducerIdentity(model=producer)
    if isinstance(producer, dict):
        return ProducerIdentity(
            model=str(producer.get("model", "")),
            temperature=float(producer.get("temperature", 0.0)),
            prompt_hash=str(producer.get("prompt_hash", "")),
            served_model=str(producer.get("served_model", "")),
        )
    return None


def _classify_adjudication(panel: PanelConfig, producer: ProducerIdentity | None) -> AdjudicationClass:
    """Classify the independence of a panel relative to the producing identity."""
    if producer is None:
        return AdjudicationClass.UNATTRIBUTED
    if (
        not producer.model.strip()
        or producer.model.strip().lower() == "unresolved"
        or producer.served_model.strip().lower() == "unresolved"
    ):
        return AdjudicationClass.UNRESOLVED

    producer_ident = producer.identity_hash()
    p_model = producer.model.strip().lower()
    p_served = producer.served_model.strip().lower() if producer.served_model else ""

    for judge in panel.judges:
        if judge.identity_hash() == producer_ident:
            return AdjudicationClass.WEAK
        j_model = judge.model.strip().lower()
        if j_model == p_model:
            return AdjudicationClass.WEAK
        if p_served and j_model == p_served:
            return AdjudicationClass.WEAK

    return AdjudicationClass.INDEPENDENT


@dataclass(frozen=True, slots=True)
class JudgeConfig:
    """One judge's configuration.

    Two judges are *independent* only if they differ on at least one of
    ``model`` / ``temperature`` / ``prompt_hash``; :meth:`identity_hash`
    collapses the three into one value used for the independence check.

    Attributes:
        model: Model identifier the judge runs on.
        temperature: Sampling temperature.
        prompt_hash: Stable identifier of the judge's prompt (a hash or a
            free-form label; only equality matters for independence).
    """

    model: str
    temperature: float
    prompt_hash: str

    def identity_hash(self) -> str:
        """Return the independence identity: ``H(model, temperature, prompt_hash)``."""
        return _sha256_of(
            {
                "model": self.model,
                "temperature": self.temperature,
                "prompt_hash": self.prompt_hash,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "temperature": self.temperature,
            "prompt_hash": self.prompt_hash,
        }


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """One judge's verdict.

    Attributes:
        config: The judge configuration that produced the verdict.
        verdict: The judge's terminal verdict.
        rationale_hash: Stable hash of the judge's rationale (never the raw
            rationale text -- the record carries hashes only).
    """

    config: JudgeConfig
    verdict: Verdict
    rationale_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "verdict": self.verdict.value,
            "rationale_hash": self.rationale_hash,
        }


class PanelIndependenceError(ValueError):
    """Raised when a panel's judges are not genuinely independent."""


@dataclass(frozen=True, slots=True)
class PanelConfig:
    """A judge panel with structural independence enforcement.

    Construction rejects a panel whose judges share an ``identity_hash``
    (same model + temperature + prompt), so a panel cannot silently agree
    on the same error (AC2).

    Attributes:
        judges: Ordered tuple of judge configurations (at least two).
        mode: Coordination mode (panel majority or maker-checker veto).
    """

    judges: tuple[JudgeConfig, ...]
    mode: PanelMode = PanelMode.PANEL

    def __post_init__(self) -> None:
        if isinstance(self.mode, str) and not isinstance(self.mode, PanelMode):
            object.__setattr__(self, "mode", PanelMode(self.mode))
        if len(self.judges) < 2:
            raise PanelIndependenceError("a panel requires at least two judges")
        seen: set[str] = set()
        for judge in self.judges:
            ident = judge.identity_hash()
            if ident in seen:
                raise PanelIndependenceError(
                    "non-independent panel: two judges share model+temperature+prompt; "
                    "vary at least one axis so the panel cannot agree on the same error"
                )
            seen.add(ident)

    def config_hash(self) -> str:
        """Return the ``sha256:``-prefixed hash of the panel configuration."""
        return _sha256_of(
            {
                "mode": self.mode.value,
                "judges": [j.to_dict() for j in self.judges],
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "judges": [j.to_dict() for j in self.judges],
        }


# ---------------------------------------------------------------------------
# Adjudication record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdjudicationRecord:
    """A signed gate verdict binding inputs, rubric, panel, and verdict.

    ``journal_entry_hash`` anchors the record in the lineage spine over the
    record's own canonical bytes (excluding the anchor). The record is the
    primary artefact: it is meaningless without the spine anchor.

    Attributes:
        run_id: The run whose journal the record anchors to.
        inputs_hash: ``sha256:`` hash of the canonical inputs the panel saw.
        rubric_hash: ``sha256:`` hash of the canonical rubric.
        panel_config: The panel configuration serialised into the record.
        per_judge_verdict: One entry per judge, in panel declaration order.
        final_verdict: The aggregated terminal verdict.
        timestamp: Integer timestamp (stable across replays of a fixture).
        journal_entry_hash: Spine anchor over the record's canonical bytes.
        produced_by: Producing agent/model/identity dictionary, if known.
        adjudication_class: Independence classification of the adjudication.
    """

    run_id: str
    inputs_hash: str
    rubric_hash: str
    panel_config: dict[str, Any]
    per_judge_verdict: tuple[dict[str, Any], ...]
    final_verdict: Verdict
    timestamp: int
    journal_entry_hash: str = ""
    produced_by: dict[str, Any] | None = None
    adjudication_class: str = ""

    def _binding(self) -> dict[str, Any]:
        """Return the anchor-free binding (the bytes the spine hashes)."""
        d: dict[str, Any] = {
            "run_id": self.run_id,
            "inputs_hash": self.inputs_hash,
            "rubric_hash": self.rubric_hash,
            "panel_config": self.panel_config,
            "per_judge_verdict": list(self.per_judge_verdict),
            "final_verdict": self.final_verdict.value,
            "timestamp": self.timestamp,
        }
        if self.produced_by is not None:
            if self.adjudication_class:
                d["adjudication_class"] = self.adjudication_class
            d["produced_by"] = self.produced_by
        return d

    def to_canonical_bytes(self) -> bytes:
        """Serialise the anchor-free binding to canonical JSON bytes."""
        return _canonical_json(self._binding())

    def to_dict(self) -> dict[str, Any]:
        """Serialise the full record (binding + anchor)."""
        return self._binding() | {"journal_entry_hash": self.journal_entry_hash}


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _aggregate(mode: PanelMode, judges: tuple[JudgeConfig, ...], verdicts: tuple[JudgeVerdict, ...]) -> Verdict:
    """Aggregate per-judge verdicts into the panel's terminal verdict.

    * ``MAKER_CHECKER``: the checker (last judge) vetoes -- any FAIL fails.
    * ``PANEL``: strict majority PASS passes; ties or majority FAIL fail
      closed. A gate that cannot muster a majority to pass does not pass.
    """
    if mode is PanelMode.MAKER_CHECKER:
        return Verdict.PASS if all(v.verdict is Verdict.PASS for v in verdicts) else Verdict.FAIL
    passes = sum(1 for v in verdicts if v.verdict is Verdict.PASS)
    return Verdict.PASS if passes * 2 > len(verdicts) else Verdict.FAIL


def _safe_name(run_id: str) -> str:
    """Return a filesystem-safe artefact name for *run_id*."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in run_id) or "run"


# ---------------------------------------------------------------------------
# Adjudicate (AC1, AC4, AC5)
# ---------------------------------------------------------------------------


def adjudicate(
    *,
    run_id: str,
    lineage_root: Path,
    hmac_key: bytes,
    inputs: Any,
    rubric: Any,
    panel: PanelConfig,
    judge_verdicts: tuple[JudgeVerdict, ...],
    now: int,
    produced_by: ProducerIdentity | JudgeConfig | dict[str, Any] | str | None = None,
) -> AdjudicationRecord:
    """Bind inputs + rubric + panel + verdicts into an anchored, signed record.

    The panel's judges are validated for independence at :class:`PanelConfig`
    construction; the supplied ``judge_verdicts`` must align with the panel's
    judges in declaration order. The record's canonical bytes are anchored in
    the lineage spine, so the returned record's ``journal_entry_hash`` is the
    spine entry hash over exactly those bytes (AC4). Two byte-identical fixture
    runs produce byte-identical records and anchors (AC5).

    Args:
        run_id: The run whose journal the record anchors to.
        lineage_root: Spine root (``.sdd/lineage``); the per-run dir lives
            beneath it.
        hmac_key: Audit-chain HMAC key that tags spine entries.
        inputs: The inputs the panel saw (hashed into ``inputs_hash``).
        rubric: The rubric the panel applied (hashed into ``rubric_hash``).
        panel: The independent panel configuration.
        judge_verdicts: Per-judge verdicts in panel declaration order.
        now: Integer timestamp; recorded and used as the spine timestamp.
        produced_by: Identity of the agent/model that produced the work being
            adjudicated.

    Returns:
        The anchored :class:`AdjudicationRecord`.

    Raises:
        ValueError: When ``judge_verdicts`` does not match the panel's judges.
    """
    if len(judge_verdicts) != len(panel.judges):
        raise ValueError("judge_verdicts count does not match the panel's judge count")
    for judge, verdict in zip(panel.judges, judge_verdicts, strict=True):
        if verdict.config.identity_hash() != judge.identity_hash():
            raise ValueError("judge_verdicts are not aligned with the panel's judges in declaration order")

    producer = _coerce_producer(produced_by)
    producer_dict = producer.to_dict() if producer is not None else None
    adj_class = _classify_adjudication(panel, producer) if producer is not None else None
    adj_class_str = adj_class.value if adj_class is not None else ""

    final = _aggregate(panel.mode, panel.judges, judge_verdicts)
    record = AdjudicationRecord(
        run_id=run_id,
        inputs_hash=_sha256_of(inputs),
        rubric_hash=_sha256_of(rubric),
        panel_config=panel.to_dict(),
        per_judge_verdict=tuple(v.to_dict() for v in judge_verdicts),
        final_verdict=final,
        timestamp=now,
        produced_by=producer_dict,
        adjudication_class=adj_class_str,
    )

    spine = LineageSpine(lineage_root, run_id=run_id, hmac_key=hmac_key)
    artifact_name = f"{_safe_name(run_id)}-{record.inputs_hash[7:23]}.json"
    artifact_path = "/".join((*_RECORD_SUBPATH, artifact_name))
    anchor = spine.record(
        artifact_path=artifact_path,
        content=record.to_canonical_bytes(),
        actor=_ADJUDICATION_ACTOR,
        step_id=record.inputs_hash,
        model=_ADJUDICATION_MODEL,
        timestamp=now,
    )
    anchored = AdjudicationRecord(
        run_id=record.run_id,
        inputs_hash=record.inputs_hash,
        rubric_hash=record.rubric_hash,
        panel_config=record.panel_config,
        per_judge_verdict=record.per_judge_verdict,
        final_verdict=record.final_verdict,
        timestamp=record.timestamp,
        journal_entry_hash=anchor,
        produced_by=record.produced_by,
        adjudication_class=record.adjudication_class,
    )
    # Persist the full record (binding + anchor) for offline `gate verify`.
    out_dir = records_dir(lineage_root, run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / artifact_name).write_text(
        _canonical_json(anchored.to_dict()).decode("utf-8"),
        encoding="utf-8",
    )
    return anchored


def records_dir(lineage_root: Path, run_id: str) -> Path:
    """Return the directory holding persisted adjudication records for *run_id*.

    Colocated with the run's spine dir so the record and its anchor share one
    root; the spine validates *run_id* against traversal at record time.
    """
    return lineage_root / run_id / "adjudication_records"


def read_records(lineage_root: Path, run_id: str) -> list[AdjudicationRecord]:
    """Load every persisted adjudication record for *run_id* (append order)."""
    out_dir = records_dir(lineage_root, run_id)
    if not out_dir.is_dir():
        return []
    records: list[AdjudicationRecord] = []
    for path in sorted(out_dir.glob("*.json")):
        record = read_record(path)
        if record is not None:
            records.append(record)
    return records


# ---------------------------------------------------------------------------
# Verify (AC2, AC3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdjudicationVerifyResult:
    """Outcome of :func:`verify_adjudication`."""

    ok: bool
    reason: str = ""
    adjudication_class: str = ""


def verify_adjudication(
    *,
    run_id: str,
    lineage_root: Path,
    hmac_key: bytes,
    record: AdjudicationRecord,
    claimed_inputs: Any,
) -> AdjudicationVerifyResult:
    """Confirm the panel saw exactly *claimed_inputs* and the record is anchored.

    Recomputes, from the record and the claimed inputs alone:

    * ``inputs_hash`` over ``claimed_inputs`` equals the record's
      ``inputs_hash`` -- so a tampered claim is detected (AC3);
    * the panel config is independent (no two judges share an identity);
    * the record is still anchored in a spine that itself verifies, and the
      recorded ``journal_entry_hash`` matches the spine anchor over the
      record's canonical bytes.

    A single-byte edit to the record, the inputs, or the spine fails the check.
    """
    recomputed = _sha256_of(claimed_inputs)
    if recomputed != record.inputs_hash:
        return AdjudicationVerifyResult(
            ok=False,
            reason="recomputed inputs_hash does not match the recorded inputs_hash (claimed inputs differ)",
        )

    # Reconstruct the panel from the record to re-run the independence check.
    try:
        panel = _panel_from_dict(record.panel_config)
    except PanelIndependenceError as exc:
        return AdjudicationVerifyResult(ok=False, reason=f"panel is not independent: {exc}")

    spine = LineageSpine(lineage_root, run_id=run_id, hmac_key=hmac_key)
    spine_result = spine.verify()
    if not spine_result.ok:
        return AdjudicationVerifyResult(
            ok=False,
            reason=f"adjudication spine failed verification ({spine_result.status.value})",
        )

    want = content_hash_of(record.to_canonical_bytes())
    anchor: str | None = None
    for entry in spine.iter_entries():
        if entry.content_hash == want:
            anchor = entry.entry_hash
            break
    if anchor is None:
        return AdjudicationVerifyResult(ok=False, reason="record is not anchored in the adjudication spine")
    if anchor != record.journal_entry_hash:
        return AdjudicationVerifyResult(
            ok=False,
            reason="recorded journal_entry_hash does not match the spine anchor over the record bytes",
        )

    adj_class = record.adjudication_class or (
        _classify_adjudication(panel, _coerce_producer(record.produced_by)).value
        if record.produced_by is not None
        else AdjudicationClass.UNATTRIBUTED.value
    )
    return AdjudicationVerifyResult(ok=True, adjudication_class=adj_class)


def _panel_from_dict(panel_config: dict[str, Any]) -> PanelConfig:
    """Rebuild a :class:`PanelConfig` from a serialised record (re-validates)."""
    judges = tuple(
        JudgeConfig(
            model=str(j["model"]),
            temperature=float(j["temperature"]),
            prompt_hash=str(j["prompt_hash"]),
        )
        for j in panel_config.get("judges", [])
    )
    mode = PanelMode(str(panel_config.get("mode", PanelMode.PANEL.value)))
    return PanelConfig(judges=judges, mode=mode)


def read_record(path: Path) -> AdjudicationRecord | None:
    """Load an :class:`AdjudicationRecord` from a JSON file, or ``None``."""
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        produced_by = raw.get("produced_by")
        if produced_by is not None and not isinstance(produced_by, dict):
            produced_by = None
        adj_class = str(raw.get("adjudication_class", ""))
        return AdjudicationRecord(
            run_id=str(raw["run_id"]),
            inputs_hash=str(raw["inputs_hash"]),
            rubric_hash=str(raw["rubric_hash"]),
            panel_config=dict(raw["panel_config"]),
            per_judge_verdict=tuple(raw["per_judge_verdict"]),
            final_verdict=Verdict(str(raw["final_verdict"])),
            timestamp=int(raw["timestamp"]),
            journal_entry_hash=str(raw.get("journal_entry_hash", "")),
            produced_by=produced_by,
            adjudication_class=adj_class,
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Cost mode (cheap-maker / capable-checker)
# ---------------------------------------------------------------------------


class CostMode(StrEnum):
    """Cost-attribution mode for adjudication runs."""

    #: A cheap model makes the first pass; a capable model checks it.
    CHEAP_MAKER_CAPABLE_CHECKER = "cheap_maker_capable_checker"
    #: All judges are attributed under a single panel label.
    UNIFORM_PANEL = "uniform_panel"


#: Ledger feature labels for per-role cost attribution.
FEATURE_MAKER = "adjudication.maker"
FEATURE_CHECKER = "adjudication.checker"
FEATURE_PANEL = "adjudication.panel"


def attribute_cost(
    *,
    ledger: SpendLedger,
    mode: CostMode,
    maker_model: str,
    maker_cost_usd: float,
    checker_model: str,
    checker_cost_usd: float,
    task_id: str = "",
) -> None:
    """Attribute maker and checker spend to the shared cost ledger.

    Reuses the existing spend ledger so a cheap-maker / capable-checker gate's
    cost is attributable per role (``adjudication.maker`` /
    ``adjudication.checker``) rather than lumped into one bucket.

    Args:
        ledger: The run's spend ledger.
        mode: Cost-attribution mode (only the two-role mode splits labels).
        maker_model: Model the maker ran on.
        maker_cost_usd: Maker spend in USD.
        checker_model: Model the checker ran on.
        checker_cost_usd: Checker spend in USD.
        task_id: Task id for the ledger rollup.
    """
    from bernstein.core.cost.spend_ledger import CallTags

    maker_label = FEATURE_MAKER if mode is CostMode.CHEAP_MAKER_CAPABLE_CHECKER else FEATURE_PANEL
    checker_label = FEATURE_CHECKER if mode is CostMode.CHEAP_MAKER_CAPABLE_CHECKER else FEATURE_PANEL
    ledger.record(
        tags=CallTags(task_id=task_id, role="maker", feature_label=maker_label),
        model=maker_model,
        cost_usd=maker_cost_usd,
    )
    ledger.record(
        tags=CallTags(task_id=task_id, role="checker", feature_label=checker_label),
        model=checker_model,
        cost_usd=checker_cost_usd,
    )


__all__ = [
    "FEATURE_CHECKER",
    "FEATURE_MAKER",
    "FEATURE_PANEL",
    "AdjudicationClass",
    "AdjudicationRecord",
    "AdjudicationVerifyResult",
    "CostMode",
    "JudgeConfig",
    "JudgeVerdict",
    "PanelConfig",
    "PanelIndependenceError",
    "PanelMode",
    "ProducerIdentity",
    "Verdict",
    "adjudicate",
    "attribute_cost",
    "read_record",
    "read_records",
    "records_dir",
    "verify_adjudication",
]
