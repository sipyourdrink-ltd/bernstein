"""Automation bridge platform payload mappings (#2512).

The adapters are deliberately thin: unwrap the platform's envelope, name the
header carrying the replay nonce, and hand a normalised intent to the shared
receipt core. These tests pin exactly that much, plus the property that all
three platforms sending the same logical trigger project the same graph.
"""

from __future__ import annotations

import pytest

from bernstein.core.trigger_sources.automation_platforms import (
    ADAPTERS,
    DEFAULT_PLATFORM,
    GENERIC_TRIGGER_ID_HEADER,
    adapter_for,
    normalise_trigger,
    resolve_platform,
)
from bernstein.core.trigger_sources.receipt import project_task_graph


def test_every_documented_platform_has_an_adapter() -> None:
    """The three recipes in docs/integrations each map to an adapter."""
    assert {"n8n", "zapier", "workato", DEFAULT_PLATFORM} <= set(ADAPTERS)


def test_unknown_platform_falls_back_to_generic() -> None:
    """An unrecognised label never raises; it maps to the generic adapter."""
    assert adapter_for("no-such-platform").platform == DEFAULT_PLATFORM


@pytest.mark.parametrize(
    ("platform", "payload"),
    [
        ("n8n", {"body": {"title": "Rotate key", "description": "quarterly"}}),
        ("zapier", {"data": {"title": "Rotate key", "description": "quarterly"}}),
        ("workato", {"input": {"title": "Rotate key", "description": "quarterly"}}),
        (DEFAULT_PLATFORM, {"title": "Rotate key", "description": "quarterly"}),
    ],
)
def test_each_platform_envelope_unwraps_to_the_same_intent(platform: str, payload: dict) -> None:
    """The envelope shape is platform-specific; the intent it yields is not."""
    _, intent, _ = normalise_trigger(payload=payload, headers={}, platform=platform)
    assert intent["title"] == "Rotate key"
    assert intent["description"] == "quarterly"


def test_the_same_logical_trigger_projects_the_same_graph_across_platforms() -> None:
    """Platform differences stop at the mapping; the graph is shared."""
    payloads = {
        "n8n": {"body": {"title": "Ship it", "description": "cut a release"}},
        "zapier": {"data": {"title": "Ship it", "description": "cut a release"}},
        "workato": {"input": {"title": "Ship it", "description": "cut a release"}},
    }
    digests = set()
    for platform, payload in payloads.items():
        _, intent, _ = normalise_trigger(payload=payload, headers={}, platform=platform)
        # Project under one platform label so the comparison isolates the
        # mapping; the label is part of the graph binding by design.
        digests.add(project_task_graph(platform="n8n", intent=intent).graph_digest)
    assert len(digests) == 1


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"x-n8n-execution-id": "exec-9"}, "n8n"),
        ({"X-Zapier-Request-Id": "zap-1"}, "zapier"),
        ({"x-workato-job-id": "job-3"}, "workato"),
        ({}, DEFAULT_PLATFORM),
    ],
)
def test_platform_resolves_from_its_own_header(headers: dict[str, str], expected: str) -> None:
    """Header matching is case-insensitive and picks the right adapter."""
    assert resolve_platform(headers) == expected


def test_declared_platform_header_wins() -> None:
    """An explicit label overrides header sniffing."""
    assert resolve_platform({"x-bernstein-platform": "workato", "x-n8n-execution-id": "e"}) == "workato"


def test_declared_unknown_platform_falls_back() -> None:
    """A declared label that names no adapter does not win."""
    assert resolve_platform({"x-bernstein-platform": "nope"}) == DEFAULT_PLATFORM


def test_generic_trigger_id_header_is_preferred() -> None:
    """An operator can always pin the replay nonce explicitly."""
    headers = {GENERIC_TRIGGER_ID_HEADER: "pinned", "x-n8n-execution-id": "exec-9"}
    _, _, trigger_id = normalise_trigger(payload={}, headers=headers, platform="n8n")
    assert trigger_id == "pinned"


def test_platform_header_supplies_the_trigger_id() -> None:
    """Without an explicit nonce the platform's execution id is used."""
    _, _, trigger_id = normalise_trigger(payload={}, headers={"x-n8n-execution-id": "exec-9"}, platform="n8n")
    assert trigger_id == "exec-9"


def test_missing_trigger_id_is_reported_not_invented() -> None:
    """No nonce means an empty id; the caller decides how to refuse."""
    _, _, trigger_id = normalise_trigger(payload={}, headers={}, platform="n8n")
    assert trigger_id == ""


def test_step_lists_normalise_to_graph_nodes() -> None:
    """A multi-step recipe payload projects one node per step."""
    payload = {"input": {"title": "Release", "steps": [{"title": "test"}, {"title": "tag"}]}}
    _, intent, _ = normalise_trigger(payload=payload, headers={}, platform="workato")
    graph = project_task_graph(platform="workato", intent=intent)
    assert [node.title for node in graph.nodes] == ["test", "tag"]


def test_scalar_step_entries_are_accepted() -> None:
    """A bare string step still projects a node rather than failing."""
    payload = {"title": "Release", "tasks": ["run tests", "cut tag"]}
    _, intent, _ = normalise_trigger(payload=payload, headers={}, platform=DEFAULT_PLATFORM)
    graph = project_task_graph(platform=DEFAULT_PLATFORM, intent=intent)
    assert [node.title for node in graph.nodes] == ["run tests", "cut tag"]


def test_numeric_priority_is_carried_through() -> None:
    """Non-string scalars in the payload are coerced, not dropped."""
    _, intent, _ = normalise_trigger(payload={"title": "x", "priority": 1}, headers={}, platform=DEFAULT_PLATFORM)
    assert intent["priority"] == "1"
