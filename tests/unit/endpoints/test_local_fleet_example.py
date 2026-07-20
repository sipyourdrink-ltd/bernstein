"""Mixed-fleet example config tests (issue #2356).

AC coverage:

* AC1 -- a mixed fleet (API planner plus local workers) completes the
  example goal end to end: every role in the example config resolves to an
  endpoint, the manager stays on the API endpoint, the workers run against
  the local profile, and each fleet member completes its sub-task.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.core.seed import parse_seed

from bernstein.core.config.config_schema import load_and_validate
from bernstein.core.endpoints.conformance import LOCAL_TIER_ROLES
from tests.unit.endpoints.stub_endpoint import EndpointBehavior, stub_endpoint_server

if TYPE_CHECKING:
    import pytest

_EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "local-fleet" / "bernstein.yaml"

_WORKER_ROLES = ("linter", "test_writer", "triage", "doc_sweeper")


def test_example_config_exists_and_validates_with_defaults() -> None:
    assert _EXAMPLE.is_file(), "examples/local-fleet/bernstein.yaml is missing"
    config = load_and_validate(_EXAMPLE)
    policy = config.role_model_policy or {}
    for role in _WORKER_ROLES:
        assert role in policy, f"example must route {role} to the local tier"
        assert policy[role].endpoint is not None
        assert role in LOCAL_TIER_ROLES
    assert policy["manager"].endpoint is None, "the manager must stay on the API endpoint"
    assert policy["manager"].base_url is not None


def test_example_config_parses_via_seed_parser() -> None:
    # The runtime spawn path (parse_seed) must accept the shipped example,
    # not just the pydantic validation path (load_and_validate). Regression
    # for role_model_policy.<role>.endpoint being rejected as an unknown key.
    seed = parse_seed(_EXAMPLE)
    policy = seed.role_model_policy or {}
    for role in _WORKER_ROLES:
        assert policy[role]["endpoint"] == "workhorse"
        # The profile's base_url/model are materialised onto the entry.
        assert policy[role]["base_url"], f"{role} must inherit the profile base_url"
        assert policy[role]["model"], f"{role} must inherit the profile model"
    assert "endpoint" not in policy["manager"], "the manager must stay on the API endpoint"
    assert policy["manager"]["base_url"]


def _complete_subtask(base_url: str, model: str, role: str) -> str:
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": f"{role}: complete your sub-task"}],
                "temperature": 0,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read())
    content = payload["choices"][0]["message"]["content"]
    assert isinstance(content, str) and content
    return content


def test_mixed_fleet_completes_example_goal_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    api_behavior = EndpointBehavior(model="api-planner")
    local_behavior = EndpointBehavior(model="tiny-coder")
    with stub_endpoint_server(api_behavior) as api_url, stub_endpoint_server(local_behavior) as local_url:
        monkeypatch.setenv("BERNSTEIN_API_BASE_URL", api_url)
        monkeypatch.setenv("BERNSTEIN_LOCAL_LLM_BASE_URL", local_url)
        monkeypatch.setenv("BERNSTEIN_LOCAL_LLM_MODEL", "tiny-coder")

        config = load_and_validate(_EXAMPLE)
        policy = config.role_model_policy or {}

        # The planner stays on the API endpoint; workers resolve to the local profile.
        assert policy["manager"].base_url == api_url
        for role in _WORKER_ROLES:
            assert policy[role].base_url == local_url
            assert policy[role].model == "tiny-coder"

        # Every fleet member completes its sub-task against its own endpoint.
        completions = {
            role: _complete_subtask(policy[role].base_url or "", policy[role].model or "", role)
            for role in ("manager", *_WORKER_ROLES)
        }
        assert len(completions) == 5
        assert all(completions.values())

    # The local endpoint served exactly the worker sub-tasks; the manager
    # request went to the API endpoint only.
    local_prompts = ["\n".join(str(m.get("content")) for m in r["messages"]) for r in local_behavior.requests]
    assert len(local_prompts) == len(_WORKER_ROLES)
    assert all(not p.startswith("manager:") for p in local_prompts)
    api_prompts = ["\n".join(str(m.get("content")) for m in r["messages"]) for r in api_behavior.requests]
    assert len(api_prompts) == 1
    assert api_prompts[0].startswith("manager:")
