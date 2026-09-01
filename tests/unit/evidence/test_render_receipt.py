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
    RenderReceipt,
    Viewport,
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
