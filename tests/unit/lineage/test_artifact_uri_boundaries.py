"""The two lineage write boundaries enforce the artifact-key grammar (#2559).

``LineageSpine.record`` and the v1 signed-write path are the only places an
artifact key enters the chain. Both now route their decision through
:func:`bernstein.core.lineage.artifact_uri.artifact_key_rejection_reason`, so a
canonical external URI becomes a first-class provenance key and an unknown or
non-canonical one is refused before any hash, HMAC or signature is computed.

Also pinned here: the by-artefact projections (tips, fork detection, the
store's sharded layout) generalise over the widened key space with no change,
because they treat the key as an opaque string.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.lineage.entry import LineageEntry
from bernstein.core.lineage.identity import AgentCard, generate_keypair
from bernstein.core.lineage.signed_write import _is_unsafe_path
from bernstein.core.lineage.spine import LineageSpine, _reject_unsafe_artifact_path
from bernstein.core.lineage.store import LineageStore
from bernstein.core.lineage.tips import compute_tips, detect_forks

_HMAC_KEY = b"0" * 64
_TS = 1700000000000000000

_CANONICAL_URIS = [
    "pr://github.com/acme/widget/2559",
    "pkg://pypi/bernstein/3.9.0",
    "deploy://prod/docs-site",
    "doc://example.test/lineage/artifacts",
]

_REFUSED = [
    "ftp://evil.test/payload",
    "http://evil.test/payload",
    "file:///etc/passwd",
    "PKG://pypi/bernstein/3.9.0",
    "pkg://PyPI/bernstein/3.9.0",
    "doc://example.test/lineage/",
    "repo://src/a.py",
    "pkg://pypi/../secrets/1.0",
    "/etc/passwd",
    "../../etc/passwd",
    "",
]


# ---------------------------------------------------------------------------
# Spine boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("uri", _CANONICAL_URIS)
def test_spine_accepts_canonical_artifact_uris(tmp_path: Path, uri: str) -> None:
    spine = LineageSpine(tmp_path, run_id="run-1", hmac_key=_HMAC_KEY)
    entry_hash = spine.record(
        artifact_path=uri,
        content=b"payload",
        actor="agent:worker-1",
        step_id="task-1",
        model="sonnet",
        timestamp=_TS,
    )
    assert entry_hash.startswith("sha256:")
    assert spine.verify().ok
    assert [e.artifact_path for e in spine.iter_entries()] == [uri]


@pytest.mark.parametrize("key", _REFUSED)
def test_spine_refuses_unknown_and_non_canonical_keys(tmp_path: Path, key: str) -> None:
    spine = LineageSpine(tmp_path, run_id="run-1", hmac_key=_HMAC_KEY)
    with pytest.raises(ValueError):
        spine.record(
            artifact_path=key,
            content=b"payload",
            actor="agent:worker-1",
            step_id="task-1",
            model="sonnet",
            timestamp=_TS,
        )
    # Refusal happens before anything is written: no chain, no partial entry.
    assert not (tmp_path / "run-1" / "spine.jsonl").exists()


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("", "empty artifact_path"),
        ("/etc/passwd", "absolute artifact_path not allowed"),
        ("../x", "path traversal in artifact_path"),
    ],
)
def test_spine_legacy_error_messages_are_unchanged(key: str, message: str) -> None:
    """Callers matching on the old error text are not broken by #2559."""
    with pytest.raises(ValueError, match=message):
        _reject_unsafe_artifact_path(key)


# ---------------------------------------------------------------------------
# v1 signed-write boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("uri", _CANONICAL_URIS)
def test_v1_boundary_accepts_canonical_artifact_uris(uri: str) -> None:
    assert _is_unsafe_path(uri) is None


@pytest.mark.parametrize("key", _REFUSED)
def test_v1_boundary_refuses_unknown_and_non_canonical_keys(key: str) -> None:
    assert _is_unsafe_path(key) is not None


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("", "empty artefact_path"),
        ("/etc/passwd", "absolute artefact_path not allowed"),
        ("../x", "path traversal in artefact_path"),
    ],
)
def test_v1_legacy_reason_strings_are_unchanged(key: str, message: str) -> None:
    assert _is_unsafe_path(key) == message


def test_v1_signed_write_seals_an_external_artifact(tmp_path: Path) -> None:
    """A published package is a first-class signed lineage record."""
    from bernstein.core.lineage.signed_write import seal_write

    priv_pem, pub_pem = generate_keypair()
    card = AgentCard(agent_id="agent:worker-1", kid="key-test-001", public_key_pem=pub_pem)
    store = LineageStore(tmp_path / "lineage")

    from bernstein.core.lineage.artifact_uri import external_reference_bytes

    uri = "pkg://pypi/bernstein/3.9.0"
    reference = external_reference_bytes(uri, digest="sha256:" + "ab" * 32)
    entry_hash = seal_write(
        store,
        _HMAC_KEY,
        artefact_path=uri,
        new_content=reference,
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv_pem,
        tool_call_id="tc-1",
        span_id="00f067aa0ba902b7",
        artefact_kind="external",
        ts_ns=_TS,
    )
    assert entry_hash.startswith("sha256:")
    entries = [e for e, _jws in store.read_log()]
    assert [e.artefact_path for e in entries] == [uri]
    assert entries[0].artefact_kind == "external"
    # The by-artefact projection keys off the URI, so the tip of a published
    # package is answerable the same way the tip of a file is.
    assert store.tip_set(uri)["open"] == [entry_hash]


def test_external_artefact_kind_is_accepted_by_the_entry_schema() -> None:
    entry = LineageEntry(
        v=1,
        artefact_path="pkg://pypi/bernstein/3.9.0",
        artefact_kind="external",
        content_hash="sha256:" + "ab" * 32,
        parent_hashes=[],
        agent_id="agent:w",
        agent_card_kid="kid-1",
        tool_call_id="tc-1",
        span_id="span-1",
        ts_ns=_TS,
        operator_hmac="",
    )
    assert entry.artefact_kind == "external"


# ---------------------------------------------------------------------------
# By-artefact projections over the widened key space
# ---------------------------------------------------------------------------


def _entry(path: str, content_hash_seed: str, parents: list[str]) -> LineageEntry:
    return LineageEntry(
        v=1,
        artefact_path=path,
        artefact_kind="external",
        content_hash="sha256:" + content_hash_seed * 32,
        parent_hashes=parents,
        agent_id="agent:w",
        agent_card_kid="kid-1",
        tool_call_id="tc-1",
        span_id="span-1",
        ts_ns=_TS,
        operator_hmac="",
    )


def test_tips_and_forks_generalise_over_uri_keys() -> None:
    """``compute_tips`` and ``detect_forks`` need no change for URI keys.

    They treat the artefact key as an opaque string, so the fork analysis that
    answers "does this artifact have exactly one open tip" works identically
    for a published package and for a source file.
    """
    from bernstein.core.lineage.entry import entry_hash as compute_hash

    genesis = _entry("pkg://pypi/bernstein/3.9.0", "aa", [])
    genesis_hash = compute_hash(genesis)
    child_a = _entry("pkg://pypi/bernstein/3.9.0", "bb", [genesis_hash])
    child_b = _entry("pkg://pypi/bernstein/3.9.0", "cc", [genesis_hash])
    other = _entry("pr://github.com/acme/widget/1", "dd", [])

    tips = compute_tips([genesis, child_a, child_b, other])
    assert set(tips) == {"pkg://pypi/bernstein/3.9.0", "pr://github.com/acme/widget/1"}
    assert len(tips["pkg://pypi/bernstein/3.9.0"]["open"]) == 2
    assert len(tips["pr://github.com/acme/widget/1"]["open"]) == 1

    forks = detect_forks([genesis, child_a, child_b, other])
    assert [f.artefact_path for f in forks] == ["pkg://pypi/bernstein/3.9.0"]


def test_store_layout_is_filesystem_safe_for_uri_keys(tmp_path: Path) -> None:
    """A URI key carries ``:`` and ``/`` yet never escapes the store.

    The store shards by the sha256 of the key rather than by the key itself, so
    a URI cannot create a directory outside the projection root or collide with
    a repo path's projection.
    """
    priv_pem, pub_pem = generate_keypair()
    card = AgentCard(agent_id="agent:worker-1", kid="key-test-001", public_key_pem=pub_pem)
    store = LineageStore(tmp_path / "lineage")

    from bernstein.core.lineage.signed_write import seal_write

    for key in ("pkg://pypi/bernstein/3.9.0", "src/a.py"):
        seal_write(
            store,
            _HMAC_KEY,
            artefact_path=key,
            new_content=b"payload-" + key.encode(),
            agent_id=card.agent_id,
            agent_card=card,
            private_key_pem=priv_pem,
            tool_call_id="tc-1",
            span_id="00f067aa0ba902b7",
            artefact_kind="external",
            ts_ns=_TS,
        )

    root = (tmp_path / "lineage").resolve()
    written = [p for p in root.rglob("*") if p.is_file()]
    assert written
    for path in written:
        assert path.resolve().is_relative_to(root)
    # Distinct keys get distinct projections; nothing is conflated.
    assert store.tip_set("pkg://pypi/bernstein/3.9.0")["open"] != store.tip_set("src/a.py")["open"]
