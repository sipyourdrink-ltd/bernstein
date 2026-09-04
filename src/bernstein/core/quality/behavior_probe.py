"""Boundary-input probing of the callables a diff changed.

Every other verification surface in the pipeline *reads* a diff: the review
rubric, intent verification and the cross-model check are model reads of the
diff text, and the generated-integration-test lane writes one happy-path test.
None of them feed the changed code an input the worker did not choose, so a
function that mishandles the empty list, zero, or the empty string reaches the
reviewer through the same channel that already missed it.

This module derives inputs from the changed surface itself and runs them:

1. :func:`derive_probe_plan` walks the changed files with :mod:`ast`, keeps the
   public callables the diff touched, and turns each parameter annotation into
   a fixed, documented list of boundary values. No model is involved and no
   randomness is used - the derivation is a total order over the signature, so
   the same files at the same content yield a byte-identical plan and a stable
   ``set_hash``.
2. :func:`probe_changed_surface` executes one probe per subprocess in the
   worktree and classifies the outcome.

The claim is deliberately narrow and objectively decidable: the gate finds
**crash-level** regressions, not semantic ones. A probe is red when the
callable raises an exception its own docstring does not document, when it
returns a value whose type contradicts its return annotation, or when it does
not return inside the per-callable budget. It never asserts what the callable
*should* have computed.

Anything the deriver could not turn into inputs is recorded in
:attr:`ProbePlan.unprobed` with a reason code from :data:`UNPROBED_REASONS` -
absence is explicit, never silent.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import itertools
import json
import logging
import os
import re
import shlex
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Version tag written into every plan document. Bump it when the derivation
#: changes shape, so an old receipt is never mistaken for a replay of a new one.
PLAN_VERSION = 1

#: Closed set of reasons a callable on the changed surface was not probed.
#: A callable is either in :attr:`ProbePlan.callables` or carries one of these.
UNPROBED_REASONS: frozenset[str] = frozenset(
    {
        # A parameter carries no annotation, so no boundary values exist for it.
        "missing-annotation",
        # A parameter (or the whole signature) uses an annotation outside the
        # supported boundary vocabulary - see ``_BOUNDARY_VALUES``.
        "unsupported-annotation",
        # Defined in a class body and needs an instance the deriver cannot build.
        "receiver-required",
        # Declared ``async def``; awaiting it is out of scope for this gate.
        "async-callable",
        # Takes ``*args`` or ``**kwargs``; the arity is not fixed by the signature.
        "variadic-parameter",
        # Takes a keyword-only parameter without a default.
        "keyword-only-parameter",
        # The file could not be parsed as Python.
        "unparsable-module",
        # Dropped because ``max_callables`` was already reached.
        "cap-exceeded",
    }
)

#: Outcome vocabulary for a single executed probe.
ProbeVerdict = Literal["ok", "tolerated", "crash", "contract", "timeout", "unloadable"]

#: Verdicts that make the gate red.
_FAILING_VERDICTS: frozenset[str] = frozenset({"crash", "contract", "timeout"})

_LONG_STRING = "x" * 1024
_LARGE_COLLECTION_SIZE = 64

# Boundary values per supported scalar annotation, in the order they are
# probed. Ordering is part of the contract: a plan is replayable only because
# nothing here is sampled.
_BOUNDARY_VALUES: dict[str, tuple[Any, ...]] = {
    "int": (0, -1, 2**31 - 1),
    "float": (0.0, -1.0, 1e308),
    "str": ("", " ", _LONG_STRING),
    "bool": (False, True),
}

# Type names accepted for a return annotation, keyed by the name of the type
# the annotation resolves to. ``bool`` is an ``int`` and an ``int`` is accepted
# where a ``float`` is annotated, per the numeric tower.
_RETURN_TOLERANCE: dict[str, frozenset[str]] = {
    "int": frozenset({"int", "bool"}),
    "float": frozenset({"float", "int", "bool"}),
    "bool": frozenset({"bool"}),
    "str": frozenset({"str"}),
    "list": frozenset({"list"}),
    "dict": frozenset({"dict"}),
    "NoneType": frozenset({"NoneType"}),
}

_RAISES_HEADER = re.compile(r"^\s*Raises\s*:\s*$")
_RAISES_ENTRY = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.]*)\s*:")
_REST_RAISES = re.compile(r":raises\s+([A-Za-z_][A-Za-z0-9_.]*)\s*:")

_CHILD_SOURCE = """\
import importlib.util
import json
import sys


def _report(payload):
    sys.stdout.write(json.dumps(payload, sort_keys=True))


def _main():
    job = json.loads(sys.stdin.read())
    try:
        spec = importlib.util.spec_from_file_location("_bernstein_probe_target", job["file"])
        if spec is None or spec.loader is None:
            raise ImportError("no loader for " + job["file"])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        target = module
        for part in job["qualname"].split("."):
            target = getattr(target, part)
    except BaseException as exc:
        _report({"outcome": "unloadable", "exception": type(exc).__name__, "message": str(exc)[:400]})
        return
    try:
        value = target(*job["arguments"])
    except BaseException as exc:
        _report({"outcome": "raised", "exception": type(exc).__name__, "message": str(exc)[:400]})
        return
    _report({"outcome": "returned", "return_type": type(value).__name__})


_main()
"""


@dataclass(frozen=True)
class BehaviorProbeConfig:
    """Configuration for the behaviour-probe gate.

    Attributes:
        enabled: Master switch.
        python_command: Shell command that starts the interpreter used to run a
            probe. Split with :func:`shlex.split`; defaults to the interpreter
            running the gate.
        per_callable_timeout_s: Wall-clock budget for one probe subprocess.
        gate_timeout_s: Wall-clock budget for the whole gate.
        max_callables: Upper bound on probed callables per run; the rest are
            recorded ``cap-exceeded``.
        max_probes_per_callable: Upper bound on probes derived for one callable.
        max_parallel_probes: How many probe subprocesses may run at once.
    """

    enabled: bool = False
    python_command: str = sys.executable
    per_callable_timeout_s: int = 15
    gate_timeout_s: int = 300
    max_callables: int = 12
    max_probes_per_callable: int = 6
    max_parallel_probes: int = 4


@dataclass(frozen=True)
class ProbeTarget:
    """One callable on the changed surface and the inputs derived for it.

    Attributes:
        module: Repository-relative path of the file defining the callable.
        qualname: Dotted name inside that module (``"Holder.helper"``).
        name: Short name of the callable.
        signature: Canonical rendering of the signature the probes came from.
        probes: Positional argument tuples, ordered smallest-encoding first.
        documented_exceptions: Exception names the docstring declares; raising
            one of them is tolerated rather than a crash.
        return_types: Type names accepted for the return annotation; empty when
            the annotation carries no decidable type.
    """

    module: str
    qualname: str
    name: str
    signature: str
    probes: tuple[tuple[Any, ...], ...]
    documented_exceptions: frozenset[str] = frozenset()
    return_types: frozenset[str] = frozenset()


@dataclass(frozen=True)
class UnprobedEntry:
    """A callable the deriver refused to probe, and why.

    Attributes:
        module: Repository-relative path of the file defining the callable.
        qualname: Dotted name inside that module.
        reason: Code drawn from :data:`UNPROBED_REASONS`.
    """

    module: str
    qualname: str
    reason: str

    def __post_init__(self) -> None:
        """Hold the reason to the closed set."""
        if self.reason not in UNPROBED_REASONS:
            raise ValueError(
                f"UnprobedEntry.reason={self.reason!r} is not in the closed set "
                f"UNPROBED_REASONS={sorted(UNPROBED_REASONS)} (qualname={self.qualname!r})"
            )


@dataclass(frozen=True)
class ProbePlan:
    """The probe set derived from a changed surface.

    Attributes:
        callables: Probed callables, in derivation order.
        unprobed: Callables that were skipped, each with a reason code.
        set_hash: SHA-256 of :meth:`document`; the identity of this probe set.
    """

    callables: tuple[ProbeTarget, ...]
    unprobed: tuple[UnprobedEntry, ...]
    set_hash: str

    def document(self) -> str:
        """Return the canonical JSON the ``set_hash`` is taken over."""
        return _plan_document(self.callables, self.unprobed)


@dataclass(frozen=True)
class ProbeOutcome:
    """What one probe did when it ran.

    Attributes:
        module: Repository-relative path of the file defining the callable.
        qualname: Dotted name of the probed callable.
        arguments: The positional arguments it was called with.
        outcome: Verdict from :data:`ProbeVerdict`.
        exception: Exception type name when one was raised; empty otherwise.
        detail: Short human-readable note (exception message, observed type).
    """

    module: str
    qualname: str
    arguments: list[Any]
    outcome: ProbeVerdict
    exception: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form used in the receipt."""
        return {
            "arguments": self.arguments,
            "detail": self.detail,
            "exception": self.exception,
            "module": self.module,
            "outcome": self.outcome,
            "qualname": self.qualname,
        }

    def render_call(self) -> str:
        """Render the probe as the call that produced it."""
        rendered = ", ".join(json.dumps(argument, sort_keys=True) for argument in self.arguments)
        return f"{self.qualname}({rendered})"


@dataclass(frozen=True)
class ProbeReceipt:
    """What the diff was probed with, and what came back.

    Attributes:
        probe_set_hash: :attr:`ProbePlan.set_hash` of the executed plan.
        outcomes: Every executed probe, ordered canonically.
        unprobed: The plan's unprobed entries, carried through.
        minimal_failing: The smallest failing probe, or ``None`` when green.
    """

    probe_set_hash: str
    outcomes: tuple[ProbeOutcome, ...]
    unprobed: tuple[UnprobedEntry, ...] = ()
    minimal_failing: ProbeOutcome | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form attached to the gate result."""
        return {
            "minimal_failing": self.minimal_failing.to_dict() if self.minimal_failing else None,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "probe_set_hash": self.probe_set_hash,
            "unprobed": [
                {"module": entry.module, "qualname": entry.qualname, "reason": entry.reason} for entry in self.unprobed
            ],
            "version": PLAN_VERSION,
        }

    def document(self) -> str:
        """Return the canonical JSON of this receipt.

        Carries no timing and no absolute paths, so two runs of the same plan
        produce byte-identical documents.
        """
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass
class BehaviorProbeResult:
    """Verdict of one behaviour-probe run.

    Attributes:
        passed: Whether every executed probe was ``ok`` or ``tolerated``.
        status: ``"pass"``, ``"fail"``, or ``"inconclusive"`` when the gate
            could not evaluate anything it was given.
        detail: Human-readable summary naming the failing call when red.
        receipt: The probe receipt for this run.
        reason: Closed-set reason code, set only for ``"inconclusive"``.
    """

    passed: bool
    status: Literal["pass", "fail", "inconclusive"]
    detail: str
    receipt: ProbeReceipt
    reason: str | None = None


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def _encode(value: Any) -> str:
    """Return the canonical JSON encoding used for ordering and hashing."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _plan_document(callables: Sequence[ProbeTarget], unprobed: Sequence[UnprobedEntry]) -> str:
    """Return the canonical JSON document a plan hashes over."""
    payload = {
        "callables": [
            {
                "module": target.module,
                "probes": [list(probe) for probe in target.probes],
                "qualname": target.qualname,
                "signature": target.signature,
            }
            for target in callables
        ],
        "unprobed": [
            {"module": entry.module, "qualname": entry.qualname, "reason": entry.reason} for entry in unprobed
        ],
        "version": PLAN_VERSION,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _resolve_annotation(node: ast.expr | None) -> tuple[Any, ...] | None:
    """Return the boundary values for an annotation, or ``None`` if unsupported.

    Args:
        node: The annotation expression, or ``None`` when unannotated.

    Returns:
        A tuple of boundary values in probe order, or ``None`` when the
        annotation is outside the supported vocabulary.
    """
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        if node.value is None:
            return (None,)
        if isinstance(node.value, str):
            # A stringised annotation ("list[int]"); re-parse it.
            try:
                inner = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                return None
            return _resolve_annotation(inner)
        return None
    if isinstance(node, ast.Name):
        return _BOUNDARY_VALUES.get(node.id)
    if isinstance(node, ast.Attribute):
        return _BOUNDARY_VALUES.get(node.attr)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _merge_union(_resolve_annotation(node.left), _resolve_annotation(node.right))
    if isinstance(node, ast.Subscript):
        return _resolve_subscript(node)
    return None


def _merge_union(left: tuple[Any, ...] | None, right: tuple[Any, ...] | None) -> tuple[Any, ...] | None:
    """Concatenate two branches of a union annotation, dropping duplicates."""
    if left is None or right is None:
        return None
    merged: list[Any] = []
    seen: set[str] = set()
    for value in (*left, *right):
        encoded = _encode(value)
        if encoded not in seen:
            seen.add(encoded)
            merged.append(value)
    return tuple(merged)


def _subscript_base(node: ast.Subscript) -> str:
    """Return the lowercase name of a subscripted annotation's base."""
    base = node.value
    if isinstance(base, ast.Name):
        return base.id.lower()
    if isinstance(base, ast.Attribute):
        return base.attr.lower()
    return ""


def _resolve_subscript(node: ast.Subscript) -> tuple[Any, ...] | None:
    """Return boundary values for ``list[...]``, ``dict[...]`` or ``Optional[...]``."""
    base = _subscript_base(node)
    if base == "optional":
        return _merge_union(_resolve_annotation(node.slice), (None,))
    if base in {"list", "sequence", "iterable"}:
        element = _resolve_annotation(node.slice)
        if element is None:
            return None
        first = element[0]
        return ([], [first], [first] * _LARGE_COLLECTION_SIZE)
    if base in {"dict", "mapping"}:
        if not isinstance(node.slice, ast.Tuple) or len(node.slice.elts) != 2:
            return None
        key = _resolve_annotation(node.slice.elts[0])
        value = _resolve_annotation(node.slice.elts[1])
        if key is None or value is None:
            return None
        return ({}, {_dict_key(key[0]): value[0]})
    return None


def _dict_key(value: Any) -> str:
    """Return a JSON-object key for a derived dictionary boundary value."""
    return value if isinstance(value, str) else _encode(value)


def _return_type_names(node: ast.expr | None) -> frozenset[str]:
    """Return the type names a return annotation permits, or an empty set."""
    values = _resolve_annotation(node)
    if values is None:
        return frozenset()
    names: set[str] = set()
    for value in values:
        observed = type(value).__name__
        names |= _RETURN_TOLERANCE.get(observed, frozenset({observed}))
    return frozenset(names)


def _documented_exceptions(node: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """Return the exception names the callable's docstring declares.

    Both the Google-style ``Raises:`` block and the reStructuredText
    ``:raises Name:`` field are recognised. Only the short name is kept, so a
    dotted ``bernstein.Error`` matches a raised ``Error``.
    """
    docstring = ast.get_docstring(node) or ""
    if not docstring:
        return frozenset()
    names: set[str] = {match.split(".")[-1] for match in _REST_RAISES.findall(docstring)}
    lines = docstring.splitlines()
    for index, line in enumerate(lines):
        if not _RAISES_HEADER.match(line):
            continue
        for follower in lines[index + 1 :]:
            if not follower.strip():
                continue
            if not follower.startswith((" ", "\t")):
                break
            match = _RAISES_ENTRY.match(follower)
            if match is None:
                continue
            names.add(match.group(1).split(".")[-1])
    return frozenset(names)


def _signature_text(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Render the signature the probes were derived from."""
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{node.name}({ast.unparse(node.args)}){returns}"


def _probe_axes(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[list[tuple[Any, ...]], str | None]:
    """Return one boundary-value tuple per probed parameter, or a reason code.

    Parameters carrying a default are not varied: the default is the callable's
    own documented value, and probing around it would report a caller's choice
    rather than the changed surface.
    """
    args = node.args
    if args.vararg is not None or args.kwarg is not None:
        return [], "variadic-parameter"
    if any(default is None for default in args.kw_defaults):
        return [], "keyword-only-parameter"
    positional = [*args.posonlyargs, *args.args]
    required = positional[: len(positional) - len(args.defaults)] if args.defaults else positional
    axes: list[tuple[Any, ...]] = []
    for argument in required:
        if argument.annotation is None:
            return [], "missing-annotation"
        values = _resolve_annotation(argument.annotation)
        if values is None:
            return [], "unsupported-annotation"
        axes.append(values)
    return axes, None


def _combinations(axes: Sequence[tuple[Any, ...]], limit: int) -> tuple[tuple[Any, ...], ...]:
    """Return probe argument tuples, smallest encoding first, capped at ``limit``.

    Ordering by encoded size is what makes the reported failing input the
    minimal one: the empty list is probed before the singleton, which is probed
    before the large collection.
    """
    if not axes:
        return ((),)
    combos = list(itertools.product(*axes))
    combos.sort(key=lambda combo: (len(_encode(list(combo))), _encode(list(combo))))
    return tuple(combos[: max(limit, 0)])


def _is_static(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True when the callable is decorated ``@staticmethod``."""
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == "staticmethod":
            return True
        if isinstance(decorator, ast.Attribute) and decorator.attr == "staticmethod":
            return True
    return False


def _touched(node: ast.stmt, lines: set[int] | None) -> bool:
    """Return True when the changed line set intersects the definition."""
    if lines is None:
        return True
    start = min([node.lineno, *(decorator.lineno for decorator in getattr(node, "decorator_list", []))])
    end = node.end_lineno or start
    return any(start <= line <= end for line in lines)


def _walk_definitions(tree: ast.Module) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, bool]]:
    """Return ``(qualname, node, needs_receiver)`` for public module-level callables.

    Only module-level functions and the methods of module-level public classes
    are considered; a nested definition has no importable name to probe.
    """
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, bool]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                found.append((node.name, node, False))
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and not member.name.startswith("_"):
                    found.append((f"{node.name}.{member.name}", member, not _is_static(member)))
    return found


def derive_probe_plan(
    root: Path,
    changed_files: Sequence[str],
    config: BehaviorProbeConfig,
    changed_lines: Mapping[str, Iterable[int]] | None = None,
) -> ProbePlan:
    """Derive the probe set for a changed surface.

    Args:
        root: Worktree root the paths are relative to.
        changed_files: Repository-relative paths from the diff.
        config: Probe configuration supplying the caps.
        changed_lines: Optional per-file set of changed line numbers. When
            given, only definitions overlapping those lines are probed.

    Returns:
        A :class:`ProbePlan` whose ``set_hash`` identifies the derived inputs.
    """
    targets: list[ProbeTarget] = []
    unprobed: list[UnprobedEntry] = []
    for rel in sorted({path for path in changed_files if path.endswith(".py")}):
        source_path = root / rel
        try:
            source = source_path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError):
            unprobed.append(UnprobedEntry(module=rel, qualname=rel, reason="unparsable-module"))
            continue
        lines = {int(line) for line in changed_lines[rel]} if changed_lines and rel in changed_lines else None
        for qualname, node, needs_receiver in _walk_definitions(tree):
            if not _touched(node, lines):
                continue
            reason = _classify(node, needs_receiver)
            if reason is not None:
                unprobed.append(UnprobedEntry(module=rel, qualname=qualname, reason=reason))
                continue
            if len(targets) >= config.max_callables:
                unprobed.append(UnprobedEntry(module=rel, qualname=qualname, reason="cap-exceeded"))
                continue
            axes, axis_reason = _probe_axes(node)
            if axis_reason is not None:  # pragma: no cover - covered by _classify
                unprobed.append(UnprobedEntry(module=rel, qualname=qualname, reason=axis_reason))
                continue
            targets.append(
                ProbeTarget(
                    module=rel,
                    qualname=qualname,
                    name=node.name,
                    signature=_signature_text(node),
                    probes=_combinations(axes, config.max_probes_per_callable),
                    documented_exceptions=_documented_exceptions(node),
                    return_types=_return_type_names(node.returns),
                )
            )
    document = _plan_document(targets, unprobed)
    return ProbePlan(
        callables=tuple(targets),
        unprobed=tuple(unprobed),
        set_hash=hashlib.sha256(document.encode("utf-8")).hexdigest(),
    )


def _classify(node: ast.FunctionDef | ast.AsyncFunctionDef, needs_receiver: bool) -> str | None:
    """Return the reason this callable cannot be probed, or ``None``."""
    if needs_receiver:
        return "receiver-required"
    if isinstance(node, ast.AsyncFunctionDef):
        return "async-callable"
    _, reason = _probe_axes(node)
    return reason


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Job:
    """One probe scheduled for execution."""

    target: ProbeTarget
    arguments: tuple[Any, ...]


def _child_environment() -> dict[str, str]:
    """Return the environment a probe subprocess runs with."""
    environment = dict(os.environ)
    # Probing must not leave ``__pycache__`` directories in the worktree the
    # gate is judging - a new untracked file would show up as worker output.
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


async def _run_job(job: _Job, root: Path, config: BehaviorProbeConfig) -> ProbeOutcome:
    """Execute one probe in its own interpreter and classify what came back."""
    payload = _encode(
        {
            "arguments": list(job.arguments),
            "file": str(root / job.target.module),
            "qualname": job.target.qualname,
        }
    )
    argv = [*shlex.split(config.python_command), "-c", _CHILD_SOURCE]
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(root),
            env=_child_environment(),
        )
    except OSError as exc:
        return _outcome(job, "unloadable", detail=f"probe interpreter did not start: {exc}")
    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(payload.encode("utf-8")),
            timeout=config.per_callable_timeout_s,
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        return _outcome(job, "timeout", detail=f"no return within {config.per_callable_timeout_s}s")
    try:
        report = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        return _outcome(job, "unloadable", detail="probe produced no readable report")
    return _classify_report(job, report)


def _classify_report(job: _Job, report: Mapping[str, Any]) -> ProbeOutcome:
    """Turn the child's report into a verdict against the callable's contract."""
    outcome = str(report.get("outcome", ""))
    if outcome == "unloadable":
        return _outcome(job, "unloadable", exception=str(report.get("exception", "")), detail="module did not import")
    if outcome == "raised":
        exception = str(report.get("exception", ""))
        verdict: ProbeVerdict = "tolerated" if exception in job.target.documented_exceptions else "crash"
        return _outcome(job, verdict, exception=exception, detail=str(report.get("message", ""))[:200])
    if outcome == "returned":
        observed = str(report.get("return_type", ""))
        if job.target.return_types and observed not in job.target.return_types:
            return _outcome(
                job,
                "contract",
                detail=f"returned {observed}, annotation admits {sorted(job.target.return_types)}",
            )
        return _outcome(job, "ok", detail=observed)
    return _outcome(job, "unloadable", detail="probe produced no readable report")


def _outcome(job: _Job, verdict: ProbeVerdict, *, exception: str = "", detail: str = "") -> ProbeOutcome:
    """Build a :class:`ProbeOutcome` for one job."""
    return ProbeOutcome(
        module=job.target.module,
        qualname=job.target.qualname,
        arguments=list(job.arguments),
        outcome=verdict,
        exception=exception,
        detail=detail,
    )


def _order_outcomes(outcomes: Iterable[ProbeOutcome]) -> tuple[ProbeOutcome, ...]:
    """Return outcomes in the canonical order the receipt records them in."""
    return tuple(
        sorted(
            outcomes,
            key=lambda item: (item.module, item.qualname, len(_encode(item.arguments)), _encode(item.arguments)),
        )
    )


def _minimal_failing(outcomes: Sequence[ProbeOutcome]) -> ProbeOutcome | None:
    """Return the smallest failing probe, or ``None`` when all probes held."""
    failing = [outcome for outcome in outcomes if outcome.outcome in _FAILING_VERDICTS]
    if not failing:
        return None
    return min(failing, key=lambda item: (len(_encode(item.arguments)), _encode(item.arguments), item.qualname))


def _summarise(plan: ProbePlan, outcomes: Sequence[ProbeOutcome], failure: ProbeOutcome | None) -> str:
    """Return the human-readable gate detail."""
    probed = len(outcomes)
    if failure is not None:
        return (
            f"{failure.render_call()} {_failure_phrase(failure)} "
            f"({probed} probes over {len(plan.callables)} callables, "
            f"probe set {plan.set_hash[:12]})"
        )
    return (
        f"{probed} probes over {len(plan.callables)} callables held; "
        f"{len(plan.unprobed)} unprobed (probe set {plan.set_hash[:12]})"
    )


def _failure_phrase(failure: ProbeOutcome) -> str:
    """Describe what the failing probe did."""
    if failure.outcome == "timeout":
        return "did not return within the per-callable budget"
    if failure.outcome == "contract":
        return f"broke its return annotation: {failure.detail}"
    return f"raised undocumented {failure.exception}"


async def probe_changed_surface(
    root: Path,
    changed_files: Sequence[str],
    config: BehaviorProbeConfig,
    changed_lines: Mapping[str, Iterable[int]] | None = None,
) -> BehaviorProbeResult:
    """Derive and run the probe set for a changed surface.

    Args:
        root: Worktree the probes execute in.
        changed_files: Repository-relative paths from the diff.
        config: Probe configuration.
        changed_lines: Optional per-file changed line numbers.

    Returns:
        A :class:`BehaviorProbeResult` carrying the receipt for this run.
    """
    plan = derive_probe_plan(root, changed_files, config, changed_lines)
    if not plan.callables:
        receipt = ProbeReceipt(probe_set_hash=plan.set_hash, outcomes=(), unprobed=plan.unprobed)
        detail = f"No probeable callable on the changed surface ({len(plan.unprobed)} unprobed)."
        return BehaviorProbeResult(
            passed=False,
            status="inconclusive",
            detail=detail,
            receipt=receipt,
            reason="evidence-missing",
        )

    jobs = [_Job(target=target, arguments=probe) for target in plan.callables for probe in target.probes]
    semaphore = asyncio.Semaphore(max(config.max_parallel_probes, 1))

    async def _guarded(job: _Job) -> ProbeOutcome:
        async with semaphore:
            return await _run_job(job, root, config)

    try:
        collected = await asyncio.wait_for(
            asyncio.gather(*[_guarded(job) for job in jobs]),
            timeout=config.gate_timeout_s,
        )
    except TimeoutError:
        logger.warning("behavior_probe exceeded its gate budget of %ss", config.gate_timeout_s)
        collected = [
            _outcome(job, "timeout", detail=f"gate budget of {config.gate_timeout_s}s exhausted") for job in jobs
        ]

    outcomes = _order_outcomes(collected)
    failure = _minimal_failing(outcomes)
    receipt = ProbeReceipt(
        probe_set_hash=plan.set_hash,
        outcomes=outcomes,
        unprobed=plan.unprobed,
        minimal_failing=failure,
    )
    return BehaviorProbeResult(
        passed=failure is None,
        status="pass" if failure is None else "fail",
        detail=_summarise(plan, outcomes, failure),
        receipt=receipt,
    )


def config_from_quality_gates(config: Any) -> BehaviorProbeConfig:
    """Build a :class:`BehaviorProbeConfig` from the quality-gates config.

    Args:
        config: A ``QualityGatesConfig``; its ``behavior_probe_*`` fields carry
            the knobs.

    Returns:
        The probe configuration for this run.
    """
    defaults = BehaviorProbeConfig()
    # An empty command means "the interpreter running the gate": the resolved
    # path belongs to the machine, not to the repository's configuration file.
    command = str(getattr(config, "behavior_probe_python_command", "") or "") or defaults.python_command
    return BehaviorProbeConfig(
        enabled=bool(getattr(config, "behavior_probe", False)),
        python_command=command,
        per_callable_timeout_s=int(
            getattr(config, "behavior_probe_per_callable_timeout_s", defaults.per_callable_timeout_s)
        ),
        gate_timeout_s=int(getattr(config, "behavior_probe_gate_timeout_s", defaults.gate_timeout_s)),
        max_callables=int(getattr(config, "behavior_probe_max_callables", defaults.max_callables)),
        max_probes_per_callable=int(
            getattr(config, "behavior_probe_max_probes_per_callable", defaults.max_probes_per_callable)
        ),
    )


__all__ = [
    "PLAN_VERSION",
    "UNPROBED_REASONS",
    "BehaviorProbeConfig",
    "BehaviorProbeResult",
    "ProbeOutcome",
    "ProbePlan",
    "ProbeReceipt",
    "ProbeTarget",
    "UnprobedEntry",
    "config_from_quality_gates",
    "derive_probe_plan",
    "probe_changed_surface",
]
