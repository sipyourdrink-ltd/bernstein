"""Bounded registry federation with donor-selected trust and offline transports."""

import json
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.protocols.volunteer.documents import sign_document
from bernstein.core.volunteer import registry

A, B, C, D = (f"https://{name}.example/index.json" for name in "abcd")
REPO = "https://github.com/example/project"


def document(*children: str, projects: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"version": 2, "indexes": list(children), "projects": projects or []}


class Transport:
    def __init__(self, documents: dict[str, Any]) -> None:
        self.documents = documents
        self.calls: list[str] = []

    def get(self, url: str, *, headers: dict[str, str]) -> registry.HTTPResponse:
        self.calls.append(url)
        value = self.documents.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return registry.HTTPResponse(404, b"", None)
        return registry.HTTPResponse(200, json.dumps(value).encode(), None)


def policy(**kwargs: Any) -> Any:
    return registry.FederationPolicy(**kwargs)


def resolve(transport: Transport, roots: list[str] | None = None, **kwargs: Any) -> Any:
    return registry.resolve_indexes(roots or [A], transport=transport, policy=policy(**kwargs))


def signed(doc: dict[str, Any], key: Ed25519PrivateKey) -> dict[str, Any]:
    envelope = sign_document(doc, signing_key=key, document_kind="registry-index")
    return {**doc, "signature": json.loads(envelope.to_json())}


def test_index_entry_preserves_added_by_instead_of_silently_dropping_it() -> None:
    entry = registry._parse_index(
        json.dumps(document(projects=[{"repo_url": REPO, "added_by": "maintainer"}])).encode(), A
    )[0]
    assert getattr(entry, "added_by", None) == "maintainer"


@pytest.mark.parametrize("urls", [(A,), (A, B), (A, B, C)])
def test_cycles_terminate_and_report_every_member(urls: tuple[str, ...]) -> None:
    transport = Transport({url: document(urls[(i + 1) % len(urls)]) for i, url in enumerate(urls)})
    result = resolve(transport, allow_unsigned=True)
    assert transport.calls == list(urls)
    assert result.cycle_members == tuple(sorted(urls))
    assert result.passed
    assert result.as_record()["cycle_members"] == list(sorted(urls))


def test_multiple_roots_do_not_hide_a_cycle() -> None:
    result = resolve(Transport({A: document(B), B: document(A)}), roots=[A, B], allow_unsigned=True)
    assert result.cycle_members == (A, B)


def test_breadth_first_traversal_deduplicates_a_diamond_without_reporting_a_cycle() -> None:
    transport = Transport({A: document(B, C), B: document(D), C: document(D), D: document()})
    result = resolve(transport, allow_unsigned=True)
    assert transport.calls == [A, B, C, D]
    assert result.cycle_members == ()


@pytest.mark.parametrize("depth,expected", [(0, [A]), (1, [A, B]), (2, [A, B, C])])
def test_depth_limit_bounds_actual_fetches_including_the_root(depth: int, expected: list[str]) -> None:
    transport = Transport({A: document(B), B: document(C), C: document()})
    result = resolve(transport, allow_unsigned=True, max_depth=depth)
    assert transport.calls == expected
    assert result.visited_indexes == tuple(expected)
    assert result.passed == (depth == 2)
    if depth < 2:
        assert "depth" in result.skipped[0].reason


def test_index_budget_bounds_fetches_even_on_wide_graphs() -> None:
    transport = Transport({A: document(B, C, D), B: document(), C: document(), D: document()})
    result = resolve(transport, allow_unsigned=True, max_indexes=2)
    assert transport.calls == [A, B]
    assert {item.repo_url for item in result.skipped} == {C, D}
    assert all("limit" in item.reason for item in result.skipped)


def test_identical_content_at_distinct_urls_is_visited_once_per_url() -> None:
    transport = Transport({A: document(B, C), B: document(), C: document()})
    result = resolve(transport, allow_unsigned=True)
    assert transport.calls == [A, B, C]
    assert not result.cycle_members


def test_a_signed_index_verifies_against_its_pinned_key() -> None:
    key = Ed25519PrivateKey.generate()
    transport = Transport({A: signed(document(projects=[{"repo_url": REPO}]), key)})
    result = resolve(transport, pinned_keys={A: key.public_key()})
    assert [entry.repo_url for entry in result.entries] == [REPO]
    assert result.passed


@pytest.mark.parametrize("corruption", ["wrong-key", "outer-document", "payload", "wrong-kind", "malformed"])
def test_invalid_signatures_are_refused_even_with_unsigned_opt_in(corruption: str) -> None:
    key = Ed25519PrivateKey.generate()
    doc = signed(document(B, projects=[{"repo_url": REPO}]), key)
    pin = key.public_key()
    if corruption == "wrong-key":
        pin = Ed25519PrivateKey.generate().public_key()
    elif corruption == "outer-document":
        doc["indexes"] = [C]
    elif corruption == "payload":
        doc["signature"]["payload"] = "broken"
    elif corruption == "wrong-kind":
        doc["signature"] = json.loads(sign_document(document(), signing_key=key, document_kind="claim").to_json())
    else:
        doc["signature"] = {}
    transport = Transport({A: doc, B: document(), C: document()})
    result = resolve(transport, pinned_keys={A: pin}, allow_unsigned=True)
    assert result.entries == ()
    assert transport.calls == [A]
    assert not result.passed
    assert "signature" in result.skipped[0].reason


@pytest.mark.parametrize("allow_unsigned", [False, True])
def test_unsigned_indexes_require_explicit_opt_in(allow_unsigned: bool) -> None:
    result = resolve(Transport({A: document(projects=[{"repo_url": REPO}])}), allow_unsigned=allow_unsigned)
    assert bool(result.entries) == allow_unsigned
    assert result.passed == allow_unsigned


def test_unsigned_is_refused_by_default() -> None:
    result = resolve(Transport({A: document()}))
    assert not result.passed
    assert "unsigned" in result.skipped[0].reason


def test_pinned_index_requires_signature_even_when_unsigned_is_allowed() -> None:
    key = Ed25519PrivateKey.generate()
    result = resolve(Transport({A: document()}), pinned_keys={A: key.public_key()}, allow_unsigned=True)
    assert not result.passed
    assert "signature" in result.skipped[0].reason


def test_signed_index_without_a_pin_is_refused_even_when_unsigned_is_allowed() -> None:
    result = resolve(Transport({A: signed(document(), Ed25519PrivateKey.generate())}), allow_unsigned=True)
    assert not result.passed
    assert "pinned" in result.skipped[0].reason


def test_deny_overrides_a_pin_and_excludes_reachable_index_before_fetch() -> None:
    key = Ed25519PrivateKey.generate()
    transport = Transport({A: document(B), B: signed(document(), key)})
    result = resolve(transport, allow_unsigned=True, pinned_keys={B: key.public_key()}, denied_indexes=frozenset({B}))
    assert transport.calls == [A]
    assert result.skipped[0].repo_url == B
    assert "denied" in result.skipped[0].reason


@pytest.mark.parametrize("children", [None, {}, [3]])
def test_malformed_child_index_lists_are_refused(children: Any) -> None:
    doc = document(projects=[{"repo_url": REPO}])
    doc["indexes"] = children
    result = resolve(Transport({A: doc}), allow_unsigned=True)
    assert not result.passed
    assert result.entries == ()
    assert "indexes" in result.skipped[0].reason


@pytest.mark.parametrize("response", [None, TimeoutError("offline timeout")])
def test_fetch_errors_are_reported_without_losing_other_indexes(response: Any) -> None:
    transport = Transport({A: document(B, C), B: response, C: document(projects=[{"repo_url": REPO}])})
    result = resolve(transport, allow_unsigned=True)
    assert [entry.repo_url for entry in result.entries] == [REPO]
    assert result.skipped[0].repo_url == B
    assert not result.passed


def test_conflicts_are_reported_and_real_project_manifest_remains_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bernstein.core.security.url_allowlist._default_resolver", lambda _host: ["140.82.112.3"])
    manifest = {
        "version": 1,
        "license": "MIT",
        "gates": [["echo", "hello"]],
        "allowed_paths": [],
        "egress_allowlist": [],
        "sandbox": "container",
        "max_wall_clock_minutes": 60,
        "task_label": "volunteer-ok",
        "local_ok": True,
    }
    documents = {
        A: document(
            B, projects=[{"repo_url": REPO, "license": "bad-hint", "local_ok": False, "manifest_digest": "a" * 64}]
        ),
        B: document(
            projects=[{"repo_url": REPO, "license": "other-hint", "local_ok": True, "manifest_digest": "b" * 64}]
        ),
        f"{REPO}/raw/main/.bernstein/volunteer.json": manifest,
    }
    result = resolve(Transport(documents), allow_unsigned=True)
    assert result.conflicting_projects == (REPO,)
    assert len(result.entries) == 1
    transport = Transport(documents)
    joinable, dropped = registry.browse_indexes(
        [A], transport=transport, local_ok_only=True, federation=policy(allow_unsigned=True)
    )
    assert len(joinable) == 1
    assert joinable[0].manifest.license == "MIT"
    assert joinable[0].manifest.local_ok
    assert joinable[0].digest == joinable[0].manifest.digest
    assert not dropped
    assert transport.calls.count(f"{REPO}/raw/main/.bernstein/volunteer.json") == 1


def test_legacy_browse_does_not_follow_child_indexes_or_require_signatures() -> None:
    transport = Transport({A: document(B), B: document()})
    assert registry.browse_indexes([A], transport=transport) == ([], [])
    assert transport.calls == [A]


def test_legacy_v1_index_is_accepted_only_with_unsigned_opt_in() -> None:
    result = resolve(Transport({A: {"version": 1, "projects": [{"repo_url": REPO}]}}), allow_unsigned=True)
    assert result.entries[0].repo_url == REPO


@pytest.mark.parametrize("kwargs", [{"max_depth": -1}, {"max_depth": True}, {"max_indexes": 0}])
def test_invalid_resolution_budgets_are_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        policy(**kwargs)


def test_unknown_index_version_is_refused_before_its_children_are_followed() -> None:
    doc = document(B)
    doc["version"] = 3
    transport = Transport({A: doc, B: document()})
    result = resolve(transport, allow_unsigned=True)
    assert not result.passed
    assert transport.calls == [A]
    assert "version" in result.skipped[0].reason


def test_policy_copies_the_callers_pin_mapping() -> None:
    key = Ed25519PrivateKey.generate()
    pins = {A: key.public_key()}
    configured = policy(pinned_keys=pins)
    pins.clear()
    transport = Transport({A: document()})
    result = registry.resolve_indexes([A], transport=transport, policy=configured)
    assert "signature" in result.skipped[0].reason


def test_a_cycle_closed_after_a_diamond_reports_both_branches() -> None:
    transport = Transport({A: document(B, C), B: document(D), C: document(D), D: document(A)})
    result = resolve(transport, allow_unsigned=True)
    assert transport.calls == [A, B, C, D]
    assert result.cycle_members == (A, B, C, D)


@pytest.mark.parametrize("correct_child_key", [False, True])
def test_each_child_index_uses_its_own_pin(correct_child_key: bool) -> None:
    root_key, child_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    transport = Transport(
        {
            A: signed(document(B), root_key),
            B: signed(document(projects=[{"repo_url": REPO}]), child_key if correct_child_key else root_key),
        }
    )
    result = resolve(transport, pinned_keys={A: root_key.public_key(), B: child_key.public_key()})
    assert bool(result.entries) == correct_child_key
    assert result.passed == correct_child_key


def test_legacy_parser_ignores_non_string_added_by_as_unknown_metadata() -> None:
    raw = document(projects=[{"repo_url": REPO, "added_by": {"unsupported": "extension"}}])
    assert registry._parse_index(json.dumps(raw).encode(), A)[0].added_by is None
