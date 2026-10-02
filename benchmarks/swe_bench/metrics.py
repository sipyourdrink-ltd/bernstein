"""Result types and aggregation for SWE-Bench evaluation runs."""

from __future__ import annotations

import json
import logging
import os
import statistics
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: What one instance did.
#:
#: ``abstained`` is not a flavour of ``failed``. A run that declines a task it
#: cannot verify, and says why, told the truth; a run that submits a wrong patch
#: did not. Scoring them the same rewards guessing, because a guess can only
#: raise the resolve rate and an abstention can only lower it (#5567).
InstanceStatus = Literal["resolved", "failed", "error", "skipped", "abstained"]
SummarySourceType = Literal["mock", "eval"]


@dataclass
class AgentTrace:
    """Execution record for one agent in a pipeline."""

    role: str
    model: str
    wall_time_s: float
    tokens_used: int
    cost_usd: float
    exit_code: int
    patch_produced: bool  # Whether this agent produced a non-empty patch


@dataclass
class InstanceResult:
    """Outcome for a single SWE-Bench instance under one scenario."""

    instance_id: str
    scenario_name: str
    status: InstanceStatus
    resolved: bool  # True iff all tests pass after applying the patch
    wall_time_s: float  # Total wall-clock time for the full pipeline
    total_tokens: int
    total_cost_usd: float
    agent_traces: list[AgentTrace] = field(default_factory=list)
    error_message: str = ""
    patch: str = ""  # Final unified diff applied to the repo
    #: Why the run declined, for `status == "abstained"` only.
    #:
    #: An abstention scores above a wrong answer, so it must cost something to
    #: claim: a reason makes the decision reviewable, and an unreasoned
    #: abstention is not one. Separate from `error_message`, which says the
    #: HARNESS broke; this says the run worked and declined to answer.
    abstention_reason: str = ""
    #: The run's *declared* probability that its patch resolves the instance,
    #: ∈ [0, 1], or ``None`` when the run declared nothing. Used by
    #: Brier-calibration scoring (#5923): ``mean((confidence - outcome)**2)``
    #: over evaluated instances that declared a confidence.
    #:
    #: ``None``, never a default value. A result written before this field
    #: existed made no prediction, and defaulting it to 1.0 would score that
    #: silence as maximal overconfidence -- a failed legacy result would
    #: contribute ``(1.0 - 0)**2 = 1.0``, the worst possible Brier term, from
    #: a run that never expressed a confidence. Calibration measures what the
    #: run said; a number it did not say is not data about it.
    confidence: float | None = None

    def __post_init__(self) -> None:
        # A probability outside [0, 1] is not one, and Brier does not notice:
        # a confidence of 5.0 on a failed instance yields a term of 25 and
        # drags the mean without erroring. Refuse it where it enters.
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            msg = f"confidence must be a probability in [0, 1], got {self.confidence!r}"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> InstanceResult:
        payload = dict(data)
        raw_traces = payload.pop("agent_traces", [])
        traces = [AgentTrace(**t) for t in raw_traces] if isinstance(raw_traces, list) else []  # type: ignore[arg-type]
        # Results written before #5567 have no abstention field.
        payload.setdefault("abstention_reason", "")
        # Results written before #5923 have no confidence field, and absence
        # is the honest value: they declared none. `None` deserialises them
        # identically to the old default without inventing a prediction.
        payload.setdefault("confidence", None)
        return cls(**payload, agent_traces=traces)  # type: ignore[arg-type]


@dataclass
class ScenarioSummary:
    """Aggregated metrics for one scenario across all evaluated instances."""

    scenario_name: str
    total_instances: int
    resolved: int
    failed: int
    errors: int
    skipped: int
    resolve_rate: float  # resolved / attempted, where attempted excludes skipped AND abstained
    mean_wall_time_s: float
    median_wall_time_s: float
    total_cost_usd: float
    mean_cost_per_instance_usd: float
    mean_tokens_per_instance: float
    #: Instances the run declined with a stated reason.
    abstained: int = 0
    #: abstained / taken_on -- how often the run said it could not tell.
    abstain_rate: float = 0.0
    #: wrong / (wrong + resolved): of the answers actually GIVEN, how many were
    #: wrong. The number an operator needs and the resolve rate cannot express,
    #: because a run can raise its resolve rate by guessing and this rate is what
    #: that costs.
    confident_error_rate: float = 0.0
    #: Weight of a wrong answer in :attr:`expected_value`:
    #: ``EV = (resolved - lambda * wrong) / attempted``. Operator-set risk appetite;
    #: higher lambda penalises confident wrong answers more heavily (#5923).
    lambda_penalty: float = 0.5
    #: ``(resolved - lambda_penalty * wrong) / attempted`` -- a single scalar
    #: reward that lets one run be ranked against another under a stated risk
    #: appetite. The three rates describe behaviour; this ranks it.
    expected_value: float = 0.0
    #: ``mean((confidence - outcome)**2)`` over evaluated instances (not skipped,
    #: not abstained, not errored). Measures how well the declared
    #: :pyattr:`InstanceResult.confidence` predicts the actual outcome.
    brier_score: float = 0.0
    verified: bool = False
    source_type: SummarySourceType = "mock"
    dataset: str = "princeton-nlp/SWE-bench_Lite"
    sample_size: int = 0
    run_at: str = ""
    commit_sha: str = ""
    scenarios: list[str] = field(default_factory=list)
    model_family: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def attempted_instances(self) -> int:
        """Instances the run gave an ANSWER for: not skipped, not abstained.

        An abstention is not an attempt at the task, it is a declared refusal to
        answer it, so counting it in the denominator would make declining look
        exactly like failing. Old summaries carry ``abstained: 0``, so this is
        the number it has always been for them.
        """
        return self.total_instances - self.skipped - self.abstained

    @property
    def taken_on_instances(self) -> int:
        """Instances the run did not skip -- attempts plus abstentions."""
        return self.total_instances - self.skipped

    @property
    def is_verified_public_result(self) -> bool:
        """Return whether this summary is safe for public benchmark claims."""
        return self.verified and self.source_type == "eval"

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ScenarioSummary:
        """Create a summary from serialized JSON with safe legacy defaults."""
        payload = dict(data)
        scenario_name = str(payload.get("scenario_name", ""))
        total_instances = _coerce_int(payload.get("total_instances", 0))

        # A bundle written before #5567 has no abstentions, so these defaults are
        # not merely safe, they are the correct values for it.
        payload.setdefault("abstained", 0)
        payload.setdefault("abstain_rate", 0.0)
        payload.setdefault("confident_error_rate", 0.0)
        # A summary written before #5923 has no EV/Brier fields.
        payload.setdefault("lambda_penalty", 0.5)
        payload.setdefault("expected_value", 0.0)
        payload.setdefault("brier_score", 0.0)
        payload.setdefault("verified", False)
        payload.setdefault("source_type", "mock")
        payload.setdefault("dataset", "princeton-nlp/SWE-bench_Lite")
        payload.setdefault("sample_size", total_instances)
        payload.setdefault("run_at", "")
        payload.setdefault("commit_sha", "")
        payload.setdefault("scenarios", [scenario_name] if scenario_name else [])
        payload.setdefault("model_family", "")
        payload.setdefault(
            "notes",
            "Legacy summary without provenance metadata. Treat as preview data, not a public benchmark claim.",
        )

        return cls(**payload)  # type: ignore[arg-type]


def _coerce_int(value: object) -> int:
    """Best-effort integer coercion for JSON payload values."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def aggregate(results: list[InstanceResult], *, lambda_penalty: float = 0.5) -> ScenarioSummary:
    """Compute summary statistics for a list of instance results.

    Args:
        results: The per-instance results to summarise.
        lambda_penalty: Weight of a wrong answer in :attr:`expected_value`,
            the operator's stated risk appetite. Ranking runs *under a stated
            appetite* is the point of the metric, so it is a parameter rather
            than a constant; the recorded value travels on the summary so a
            reader knows which appetite produced the number.
    """
    if not results:
        raise ValueError("Cannot aggregate empty results list")

    scenario_name = results[0].scenario_name
    total = len(results)
    resolved = sum(1 for r in results if r.resolved)
    failed = sum(1 for r in results if r.status == "failed" and not r.resolved)
    errors = sum(1 for r in results if r.status == "error")
    skipped = sum(1 for r in results if r.status == "skipped")
    abstained = sum(1 for r in results if r.status == "abstained" and not r.resolved)
    # An abstention is not an attempt: the run declined to answer, so it belongs
    # in neither half of the resolve rate. Counting it in the denominator alone
    # is what made declining score below guessing (#5567). For a run with no
    # abstentions -- every bundle written before this -- the number is unchanged.
    taken_on = total - skipped
    attempted = taken_on - abstained

    resolve_rate = resolved / attempted if attempted > 0 else 0.0
    abstain_rate = abstained / taken_on if taken_on > 0 else 0.0
    # Of the answers actually GIVEN, how many were wrong. `errors` is excluded on
    # both sides: a harness crash is not the run being confidently wrong, and
    # counting it as one would move this number for reasons the run did not
    # cause. Guessing on a task the run cannot verify raises `resolve_rate` at
    # best and raises THIS at worst, which is the trade the rate exists to show.
    answered = failed + resolved
    confident_error_rate = failed / answered if answered > 0 else 0.0

    wall_times = [r.wall_time_s for r in results if r.status not in ("skipped", "error")]
    mean_wall = statistics.mean(wall_times) if wall_times else 0.0
    median_wall = statistics.median(wall_times) if wall_times else 0.0

    total_cost = sum(r.total_cost_usd for r in results)
    mean_cost = total_cost / attempted if attempted > 0 else 0.0
    mean_tokens = (
        statistics.mean([r.total_tokens for r in results if r.status not in ("skipped", "error")])
        if wall_times
        else 0.0
    )

    # -- Expected value (#5923) ------------------------------------------------
    # EV = (resolved - lambda * wrong) / attempted. `wrong` is `failed`: a
    # harness error is not the run being confidently wrong, and counting it as
    # one would move EV for something the run did not cause. Same exclusion
    # rule as `confident_error_rate` above.
    expected_value = (resolved - lambda_penalty * failed) / attempted if attempted > 0 else 0.0

    # -- Brier calibration score (#5923) ---------------------------------------
    # mean((confidence - outcome)^2) over evaluated instances. Evaluated means
    # the run made a prediction: not skipped, not abstained, not a harness error,
    # and it declared a confidence. `outcome` is 1 when resolved, 0 otherwise.
    #
    # A result with no declared confidence leaves the mean entirely rather than
    # contributing an assumed one: Brier measures how well a declared
    # probability tracked reality, and there is nothing to score when none was
    # declared.
    evaluated = [r for r in results if r.status not in ("skipped", "error", "abstained") and r.confidence is not None]
    if evaluated:
        brier_score = statistics.mean(
            (r.confidence - (1.0 if r.resolved else 0.0)) ** 2 for r in evaluated if r.confidence is not None
        )
    else:
        brier_score = 0.0

    return ScenarioSummary(
        scenario_name=scenario_name,
        total_instances=total,
        resolved=resolved,
        failed=failed,
        errors=errors,
        skipped=skipped,
        resolve_rate=resolve_rate,
        abstained=abstained,
        abstain_rate=abstain_rate,
        confident_error_rate=confident_error_rate,
        lambda_penalty=lambda_penalty,
        expected_value=expected_value,
        brier_score=brier_score,
        mean_wall_time_s=mean_wall,
        median_wall_time_s=median_wall,
        total_cost_usd=total_cost,
        mean_cost_per_instance_usd=mean_cost,
        mean_tokens_per_instance=mean_tokens,
        sample_size=total,
        scenarios=[scenario_name],
    )


def _repair_torn_tail(path: Path) -> None:
    """Make the file end on a line boundary before anything is appended to it.

    Every complete write ends in ``\\n``, so a file whose last byte is anything
    else was cut off mid-append. Appending straight after that fuses the
    fragment and the new result into one invalid line. ``load`` then drops the
    result the resumed run just paid for, and the append after that pushes the
    bad line into the middle of the file, where ``load`` raises on every read.

    Two cases, decided the way ``load`` decides them. A tail that parses is a
    whole result that lost only its newline, and ``load`` already counts it, so
    it gets the newline. A tail that does not parse is the fragment ``load``
    already dropped, so it is cut back to the last newline. Either way the
    bytes on disk now say what the reader was already reporting.
    """
    if not path.exists():
        return
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            return
        f.seek(-1, os.SEEK_END)
        if f.read(1) == b"\n":
            return
        f.seek(0)
        data = f.read()
    keep = data.rfind(b"\n") + 1
    tail = data[keep:]
    try:
        json.loads(tail)
    except ValueError:
        with path.open("r+b") as f:
            f.truncate(keep)
            f.flush()
            os.fsync(f.fileno())
        logger.warning(
            "%s: removed a torn final line (%d bytes) before appending, left by an interrupted append.",
            path,
            len(tail),
        )
        return
    with path.open("ab") as f:
        f.write(b"\n")
        f.flush()
        os.fsync(f.fileno())


class ResultStore:
    """Persist and load per-instance results as JSONL files."""

    def __init__(self, results_dir: Path) -> None:
        self.results_dir = results_dir
        results_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, scenario_name: str) -> Path:
        return self.results_dir / f"{scenario_name}.jsonl"

    def append(self, result: InstanceResult) -> None:
        """Append one result to the scenario's JSONL file, durably.

        ``fsync`` because this file IS the resume point. Without it a finished
        instance can sit in the OS page cache when the process dies, and the
        next run re-evaluates it - which on SWE-Bench is a real model call and
        real money, paid again for work that was already done.
        """
        path = self._path_for(result.scenario_name)
        _repair_torn_tail(path)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result.to_dict()) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def load(self, scenario_name: str) -> list[InstanceResult]:
        """Load all results for a scenario.

        A torn FINAL line is dropped; a malformed line anywhere else raises.

        The distinction is the whole point. Appending is not atomic, so a
        process killed mid-write leaves a partial last line - and a bare
        ``json.loads`` over every line made that one fragment poison the file:
        ``already_evaluated`` raised, so a crashed benchmark run could not
        resume, and every instance it HAD completed was re-evaluated. That is
        the opposite of what this file is for.

        Skipping every unparseable line would trade that for something worse -
        silent data loss in the middle of a file, where corruption means
        something went wrong that nobody will now hear about. Only the last
        line can be torn by an interrupted append, so only the last line is
        forgiven.

        No tear can parse as an incomplete result: a proper prefix of a JSON
        object is never valid JSON, so a final line that parses is complete.

        ``load`` only reads. The torn bytes stay on disk until the next
        ``append`` removes them, which is what keeps the first resumed result
        from being written onto the end of the fragment.
        """
        path = self._path_for(scenario_name)
        if not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            # Every line this store writes is ASCII (``json.dumps`` escapes the
            # rest), so a tear cannot split a character. Bytes that do not
            # decode are corruption, and get the same contract as any other.
            raise ValueError(f"{path}: not valid UTF-8, so it is corrupt rather than torn.") from exc
        lines = [line.strip() for line in text.splitlines()]
        populated = [(index, line) for index, line in enumerate(lines) if line]
        results: list[InstanceResult] = []
        for position, (index, line) in enumerate(populated):
            try:
                data: dict[str, object] = json.loads(line)
            except ValueError as exc:
                is_last = position == len(populated) - 1
                if not is_last:
                    raise ValueError(
                        f"{path}: line {index + 1} is not valid JSON, and it is not the last line. "
                        "A torn final line is a crash artefact; one in the middle is corruption, "
                        "and dropping it would lose a completed result silently."
                    ) from exc
                logger.warning(
                    "%s: dropping a torn final line (%d chars) - an append was interrupted. "
                    "The %d complete result(s) before it are intact.",
                    path,
                    len(line),
                    len(results),
                )
                break
            results.append(InstanceResult.from_dict(data))
        return results

    def load_all(self) -> dict[str, list[InstanceResult]]:
        """Load results for all scenarios present in results_dir."""
        out: dict[str, list[InstanceResult]] = {}
        for path in sorted(self.results_dir.glob("*.jsonl")):
            scenario_name = path.stem
            out[scenario_name] = self.load(scenario_name)
        return out

    def already_evaluated(self, scenario_name: str, instance_id: str) -> bool:
        """Check whether an instance has already been evaluated (for resumption)."""
        return any(result.instance_id == instance_id for result in self.load(scenario_name))

    def save_summary(self, summary: ScenarioSummary) -> Path:
        """Write scenario summary to a JSON file and return the path."""
        path = self.results_dir / f"{summary.scenario_name}_summary.json"
        path.write_text(json.dumps(summary.to_dict(), indent=2), encoding="utf-8")
        return path
