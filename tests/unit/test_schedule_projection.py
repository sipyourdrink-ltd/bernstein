"""Unit tests for the deterministic schedule projection (#1798).

The projection is the load-bearing property of the recurring-goals
feature: two operators with identical
``(schedule_id, fire_time, last_state)`` MUST land on the byte-identical
task graph and the byte-identical projection_hash.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from bernstein.core.orchestration.schedule_projection import (
    SCHEDULE_PROJECTION_REV,
    project_schedule_fire,
)


class TestProjectionDeterminism:
    def test_same_inputs_byte_identical(self) -> None:
        a = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state=None,
            goal="Send daily digest",
        )
        b = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state=None,
            goal="Send daily digest",
        )
        assert a.canonical_bytes == b.canonical_bytes
        assert a.projection_hash == b.projection_hash

    def test_different_schedule_id_differs(self) -> None:
        a = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state=None,
            goal="g",
        )
        b = project_schedule_fire(
            schedule_id="sched_beta",
            fire_time=1_700_000_000,
            last_state=None,
            goal="g",
        )
        assert a.projection_hash != b.projection_hash

    def test_different_fire_time_differs(self) -> None:
        a = project_schedule_fire(schedule_id="sched_alpha", fire_time=1, last_state=None, goal="g")
        b = project_schedule_fire(schedule_id="sched_alpha", fire_time=2, last_state=None, goal="g")
        assert a.projection_hash != b.projection_hash

    def test_different_last_state_differs(self) -> None:
        a = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state={"key": "A"},
            goal="g",
        )
        b = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state={"key": "B"},
            goal="g",
        )
        assert a.projection_hash != b.projection_hash

    def test_last_state_key_order_independent(self) -> None:
        """Two callers that pass an equal mapping in different insertion
        order MUST still land on the same projection. Python dicts preserve
        insertion order so this is a real failure mode for naive callers.
        """
        a = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state={"a": 1, "b": 2},
            goal="g",
        )
        b = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state={"b": 2, "a": 1},
            goal="g",
        )
        assert a.canonical_bytes == b.canonical_bytes

    def test_rev_baked_into_payload(self) -> None:
        result = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state=None,
            goal="g",
        )
        decoded = json.loads(result.canonical_bytes.decode())
        assert decoded["rev"] == SCHEDULE_PROJECTION_REV


class TestProjectionInputContract:
    def test_float_fire_time_rejected(self) -> None:
        """``fire_time`` MUST be an int.

        Allowing float values lets sub-second jitter fork two operators'
        projections; the AC mandates byte-identical output, so we reject
        the float at the type contract layer.
        """
        with pytest.raises(TypeError):
            project_schedule_fire(
                schedule_id="sched_alpha",
                fire_time=1_700_000_000.5,  # type: ignore[arg-type]
                last_state=None,
                goal="g",
            )

    def test_root_node_present(self) -> None:
        result = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state=None,
            goal="g",
        )
        assert len(result.nodes) == 1
        node = result.nodes[0]
        assert node.task_id.startswith("sched-task-")
        # task_id is deterministic in the schedule id, fire_time, and rev.
        result2 = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state=None,
            goal="g",
        )
        assert result.nodes[0].task_id == result2.nodes[0].task_id

    def test_genesis_digest_when_no_state(self) -> None:
        result = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state=None,
            goal="g",
        )
        assert result.last_state_digest == "genesis"

    def test_scenario_id_baked_into_metadata(self) -> None:
        result = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state=None,
            scenario_id="security-pentest",
        )
        node = result.nodes[0]
        meta = dict(node.metadata)
        assert meta.get("scenario_id") == "security-pentest"

    def test_node_metadata_sorted(self) -> None:
        """metadata sort must be stable so the canonical bytes do not flip
        when the caller emits the same tags in a different order.
        """
        result = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state=None,
            goal="g",
        )
        decoded = json.loads(result.canonical_bytes.decode())
        meta = decoded["nodes"][0]["metadata"]
        # the encoder sorts metadata tuples → list-of-lists; ensure it's sorted.
        sorted_meta = sorted(meta)
        assert meta == sorted_meta


class TestLastStateCanonicalisation:
    """``last_state`` must be as drift-proof as ``fire_time`` (#1852).

    ``fire_time`` is pinned to ``int`` and floats are rejected because
    they permit cross-host drift. ``last_state`` feeds the same
    byte-identical-projection contract but is digested with a plain
    ``json.dumps(..., sort_keys=True)`` over arbitrary ``Any``, which is
    not canonical for sets (hash-randomised member order, or a hard
    ``TypeError``), floats (platform-dependent repr), or non-finite
    floats (``NaN``/``Infinity`` round-trip non-portably). These tests
    pin the canonical contract so the latent hazard cannot land once a
    caller folds real state into the projection.
    """

    def test_set_member_order_does_not_perturb_hash(self) -> None:
        """A set member must not fork the projection on iteration order."""
        a = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state={"a": 1, "b": {2, 1}},
            goal="g",
        )
        b = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state={"b": {1, 2}, "a": 1},
            goal="g",
        )
        assert a.projection_hash == b.projection_hash
        assert a.canonical_bytes == b.canonical_bytes

    def test_nested_container_order_does_not_perturb_hash(self) -> None:
        """Nested sets inside lists/dicts must also canonicalise."""
        a = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state={"outer": [{"tags": {"x", "y"}}, 1]},
            goal="g",
        )
        b = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state={"outer": [{"tags": {"y", "x"}}, 1]},
            goal="g",
        )
        assert a.projection_hash == b.projection_hash

    def test_frozenset_member_canonicalised(self) -> None:
        a = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state={"f": frozenset({3, 1, 2})},
            goal="g",
        )
        b = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state={"f": frozenset({2, 3, 1})},
            goal="g",
        )
        assert a.projection_hash == b.projection_hash

    def test_float_member_rejected(self) -> None:
        """A float in ``last_state`` mirrors the ``fire_time`` float guard.

        Floats serialise with a platform/version-dependent repr, so two
        operators can disagree on the digest for inputs that compare
        equal in Python. We reject rather than silently fork.
        """
        with pytest.raises(TypeError):
            project_schedule_fire(
                schedule_id="sched_alpha",
                fire_time=1_700_000_000,
                last_state={"x": 0.1 + 0.2},
                goal="g",
            )

    def test_nested_float_member_rejected(self) -> None:
        with pytest.raises(TypeError):
            project_schedule_fire(
                schedule_id="sched_alpha",
                fire_time=1_700_000_000,
                last_state={"outer": {"inner": [1, 2.5]}},
                goal="g",
            )

    def test_nan_member_rejected(self) -> None:
        with pytest.raises(TypeError):
            project_schedule_fire(
                schedule_id="sched_alpha",
                fire_time=1_700_000_000,
                last_state={"x": float("nan")},
                goal="g",
            )

    def test_infinity_member_rejected(self) -> None:
        with pytest.raises(TypeError):
            project_schedule_fire(
                schedule_id="sched_alpha",
                fire_time=1_700_000_000,
                last_state={"x": float("inf")},
                goal="g",
            )

    def test_genesis_path_unchanged(self) -> None:
        """The ``last_state=None`` sentinel path must not regress."""
        result = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state=None,
            goal="g",
        )
        assert result.last_state_digest == "genesis"

    def test_none_state_projection_hash_is_stable_across_rev(self) -> None:
        """The canonicalisation change must not move any recorded hash.

        Every projection_hash recorded today was produced with
        ``last_state=None`` (the supervisor's only caller). Pinning the
        exact hash proves the #1852 canonicalisation is a no-op for the
        ``None`` path, which is why ``SCHEDULE_PROJECTION_REV`` does NOT
        need to be bumped: no historical receipt's hash changes.
        """
        result = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state=None,
            goal="Send daily digest",
        )
        assert result.projection_hash == "b0323f98fd799b2d84441887e8885b2de73df61429d1cded3f245c3153e0ee11"

    def test_canonical_form_matches_fingerprint_helper(self) -> None:
        """The canonical set/dict shape must equal ``fingerprint._canonicalize``.

        The ticket calls out divergence between two canonicalisers as a
        bad outcome: if the schedule projection and the persistence
        fingerprint canonicalise the same value differently, two
        subsystems reintroduce drift. This pins byte equivalence for the
        overlapping (non-float) value space.
        """
        from bernstein.core.orchestration.schedule_projection import _canonicalize_last_state
        from bernstein.core.persistence.fingerprint import _canonicalize

        value = {"a": 1, "b": {2, 1}, "nested": [{"tags": {"x", "y"}}], "fz": frozenset({3, 1})}
        assert json.dumps(_canonicalize_last_state(value), sort_keys=True) == json.dumps(
            _canonicalize(value), sort_keys=True
        )

    def test_empty_mapping_is_genesis(self) -> None:
        result = project_schedule_fire(
            schedule_id="sched_alpha",
            fire_time=1_700_000_000,
            last_state={},
            goal="g",
        )
        assert result.last_state_digest == "genesis"

    def test_hash_seed_independent_for_set_state(self) -> None:
        """Two processes with different PYTHONHASHSEED agree on the hash.

        Set member order is hash-randomised per process; without
        canonicalisation the digest forks between operators. We force two
        distinct seeds in subprocesses and assert identical hashes.
        """
        snippet = (
            "from bernstein.core.orchestration.schedule_projection import project_schedule_fire;"
            "r = project_schedule_fire(schedule_id='s', fire_time=1700000000,"
            " last_state={'tags': {'alpha', 'beta', 'gamma', 'delta'}}, goal='g');"
            "print(r.projection_hash)"
        )

        def _run(seed: str) -> str:
            proc = subprocess.run(
                [sys.executable, "-c", snippet],
                check=True,
                capture_output=True,
                text=True,
                env={"PYTHONHASHSEED": seed, "PYTHONPATH": _src_path()},
            )
            return proc.stdout.strip()

        assert _run("0") == _run("12345")


def _src_path() -> str:
    """Return the bernstein src dir for subprocess PYTHONPATH propagation."""
    import bernstein

    # bernstein/__init__.py -> bernstein/ -> src/
    return str(__import__("pathlib").Path(bernstein.__file__).resolve().parents[1])


class TestProjectionPurity:
    """The projection function must be pure.

    We assert purity through observable contracts (no environment reads,
    no random output, no time.time() drift). These tests run the same
    inputs many times and check we get the same byte-output.
    """

    def test_repeated_calls_byte_stable(self) -> None:
        previous = None
        for _ in range(8):
            result = project_schedule_fire(
                schedule_id="sched_alpha",
                fire_time=1_700_000_000,
                last_state={"k": "v"},
                goal="Daily digest",
                scenario_id="security-pentest",
            )
            if previous is None:
                previous = result.canonical_bytes
            assert result.canonical_bytes == previous


class TestResponseProfileFold:
    """The response-style profile is an input to task identity when (and
    only when) a profile is explicitly declared."""

    def _fire(self, **overrides):
        kwargs = {
            "schedule_id": "sched_style",
            "fire_time": 1_700_000_000,
            "last_state": None,
            "goal": "Nightly triage",
        }
        kwargs.update(overrides)
        return project_schedule_fire(**kwargs)

    def test_no_profile_is_byte_identical_to_pre_change_shape(self) -> None:
        """Backward compatibility: with no profile declared, the canonical
        payload contains no profile keys and the task hash is unchanged."""
        result = self._fire()
        payload = json.loads(result.canonical_bytes.decode())
        assert "response_profile" not in payload
        assert "profile_content_sha256" not in payload
        node_metadata_keys = {k for k, _v in [tuple(item) for item in payload["nodes"][0]["metadata"]]}
        assert "response_profile" not in node_metadata_keys

    def test_profile_folds_into_task_identity(self) -> None:
        plain = self._fire()
        profiled = self._fire(response_profile="terse", profile_content_sha256="c" * 64)
        assert plain.projection_hash != profiled.projection_hash
        assert plain.nodes[0].task_id != profiled.nodes[0].task_id

    def test_profile_fold_is_deterministic(self) -> None:
        a = self._fire(response_profile="terse", profile_content_sha256="c" * 64)
        b = self._fire(response_profile="terse", profile_content_sha256="c" * 64)
        assert a.canonical_bytes == b.canonical_bytes
        assert a.projection_hash == b.projection_hash

    def test_different_profile_hash_forks_identity(self) -> None:
        a = self._fire(response_profile="terse", profile_content_sha256="c" * 64)
        b = self._fire(response_profile="terse", profile_content_sha256="d" * 64)
        assert a.projection_hash != b.projection_hash
        assert a.nodes[0].task_id != b.nodes[0].task_id

    def test_profile_lands_in_canonical_payload_and_node_metadata(self) -> None:
        result = self._fire(response_profile="verbose", profile_content_sha256="e" * 64)
        payload = json.loads(result.canonical_bytes.decode())
        assert payload["response_profile"] == "verbose"
        assert payload["profile_content_sha256"] == "e" * 64
        metadata = {k: v for k, v in [tuple(item) for item in payload["nodes"][0]["metadata"]]}
        assert metadata["response_profile"] == "verbose"
        assert metadata["mode"] == "verbose"
