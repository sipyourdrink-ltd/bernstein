"""Render receipt schema and delta algebra for UI snapshots (issues #2362, #3276).

A render receipt captures the deterministic output of a UI render pass:
environment metadata, layout geometry, computed styles, and accessibility
tree. The receipt is sealed via sorted-JSON canonical bytes hashed with
SHA-256, enabling reproducible comparisons across renders.

Determinism
-----------

The canonical projection sorts and deduplicates every observation sequence, so
two captures that saw the same layout boxes, computed styles and accessibility
nodes hash identically no matter what order the probe walked the tree in. A
receipt whose hash depends on walk order cannot be compared across two
worktrees, because nothing forces two walks to agree on order.

Comparison
----------

:func:`render_delta` is a pure function of two receipts. It returns either a
sorted tuple of named ``(route, viewport, declared_state, element_path,
property, before, after)`` deltas, or an explicit *incomparable* result naming
the receipt field that made the two captures non-comparable -- a differing
environment descriptor hash, a different route or declared state, a different
schema or property-vocabulary version, an undeclared environment, or a receipt
that declares two values for one element property.

Incomparability is a distinct outcome, not an empty delta set. A caller that
read an incomparable comparison as "nothing changed" would be asserting a
property that was never checked, which is exactly the claim the receipt exists
to make falsifiable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Sentinel epoch for default clock_value when none is supplied.
_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)  # noqa: UP017

#: Schema version stamped into every render receipt. Bump only on a
#: wire-format change.
RENDER_RECEIPT_SCHEMA_VERSION = 1

__all__ = [
    "RENDER_RECEIPT_SCHEMA_VERSION",
    "A11yNode",
    "ComputedStyle",
    "EnvironmentDescriptor",
    "LayoutBox",
    "RenderDelta",
    "RenderObservationDelta",
    "RenderReceipt",
    "Viewport",
    "render_delta",
]


# ---------------------------------------------------------------------------
# Canonical hashing helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    """Return the ``sha256:``-prefixed hex digest of ``data``."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical_rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Return the canonical projection of an observation sequence.

    Rows are serialised, deduplicated by their canonical bytes and sorted by
    those bytes, so the projection depends only on the set of observations and
    not on the order the probe emitted them in.

    Args:
        rows: Observation dataclasses exposing ``to_dict()``.

    Returns:
        The sorted, deduplicated list of observation dicts.
    """
    by_bytes: dict[bytes, dict[str, Any]] = {}
    for row in rows:
        payload = row.to_dict()
        by_bytes.setdefault(_canonical_bytes(payload), payload)
    return [by_bytes[key] for key in sorted(by_bytes)]


# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Viewport:
    """Render viewport dimensions.

    Attributes:
        width: Viewport width in pixels.
        height: Viewport height in pixels.
    """

    width: int = 0
    height: int = 0

    def to_dict(self) -> dict[str, int]:
        """Convert viewport to a dict."""
        return {"width": self.width, "height": self.height}

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Viewport:
        """Construct viewport from a dict."""
        return cls(width=int(row.get("width", 0)), height=int(row.get("height", 0)))


# ---------------------------------------------------------------------------
# Environment descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvironmentDescriptor:
    """Static environment signals present at render time.

    Attributes:
        engine_build_identity: Browser/engine version identifier.
        viewport: Render viewport dimensions.
        device_pixel_ratio: Device pixel ratio at render time.
        locale: Active locale string (e.g. ``"en-US"``).
        timezone: Active timezone identifier (e.g. ``"America/New_York"``).
        clock_value: UTC timestamp of the render clock.
        font_set_hash: SHA-256 of the resolved font set.
        animation_disabled: Whether animations are disabled.
        caret_disabled: Whether the caret is suppressed.
        reduced_motion: Whether reduced-motion preference is active.
        colour_scheme: Active colour scheme (``"light"``, ``"dark"``, ``"no-preference"``).
    """

    engine_build_identity: str = ""
    viewport: Viewport = field(default_factory=Viewport)
    device_pixel_ratio: float = 1.0
    locale: str = ""
    timezone: str = ""
    clock_value: datetime = field(default_factory=lambda: _EPOCH_UTC)
    font_set_hash: str = ""
    animation_disabled: bool = False
    caret_disabled: bool = False
    reduced_motion: bool = False
    colour_scheme: str = "no-preference"

    def to_dict(self) -> dict[str, Any]:
        """Convert environment descriptor to a dict."""
        return {
            "engine_build_identity": self.engine_build_identity,
            "viewport": self.viewport.to_dict(),
            "device_pixel_ratio": self.device_pixel_ratio,
            "locale": self.locale,
            "timezone": self.timezone,
            "clock_value": self.clock_value.isoformat(),
            "font_set_hash": self.font_set_hash,
            "animation_disabled": self.animation_disabled,
            "caret_disabled": self.caret_disabled,
            "reduced_motion": self.reduced_motion,
            "colour_scheme": self.colour_scheme,
        }

    def descriptor_hash(self) -> str:
        """Return the ``sha256:`` digest of the canonical descriptor bytes.

        Two receipts are only comparable when this value agrees. It is the
        check that makes a delta set mean "the rendered state changed" rather
        than "the two captures ran under different conditions".
        """
        return _sha256_hex(_canonical_bytes(self.to_dict()))

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> EnvironmentDescriptor:
        """Construct environment descriptor from a dict."""
        vp_raw = row.get("viewport", {})
        if isinstance(vp_raw, Viewport):
            viewport = vp_raw
        elif isinstance(vp_raw, dict):
            viewport = Viewport.from_dict(vp_raw)
        elif isinstance(vp_raw, (list, tuple)) and len(vp_raw) == 2:
            viewport = Viewport(width=int(vp_raw[0]), height=int(vp_raw[1]))
        else:
            viewport = Viewport()

        clock_raw = row.get("clock_value")
        if isinstance(clock_raw, datetime):
            clock_value = clock_raw
        elif isinstance(clock_raw, str) and clock_raw:
            clock_value = datetime.fromisoformat(clock_raw)
        else:
            clock_value = _EPOCH_UTC

        return cls(
            engine_build_identity=str(row.get("engine_build_identity", "")),
            viewport=viewport,
            device_pixel_ratio=float(row.get("device_pixel_ratio", 1.0)),
            locale=str(row.get("locale", "")),
            timezone=str(row.get("timezone", "")),
            clock_value=clock_value,
            font_set_hash=str(row.get("font_set_hash", "")),
            animation_disabled=bool(row.get("animation_disabled", False)),
            caret_disabled=bool(row.get("caret_disabled", False)),
            reduced_motion=bool(row.get("reduced_motion", False)),
            colour_scheme=str(row.get("colour_scheme", row.get("color_scheme", "no-preference"))),
        )


# ---------------------------------------------------------------------------
# Layout box
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LayoutBox:
    """One element's layout geometry in the layout tree.

    Attributes:
        element_path: Dot-separated path to this element in the DOM tree.
        border_box: Border-box rect ``(x, y, width, height)``.
        content_box: Content-box rect ``(x, y, width, height)``.
        scroll_extent: Scroll extent rect ``(x, y, width, height)``.
        stacking_order: Z-index/stacking context order.
        paint_order: CSS paint-order index.
    """

    element_path: str = ""
    border_box: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    content_box: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    scroll_extent: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    stacking_order: int = 0
    paint_order: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert layout box to a dict."""
        return {
            "element_path": self.element_path,
            "border_box": list(self.border_box),
            "content_box": list(self.content_box),
            "scroll_extent": list(self.scroll_extent),
            "stacking_order": self.stacking_order,
            "paint_order": self.paint_order,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> LayoutBox:
        """Construct layout box from a dict."""

        def _box(raw: object) -> tuple[float, float, float, float]:
            if isinstance(raw, (list, tuple)) and len(raw) == 4:
                return (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
            return (0.0, 0.0, 0.0, 0.0)

        return cls(
            element_path=str(row.get("element_path", "")),
            border_box=_box(row.get("border_box")),
            content_box=_box(row.get("content_box")),
            scroll_extent=_box(row.get("scroll_extent")),
            stacking_order=int(row.get("stacking_order", 0)),
            paint_order=int(row.get("paint_order", 0)),
        )


# ---------------------------------------------------------------------------
# Computed style
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ComputedStyle:
    """Computed style properties for one element.

    Attributes:
        element_path: Dot-separated path to this element in the DOM tree.
        properties: Flat mapping of CSS property name to computed value.
    """

    element_path: str = ""
    properties: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert computed style to a dict."""
        return {
            "element_path": self.element_path,
            "properties": dict(self.properties),
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> ComputedStyle:
        """Construct computed style from a dict."""
        props_raw = row.get("properties")
        props: dict[str, str] = {}
        if isinstance(props_raw, dict):
            props = {str(k): str(v) for k, v in props_raw.items()}
        return cls(
            element_path=str(row.get("element_path", "")),
            properties=props,
        )


# ---------------------------------------------------------------------------
# Accessibility node
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class A11yNode:
    """One node in the accessibility tree.

    Attributes:
        element_path: Dot-separated path to this element in the DOM tree.
        role: ARIA role string.
        name: Computed accessible name.
        state: Flat mapping of ARIA state attributes.
    """

    element_path: str = ""
    role: str = ""
    name: str = ""
    state: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert accessibility node to a dict."""
        return {
            "element_path": self.element_path,
            "role": self.role,
            "name": self.name,
            "state": dict(self.state),
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> A11yNode:
        """Construct accessibility node from a dict."""
        state_raw = row.get("state")
        state: dict[str, str] = {}
        if isinstance(state_raw, dict):
            state = {str(k): str(v) for k, v in state_raw.items()}
        return cls(
            element_path=str(row.get("element_path", "")),
            role=str(row.get("role", "")),
            name=str(row.get("name", "")),
            state=state,
        )


# ---------------------------------------------------------------------------
# Render receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderReceipt:
    """A sealed render-receipt for a UI snapshot.

    The binding (all fields except ``receipt_hash``) is serialised to sorted-JSON
    canonical bytes and hashed with SHA-256. The hash is stable across dict
    insertion order and host byte-order, enabling reproducible comparisons.

    Attributes:
        version: Schema version, stamped at serialisation time.
        route: Rendered route/path identifier.
        viewport: Render viewport dimensions.
        declared_state: Serialised declared application state at render time.
        layout_tree: Sequence of layout boxes in paint order.
        computed_styles: Sequence of computed styles, one per element.
        accessibility_tree: Sequence of accessibility nodes.
        environment: Static environment signals present at render time.
        unstable_properties: Additional unstable/experimental properties emitted
            by the render engine.
        property_vocabulary_version: Identifier for the CSS property vocabulary
            used in computed styles.
    """

    version: int = RENDER_RECEIPT_SCHEMA_VERSION
    route: str = ""
    viewport: Viewport = field(default_factory=Viewport)
    declared_state: str = ""
    layout_tree: tuple[LayoutBox, ...] = ()
    computed_styles: tuple[ComputedStyle, ...] = ()
    accessibility_tree: tuple[A11yNode, ...] = ()
    environment: EnvironmentDescriptor | None = None
    unstable_properties: dict[str, str] = field(default_factory=dict)
    property_vocabulary_version: str = ""

    def _binding(self) -> dict[str, Any]:
        """Return the canonical binding dict (excludes receipt_hash)."""
        binding: dict[str, Any] = {
            "v": self.version,
            "route": self.route,
            "viewport": self.viewport.to_dict(),
            "declared_state": self.declared_state,
            "layout_tree": _canonical_rows(self.layout_tree),
            "computed_styles": _canonical_rows(self.computed_styles),
            "accessibility_tree": _canonical_rows(self.accessibility_tree),
            "property_vocabulary_version": self.property_vocabulary_version,
        }
        if self.environment is not None:
            binding["environment"] = self.environment.to_dict()
        if self.unstable_properties:
            binding["unstable_properties"] = dict(self.unstable_properties)
        return binding

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical bytes for hashing."""
        return _canonical_bytes(self._binding())

    def receipt_hash(self) -> str:
        """Return the ``sha256:`` digest of the canonical binding bytes."""
        return _sha256_hex(self.to_canonical_bytes())

    def to_dict(self) -> dict[str, Any]:
        """Convert render receipt to a dict including receipt_hash."""
        out = self._binding()
        out["receipt_hash"] = self.receipt_hash()
        return out

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> RenderReceipt:
        """Construct render receipt from a dict."""
        vp_raw = row.get("viewport", {})
        if isinstance(vp_raw, Viewport):
            viewport = vp_raw
        elif isinstance(vp_raw, dict):
            viewport = Viewport.from_dict(vp_raw)
        elif isinstance(vp_raw, (list, tuple)) and len(vp_raw) == 2:
            viewport = Viewport(width=int(vp_raw[0]), height=int(vp_raw[1]))
        else:
            viewport = Viewport()

        layout_tree = tuple(
            b if isinstance(b, LayoutBox) else LayoutBox.from_dict(b) for b in row.get("layout_tree", ())
        )
        computed_styles = tuple(
            s if isinstance(s, ComputedStyle) else ComputedStyle.from_dict(s) for s in row.get("computed_styles", ())
        )
        accessibility_tree = tuple(
            n if isinstance(n, A11yNode) else A11yNode.from_dict(n) for n in row.get("accessibility_tree", ())
        )

        env: EnvironmentDescriptor | None = None
        env_raw = row.get("environment")
        if isinstance(env_raw, EnvironmentDescriptor):
            env = env_raw
        elif isinstance(env_raw, dict):
            env = EnvironmentDescriptor.from_dict(env_raw)

        unstable_raw = row.get("unstable_properties")
        unstable_properties: dict[str, str] = {}
        if isinstance(unstable_raw, dict):
            unstable_properties = {str(k): str(v) for k, v in unstable_raw.items()}

        version = int(row.get("version", row.get("v", RENDER_RECEIPT_SCHEMA_VERSION)))

        return cls(
            version=version,
            route=str(row.get("route", "")),
            viewport=viewport,
            declared_state=str(row.get("declared_state", "")),
            layout_tree=layout_tree,
            computed_styles=computed_styles,
            accessibility_tree=accessibility_tree,
            environment=env,
            unstable_properties=unstable_properties,
            property_vocabulary_version=str(row.get("property_vocabulary_version", "")),
        )


# ---------------------------------------------------------------------------
# Delta algebra (issue #3276)
# ---------------------------------------------------------------------------


#: Namespace prefixes keeping the three observation trees from colliding on a
#: shared element path. The ``property`` field stays an opaque string here; the
#: property-class taxonomy is left to the step that has a real capture to
#: constrain it.
_LAYOUT_NS = "layout"
_STYLE_NS = "style"
_A11Y_NS = "a11y"
_UNSTABLE_NS = "unstable"


class _ObservationConflict(Exception):
    """One receipt declares two values for a single element property.

    Raised while flattening and turned into an incomparable result. A receipt
    that disagrees with itself cannot be one side of a comparison, and picking
    a winner would make the delta set depend on emission order.
    """

    def __init__(self, element_path: str, prop: str) -> None:
        super().__init__(f"receipt declares conflicting values for {element_path!r} property {prop!r}")
        self.element_path = element_path
        self.property = prop


def _scalar(value: Any) -> str:
    """Render one observation value as a canonical string."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _flatten_observations(receipt: RenderReceipt) -> dict[tuple[str, str], str]:
    """Flatten a receipt's three trees into ``(element_path, property)`` values.

    Args:
        receipt: The receipt to project.

    Returns:
        Mapping of ``(element_path, namespaced property)`` to canonical value.

    Raises:
        _ObservationConflict: When the receipt declares two different values
            for one element property.
    """
    observed: dict[tuple[str, str], str] = {}

    def record(element_path: str, prop: str, value: Any) -> None:
        key = (element_path, prop)
        rendered = _scalar(value)
        previous = observed.get(key)
        if previous is not None and previous != rendered:
            raise _ObservationConflict(element_path, prop)
        observed[key] = rendered

    for box in receipt.layout_tree:
        record(box.element_path, f"{_LAYOUT_NS}.border_box", list(box.border_box))
        record(box.element_path, f"{_LAYOUT_NS}.content_box", list(box.content_box))
        record(box.element_path, f"{_LAYOUT_NS}.scroll_extent", list(box.scroll_extent))
        record(box.element_path, f"{_LAYOUT_NS}.stacking_order", box.stacking_order)
        record(box.element_path, f"{_LAYOUT_NS}.paint_order", box.paint_order)

    for style in receipt.computed_styles:
        for prop, value in style.properties.items():
            record(style.element_path, f"{_STYLE_NS}.{prop}", value)

    for node in receipt.accessibility_tree:
        record(node.element_path, f"{_A11Y_NS}.role", node.role)
        record(node.element_path, f"{_A11Y_NS}.name", node.name)
        for state_key, state_value in node.state.items():
            record(node.element_path, f"{_A11Y_NS}.state.{state_key}", state_value)

    for prop, value in receipt.unstable_properties.items():
        record("", f"{_UNSTABLE_NS}.{prop}", value)

    return observed


@dataclass(frozen=True, slots=True)
class RenderObservationDelta:
    """One named property change between two render receipts.

    Attributes:
        route: Route both receipts were captured on.
        viewport: Viewport both receipts were captured at.
        declared_state: Declared application state both receipts were captured in.
        element_path: Path of the element whose property changed.
        property: Namespaced property name, e.g. ``style.color``.
        before: Value in the base receipt, or ``None`` when the observation
            appeared only in the head receipt.
        after: Value in the head receipt, or ``None`` when the observation
            disappeared.
    """

    route: str = ""
    viewport: Viewport = field(default_factory=Viewport)
    declared_state: str = ""
    element_path: str = ""
    property: str = ""
    before: str | None = None
    after: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert the delta to a dict."""
        return {
            "route": self.route,
            "viewport": self.viewport.to_dict(),
            "declared_state": self.declared_state,
            "element_path": self.element_path,
            "property": self.property,
            "before": self.before,
            "after": self.after,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> RenderObservationDelta:
        """Construct a delta from a dict."""
        before = row.get("before")
        after = row.get("after")
        return cls(
            route=str(row.get("route", "")),
            viewport=Viewport.from_dict(row.get("viewport", {})),
            declared_state=str(row.get("declared_state", "")),
            element_path=str(row.get("element_path", "")),
            property=str(row.get("property", "")),
            before=None if before is None else str(before),
            after=None if after is None else str(after),
        )


@dataclass(frozen=True, slots=True)
class RenderDelta:
    """The outcome of comparing two render receipts.

    Exactly one of two shapes. A *comparable* result carries an empty
    ``incomparable_reason`` and a sorted, possibly empty tuple of deltas. An
    *incomparable* result carries a reason naming the field responsible and no
    deltas at all, so it can never be read as "nothing changed".

    Attributes:
        base_descriptor_hash: Environment descriptor hash of the base receipt,
            empty when the base declared no environment.
        head_descriptor_hash: Environment descriptor hash of the head receipt,
            empty when the head declared no environment.
        incomparable_reason: Why the two receipts were not compared; empty when
            they were.
        deltas: Sorted named property changes; always empty when incomparable.
    """

    base_descriptor_hash: str = ""
    head_descriptor_hash: str = ""
    incomparable_reason: str = ""
    deltas: tuple[RenderObservationDelta, ...] = ()

    @property
    def is_incomparable(self) -> bool:
        """Whether the two receipts were rejected as non-comparable."""
        return bool(self.incomparable_reason)

    @property
    def is_clean(self) -> bool:
        """Whether the two receipts were compared and agreed on every property."""
        return not self.incomparable_reason and not self.deltas

    def to_dict(self) -> dict[str, Any]:
        """Convert the comparison outcome to a dict."""
        return {
            "base_descriptor_hash": self.base_descriptor_hash,
            "head_descriptor_hash": self.head_descriptor_hash,
            "incomparable_reason": self.incomparable_reason,
            "deltas": [delta.to_dict() for delta in self.deltas],
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> RenderDelta:
        """Construct a comparison outcome from a dict."""
        return cls(
            base_descriptor_hash=str(row.get("base_descriptor_hash", "")),
            head_descriptor_hash=str(row.get("head_descriptor_hash", "")),
            incomparable_reason=str(row.get("incomparable_reason", "")),
            deltas=tuple(
                d if isinstance(d, RenderObservationDelta) else RenderObservationDelta.from_dict(d)
                for d in cast("Iterable[Any]", row.get("deltas", ()))
            ),
        )


def _incomparability_reason(base: RenderReceipt, head: RenderReceipt) -> str:
    """Return why ``base`` and ``head`` cannot be compared, or an empty string."""
    if base.environment is None or head.environment is None:
        return "environment not declared on both receipts"

    for label, base_value, head_value in (
        ("version", base.version, head.version),
        ("route", base.route, head.route),
        ("viewport", base.viewport, head.viewport),
        ("declared_state", base.declared_state, head.declared_state),
        (
            "property_vocabulary_version",
            base.property_vocabulary_version,
            head.property_vocabulary_version,
        ),
    ):
        if base_value != head_value:
            return f"{label} differs: {base_value!r} != {head_value!r}"

    base_hash = base.environment.descriptor_hash()
    head_hash = head.environment.descriptor_hash()
    if base_hash != head_hash:
        return f"environment descriptor hash differs: {base_hash} != {head_hash}"

    return ""


def render_delta(base: RenderReceipt, head: RenderReceipt) -> RenderDelta:
    """Compare two render receipts property by property.

    Args:
        base: Receipt captured from the merge base.
        head: Receipt captured from the head, under the same declared
            environment.

    Returns:
        A :class:`RenderDelta` carrying the sorted named property changes, or an
        incomparable result naming the field that made the comparison invalid.
        The function performs no I/O and consults nothing outside its arguments,
        so a verifier re-derives the same result from receipt bytes alone.
    """
    base_hash = base.environment.descriptor_hash() if base.environment is not None else ""
    head_hash = head.environment.descriptor_hash() if head.environment is not None else ""

    def rejected(reason: str) -> RenderDelta:
        return RenderDelta(
            base_descriptor_hash=base_hash,
            head_descriptor_hash=head_hash,
            incomparable_reason=reason,
        )

    mismatch = _incomparability_reason(base, head)
    if mismatch:
        return rejected(mismatch)

    try:
        base_observations = _flatten_observations(base)
        head_observations = _flatten_observations(head)
    except _ObservationConflict as conflict:
        return rejected(str(conflict))

    deltas: list[RenderObservationDelta] = []
    for element_path, prop in sorted(set(base_observations) | set(head_observations)):
        before = base_observations.get((element_path, prop))
        after = head_observations.get((element_path, prop))
        if before == after:
            continue
        deltas.append(
            RenderObservationDelta(
                route=head.route,
                viewport=head.viewport,
                declared_state=head.declared_state,
                element_path=element_path,
                property=prop,
                before=before,
                after=after,
            )
        )

    return RenderDelta(
        base_descriptor_hash=base_hash,
        head_descriptor_hash=head_hash,
        deltas=tuple(deltas),
    )
