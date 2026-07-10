"""Certified local configuration golden replays (issue #2356).

AC coverage:

* AC4 -- the docs list at least 3 verified local configurations. Each row in
  the certified-config table is backed by a golden transcript recorded from
  that engine and replayed through the conformance evaluator here, so the
  table cannot drift from what the certifier actually accepts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.endpoints.conformance import (
    LOCAL_TIER_ROLES,
    evaluate_roles,
    run_conformance,
)

_GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
_DOCS_PAGE = Path(__file__).resolve().parents[3] / "docs" / "reference" / "local-endpoints.md"

_ENGINE_FIXTURES = ("ollama.json", "lmstudio.json", "mlx_lm.json")


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((_GOLDEN_DIR / name).read_text(encoding="utf-8"))


def _transport_for(fixture: dict[str, Any]):
    responses = fixture["responses"]

    def transport(method: str, url: str, headers: object, body: bytes | None, timeout: float) -> tuple[int, bytes]:
        if method == "GET":
            entry = responses["models"]
        else:
            request = json.loads(body or b"{}")
            prompt = "\n".join(str(m.get("content", "")) for m in request.get("messages", []))
            if request.get("tools"):
                entry = responses["tool"]
            elif "unified diff" in prompt:
                entry = responses["patch"]
            elif len(prompt) > 8000:
                entry = responses["context"]
            else:
                entry = responses["chat"]
        return entry["status"], json.dumps(entry["body"]).encode("utf-8")

    return transport


@pytest.mark.parametrize("fixture_name", _ENGINE_FIXTURES)
def test_golden_engine_transcript_certifies_every_role(fixture_name: str) -> None:
    fixture = _load_fixture(fixture_name)
    transcript = run_conformance(
        base_url="http://127.0.0.1:11434/v1",
        model=fixture["model"],
        transport=_transport_for(fixture),
    )
    verdicts = evaluate_roles(transcript, (*sorted(LOCAL_TIER_ROLES), "manager"))
    rejected = [(v.role, v.reasons) for v in verdicts if not v.certified]
    assert rejected == [], f"{fixture['engine']} golden transcript must certify: {rejected}"


def test_docs_certified_config_table_matches_golden_fixtures() -> None:
    assert _DOCS_PAGE.is_file(), "docs/reference/local-endpoints.md is missing"
    text = _DOCS_PAGE.read_text(encoding="utf-8")
    assert len(_ENGINE_FIXTURES) >= 3
    for name in _ENGINE_FIXTURES:
        fixture = _load_fixture(name)
        assert fixture["engine"] in text, f"docs must list engine {fixture['engine']}"
        assert fixture["model"] in text, f"docs must list model {fixture['model']}"
        assert f"{fixture['ram_budget_gb']} GB" in text, f"docs must list the RAM budget for {fixture['engine']}"
