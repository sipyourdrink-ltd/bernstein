"""Issue #5276: the lineage signing key is persisted and reused, not per-invocation.

The module docstring for :mod:`bernstein.core.lineage.identity` used to say the
layer signs "with an Ed25519 keypair issued per agent invocation".
:func:`load_or_create_signing_identity` has never done that: it creates the
keypair once per identity directory and reads the same PEM files back on every
later call.

The gap mattered because anyone reasoning about key exposure from the docstring
assumed a short-lived key. The real key is long-lived and on disk, which is the
property that decides custody and rotation. These tests pin the behaviour so
the docstring cannot drift back to describing a key the code never issues.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.lineage.identity import (
    AgentCard,
    load_or_create_signing_identity,
    sign_detached,
    verify_detached,
)

PRIVATE_NAME = "test_signing.pem"
PUBLIC_NAME = "test_signing.pub"


def _load(identity_dir: Path) -> tuple[str, str]:
    return load_or_create_signing_identity(
        identity_dir,
        private_name=PRIVATE_NAME,
        public_name=PUBLIC_NAME,
    )


def test_signing_identity_is_reused_across_invocations(tmp_path: Path) -> None:
    """A second call returns the first call's key, not a fresh one."""
    identity_dir = tmp_path / "identity"

    first_private, first_public = _load(identity_dir)
    second_private, second_public = _load(identity_dir)

    assert second_private == first_private
    assert second_public == first_public

    # The property that actually matters: a receipt signed before a restart still
    # verifies afterwards, because the key on the second call IS the first key.
    payload = b"lineage entry bytes"
    jws = sign_detached(payload, first_private, kid="k1")
    card = AgentCard(agent_id="a1", kid="k1", public_key_pem=second_public)
    assert verify_detached(payload, jws, card) is True


def test_the_keypair_is_written_to_the_identity_directory(tmp_path: Path) -> None:
    """The key is on disk under ``identity_dir``, which is why it survives the process."""
    identity_dir = tmp_path / "identity"
    private_pem, public_pem = _load(identity_dir)

    assert (identity_dir / PRIVATE_NAME).read_text(encoding="ascii") == private_pem
    assert (identity_dir / PUBLIC_NAME).read_text(encoding="ascii") == public_pem


def test_a_separate_identity_directory_gets_a_separate_key(tmp_path: Path) -> None:
    """Reuse is scoped to the directory: two installs do not share one key."""
    one = _load(tmp_path / "one")
    two = _load(tmp_path / "two")

    assert one[0] != two[0]
    assert one[1] != two[1]

    # And a signature from one does not verify under the other.
    payload = b"lineage entry bytes"
    jws = sign_detached(payload, one[0], kid="k1")
    other_card = AgentCard(agent_id="a1", kid="k1", public_key_pem=two[1])
    assert verify_detached(payload, jws, other_card) is False
