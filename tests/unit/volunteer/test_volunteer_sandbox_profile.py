"""The volunteer sandbox profile is a derived, verifiable containment decision.

Tests are named for the property each protects.  The properties worth naming
here are not "the function returns a dataclass" -- they are the ones a
maintainer relies on months later when they have a receipt, a commit, and no
memory of the run: that the profile reproduces from the manifest, that a donor
cannot loosen it without the digest saying so, and that nothing from the host
environment reaches the sandbox.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from bernstein.core.volunteer.manifest import load_manifest
from bernstein.core.volunteer.sandbox_profile import (
    BACKEND_PREFERENCE,
    PACKAGE_REGISTRY_HOSTS,
    SANDBOX_ENV_ALLOWLIST,
    VOLUNTEER_PROFILE_NAME,
    SandboxProfileRefusal,
    backend_options,
    build_volunteer_profile,
    describe_refusal,
    effective_egress,
    profile_matches,
    sandbox_env,
)

ALL_BACKENDS = ("microvm", "container-userns", "container")


def _manifest(**overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "version": 1,
        "license": "Apache-2.0",
        "gates": [["uv", "run", "pytest", "-q"]],
        "allowed_paths": ["src/**"],
        "egress_allowlist": [],
        "sandbox": "container",
        "max_wall_clock_minutes": 30,
        "local_ok": True,
    }
    payload.update(overrides)
    return load_manifest(json.dumps(payload))


def _profile(*, manifest: Any = None, backends: Any = ALL_BACKENDS, **kwargs: Any) -> Any:
    return build_volunteer_profile(
        manifest if manifest is not None else _manifest(),
        available_backends=backends,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The profile reproduces, or the chain is worthless
# ---------------------------------------------------------------------------


def test_same_manifest_and_donor_limits_reproduce_the_same_digest() -> None:
    """The whole verification story rests on this.

    A maintainer rebuilds the profile from the manifest at the submitted commit
    and compares digests.  If the build were not pure, the comparison would
    fail for honest runs and the check would be turned off within a week.
    """
    first = _profile(donor_wall_clock_minutes=20)
    second = _profile(donor_wall_clock_minutes=20)

    assert first.digest == second.digest
    assert profile_matches(second, expected_digest=first.digest)


def test_profile_names_the_manifest_it_came_from() -> None:
    """Without the back-reference the chain has a gap at its most useful link."""
    manifest = _manifest()

    assert _profile(manifest=manifest).manifest_sha256 == manifest.digest


def test_a_different_policy_produces_a_different_profile_digest() -> None:
    """Two projects with different bars must not share a containment identity."""
    strict = _manifest(max_wall_clock_minutes=10)
    loose = _manifest(max_wall_clock_minutes=120)

    assert _profile(manifest=strict).digest != _profile(manifest=loose).digest


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backend", "container"),
        ("egress_allowlist", ("example.com",)),
        ("env_allowlist", ("PATH",)),
        ("memory_mb", 512),
        ("cpu_quota_us", 50_000),
        ("wall_clock_seconds", 60),
        ("plain_container_accepted", True),
        ("manifest_sha256", "f" * 64),
    ],
)
def test_every_field_of_the_containment_decision_is_in_the_digest(field: str, value: Any) -> None:
    """A field outside the digest is a knob a donor could turn quietly.

    The point of hashing the decision is that "the sandbox was hardened" stops
    being testimony.  Any field the digest does not cover is testimony again.
    """
    import dataclasses

    baseline = _profile()
    mutated = dataclasses.replace(baseline, **{field: value})

    assert mutated.digest != baseline.digest


def test_donor_limits_tighten_but_never_loosen_the_project_ceiling() -> None:
    """A donor lends less time than the project asks for, never more."""
    manifest = _manifest(max_wall_clock_minutes=30)

    assert _profile(manifest=manifest, donor_wall_clock_minutes=10).wall_clock_seconds == 600
    assert _profile(manifest=manifest, donor_wall_clock_minutes=600).wall_clock_seconds == 1800


# ---------------------------------------------------------------------------
# No host credential material reaches the sandbox
# ---------------------------------------------------------------------------


def test_a_secret_in_the_worker_environment_is_not_in_the_sandbox_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canary the issue asks for, at the seam it actually crosses.

    ``sandbox_env`` is what becomes the container's ``--env`` list, so a value
    absent here is absent inside.  This is a test of construction, not of a
    filter: the function never reads ``os.environ`` at all.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-canary-do-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "canary-aws")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_canary")

    env = sandbox_env(_profile())

    assert "sk-ant-canary-do-not-leak" not in json.dumps(env)
    assert not {"ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"} & set(env)


def test_sandbox_environment_does_not_depend_on_the_host_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An allowlist that starts from nothing has no denylist to get wrong.

    Same profile, wildly different host environment, byte-identical result --
    which is also what makes the sandbox environment part of a reproducible
    run rather than a property of whoever's laptop it was.
    """
    profile = _profile()
    before = sandbox_env(profile)

    for index in range(50):
        monkeypatch.setenv(f"SOME_HOST_VAR_{index}", "value")
    monkeypatch.setenv("PATH", "/nonsense")
    monkeypatch.setenv("HOME", "/home/donor")

    assert sandbox_env(profile) == before


def test_sandbox_environment_carries_nothing_outside_the_profile_allowlist() -> None:
    assert set(sandbox_env(_profile())) <= set(SANDBOX_ENV_ALLOWLIST)


def test_home_is_inside_the_workspace_not_on_the_host() -> None:
    """A HOME pointing at the donor's real home is where credentials live."""
    assert sandbox_env(_profile())["HOME"] == "/workspace"
    assert backend_options(_profile())["mount_home"] is False
    assert backend_options(_profile())["inherit_host_env"] is False


# ---------------------------------------------------------------------------
# Egress is deny-all plus an explicit list
# ---------------------------------------------------------------------------


def test_a_host_the_project_did_not_list_is_not_reachable() -> None:
    manifest = _manifest(egress_allowlist=["api.example.com"])

    reachable = effective_egress(manifest)

    assert "api.example.com" in reachable
    assert "evil.example" not in reachable
    assert "github.com" not in reachable


def test_package_registries_are_reachable_without_the_project_listing_them() -> None:
    """Otherwise no real project's gates can install anything.

    Naming the set here keeps it reviewable and hashed into every profile,
    which is better than every manifest pasting its own list.
    """
    reachable = effective_egress(_manifest(egress_allowlist=[]))

    assert set(PACKAGE_REGISTRY_HOSTS) <= set(reachable)


def test_egress_list_is_sorted_and_deduplicated_so_two_spellings_hash_alike() -> None:
    """Order is not policy; a reordered list must not be a different profile."""
    listed = _manifest(egress_allowlist=["pypi.org", "api.example.com"])
    reversed_order = _manifest(egress_allowlist=["api.example.com", "pypi.org"])

    assert effective_egress(listed) == effective_egress(reversed_order)
    assert list(effective_egress(listed)) == sorted(set(effective_egress(listed)))


def test_backend_options_disable_the_network_when_nothing_is_reachable() -> None:
    """An empty effective list has to mean the network is off, not off by convention."""
    import dataclasses

    airgapped = dataclasses.replace(_profile(), egress_allowlist=())

    assert backend_options(airgapped)["network_disabled"] is True
    assert backend_options(_profile())["network_disabled"] is False


def test_a_reachable_host_is_pinned_to_a_resolved_address() -> None:
    """The list has to enforce something, or it is a boundary only in review.

    Allowlisted names get static entries; the sandbox gets a resolver that
    answers nothing, so anything outside the list does not resolve.
    """
    manifest = _manifest(egress_allowlist=["api.example.com"])
    options = backend_options(
        _profile(manifest=manifest),
        resolved_hosts={"api.example.com": "203.0.113.7", "pypi.org": "151.101.0.223"},
    )

    assert options["extra_hosts"] == {"api.example.com": "203.0.113.7", "pypi.org": "151.101.0.223"}
    assert options["dns"] == ["0.0.0.0"]


def test_a_resolved_host_outside_the_allowlist_is_not_pinned() -> None:
    """A caller that over-resolves must not be able to widen the boundary."""
    options = backend_options(
        _profile(),
        resolved_hosts={"evil.example": "203.0.113.9", "pypi.org": "151.101.0.223"},
    )

    assert "evil.example" not in options["extra_hosts"]


def test_an_airgapped_profile_gets_no_resolver_because_it_gets_no_network() -> None:
    """Nothing to pin when there is no interface; the stronger control wins."""
    import dataclasses

    options = backend_options(dataclasses.replace(_profile(), egress_allowlist=()))

    assert options["network_disabled"] is True
    assert "dns" not in options
    assert "extra_hosts" not in options


def test_backend_options_carry_the_caps_the_profile_decided() -> None:
    profile = _profile(donor_wall_clock_minutes=5)
    options = backend_options(profile)

    assert options["timeout_seconds"] == 300
    assert options["memory_mb"] == profile.memory_mb
    assert options["cpu_quota"] == profile.cpu_quota_us


# ---------------------------------------------------------------------------
# Backend selection, and the dual opt-in
# ---------------------------------------------------------------------------


def test_microvm_is_preferred_when_the_host_has_one() -> None:
    assert _profile(backends=ALL_BACKENDS).backend == "microvm"
    assert BACKEND_PREFERENCE[0] == "microvm"


def test_user_space_kernel_container_is_taken_before_a_plain_one() -> None:
    """It contains a kernel exploit; a plain container does not."""
    assert _profile(backends=("container-userns", "container")).backend == "container-userns"


def test_plain_container_is_refused_when_only_the_project_agreed() -> None:
    """The dual opt-in: the project's manifest is one consent, not both.

    A donor who never said "you may share my kernel with a stranger's code"
    has not said it by installing the software.
    """
    with pytest.raises(SandboxProfileRefusal) as excinfo:
        _profile(backends=("container",), donor_accepts_plain_container=False)

    assert excinfo.value.reason == "plain_container_needs_dual_opt_in"


def test_plain_container_runs_only_when_both_sides_agreed() -> None:
    profile = _profile(backends=("container",), donor_accepts_plain_container=True)

    assert profile.backend == "container"
    assert profile.plain_container_accepted is True


def test_the_dual_opt_in_is_visible_in_the_digest() -> None:
    """A weaker sandbox must not be able to masquerade as the default one."""
    weak = _profile(backends=("container",), donor_accepts_plain_container=True)
    strong = _profile(backends=("container-userns",), donor_accepts_plain_container=True)

    assert weak.digest != strong.digest


def test_a_project_demanding_a_microvm_does_not_get_a_container() -> None:
    """The manifest's ``sandbox`` field is a floor, and a floor that bends is decoration."""
    strict = _manifest(sandbox="microvm")

    with pytest.raises(SandboxProfileRefusal) as excinfo:
        _profile(manifest=strict, backends=("container-userns", "container"), donor_accepts_plain_container=True)

    assert excinfo.value.reason == "microvm_required_but_unavailable"


def test_a_host_with_no_usable_backend_refuses_rather_than_improvising() -> None:
    with pytest.raises(SandboxProfileRefusal) as excinfo:
        _profile(backends=())

    assert excinfo.value.reason == "no_acceptable_backend"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"donor_wall_clock_minutes": 0}, "wall_clock_below_floor"),
        ({"donor_memory_mb": 64}, "memory_below_floor"),
    ],
)
def test_a_donor_budget_too_small_to_run_anything_refuses_early(kwargs: Any, reason: str) -> None:
    """Better a refusal than a task that is killed for certain."""
    with pytest.raises(SandboxProfileRefusal) as excinfo:
        _profile(**kwargs)

    assert excinfo.value.reason == reason


def test_a_refusal_is_a_record_a_runner_can_persist() -> None:
    """Refusals are the common case on a mixed donor fleet.

    They need the same structure as a success or they become log lines nobody
    counts, and "why did nothing ever run on my machine" becomes unanswerable.
    """
    manifest = _manifest()
    try:
        _profile(manifest=manifest, backends=())
    except SandboxProfileRefusal as error:
        record = describe_refusal(error, manifest_sha256=manifest.digest)
    else:  # pragma: no cover - the call above always refuses
        pytest.fail("expected a refusal")

    assert record["outcome"] == "refused"
    assert record["reason"] == "no_acceptable_backend"
    assert record["manifest_sha256"] == manifest.digest
    assert record["profile"] == VOLUNTEER_PROFILE_NAME


def test_the_environment_allowlist_never_grows_by_accident() -> None:
    """A pin, so adding a variable is a decision somebody made on purpose.

    Every name here is one more place a credential could hide on its way into
    a sandbox running a stranger's code.
    """
    assert set(SANDBOX_ENV_ALLOWLIST) == {
        "BERNSTEIN_VOLUNTEER",
        "CI",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "TMPDIR",
    }
    assert not {name for name in SANDBOX_ENV_ALLOWLIST if "KEY" in name or "TOKEN" in name or "SECRET" in name}


def test_the_host_environment_is_not_consulted_even_when_it_holds_the_same_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An allowlisted *name* must not become a channel for a host *value*."""
    monkeypatch.setenv("PATH", "/attacker/controlled")
    monkeypatch.setenv("HOME", "/home/donor")

    env = sandbox_env(_profile())

    assert env["PATH"] != os.environ["PATH"]
    assert env["HOME"] == "/workspace"
