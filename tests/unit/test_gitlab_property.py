"""Hypothesis property-based tests for the GitLab integration."""

from __future__ import annotations

import json

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bernstein.gitlab_app.mapper import (
    merge_request_to_tasks,
    note_to_task,
    pipeline_to_tasks,
)
from bernstein.gitlab_app.slash_commands import parse_slash_command, slash_command_to_task
from bernstein.gitlab_app.webhooks import GitLabWebhookEvent, parse_webhook, verify_token

_ASCII = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=0,
    max_size=64,
)

_NON_EMPTY_ASCII = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=32,
)


def _build_event(kind: str, action: str = "open", note: str = "") -> GitLabWebhookEvent:
    payload = {
        "object_kind": kind,
        "object_attributes": {
            "iid": 1,
            "title": "T",
            "description": "B",
            "note": note,
            "noteable_type": "MergeRequest",
            "action": action,
            "id": 1,
            "sha": "abcdef0123",
            "ref": "main",
            "url": "https://x",
            "labels": [],
        },
        "project": {"path_with_namespace": "a/b", "id": 1},
        "user": {"username": "u"},
        "merge_request": {"iid": 1, "title": "T"},
        "builds": [],
    }
    return GitLabWebhookEvent(
        event_type="X Hook",
        object_kind=kind,
        action=action,
        project_path="a/b",
        sender="u",
        payload=payload,
    )


class TestVerifyTokenProperty:
    @given(_NON_EMPTY_ASCII)
    def test_self_matches(self, token: str) -> None:
        assert verify_token(token, token) is True

    @given(_NON_EMPTY_ASCII, _NON_EMPTY_ASCII)
    def test_distinct_no_match(self, a: str, b: str) -> None:
        if a == b:
            return
        assert verify_token(a, b) is False

    @given(_ASCII)
    def test_empty_one_side(self, t: str) -> None:
        assert verify_token("", t) is False
        assert verify_token(t, "") is False


class TestParseWebhookRoundTrip:
    @given(
        kind=st.sampled_from(["merge_request", "pipeline", "note", "issue"]),
        sender=st.text(
            alphabet=st.characters(min_codepoint=97, max_codepoint=122),
            min_size=1,
            max_size=12,
        ),
        path=st.tuples(
            st.text(min_size=1, max_size=10, alphabet=st.characters(min_codepoint=97, max_codepoint=122)),
            st.text(min_size=1, max_size=10, alphabet=st.characters(min_codepoint=97, max_codepoint=122)),
        ),
    )
    @settings(derandomize=True, suppress_health_check=[HealthCheck.too_slow], deadline=None, max_examples=80)
    def test_round_trip(self, kind: str, sender: str, path: tuple[str, str]) -> None:
        ns, proj = path
        full = f"{ns}/{proj}"
        payload = {
            "object_kind": kind,
            "object_attributes": {"action": "open"},
            "project": {"path_with_namespace": full},
            "user": {"username": sender},
        }
        body = json.dumps(payload).encode("utf-8")
        evt = parse_webhook({"X-Gitlab-Event": "Test Hook"}, body)
        assert evt.object_kind == kind
        assert evt.sender == sender
        assert evt.project_path == full


class TestMergeRequestActionInvariants:
    @given(action=st.text(min_size=0, max_size=20))
    def test_non_open_actions_yield_no_tasks(self, action: str) -> None:
        if action in {"open", "reopen"}:
            return
        event = _build_event("merge_request", action=action)
        assert merge_request_to_tasks(event) == []

    @given(
        body=st.text(min_size=0, max_size=3000),
    )
    @settings(derandomize=True, max_examples=60, deadline=None)
    def test_open_always_creates_one_task(self, body: str) -> None:
        event = _build_event("merge_request", action="open")
        # mutate description
        event.payload["object_attributes"]["description"] = body
        out = merge_request_to_tasks(event)
        assert len(out) == 1
        # description always truncates to body + prefix, body length ≤ 2000
        assert out[0]["description"].endswith(body[:2000])


class TestPipelineStatusInvariants:
    @given(status=st.text(min_size=0, max_size=20))
    def test_non_failed_yields_no_tasks(self, status: str) -> None:
        if status == "failed":
            return
        event = _build_event("pipeline")
        event.payload["object_attributes"]["status"] = status
        assert pipeline_to_tasks(event) == []

    def test_failed_always_creates_one(self) -> None:
        event = _build_event("pipeline")
        event.payload["object_attributes"]["status"] = "failed"
        out = pipeline_to_tasks(event)
        assert len(out) == 1


class TestSlashCommandInvariants:
    @given(verb=st.text(min_size=1, max_size=20, alphabet=st.characters(min_codepoint=97, max_codepoint=122)))
    def test_parse_returns_lower_verb(self, verb: str) -> None:
        text = f"/bernstein {verb} foo bar"
        out = parse_slash_command(text)
        assert out is not None
        assert out[0] == verb.lower()

    @given(verb=st.sampled_from(["fix", "plan", "evolve", "qa", "review"]), args=_ASCII)
    @settings(derandomize=True, max_examples=60)
    def test_known_verbs_always_produce_task(self, verb: str, args: str) -> None:
        event = _build_event("note", note=f"/bernstein {verb} {args}")
        task = slash_command_to_task(event, verb, args)
        assert task is not None
        assert task["title"]
        assert task["role"]

    @given(verb=st.text(min_size=1, max_size=8, alphabet=st.characters(min_codepoint=97, max_codepoint=122)))
    @settings(derandomize=True, max_examples=40)
    def test_unknown_verb_none(self, verb: str) -> None:
        if verb in {"fix", "plan", "evolve", "qa", "review"}:
            return
        event = _build_event("note")
        assert slash_command_to_task(event, verb, "") is None


class TestNoteToTaskInvariants:
    @given(text=_ASCII)
    @settings(derandomize=True, max_examples=60)
    def test_non_actionable_non_slash_returns_none(self, text: str) -> None:
        actionable_words = (
            "fix",
            "change",
            "update",
            "replace",
            "remove",
            "add",
            "refactor",
            "should",
            "must",
            "consider",
        )
        if "/bernstein" in text.lower():
            return
        if any(w in text.lower() for w in actionable_words):
            return
        if "```suggestion" in text.lower():
            return
        event = _build_event("note", note=text)
        assert note_to_task(event) is None
