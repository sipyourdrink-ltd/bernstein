"""Tests for render receipt schema and dataclasses (issue #2362)."""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from bernstein.core.evidence.render_receipt import (
    RENDER_RECEIPT_SCHEMA_VERSION,
    A11yNode,
    ComputedStyle,
    EnvironmentDescriptor,
    LayoutBox,
    RenderDelta,
    RenderReceipt,
    Viewport,
    render_delta,
)


def test_viewport_basics() -> None:
    vp = Viewport(width=1920, height=1080)
    assert vp.width == 1920
    assert vp.height == 1080
    assert vp.to_dict() == {"width": 1920, "height": 1080}
    assert Viewport.from_dict({"width": 1920, "height": 1080}) == vp

    with pytest.raises(FrozenInstanceError):
        vp.width = 100  # type: ignore[misc]


def test_environment_descriptor_defaults_and_roundtrip() -> None:
    env = EnvironmentDescriptor()
    assert env.engine_build_identity == ""
    assert env.viewport == Viewport(0, 0)
    assert env.device_pixel_ratio == 1.0
    assert env.locale == ""
    assert env.timezone == ""
    assert env.clock_value == datetime(1970, 1, 1, tzinfo=UTC)
    assert env.font_set_hash == ""
    assert not env.animation_disabled
    assert not env.caret_disabled
    assert not env.reduced_motion
    assert env.colour_scheme == "no-preference"

    d = env.to_dict()
    assert d["colour_scheme"] == "no-preference"
    assert d["viewport"] == {"width": 0, "height": 0}

    reloaded = EnvironmentDescriptor.from_dict(d)
    assert reloaded == env

    # Custom environment descriptor
    custom_dt = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)
    custom_env = EnvironmentDescriptor(
        engine_build_identity="Chromium/128.0",
        viewport=Viewport(width=1280, height=800),
        device_pixel_ratio=2.0,
        locale="en-US",
        timezone="America/New_York",
        clock_value=custom_dt,
        font_set_hash="sha256:abc123",
        animation_disabled=True,
        caret_disabled=True,
        reduced_motion=True,
        colour_scheme="dark",
    )
    custom_dict = custom_env.to_dict()
    assert EnvironmentDescriptor.from_dict(custom_dict) == custom_env

    with pytest.raises(FrozenInstanceError):
        custom_env.locale = "fr-FR"  # type: ignore[misc]


def test_layout_box_basics_and_roundtrip() -> None:
    box = LayoutBox(
        element_path="html.body.div#app.main",
        border_box=(10.0, 20.0, 300.0, 400.0),
        content_box=(15.0, 25.0, 290.0, 390.0),
        scroll_extent=(0.0, 0.0, 300.0, 800.0),
        stacking_order=2,
        paint_order=5,
    )
    assert box.element_path == "html.body.div#app.main"
    assert box.border_box == (10.0, 20.0, 300.0, 400.0)
    assert box.content_box == (15.0, 25.0, 290.0, 390.0)
    assert box.scroll_extent == (0.0, 0.0, 300.0, 800.0)
    assert box.stacking_order == 2
    assert box.paint_order == 5

    d = box.to_dict()
    assert d["border_box"] == [10.0, 20.0, 300.0, 400.0]
    assert LayoutBox.from_dict(d) == box

    with pytest.raises(FrozenInstanceError):
        box.stacking_order = 10  # type: ignore[misc]


def test_computed_style_basics_and_roundtrip() -> None:
    style = ComputedStyle(
        element_path="html.body.header.h1",
        properties={"font-size": "24px", "color": "rgb(0, 0, 0)", "display": "block"},
    )
    assert style.element_path == "html.body.header.h1"
    assert style.properties["font-size"] == "24px"

    d = style.to_dict()
    assert ComputedStyle.from_dict(d) == style

    with pytest.raises(FrozenInstanceError):
        style.element_path = "other"  # type: ignore[misc]


def test_a11y_node_basics_and_roundtrip() -> None:
    node = A11yNode(
        element_path="html.body.button#submit",
        role="button",
        name="Submit Form",
        state={"disabled": "false", "expanded": "true"},
    )
    assert node.element_path == "html.body.button#submit"
    assert node.role == "button"
    assert node.name == "Submit Form"
    assert node.state["expanded"] == "true"

    d = node.to_dict()
    assert A11yNode.from_dict(d) == node

    with pytest.raises(FrozenInstanceError):
        node.role = "link"  # type: ignore[misc]


def test_render_receipt_empty_defaults() -> None:
    receipt = RenderReceipt()
    assert receipt.version == RENDER_RECEIPT_SCHEMA_VERSION
    assert receipt.route == ""
    assert receipt.viewport == Viewport(0, 0)
    assert receipt.declared_state == ""
    assert receipt.layout_tree == ()
    assert receipt.computed_styles == ()
    assert receipt.accessibility_tree == ()
    assert receipt.environment is None
    assert receipt.unstable_properties == {}
    assert receipt.property_vocabulary_version == ""

    d = receipt.to_dict()
    assert "receipt_hash" in d
    assert d["receipt_hash"].startswith("sha256:")
    assert RenderReceipt.from_dict(d) == receipt


def test_render_receipt_populated_and_roundtrip() -> None:
    env = EnvironmentDescriptor(
        engine_build_identity="Webkit/605.1",
        viewport=Viewport(width=1024, height=768),
        device_pixel_ratio=1.0,
        locale="en-GB",
        timezone="Europe/London",
        clock_value=datetime(2026, 8, 30, 0, 0, 0, tzinfo=UTC),
        font_set_hash="sha256:fedcba",
        animation_disabled=False,
        caret_disabled=False,
        reduced_motion=False,
        colour_scheme="light",
    )
    box = LayoutBox(
        element_path="root.container",
        border_box=(0.0, 0.0, 1024.0, 768.0),
        content_box=(0.0, 0.0, 1024.0, 768.0),
        scroll_extent=(0.0, 0.0, 1024.0, 768.0),
        stacking_order=0,
        paint_order=0,
    )
    style = ComputedStyle(
        element_path="root.container",
        properties={"background-color": "#ffffff"},
    )
    a11y = A11yNode(
        element_path="root.container",
        role="main",
        name="Main Content",
        state={},
    )
    receipt = RenderReceipt(
        version=1,
        route="/dashboard",
        viewport=Viewport(1024, 768),
        declared_state='{"user": "alice"}',
        layout_tree=(box,),
        computed_styles=(style,),
        accessibility_tree=(a11y,),
        environment=env,
        unstable_properties={"cssSubgrid": "enabled", "experimentalFont": "true"},
        property_vocabulary_version="2026.1",
    )

    d = receipt.to_dict()
    assert d["v"] == 1
    assert d["route"] == "/dashboard"
    assert d["receipt_hash"] == receipt.receipt_hash()

    reloaded = RenderReceipt.from_dict(d)
    assert reloaded == receipt
    assert reloaded.to_canonical_bytes() == receipt.to_canonical_bytes()
    assert reloaded.receipt_hash() == receipt.receipt_hash()

    with pytest.raises(FrozenInstanceError):
        receipt.route = "/settings"  # type: ignore[misc]


def test_canonical_bytes_stability_across_dict_insertion_order() -> None:
    """Receipt hashing must be independent of dict insertion order."""
    # Construct receipt 1 with key order A
    receipt1 = RenderReceipt(
        route="/test",
        viewport=Viewport(800, 600),
        declared_state="state",
        computed_styles=(
            ComputedStyle(
                element_path="p",
                properties={"color": "red", "font-size": "16px", "margin": "0"},
            ),
        ),
        accessibility_tree=(
            A11yNode(
                element_path="p",
                role="paragraph",
                name="Text",
                state={"hidden": "false", "busy": "false"},
            ),
        ),
        unstable_properties={"propB": "valB", "propA": "valA", "propC": "valC"},
    )

    # Construct receipt 2 with reversed key order in inner dicts
    receipt2 = RenderReceipt(
        route="/test",
        viewport=Viewport(800, 600),
        declared_state="state",
        computed_styles=(
            ComputedStyle(
                element_path="p",
                properties={"margin": "0", "font-size": "16px", "color": "red"},
            ),
        ),
        accessibility_tree=(
            A11yNode(
                element_path="p",
                role="paragraph",
                name="Text",
                state={"busy": "false", "hidden": "false"},
            ),
        ),
        unstable_properties={"propC": "valC", "propA": "valA", "propB": "valB"},
    )

    bytes1 = receipt1.to_canonical_bytes()
    bytes2 = receipt2.to_canonical_bytes()

    assert bytes1 == bytes2
    assert receipt1.receipt_hash() == receipt2.receipt_hash()
    assert receipt1.receipt_hash() == "sha256:" + hashlib.sha256(bytes1).hexdigest()


# ---------------------------------------------------------------------------
# Environment descriptor hash (issue #3276, step 1)
# ---------------------------------------------------------------------------


def _env(**overrides: object) -> EnvironmentDescriptor:
    """Build a fully declared environment descriptor with optional overrides."""
    base: dict[str, object] = {
        "engine_build_identity": "Chromium/128.0.6613.84",
        "viewport": Viewport(width=1280, height=800),
        "device_pixel_ratio": 2.0,
        "locale": "en-US",
        "timezone": "UTC",
        "clock_value": datetime(2026, 1, 1, tzinfo=UTC),
        "font_set_hash": "sha256:f0f0",
        "animation_disabled": True,
        "caret_disabled": True,
        "reduced_motion": True,
        "colour_scheme": "light",
    }
    base.update(overrides)
    return EnvironmentDescriptor(**base)  # type: ignore[arg-type]


def _receipt(**overrides: object) -> RenderReceipt:
    """Build a fully declared receipt with optional overrides."""
    base: dict[str, object] = {
        "route": "/dashboard",
        "viewport": Viewport(width=1280, height=800),
        "declared_state": "signed-in",
        "environment": _env(),
        "property_vocabulary_version": "2026.1",
    }
    base.update(overrides)
    return RenderReceipt(**base)  # type: ignore[arg-type]


def test_environment_descriptor_hash_is_stable_and_sensitive_to_every_field() -> None:
    """Two identical descriptors hash equal; changing any field changes the hash."""
    assert _env().descriptor_hash() == _env().descriptor_hash()
    assert _env().descriptor_hash().startswith("sha256:")

    baseline = _env().descriptor_hash()
    for field_name, changed in (
        ("engine_build_identity", "Chromium/129.0.0.0"),
        ("viewport", Viewport(width=390, height=844)),
        ("device_pixel_ratio", 1.0),
        ("locale", "de-DE"),
        ("timezone", "Europe/Berlin"),
        ("clock_value", datetime(2026, 1, 2, tzinfo=UTC)),
        ("font_set_hash", "sha256:0f0f"),
        ("animation_disabled", False),
        ("caret_disabled", False),
        ("reduced_motion", False),
        ("colour_scheme", "dark"),
    ):
        assert _env(**{field_name: changed}).descriptor_hash() != baseline, field_name


# ---------------------------------------------------------------------------
# Order-independent receipt bytes (issue #3276, step 1, acceptance (a))
# ---------------------------------------------------------------------------


def test_receipt_hash_is_independent_of_observation_insertion_order() -> None:
    """The same observations in a different sequence order hash identically."""
    boxes = (
        LayoutBox(element_path="root.a", border_box=(0.0, 0.0, 10.0, 10.0)),
        LayoutBox(element_path="root.b", border_box=(0.0, 10.0, 10.0, 10.0)),
        LayoutBox(element_path="root.c", border_box=(0.0, 20.0, 10.0, 10.0)),
    )
    styles = (
        ComputedStyle(element_path="root.a", properties={"color": "red"}),
        ComputedStyle(element_path="root.b", properties={"color": "blue"}),
    )
    nodes = (
        A11yNode(element_path="root.a", role="button", name="Save"),
        A11yNode(element_path="root.b", role="link", name="Home"),
    )

    forward = _receipt(layout_tree=boxes, computed_styles=styles, accessibility_tree=nodes)
    reversed_ = _receipt(
        layout_tree=tuple(reversed(boxes)),
        computed_styles=tuple(reversed(styles)),
        accessibility_tree=tuple(reversed(nodes)),
    )

    assert forward.to_canonical_bytes() == reversed_.to_canonical_bytes()
    assert forward.receipt_hash() == reversed_.receipt_hash()


def test_receipt_hash_collapses_duplicate_observations() -> None:
    """A repeated identical observation cannot change the receipt hash."""
    box = LayoutBox(element_path="root.a", border_box=(0.0, 0.0, 10.0, 10.0))
    once = _receipt(layout_tree=(box,))
    twice = _receipt(layout_tree=(box, box))
    assert once.receipt_hash() == twice.receipt_hash()


# ---------------------------------------------------------------------------
# render_delta: comparability (issue #3276, step 1, acceptance (b))
# ---------------------------------------------------------------------------


def test_render_delta_rejects_receipts_captured_in_different_environments() -> None:
    """Differing environment descriptor hashes are incomparable, not diffable."""
    base = _receipt(computed_styles=(ComputedStyle(element_path="root.a", properties={"color": "red"}),))
    head = _receipt(
        environment=_env(colour_scheme="dark"),
        computed_styles=(ComputedStyle(element_path="root.a", properties={"color": "blue"}),),
    )
    assert base.environment is not None
    assert head.environment is not None
    assert base.environment.descriptor_hash() != head.environment.descriptor_hash()

    result = render_delta(base, head)
    assert result.is_incomparable
    assert result.base_descriptor_hash == base.environment.descriptor_hash()
    assert result.head_descriptor_hash == head.environment.descriptor_hash()


def test_incomparable_result_carries_no_deltas() -> None:
    """An incomparable result must not be readable as an empty (clean) delta set."""
    base = _receipt(computed_styles=(ComputedStyle(element_path="root.a", properties={"color": "red"}),))
    head = _receipt(
        environment=_env(locale="de-DE"),
        computed_styles=(ComputedStyle(element_path="root.a", properties={"color": "blue"}),),
    )
    result = render_delta(base, head)
    assert result.is_incomparable
    assert result.deltas == ()
    assert not result.is_clean


def test_incomparable_reason_names_the_field_that_differs() -> None:
    """The reason string names the receipt field responsible for the rejection."""
    base = _receipt()
    assert "environment" in render_delta(base, _receipt(environment=_env(locale="fr-FR"))).incomparable_reason
    assert "route" in render_delta(base, _receipt(route="/settings")).incomparable_reason
    assert "declared_state" in render_delta(base, _receipt(declared_state="signed-out")).incomparable_reason
    assert (
        "property_vocabulary_version"
        in render_delta(base, _receipt(property_vocabulary_version="2027.1")).incomparable_reason
    )
    assert "version" in render_delta(base, _receipt(version=2)).incomparable_reason


def test_render_delta_rejects_a_receipt_with_no_declared_environment() -> None:
    """A receipt that declares no environment cannot be compared at all."""
    undeclared = _receipt(environment=None)
    result = render_delta(undeclared, _receipt())
    assert result.is_incomparable
    assert "environment" in result.incomparable_reason
    assert render_delta(_receipt(), undeclared).is_incomparable
    assert render_delta(undeclared, undeclared).is_incomparable


def test_render_delta_rejects_a_receipt_with_conflicting_values_for_one_property() -> None:
    """A receipt declaring two values for one element property is not diffable."""
    conflicted = _receipt(
        computed_styles=(
            ComputedStyle(element_path="root.a", properties={"color": "red"}),
            ComputedStyle(element_path="root.a", properties={"color": "blue"}),
        ),
    )
    result = render_delta(conflicted, _receipt())
    assert result.is_incomparable
    assert "root.a" in result.incomparable_reason
    assert "style.color" in result.incomparable_reason


# ---------------------------------------------------------------------------
# render_delta: the delta set
# ---------------------------------------------------------------------------


def test_render_delta_names_the_changed_element_path_and_property() -> None:
    """A one-declaration style change yields exactly one named delta."""
    base = _receipt(
        computed_styles=(ComputedStyle(element_path="root.a", properties={"color": "red", "display": "block"}),),
    )
    head = _receipt(
        computed_styles=(ComputedStyle(element_path="root.a", properties={"color": "blue", "display": "block"}),),
    )
    result = render_delta(base, head)
    assert not result.is_incomparable
    assert len(result.deltas) == 1
    delta = result.deltas[0]
    assert delta.route == "/dashboard"
    assert delta.viewport == Viewport(1280, 800)
    assert delta.declared_state == "signed-in"
    assert delta.element_path == "root.a"
    assert delta.property == "style.color"
    assert delta.before == "red"
    assert delta.after == "blue"


def test_render_delta_is_independent_of_observation_order() -> None:
    """The delta set is a pure function of the observations, not of their order."""
    base_styles = (
        ComputedStyle(element_path="root.a", properties={"color": "red"}),
        ComputedStyle(element_path="root.b", properties={"color": "green"}),
    )
    head_styles = (
        ComputedStyle(element_path="root.a", properties={"color": "blue"}),
        ComputedStyle(element_path="root.b", properties={"color": "yellow"}),
    )
    forward = render_delta(_receipt(computed_styles=base_styles), _receipt(computed_styles=head_styles))
    backward = render_delta(
        _receipt(computed_styles=tuple(reversed(base_styles))),
        _receipt(computed_styles=tuple(reversed(head_styles))),
    )
    assert forward == backward
    assert [d.element_path for d in forward.deltas] == ["root.a", "root.b"]


def test_render_delta_reports_appearing_and_disappearing_observations() -> None:
    """An observation present on one side only is a delta with a null counterpart."""
    base = _receipt(computed_styles=(ComputedStyle(element_path="root.gone", properties={"color": "red"}),))
    head = _receipt(computed_styles=(ComputedStyle(element_path="root.new", properties={"color": "blue"}),))
    result = render_delta(base, head)
    by_path = {d.element_path: d for d in result.deltas}
    assert by_path["root.gone"].before == "red"
    assert by_path["root.gone"].after is None
    assert by_path["root.new"].before is None
    assert by_path["root.new"].after == "blue"


def test_render_delta_covers_layout_a11y_and_unstable_observations() -> None:
    """No part of the receipt binding is silently excluded from the comparison."""
    base = _receipt(
        layout_tree=(LayoutBox(element_path="root.a", border_box=(0.0, 0.0, 10.0, 10.0), paint_order=1),),
        accessibility_tree=(A11yNode(element_path="root.a", role="button", name="Save", state={"disabled": "false"}),),
        unstable_properties={"cssSubgrid": "enabled"},
    )
    head = _receipt(
        layout_tree=(LayoutBox(element_path="root.a", border_box=(0.0, 0.0, 20.0, 10.0), paint_order=2),),
        accessibility_tree=(A11yNode(element_path="root.a", role="button", name="Store", state={"disabled": "true"}),),
        unstable_properties={"cssSubgrid": "disabled"},
    )
    changed = {d.property for d in render_delta(base, head).deltas}
    assert "layout.border_box" in changed
    assert "layout.paint_order" in changed
    assert "a11y.name" in changed
    assert "a11y.state.disabled" in changed
    assert "unstable.cssSubgrid" in changed
    assert "a11y.role" not in changed


def test_no_op_edit_produces_an_empty_delta_set() -> None:
    """Two receipts over identical observations compare clean."""
    styles = (ComputedStyle(element_path="root.a", properties={"color": "red"}),)
    result = render_delta(_receipt(computed_styles=styles), _receipt(computed_styles=styles))
    assert not result.is_incomparable
    assert result.deltas == ()
    assert result.is_clean


def test_render_delta_round_trips_through_dict() -> None:
    """A delta result survives serialisation, so it can be sealed and re-read."""
    base = _receipt(computed_styles=(ComputedStyle(element_path="root.a", properties={"color": "red"}),))
    head = _receipt(computed_styles=(ComputedStyle(element_path="root.a", properties={"color": "blue"}),))
    result = render_delta(base, head)
    assert RenderDelta.from_dict(result.to_dict()) == result

    incomparable = render_delta(base, _receipt(route="/other"))
    assert RenderDelta.from_dict(incomparable.to_dict()) == incomparable
