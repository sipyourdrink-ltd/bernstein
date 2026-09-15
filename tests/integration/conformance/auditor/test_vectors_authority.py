"""Authority vectors: who authorized the sub-agent, and to do what (#5059).

Four vectors land here - questions 3, 4, 5 and 21 - and all four are expected
to fail. That is the deliverable. This group is the scoreboard for the
authority plane, and #5046, #5047 and #5055 are the issues that turn it green.

Every vector answers its question from the exported bundle alone. No evidence
field is added to make one pass: a weak green here would hide the largest gap
in the project, which is exactly what the scoreboard exists to show.

Each ``xfail`` is ``strict=True``, so the day the missing field lands the build
fails until the vector is un-marked, and an accidental pass can never flatter
the score. Which is why nothing here searches the bundle for a *substring*.
The bundle is read structurally - object keys and event types, matched whole -
because the vocabulary of authority overlaps the vocabulary of everything
else: ``core/replay/provider_state.py`` records a journal event whose payload
key is literally ``capability`` on every spawn, meaning mutation-observability
and not permission, and any audit detail carrying ``401 Unauthorized``
contains ``authorized``. A substring search would have gone green on either
one while no authorization record existed anywhere - the precise failure this
group is here to report. :class:`TestTheSearchItself` pins that.

What the bundle records about the spawn at all is deliberately not asserted
here. ``agent_spawned.started_by`` and the ``agent.delegated`` audit event
exist only because ``scenario.py`` writes them; no production writer emits
either field (``grep -rn started_by src/`` is empty), so a test asserting them
would pin the fixture rather than the product and could not fail if production
regressed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from tests.integration.conformance.auditor import scenario

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.integration.conformance.auditor.bundle_reader import BundleReader

#: Object keys that would *be* a record of authority, matched whole.
#:
#: Deliberately wide across spellings - a vector that looked for one field name
#: would stay red the day someone landed a differently named one - and
#: deliberately exact. Two names that a substring search would have caught are
#: absent on purpose:
#:
#: * ``capability``, because ``provider_state.record_mutation_capability``
#:   already writes that key on the spawn path with an unrelated meaning;
#: * bare ``scope`` and ``permission``, because they are common enough in
#:   unrelated payloads that matching them proves nothing about delegation.
#:
#: Their authority-bearing spellings (``capability_ceiling``,
#: ``permitted_scope``, ``delegation_scope``) are listed instead.
_AUTHORITY_KEYS = frozenset(
    {
        "authorised",
        "authorised_by",
        "authorized",
        "authorized_by",
        "capability_ceiling",
        "delegation_receipt",
        "delegation_receipt_id",
        "delegation_scope",
        "grant",
        "grant_id",
        "granted_by",
        "grants",
        "parent_identity",
        "parent_identity_id",
        "permitted_paths",
        "permitted_scope",
        "permitted_tools",
    }
)

#: Event types that would themselves be an authorization record. Matched whole
#: against ``event``/``event_type``, so ``agent.delegated`` - which records
#: that a spawn happened, not that it was permitted - is not among them.
_AUTHORITY_EVENT_TYPES = frozenset(
    {
        "agent.authorized",
        "delegation.granted",
        "delegation.receipt",
        "grant.issued",
        "grant.revoked",
        "identity.delegated",
    }
)


def _bundle_documents(bundle: BundleReader) -> Iterator[Any]:
    """Every JSON document the bundle holds, including inside ``article12.zip``.

    The zip is not a sealed box to an auditor - it is four more files, three
    JSON and one JSONL - so a grant record landing in there has to count as
    found. Reading it any other way would mean this group could report "no
    authorization evidence" while the evidence sat one archive deep.
    """
    for name in bundle.names():
        if name.endswith(".json"):
            yield bundle.read_json(name)
        elif name.endswith(".zip"):
            for member in bundle.zip_members(name):
                raw = bundle.read_zip_member(name, member).decode("utf-8")
                if member.endswith(".jsonl"):
                    yield from (json.loads(line) for line in raw.splitlines() if line.strip())
                elif member.endswith(".json"):
                    yield json.loads(raw)


def _objects(node: Any) -> Iterator[dict[str, Any]]:
    """Every JSON object anywhere under *node*, the node itself included."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _objects(value)


def _authority_records(documents: Iterator[Any] | list[Any]) -> list[dict[str, Any]]:
    """Objects that are, structurally, a record of authority."""

    def _is_authority(obj: dict[str, Any]) -> bool:
        # A key whose *name* is a grant field, or an event whose *type* is a
        # grant. Both matched whole: the point of reading the bundle this way
        # is that no value a payload happens to contain can answer for one.
        return bool(_AUTHORITY_KEYS & obj.keys()) or bool(
            {obj.get("event"), obj.get("event_type")} & _AUTHORITY_EVENT_TYPES
        )

    return [obj for document in documents for obj in _objects(document) if _is_authority(obj)]


def _permitted_set(documents: list[Any], agent_id: str) -> dict[str, frozenset[str]] | None:
    """The tools and paths *agent_id* was permitted, if the bundle states them.

    ``None`` means no readable permitted set, which is a different finding from
    an empty one: an empty grant permits nothing, and every action would then
    be outside it.
    """
    for record in _authority_records(documents):
        subject = record.get("agent_id") or record.get("resource_id") or record.get("subject")
        if subject != agent_id:
            continue
        tools = record.get("permitted_tools")
        paths = record.get("permitted_paths")
        scope = record.get("permitted_scope") or record.get("delegation_scope")
        if isinstance(scope, dict):
            tools = tools if tools is not None else scope.get("tools")
            paths = paths if paths is not None else scope.get("paths")
        if tools is None and paths is None:
            continue
        return {
            "tools": frozenset(tools or ()),
            "paths": frozenset(paths or ()),
        }
    return None


def _journal_events(bundle: BundleReader) -> list[dict[str, Any]]:
    receipt = bundle.read_json(scenario.RUN_RECEIPT_NAME)
    return list(receipt["journal"]["events"])


def _actions_of(bundle: BundleReader, agent_id: str) -> dict[str, frozenset[str]]:
    """What *agent_id* is recorded as having actually done."""
    mine = [event for event in _journal_events(bundle) if event.get("agent_id") == agent_id]
    return {
        "tools": frozenset(event["tool"] for event in mine if event.get("event") == "tool_call" and event.get("tool")),
        "paths": frozenset(event["path"] for event in mine if event.get("path")),
    }


class TestTheSearchItself:
    """The detector above, tested on records that would fool a substring search.

    Not a conformance vector - it answers no auditor question. It is here
    because every vector in this file is ``xfail(strict=True)``: a detector
    that says "found authorization evidence" too easily does not just weaken
    this group, it turns the whole suite red with an XPASS while the gap it
    reports is still wide open.
    """

    def test_a_mutation_capability_record_is_not_an_authorization_record(self) -> None:
        """``provider_state.py`` writes this on every spawn. It is not a grant.

        ``record_mutation_capability`` records whether an adapter can observe
        provider-side mutations - ``capability`` as in "is able to", not "is
        permitted to". Searching the bundle text for ``"capability"`` would
        have matched it on any real run.
        """
        spawn_tick = {
            "journal": {
                "events": [
                    {"event": "provider_state_capability", "adapter": "claude", "capability": "observed"},
                ]
            }
        }
        assert _authority_records([spawn_tick]) == []

    def test_an_unauthorized_error_detail_is_not_an_authorization_record(self) -> None:
        """``"401 Unauthorized"`` contains ``"authorized"``.

        An audit detail quoting an upstream HTTP failure is the opposite of a
        grant, and a substring search would have read it as one.
        """
        failure = {
            "events": [
                {"event_type": "tool.invoked", "details": {"error": "401 Unauthorized", "status": 401}},
            ]
        }
        assert _authority_records([failure]) == []

    def test_an_attribution_record_is_not_an_authorization_record(self) -> None:
        """Who started it is not who permitted it, and only one is a grant.

        This is the distinction the whole group turns on, so the detector has
        to hold it: ``agent.delegated`` says a spawn happened.
        """
        attribution = {
            "events": [
                {"event_type": "agent.delegated", "resource_id": "agent-B", "details": {"parent": "agent-A"}},
                {"event": "agent_spawned", "agent_id": "agent-B", "started_by": "agent-A"},
            ]
        }
        assert _authority_records([attribution]) == []

    def test_a_real_grant_record_is_found_wherever_it_sits(self) -> None:
        """The detector has to be able to go green, or it proves nothing.

        Nested arbitrarily deep, because a delegation receipt landing inside
        the article 12 archive or under a payload envelope is still evidence.
        """
        buried = {"payload": {"artefacts": [{"delegation_receipt_id": "dr-1", "agent_id": "agent-B"}]}}
        assert len(_authority_records([buried])) == 1


class TestQuestion3:
    """Was the sub-agent authorized to act, and by whom?"""

    @pytest.mark.auditor_question(3)
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "the bundle records that agent-A started agent-B and never that it "
            "was permitted to: no grant, delegation receipt or capability "
            "ceiling appears on any record, in any bundle member, so "
            "attribution cannot be distinguished from authorization. Needs the "
            "delegation receipt of #5047, minted on the spawn path of #5046"
        ),
    )
    def test_the_bundle_says_the_sub_agent_was_authorized_and_by_whom(self, auditor_bundle: BundleReader) -> None:
        """Attribution is not authorization, and only one of them is recorded."""
        records = _authority_records(list(_bundle_documents(auditor_bundle)))
        assert records, (
            "no authorization record of any kind in the bundle: a reader can "
            "see that agent-A started agent-B and cannot see that agent-A held "
            "anything to delegate"
        )


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
            "feeds it - needs #5047"
        ),
    )
    def test_the_bundle_states_what_the_sub_agent_was_permitted_to_do(self, auditor_bundle: BundleReader) -> None:
        """A permitted set the auditor can read, not infer from what happened."""
        permitted = _permitted_set(list(_bundle_documents(auditor_bundle)), scenario.AGENT_B)
        assert permitted is not None, (
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
        """Containment, actually compared - not question 4 asked a second time.

        The permitted set existing is question 4's answer. This one is the
        comparison: every tool agent-B called and every path it touched has to
        appear in that set. So when a grant does land, this vector goes green
        only if agent-B stayed inside it, and reports the specific overrun if
        it did not.
        """
        permitted = _permitted_set(list(_bundle_documents(auditor_bundle)), scenario.AGENT_B)
        assert permitted is not None, "no permitted set is recorded, so no action can be judged inside or outside it"

        did = _actions_of(auditor_bundle, scenario.AGENT_B)
        outside = {
            "tools": did["tools"] - permitted["tools"],
            "paths": did["paths"] - permitted["paths"],
        }
        assert not outside["tools"] and not outside["paths"], (
            f"agent-B acted outside its recorded grant: tools {sorted(outside['tools'])}, "
            f"paths {sorted(outside['paths'])}"
        )

    def test_what_the_sub_agent_actually_did_is_recorded(self, auditor_bundle: BundleReader) -> None:
        """The half that holds: the actions exist, only the yardstick is absent.

        Worth pinning separately. If these rows ever stopped being recorded,
        question 5 would still be red and the reason would have quietly
        changed from "no permission to compare against" to "no actions
        either", which is a different and worse defect.

        Unlike the spawn attribution, these are production rows: ``tool_call``
        journal events are written by ``core/instrumentation.py`` and read by
        ``core/observability/trust_record.py``, so this can fail if production
        regresses.
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
        """The question an incident responder asks first, and cannot ask here.

        Answering it needs two things: a grant to have an identity, and
        records to carry it. Without the first, the second cannot be asked
        about - so this checks that some authority record names a grant the
        blast radius could be computed over.
        """
        documents = list(_bundle_documents(auditor_bundle))
        identifiable = [
            record
            for record in _authority_records(documents)
            if record.keys() & {"grant_id", "delegation_receipt_id", "parent_identity_id"}
        ]
        assert identifiable, (
            "no grant identity appears on any record, so 'derived from the "
            "same grant' cannot be evaluated for any principal"
        )
