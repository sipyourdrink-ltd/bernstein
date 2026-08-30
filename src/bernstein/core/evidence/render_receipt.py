"""Render receipt schema for UI snapshot verification (issue #2362).

A render receipt captures the deterministic output of a UI render pass:
environment metadata, layout geometry, computed styles, and accessibility
tree. The receipt is sealed via sorted-JSON canonical bytes hashed with
SHA-256, enabling reproducible comparisons across renders.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

#: Sentinel epoch for default clock_value when none is supplied.
_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)  # noqa: UP017

__all__ = [
    "A11yNode",
    "ComputedStyle",
    "DeltaKind",
    "DeltaSet",
    "EnvironmentDescriptor",
    "EnvironmentMismatchError",
    "LayoutBox",
    "PropertyClass",
    "RenderDelta",
    "RenderReceipt",
    "UnresolvedDelta",
    "UnresolvedReason",
    "Viewport",
    "render_delta",
]


# ---------------------------------------------------------------------------
# Property classes and enums for delta comparison
# ---------------------------------------------------------------------------


class PropertyClass(Enum):
    """Classification of CSS properties for delta analysis."""

    TOKEN = "token"  # Individual tokens (keywords, identifiers, numbers, etc.)
    GEOMETRY = "geometry"  # Position, size, spacing properties
    OVERFLOW = "overflow"  # Overflow and clipping properties
    VISIBILITY = "visibility"  # Display, opacity, visibility properties
    TYPOGRAPHY = "typography"  # Font, text, spacing properties
    PAINT = "paint"  # Colors, backgrounds, borders, shadows
    A11Y = "a11y"  # Accessibility-related properties


class UnresolvedReason(Enum):
    """Reason why a delta could not be resolved during comparison."""

    CANVAS = "canvas"  # Canvas rendering cannot be inspected
    WEBGL = "webgl"  # WebGL rendering cannot be inspected
    VIDEO = "video"  # Video elements cannot be inspected
    CROSS_ORIGIN_IFRAME = "cross_origin_iframe"  # Cross-origin iframe content


class DeltaKind(Enum):
    """Type of change detected between two render receipts."""

    ADDED = "added"  # Element or property appeared in head
    REMOVED = "removed"  # Element or property disappeared from head
    CHANGED = "changed"  # Element or property value changed


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


# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Viewport:
    """Render viewport dimensions."""

    width: int
    height: int


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

    engine_build_identity: str
    viewport: Viewport
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
        return {
            "engine_build_identity": self.engine_build_identity,
            "viewport": {"width": self.viewport.width, "height": self.viewport.height},
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

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> EnvironmentDescriptor:
        vp = row["viewport"]
        clock_raw = row.get("clock_value", "")
        clock_value = datetime.fromisoformat(clock_raw) if clock_raw else _EPOCH_UTC
        return cls(
            engine_build_identity=str(row.get("engine_build_identity", "")),
            viewport=Viewport(width=int(vp["width"]), height=int(vp["height"])),
            device_pixel_ratio=float(row.get("device_pixel_ratio", 1.0)),
            locale=str(row.get("locale", "")),
            timezone=str(row.get("timezone", "")),
            clock_value=clock_value,
            font_set_hash=str(row.get("font_set_hash", "")),
            animation_disabled=bool(row.get("animation_disabled", False)),
            caret_disabled=bool(row.get("caret_disabled", False)),
            reduced_motion=bool(row.get("reduced_motion", False)),
            colour_scheme=str(row.get("colour_scheme", "no-preference")),
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
        def _box(raw: object) -> tuple[float, float, float, float]:
            if isinstance(raw, (list, tuple)) and len(raw) == 4:
                vals = raw
                return (float(vals[0]), float(vals[1]), float(vals[2]), float(vals[3]))
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
        return {
            "element_path": self.element_path,
            "properties": dict(self.properties),
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> ComputedStyle:
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
        return {
            "element_path": self.element_path,
            "role": self.role,
            "name": self.name,
            "state": dict(self.state),
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> A11yNode:
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


#: Schema version stamped into every render receipt. Bump only on a
#: wire-format change.
RENDER_RECEIPT_SCHEMA_VERSION = 1


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

    route: str
    viewport: Viewport
    declared_state: str
    layout_tree: tuple[LayoutBox, ...] = ()
    computed_styles: tuple[ComputedStyle, ...] = ()
    accessibility_tree: tuple[A11yNode, ...] = ()
    environment: EnvironmentDescriptor | None = None
    unstable_properties: dict[str, str] = field(default_factory=dict)
    version: int = RENDER_RECEIPT_SCHEMA_VERSION
    property_vocabulary_version: str = ""

    def _binding(self) -> dict[str, Any]:
        """Return the canonical binding dict (excludes receipt_hash)."""
        binding: dict[str, Any] = {
            "v": self.version,
            "route": self.route,
            "viewport": {"width": self.viewport.width, "height": self.viewport.height},
            "declared_state": self.declared_state,
            "layout_tree": [box.to_dict() for box in self.layout_tree],
            "computed_styles": [style.to_dict() for style in self.computed_styles],
            "accessibility_tree": [node.to_dict() for node in self.accessibility_tree],
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
        out = self._binding()
        out["receipt_hash"] = self.receipt_hash()
        return out

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> RenderReceipt:
        vp = row["viewport"]
        viewport = Viewport(width=int(vp["width"]), height=int(vp["height"]))

        layout_tree = tuple(LayoutBox.from_dict(b) for b in row.get("layout_tree", []))
        computed_styles = tuple(ComputedStyle.from_dict(s) for s in row.get("computed_styles", []))
        accessibility_tree = tuple(A11yNode.from_dict(n) for n in row.get("accessibility_tree", []))

        env: EnvironmentDescriptor | None = None
        env_raw = row.get("environment")
        if isinstance(env_raw, dict):
            env = EnvironmentDescriptor.from_dict(env_raw)

        unstable_raw = row.get("unstable_properties")
        unstable_properties: dict[str, str] = {}
        if isinstance(unstable_raw, dict):
            unstable_properties = {str(k): str(v) for k, v in unstable_raw.items()}

        return cls(
            version=int(row.get("v", RENDER_RECEIPT_SCHEMA_VERSION)),
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
