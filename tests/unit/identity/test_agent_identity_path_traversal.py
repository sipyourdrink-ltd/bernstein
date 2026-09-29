"""Path traversal guards for :mod:`bernstein.core.identity.agent_jwt`.

The store keys every persisted identity on ``<base_dir>/agent_identities/
<identity_id>.json``.  ``identity_id`` reaches the store from request path
parameters (``/identities/{identity_id}``) and from a JWT ``sub`` claim, so it
is caller-controlled and must not be able to escape the identities directory
or alias another identity's record.

To make each case fail without the guard, a victim record is planted at the
location a naive ``<dir>/<id>.json`` join actually resolves to - outside the
identities directory - and the assertion proves the hostile id resolves to
nothing instead of loading that victim.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.identity.agent_jwt import AgentIdentityStore

#: Hostile identity ids whose ``<dir>/<id>.json`` join resolves outside the
#: identities directory.
_HOSTILE_IDS: tuple[str, ...] = (
    "../victim",
    "..\\victim",
    "sub/../victim",
    "sub\\..\\victim",
    "/abs/victim",
)


@pytest.fixture()
def store_with_escaped_victim(tmp_path: Path) -> AgentIdentityStore:
    """A store whose ``victim`` record sits at ``base_dir/victim.json``."""
    store = AgentIdentityStore(tmp_path)
    store.create_identity("victim", "backend")
    (tmp_path / "agent_identities" / "victim.json").rename(tmp_path / "victim.json")
    return store


def test_hostile_identity_id_cannot_read_outside_the_directory(
    store_with_escaped_victim: AgentIdentityStore,
) -> None:
    """``get`` on a traversal id must resolve to nothing, not the victim record."""
    for hostile in _HOSTILE_IDS:
        assert store_with_escaped_victim.get(hostile) is None, f"get({hostile!r}) escaped the identities directory"


def test_hostile_identity_id_cannot_revoke_outside_the_directory(
    store_with_escaped_victim: AgentIdentityStore,
) -> None:
    """``revoke`` must not act on a record outside the identities directory."""
    identities_dir = store_with_escaped_victim._identities_dir
    for hostile in _HOSTILE_IDS:
        assert store_with_escaped_victim.revoke(hostile) is False, f"revoke({hostile!r}) resolved a record"

    # The escaped victim file is untouched, and no new file was created outside.
    victim = store_with_escaped_victim._base_dir / "victim.json"
    assert victim.exists(), "revoke clobbered the escaped victim file"
    escaped = [
        p
        for p in store_with_escaped_victim._base_dir.iterdir()
        if p.is_file() and p.parent != identities_dir and p.name.endswith(".json")
    ]
    assert escaped == [victim], f"revoke wrote outside the identities directory: {escaped}"


def test_hostile_identity_id_cannot_authorize_another_record(
    store_with_escaped_victim: AgentIdentityStore,
) -> None:
    """``authorize`` must not load the escaped victim for a traversal id."""
    for hostile in _HOSTILE_IDS:
        assert store_with_escaped_victim.authorize(hostile, "files:read") is False, (
            f"authorize({hostile!r}) resolved a record"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
