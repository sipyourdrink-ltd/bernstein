"""A declared role modality must be one something in this tree actually runs.

The team manifest accepts a per-role modality declaration and validates it
against the :class:`~bernstein.core.orchestration.activity.ActivityKind` enum.
Enum membership is the wrong gate: ``data`` and ``ops`` are enum members that
ship only collector classes, with no worker driving them, so a manifest
declaring one parses cleanly and then runs an ordinary coding spawn. A
declaration that is parsed, validated and then silently downgraded reads as a
supported configuration.

These tests pin the narrower gate: a modality is accepted only when the
registry in :mod:`bernstein.core.orchestration.activity_modalities` names
something that runs it, and the registry is held honest by resolving every
entry it claims.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bernstein.core.orchestration.activity import ActivityKind
from bernstein.core.orchestration.activity_modalities import (
    RUNNABLE_ACTIVITY_KINDS,
    activity_worker_for_kind,
)
from bernstein.core.teams.manifest import TeamManifestValidationError, load_team_manifest

if TYPE_CHECKING:
    from pathlib import Path

_MANIFEST = """\
name = "crew"
version = "1.0.0"

[coordination]
parallelism = 1
review_chain = false

[[roles]]
agent_kind = "{kind}"
role = "scout"
"""

#: A manifest whose first role declares an unrunnable modality and whose second
#: role is an ordinary coding role. Used to pin that the refusal takes the whole
#: load down rather than letting the bad role degrade to the good one's spawn.
_MIXED = """\
name = "crew"
version = "1.0.0"

[coordination]
parallelism = 2
review_chain = false

[[roles]]
agent_kind = "data"
role = "loader"

[[roles]]
role = "backend"
"""


def _write(tmp_path: Path, name: str, body: str) -> Path:
    teams_dir = tmp_path / "templates" / "teams"
    teams_dir.mkdir(parents=True, exist_ok=True)
    path = teams_dir / f"{name}.toml"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The registry: what this tree can run
# ---------------------------------------------------------------------------


def test_every_registered_modality_resolves_to_an_importable_worker() -> None:
    """Load-bearing: the accept-list is only honest if its workers exist.

    ``RUNNABLE_ACTIVITY_KINDS`` is what the manifest lets an operator declare.
    If an entry named a class that had been renamed or removed, the manifest
    would keep accepting a modality nothing could run -- exactly the defect this
    change closes, reintroduced one rename later.
    """
    registered = RUNNABLE_ACTIVITY_KINDS - {ActivityKind.CODING}
    assert registered, "the registry must name at least one activity worker"
    for kind in registered:
        worker = activity_worker_for_kind(kind)
        assert worker is not None, f"{kind.value} is accepted but resolves to no worker"
        assert isinstance(worker, type)


def test_coding_has_no_activity_worker() -> None:
    """``coding`` is runnable, but as the ordinary spawn rather than an activity."""
    assert ActivityKind.CODING in RUNNABLE_ACTIVITY_KINDS
    assert activity_worker_for_kind(ActivityKind.CODING) is None


@pytest.mark.parametrize("kind", [ActivityKind.DATA, ActivityKind.OPS])
def test_collector_only_modality_is_not_runnable(kind: ActivityKind) -> None:
    """``data`` and ``ops`` ship collectors only: no worker, so not declarable."""
    assert kind not in RUNNABLE_ACTIVITY_KINDS
    assert activity_worker_for_kind(kind) is None


# ---------------------------------------------------------------------------
# The manifest gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["data", "ops"])
def test_modality_with_no_worker_is_refused_at_manifest_load(tmp_path: Path, kind: str) -> None:
    with pytest.raises(TeamManifestValidationError):
        load_team_manifest(_write(tmp_path, kind, _MANIFEST.format(kind=kind)))


@pytest.mark.parametrize("kind", ["data", "ops"])
def test_refusal_names_the_kind_and_the_role(tmp_path: Path, kind: str) -> None:
    """The message has to be actionable without opening the source."""
    with pytest.raises(TeamManifestValidationError) as excinfo:
        load_team_manifest(_write(tmp_path, kind, _MANIFEST.format(kind=kind)))
    message = str(excinfo.value)
    assert kind in message
    assert "scout" in message


def test_refused_role_does_not_fall_back_to_a_coding_spawn(tmp_path: Path) -> None:
    """The whole load fails; no role is quietly downgraded to coding."""
    with pytest.raises(TeamManifestValidationError) as excinfo:
        load_team_manifest(_write(tmp_path, "mixed", _MIXED))
    assert "loader" in str(excinfo.value)


@pytest.mark.parametrize("kind", ["research", "browser"])
def test_runnable_modality_still_parses(tmp_path: Path, kind: str) -> None:
    manifest = load_team_manifest(_write(tmp_path, kind, _MANIFEST.format(kind=kind)))
    assert manifest.roles[0].agent_kind is ActivityKind(kind)


def test_role_omitting_the_key_still_defaults_to_coding(tmp_path: Path) -> None:
    body = _MANIFEST.format(kind="research").replace('agent_kind = "research"\n', "")
    manifest = load_team_manifest(_write(tmp_path, "legacy", body))
    assert manifest.roles[0].agent_kind is ActivityKind.CODING


def test_unknown_modality_message_lists_only_runnable_kinds(tmp_path: Path) -> None:
    """A rejection must not advertise a modality the next load would refuse.

    Listing every enum member sends an operator straight from ``telepathy`` to
    ``data``, which is refused in turn.
    """
    with pytest.raises(TeamManifestValidationError) as excinfo:
        load_team_manifest(_write(tmp_path, "bad", _MANIFEST.format(kind="telepathy")))
    _, separator, listed = str(excinfo.value).partition("is not one of: ")
    assert separator, "the rejection must still enumerate the accepted modalities"
    assert {item.strip() for item in listed.split(",")} == {kind.value for kind in RUNNABLE_ACTIVITY_KINDS}
