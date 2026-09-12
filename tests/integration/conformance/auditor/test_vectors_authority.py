"""Authority vectors: who authorized the sub-agent, and to do what (#5059).

Four vectors land here — questions 3, 4, 5 and 21 — and all four are expected
to fail. That is the deliverable. This group is the scoreboard for the
authority plane, and #5046, #5047 and #5055 are the issues that turn it green.

Every vector answers its question from the exported bundle alone. No evidence
field is added to make one pass: a weak green here would hide the largest gap
in the project, which is exactly what the scoreboard exists to show.

Each ``xfail`` is ``strict=True``, so the day the missing field lands the build
fails until the vector is un-marked, and an accidental pass can never flatter
the score.

Question 3 is the partly-answerable one. The bundle does say *who* started the
sub-agent — the journal's ``agent_spawned.started_by`` and the audit chain's
``agent.delegated`` both name ``agent-A``. What it never says is that agent-A
held any authority to delegate, or that the delegation was permitted. So the
question-marked vector asserts the whole question and carries the ``xfail``,
and a separate unmarked test pins the attribution half that does hold, so a
regression there turns something red instead of disappearing into the
``xfail``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from tests.integration.conformance.auditor import scenario

if TYPE_CHECKING:
    from tests.integration.conformance.auditor.bundle_reader import BundleReader

#: Every spelling an authorization record could plausibly use. A vector that
#: looked for one field name would pass the day someone added a differently
#: named one, so the search is over the whole bundle text and deliberately
#: wide: if any of these appears, the gap this file records has moved.
_AUTHORITY_TERMS = (
    "grant",
    "scope",
    "delegation_receipt",
    "capability",
    "permission",
    "authorized",
    "authorised",
    "parent_identity",
)


def _bundle_text(bundle: BundleReader) -> str:
    """Every JSON member of the bundle, as one searchable string."""
    return "".join(json.dumps(bundle.read_json(name)) for name in bundle.names() if name.endswith(".json"))


def _journal_events(bundle: BundleReader) -> list[dict[str, Any]]:
    receipt = bundle.read_json(scenario.RUN_RECEIPT_NAME)
    return list(receipt["journal"]["events"])


def _audit_events(bundle: BundleReader) -> list[dict[str, Any]]:
    return list(bundle.read_json(scenario.AUDIT_RECEIPT_NAME)["events"])


class TestQuestion3:
    """Was the sub-agent authorized to act, and by whom?"""

    @pytest.mark.auditor_question(3)
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "the bundle names who started the sub-agent but never that it was "
            "authorized: no grant, capability or delegation receipt appears "
            "anywhere in run-receipt.json or audit-receipt.json, so 'agent-A "
            "started agent-B' cannot be distinguished from 'agent-A was "
            "permitted to'. Needs the delegation receipt of #5047, minted on "
            "the spawn path of #5046"
        ),
    )
    def test_the_bundle_says_the_sub_agent_was_authorized_and_by_whom(self, auditor_bundle: BundleReader) -> None:
        """Attribution is not authorization, and only one of them is recorded."""
        text = _bundle_text(auditor_bundle)
        found = [term for term in _AUTHORITY_TERMS if term in text]
        assert found, (
            "no authorization evidence of any kind in the bundle: a reader can "
            "see that agent-A started agent-B and cannot see that agent-A held "
            "anything to delegate"
        )

    def test_the_bundle_does_at_least_say_who_started_the_sub_agent(self, auditor_bundle: BundleReader) -> None:
        """The half that holds, pinned so a regression in it goes red.

        The issue predicts this fails because `parent_identity_id` is never
        populated at spawn. The attribution is in fact recorded, twice and in
        two independent places — it is the *authorization* that is missing.
        """
        spawned = [
            event
            for event in _journal_events(auditor_bundle)
            if event.get("event") == "agent_spawned" and event.get("agent_id") == scenario.AGENT_B
        ]
        assert len(spawned) == 1
        assert spawned[0].get("started_by") == scenario.AGENT_A

        delegated = [event for event in _audit_events(auditor_bundle) if event.get("event_type") == "agent.delegated"]
        assert len(delegated) == 1
        assert delegated[0]["resource_id"] == scenario.AGENT_B
        assert delegated[0]["details"]["parent"] == scenario.AGENT_A


class TestQuestion4:
    """What exactly was the sub-agent permitted to do?"""

    @pytest.mark.auditor_question(4)
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "no grant record exists: nothing in the bundle states the tools, "
            "paths or duration agent-B was permitted, so the permitted set is "
            "unreadable even in principle. `DelegationScope` in "
            "core/identity/delegation_scope.py models exactly this and nothing "
            "feeds it — needs #5047"
        ),
    )
    def test_the_bundle_states_what_the_sub_agent_was_permitted_to_do(self, auditor_bundle: BundleReader) -> None:
        """A permitted set the auditor can read, not infer from what happened."""
        text = _bundle_text(auditor_bundle)
        assert any(term in text for term in ("grant", "scope", "capability", "permission")), (
            "the bundle records what agent-B did and never what it was allowed to do; the two are not the same document"
        )


class TestQuestion5:
    """Did the sub-agent stay inside that permission?"""

    @pytest.mark.auditor_question(5)
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "there is nothing to compare against: question 4 has no answer, so "
            "'stayed inside' has no referent. The bundle shows agent-B read "
            "docs/customer-list.csv and called a model; whether either was "
            "permitted is unanswerable. Needs #5047, then the narrowing check "
            "in delegation_scope.py to run against a recorded grant"
        ),
    )
    def test_the_bundle_shows_the_sub_agent_stayed_inside_its_permission(self, auditor_bundle: BundleReader) -> None:
        """Containment is a comparison, and one side of it is missing."""
        text = _bundle_text(auditor_bundle)
        assert any(term in text for term in ("grant", "scope", "capability", "permission")), (
            "no permitted set is recorded, so no action can be judged inside or outside it"
        )

    def test_what_the_sub_agent_actually_did_is_recorded(self, auditor_bundle: BundleReader) -> None:
        """The half that holds: the actions exist, only the yardstick is absent.

        Worth pinning separately. If these rows ever stopped being recorded,
        question 5 would still be red and the reason would have quietly
        changed from "no permission to compare against" to "no actions
        either", which is a different and worse defect.
        """
        by_agent_b = [event for event in _journal_events(auditor_bundle) if event.get("agent_id") == scenario.AGENT_B]
        kinds = {event.get("event") for event in by_agent_b}
        assert {"tool_call", "model_call"} <= kinds
        tool_call = next(event for event in by_agent_b if event.get("event") == "tool_call")
        assert tool_call["path"] == scenario.SENSITIVE_PATH


class TestQuestion21:
    """Which other principals hold authority derived from the same grant?"""

    @pytest.mark.auditor_question(21)
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "no grant graph: with no grant identity on any record there is "
            "nothing for a sibling to be derived *from*, so the blast radius "
            "of one compromised grant cannot be read off the bundle. Needs "
            "#5047 for the receipt and #5055 for the chain that links them"
        ),
    )
    def test_the_bundle_names_every_principal_sharing_the_sub_agents_grant(self, auditor_bundle: BundleReader) -> None:
        """The question an incident responder asks first, and cannot ask here."""
        text = _bundle_text(auditor_bundle)
        assert any(term in text for term in ("grant", "delegation_receipt", "parent_identity")), (
            "no grant identity appears on any record, so 'derived from the "
            "same grant' cannot be evaluated for any principal"
        )
