"""Figure grounding for report artifacts (issue #2888).

A schema-valid report can still be *fabricated*: every number in the prose can
come from the model's imagination and the completion path will accept it. This
module closes that gap. A report-kind artifact declares its figures in a
machine-checkable sidecar (``figures.json``) that lives *inside* the canonical
artifact bytes, so a figure value is covered by the artifact's own
``content_hash``. Each figure binds ``{value, unit, label, anchor}`` where the
anchor is a lineage-record reference. The ``figures_grounded`` evaluator then
enforces the binding:

* every declared figure's anchor must resolve to a lineage record that verifies
  (signature + chain anchor); and
* every *material* numeric token in the report body must appear in the sidecar.

The tokenizer that decides which numbers are material is the false-positive
surface: section numbers, ISO dates, versions, and allowlisted patterns are
exempt; quantities, currency amounts, percentages, and counts demand an anchor.
Its policy is pinned by an extensible vector suite.

This module is deliberately free of any lineage/store import: anchor resolution
is *injected* as a callable so the tokenizer, the sidecar, and the pure
evaluator can be exercised hermetically. The lineage-wired resolver lives in
:mod:`bernstein.core.lineage.figure_grounding`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from bernstein.core.tasks.artifacts import (
    CanonicalisationError,
    _canonical_json_bytes,
    _normalise_newlines,
)

# ---------------------------------------------------------------------------
# Tokenizer policy
# ---------------------------------------------------------------------------

#: Measurement units that mark a preceding number as a *quantity* (material).
#: Deliberately excludes ambiguous counted nouns ("users", "steps", "requests")
#: - those stay counts governed by the materiality threshold so "3 steps" does
#: not spuriously demand an anchor.
_DEFAULT_UNITS: frozenset[str] = frozenset(
    {
        # bytes
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
        "PB",
        "KiB",
        "MiB",
        "GiB",
        "TiB",
        # time
        "ns",
        "us",
        "µs",
        "ms",
        "s",
        "sec",
        "secs",
        "min",
        "mins",
        "h",
        "hr",
        "hrs",
        "d",
        # frequency / rate
        "Hz",
        "kHz",
        "MHz",
        "GHz",
        "rps",
        "qps",
        "bps",
        "kbps",
        "Mbps",
        "Gbps",
        # mass / length
        "mg",
        "g",
        "kg",
        "t",
        "mm",
        "cm",
        "m",
        "km",
        # misc
        "px",
        "dpi",
        "fps",
        "W",
        "kW",
        "kWh",
        "V",
        "A",
    }
)

#: Structural markers that make a following number a section/figure reference.
_SECTION_MARKERS = ("Section", "Sec", "Chapter", "Ch", "Appendix", "Figure", "Fig", "Table", "Step", "Item", "Note")

#: Default materiality floor for *bare* integers (no grouping, decimal, unit,
#: currency, or percent): a bare integer strictly below this is treated as an
#: incidental count and stays exempt. Grouped, decimal, unit, currency, and
#: percentage numbers are always material regardless of this floor.
_DEFAULT_MATERIALITY_MIN = 1000


@dataclass(frozen=True)
class TokenizerPolicy:
    """Extensible policy pinning the figure tokenizer's false-positive rules."""

    materiality_min: int = _DEFAULT_MATERIALITY_MIN
    units: frozenset[str] = _DEFAULT_UNITS
    allowlist: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenizerPolicy:
        units = data.get("units")
        return cls(
            materiality_min=int(data.get("materiality_min", _DEFAULT_MATERIALITY_MIN)),
            units=frozenset(units) if units else _DEFAULT_UNITS,
            allowlist=tuple(data.get("allowlist", ())),
        )


DEFAULT_POLICY = TokenizerPolicy()


# ---------------------------------------------------------------------------
# Numeric token
# ---------------------------------------------------------------------------

#: Material categories demand a grounding anchor.
MATERIAL_CATEGORIES: frozenset[str] = frozenset({"currency", "percentage", "quantity", "count", "range"})
#: Exempt categories never demand an anchor.
EXEMPT_CATEGORIES: frozenset[str] = frozenset({"date", "version", "section", "allowlisted", "below_threshold"})


@dataclass(frozen=True)
class NumericToken:
    """One numeric token found in a report body, classified by the policy."""

    surface: str
    numeric_key: str
    category: str
    material: bool
    start: int
    end: int
    line: int
    col: int
    numeric_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.numeric_keys:
            object.__setattr__(self, "numeric_keys", (self.numeric_key,))


# ---------------------------------------------------------------------------
# Numeric key normalisation
# ---------------------------------------------------------------------------

_NUM_CORE = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_NUM_CORE_RE = re.compile(_NUM_CORE)
_MULTIPLIERS = "KMBT"


def numeric_key_of(value: str) -> str:
    """Return a canonical numeric key for ``value`` for cross-form matching.

    Strips grouping separators and a leading currency/label, folds trailing
    zeros on a single-decimal number, and appends a normalised magnitude
    suffix (K/M/B/T) when present, so ``"$1,234.00"``, ``"1234"`` and a figure
    declared as ``"1,234"`` all reduce to the same key, and ``"$4.5M"`` reduces
    to ``"4.5M"``.
    """
    m = _NUM_CORE_RE.search(value)
    if not m:
        return value.strip()
    core = m.group(0).replace(",", "")
    if core.count(".") == 1:
        core = core.rstrip("0").rstrip(".")
    suffix = ""
    tail = value[m.end() : m.end() + 1]
    if tail.upper() in _MULTIPLIERS:
        after = value[m.end() + 1 : m.end() + 2]
        if not after.isalnum():
            suffix = tail.upper()
    return core + suffix


# ---------------------------------------------------------------------------
# Exempt-span and material-span detection
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?)?\b")
_VERSION_RE = re.compile(r"\bv\d+(?:\.\d+)+\b|\b\d+\.\d+\.\d+\b")
_SECTION_RE = re.compile(
    r"(?:§\s*|\b(?:" + "|".join(_SECTION_MARKERS) + r")\.?\s+)\d+(?:\.\d+)*",
)


def _line_col(text: str, pos: int) -> tuple[int, int]:
    prefix = text[:pos]
    line = prefix.count("\n") + 1
    col = pos - (prefix.rfind("\n") + 1) + 1
    return line, col


def _overlaps(start: int, end: int, occupied: list[tuple[int, int]]) -> bool:
    return any(start < oe and os < end for os, oe in occupied)


def _units_alt(policy: TokenizerPolicy) -> str:
    # Longest-first so "Mbps" wins over "m"; escape for safety.
    return "|".join(re.escape(u) for u in sorted(policy.units, key=len, reverse=True))


def _range_re(policy: TokenizerPolicy) -> re.Pattern[str]:
    suffix = rf"%|\s?percent\b|\s(?:{_units_alt(policy)})\b"
    # The character class intentionally admits ASCII hyphen, en dash, and em
    # dash as range connectors (e.g. "10-20%" written with any of the three).
    return re.compile(rf"(?P<lo>{_NUM_CORE})\s*[-–—]\s*(?P<hi>{_NUM_CORE})(?:{suffix})")  # noqa: RUF001


def _classify_number(
    text: str,
    m: re.Match[str],
    policy: TokenizerPolicy,
) -> NumericToken:
    """Classify a bare number match into a material/exempt category."""
    num_start, num_end = m.start(), m.end()
    core = m.group(0)

    # Currency prefix: adjacent symbol, or an ISO code within a short window.
    cur_start = num_start
    prefix = text[max(0, num_start - 5) : num_start]
    has_currency = False
    if num_start > 0 and text[num_start - 1] in "$€£¥":
        has_currency = True
        cur_start = num_start - 1
    else:
        code = re.search(r"(USD|EUR|GBP|JPY|CHF|CAD|AUD)\s?$", prefix)
        if code:
            has_currency = True
            cur_start = num_start - (len(prefix) - code.start())

    # Magnitude suffix (K/M/B/T) directly after the number.
    end = num_end
    tail = text[num_end : num_end + 1]
    if tail.upper() in _MULTIPLIERS and not text[num_end + 1 : num_end + 2].isalnum():
        end = num_end + 1

    # Percent.
    has_percent = False
    pm = re.match(r"%|\s?percent\b", text[end:])
    if pm:
        has_percent = True
        end = end + pm.end()

    # Unit word.
    unit_hit = False
    if not has_percent:
        um = re.match(rf"\s(?P<u>{_units_alt(policy)})\b", text[end:])
        if um:
            unit_hit = True
            end = end + um.end()

    surface = text[cur_start:end]
    numeric_key = numeric_key_of(text[num_start:end])
    line, col = _line_col(text, cur_start)

    if has_currency:
        category = "currency"
    elif has_percent:
        category = "percentage"
    elif unit_hit:
        category = "quantity"
    else:
        category = _classify_bare(core, policy)

    material = category in MATERIAL_CATEGORIES
    return NumericToken(surface, numeric_key, category, material, cur_start, end, line, col)


def _classify_bare(core: str, policy: TokenizerPolicy) -> str:
    """Classify a bare number (no currency/percent/unit) as count or exempt."""
    if "," in core or "." in core:
        # Grouped or decimal: a measured count, always material.
        return "count"
    try:
        magnitude = int(core)
    except ValueError:  # pragma: no cover - core is always digits here
        return "below_threshold"
    # A bare four-digit year reads as a date, not a measured count. Exempting it
    # keeps "in 2026 we shipped" from spuriously demanding an anchor; a genuine
    # large count is almost always grouped ("2,026") or out of the year range.
    if len(core) == 4 and 1900 <= magnitude <= 2999:
        return "date"
    return "count" if magnitude >= policy.materiality_min else "below_threshold"


def tokenize_numbers(text: str, policy: TokenizerPolicy | None = None) -> list[NumericToken]:
    """Return the ordered numeric tokens in ``text`` classified by ``policy``.

    Exempt spans (allowlist, ISO date, version, section reference) are detected
    first and their character ranges suppress overlapping bare-number matches,
    so ``§3.2`` yields one exempt section token, not two decimals. Material
    ranges (``10-20%``) are detected before bare numbers so both endpoints
    collapse into a single range token.
    """
    policy = policy or DEFAULT_POLICY
    tokens: list[NumericToken] = []
    occupied: list[tuple[int, int]] = []

    def _emit_span(start: int, end: int, category: str) -> None:
        line, col = _line_col(text, start)
        surface = text[start:end]
        tokens.append(NumericToken(surface, numeric_key_of(surface), category, False, start, end, line, col))
        occupied.append((start, end))

    # 1. Exempt spans, in precedence order.
    for pattern in policy.allowlist:
        for m in re.finditer(pattern, text):
            if not _overlaps(m.start(), m.end(), occupied):
                _emit_span(m.start(), m.end(), "allowlisted")
    for m in _DATE_RE.finditer(text):
        if not _overlaps(m.start(), m.end(), occupied):
            _emit_span(m.start(), m.end(), "date")
    for m in _VERSION_RE.finditer(text):
        if not _overlaps(m.start(), m.end(), occupied):
            _emit_span(m.start(), m.end(), "version")
    for m in _SECTION_RE.finditer(text):
        if not _overlaps(m.start(), m.end(), occupied):
            _emit_span(m.start(), m.end(), "section")

    # 2. Material ranges (before bare numbers so endpoints do not split).
    for m in _range_re(policy).finditer(text):
        if _overlaps(m.start(), m.end(), occupied):
            continue
        line, col = _line_col(text, m.start())
        lo = numeric_key_of(m.group("lo"))
        hi = numeric_key_of(m.group("hi"))
        surface = text[m.start() : m.end()]
        tokens.append(NumericToken(surface, lo, "range", True, m.start(), m.end(), line, col, numeric_keys=(lo, hi)))
        occupied.append((m.start(), m.end()))

    # 3. Bare numbers.
    for m in _NUM_CORE_RE.finditer(text):
        if _overlaps(m.start(), m.end(), occupied):
            continue
        # A number glued to a preceding letter is part of an identifier
        # ("P99", "IPv4", "H2"), not a standalone figure - skip it entirely.
        if m.start() > 0 and text[m.start() - 1].isalpha():
            continue
        tok = _classify_number(text, m, policy)
        tokens.append(tok)
        occupied.append((tok.start, tok.end))

    tokens.sort(key=lambda t: t.start)
    return tokens


# ---------------------------------------------------------------------------
# Figure sidecar + report bundle
# ---------------------------------------------------------------------------

#: Anchor kinds available today. ``receipt`` is reserved for the query-receipt
#: work (issue #2887): the resolver registry accepts it as a plug point so a
#: receipt-id anchor kind lands without reworking this contract.
ANCHOR_KINDS: frozenset[str] = frozenset({"attachment", "artifact", "receipt"})
#: Anchor kinds whose ``ref`` is a ``sha256:`` content hash.
_HASH_ANCHOR_KINDS: frozenset[str] = frozenset({"attachment", "artifact"})


@dataclass(frozen=True)
class FigureAnchor:
    """A reference from a figure to the lineage record that grounds it."""

    kind: str
    ref: str

    def __post_init__(self) -> None:
        if self.kind not in ANCHOR_KINDS:
            raise ValueError(f"unknown figure anchor kind: {self.kind!r}")
        if not self.ref:
            raise ValueError("figure anchor ref must be non-empty")
        if self.kind in _HASH_ANCHOR_KINDS and not self.ref.startswith("sha256:"):
            raise ValueError(f"{self.kind} anchor ref must be a 'sha256:' content hash, got {self.ref!r}")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "ref": self.ref}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FigureAnchor:
        return cls(kind=str(data["kind"]), ref=str(data["ref"]))


@dataclass(frozen=True)
class Figure:
    """One declared figure: a material number and its grounding anchor."""

    value: str
    unit: str
    label: str
    anchor: FigureAnchor

    @property
    def numeric_key(self) -> str:
        return numeric_key_of(self.value)

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "unit": self.unit, "label": self.label, "anchor": self.anchor.to_dict()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Figure:
        return cls(
            value=str(data["value"]),
            unit=str(data.get("unit", "")),
            label=str(data.get("label", "")),
            anchor=FigureAnchor.from_dict(data["anchor"]),
        )


@dataclass(frozen=True)
class ReportBundle:
    """A report body plus its ``figures.json`` sidecar, hashed as one unit.

    The canonical bytes are a single JCS object ``{"body": ..., "figures":
    [...]}`` so the sidecar is *inside* the artifact's ``content_hash``: editing
    a figure value after completion changes the hash.
    """

    body: str
    figures: tuple[Figure, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.figures, tuple):
            object.__setattr__(self, "figures", tuple(self.figures))

    def to_canonical_obj(self) -> dict[str, Any]:
        normalised = _normalise_newlines(self.body)
        if not unicodedata.is_normalized("NFC", normalised):
            raise CanonicalisationError("report body is not NFC-normalised (reject-don't-repair policy)")
        return {"body": normalised, "figures": [f.to_dict() for f in self.figures]}

    @classmethod
    def from_canonical_obj(cls, obj: dict[str, Any]) -> ReportBundle:
        figures = tuple(Figure.from_dict(f) for f in obj.get("figures", []))
        return cls(body=str(obj.get("body", "")), figures=figures)


def canonicalise_report_bundle(bundle: ReportBundle) -> bytes:
    """Return the canonical bytes for a report bundle (body + sidecar).

    Raises :class:`CanonicalisationError` when the body is not NFC-normalised.
    """
    return _canonical_json_bytes(bundle.to_canonical_obj())


def parse_report_bundle(canonical_bytes: bytes) -> ReportBundle:
    """Parse canonical report-bundle bytes back into a :class:`ReportBundle`.

    Raises :class:`CanonicalisationError` when the bytes are not a report
    bundle (a plain report kind, a dataset, etc. - callers that may see either
    should catch this to skip figure grounding).
    """
    import json

    try:
        obj = json.loads(canonical_bytes)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CanonicalisationError(f"not a report bundle: {exc}") from exc
    if not isinstance(obj, dict) or "body" not in obj or "figures" not in obj:
        raise CanonicalisationError("not a report bundle: missing body/figures keys")
    return ReportBundle.from_canonical_obj(obj)


def is_report_bundle(canonical_bytes: bytes) -> bool:
    """Return True iff ``canonical_bytes`` parse as a figures report bundle."""
    try:
        parse_report_bundle(canonical_bytes)
    except CanonicalisationError:
        return False
    return True


# ---------------------------------------------------------------------------
# figures_grounded evaluator (pure; anchor resolution injected)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnchorResolution:
    """Result of resolving one figure anchor against the lineage log."""

    ok: bool
    statement: str


@dataclass(frozen=True)
class FigureProvenance:
    """Per-figure provenance line for the operator-facing verdict."""

    label: str
    value: str
    anchor: FigureAnchor
    ok: bool
    statement: str


@dataclass(frozen=True)
class UnanchoredNumber:
    """A material number in the body with no matching sidecar figure."""

    surface: str
    numeric_key: str
    category: str
    line: int
    col: int


@dataclass(frozen=True)
class FiguresVerdict:
    """Outcome of ``figures_grounded``. ``ok`` iff no figure or number fails."""

    ok: bool
    has_figures: bool
    provenances: tuple[FigureProvenance, ...]
    unanchored: tuple[UnanchoredNumber, ...]
    failures: tuple[str, ...]


#: An injected anchor resolver: given an anchor, return whether it resolves to a
#: verifying lineage record and a human-readable provenance statement.
AnchorResolver = Callable[[FigureAnchor], AnchorResolution]


def evaluate_figures_grounded(
    bundle: ReportBundle,
    *,
    resolve_anchor: AnchorResolver,
    policy: TokenizerPolicy | None = None,
) -> FiguresVerdict:
    """Evaluate the ``figures_grounded`` contract on a parsed report bundle.

    Two closed checks, no network:

    1. Every declared figure's anchor must resolve to a verifying lineage
       record (``resolve_anchor`` is the injected, lineage-wired resolver).
    2. Every *material* numeric token in the body must appear in the sidecar.

    The failure list names each failing figure and each unanchored number with
    its location, so the exact gap is fixable.
    """
    policy = policy or DEFAULT_POLICY
    failures: list[str] = []

    provenances: list[FigureProvenance] = []
    for fig in bundle.figures:
        res = resolve_anchor(fig.anchor)
        provenances.append(FigureProvenance(fig.label, fig.value, fig.anchor, res.ok, res.statement))
        if not res.ok:
            label = fig.label or fig.value
            failures.append(f"figure {label!r} (value {fig.value}) is not grounded: {res.statement}")

    declared: set[str] = set()
    for fig in bundle.figures:
        declared.add(fig.numeric_key)

    unanchored: list[UnanchoredNumber] = []
    for tok in tokenize_numbers(bundle.body, policy):
        if not tok.material:
            continue
        if any(k in declared for k in tok.numeric_keys):
            continue
        unanchored.append(UnanchoredNumber(tok.surface, tok.numeric_key, tok.category, tok.line, tok.col))
        failures.append(
            f"unanchored {tok.category} {tok.surface!r} at line {tok.line}, col {tok.col} "
            f"is not declared in figures.json"
        )

    return FiguresVerdict(
        ok=not failures,
        has_figures=bool(bundle.figures),
        provenances=tuple(provenances),
        unanchored=tuple(unanchored),
        failures=tuple(failures),
    )


def evaluate_figures_grounded_bytes(
    canonical_bytes: bytes,
    *,
    resolve_anchor: AnchorResolver,
    policy: TokenizerPolicy | None = None,
) -> FiguresVerdict:
    """Parse ``canonical_bytes`` as a report bundle and evaluate grounding."""
    bundle = parse_report_bundle(canonical_bytes)
    return evaluate_figures_grounded(bundle, resolve_anchor=resolve_anchor, policy=policy)


__all__ = [
    "ANCHOR_KINDS",
    "DEFAULT_POLICY",
    "EXEMPT_CATEGORIES",
    "MATERIAL_CATEGORIES",
    "AnchorResolution",
    "AnchorResolver",
    "Figure",
    "FigureAnchor",
    "FigureProvenance",
    "FiguresVerdict",
    "NumericToken",
    "ReportBundle",
    "TokenizerPolicy",
    "UnanchoredNumber",
    "canonicalise_report_bundle",
    "evaluate_figures_grounded",
    "evaluate_figures_grounded_bytes",
    "is_report_bundle",
    "numeric_key_of",
    "parse_report_bundle",
    "tokenize_numbers",
]
