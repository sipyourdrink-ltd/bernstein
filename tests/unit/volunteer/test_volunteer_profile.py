"""Donor policy profile for autopilot mode (``.bernstein/volunteer_profile.json``).

The profile file is optional: flags alone must fully configure a run. The file
only persists preferences between runs to avoid re-typing the same arguments.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from bernstein.core.volunteer.volunteer_profile import (
    VolunteerProfile,
    VolunteerProfileError,
    load_profile,
)

VALID: dict[str, Any] = {
    "version": 1,
    "allowed_projects": ["owner/repo"],
    "allowed_licenses": ["MIT", "Apache-2.0"],
    "allowed_task_types": ["bug", "feature"],
    "max_size": "m",
    "adapters": ["claude-code"],
    "models": ["claude-3-7-sonnet-20250219"],
    "max_cpu_percent": 80,
    "max_memory_mb": 8192,
    "max_gpu_percent": 0,
    "wall_clock_budget_minutes": 120,
    "token_budget": 100000,
    "egress_policy": "package-registries-only",
}


def _profile(**overrides: Any) -> str:
    """A valid profile document with fields replaced or removed."""
    payload = dict(VALID)
    for key, value in overrides.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    return json.dumps(payload)


def _load(**overrides: Any) -> VolunteerProfile:
    return load_profile(_profile(**overrides))


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_a_valid_profile_loads_every_declared_field() -> None:
    profile = _load()

    assert profile.version == 1
    assert profile.allowed_projects == ("owner/repo",)
    assert profile.allowed_licenses == ("MIT", "Apache-2.0")
    assert profile.allowed_task_types == ("bug", "feature")
    assert profile.max_size == "m"
    assert profile.adapters == ("claude-code",)
    assert profile.models == ("claude-3-7-sonnet-20250219",)
    assert profile.max_cpu_percent == 80
    assert profile.max_memory_mb == 8192
    assert profile.max_gpu_percent == 0
    assert profile.wall_clock_budget_minutes == 120
    assert profile.token_budget == 100000
    assert profile.egress_policy == "package-registries-only"


def test_flags_alone_fully_configure_a_run_with_no_profile_file_present() -> None:
    """The profile file is optional per the issue's proposed approach."""
    profile = VolunteerProfile.from_flags(
        allowed_projects=["owner/repo"],
        allowed_licenses=["MIT"],
        max_size="s",
        adapters=["claude-code"],
        models=["claude-3-7-sonnet-20250219"],
    )

    assert profile.allowed_projects == ("owner/repo",)
    assert profile.allowed_licenses == ("MIT",)
    assert profile.max_size == "s"
    assert profile.adapters == ("claude-code",)
    assert profile.models == ("claude-3-7-sonnet-20250219",)


def test_a_profile_field_conflicting_with_an_explicit_flag_prefers_the_flag() -> None:
    """Flags always override the file; the file only fills in what flags did not set."""
    profile = VolunteerProfile.from_flags_and_file(
        file_content=_profile(max_size="l"),
        allowed_projects=["owner/repo"],
        max_size="s",
    )

    assert profile.max_size == "s"


def test_an_unsupported_schema_version_is_refused_naming_the_field() -> None:
    with pytest.raises(VolunteerProfileError) as exc_info:
        _load(version=99)

    assert exc_info.value.field == "version"
    assert "unsupported schema version 99" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


def test_version_is_required() -> None:
    with pytest.raises(VolunteerProfileError) as exc_info:
        _load(version=None)

    assert exc_info.value.field == "version"
    assert "required" in str(exc_info.value)


def test_allowed_projects_must_be_a_list() -> None:
    with pytest.raises(VolunteerProfileError) as exc_info:
        _load(allowed_projects="owner/repo")

    assert exc_info.value.field == "allowed_projects"
    assert "expected a list" in str(exc_info.value)


def test_allowed_projects_must_be_nonempty() -> None:
    with pytest.raises(VolunteerProfileError) as exc_info:
        _load(allowed_projects=[])

    assert exc_info.value.field == "allowed_projects"
    assert "empty" in str(exc_info.value)


def test_max_size_must_be_one_of_the_defined_values() -> None:
    with pytest.raises(VolunteerProfileError) as exc_info:
        _load(max_size="huge")

    assert exc_info.value.field == "max_size"
    assert "not one of" in str(exc_info.value)


def test_max_cpu_percent_must_be_positive() -> None:
    with pytest.raises(VolunteerProfileError) as exc_info:
        _load(max_cpu_percent=0)

    assert exc_info.value.field == "max_cpu_percent"
    assert "must be at least 1" in str(exc_info.value)


def test_egress_policy_must_be_one_of_the_defined_values() -> None:
    with pytest.raises(VolunteerProfileError) as exc_info:
        _load(egress_policy="anything-goes")

    assert exc_info.value.field == "egress_policy"
    assert "not one of" in str(exc_info.value)
