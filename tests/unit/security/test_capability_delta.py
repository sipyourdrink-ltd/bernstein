from bernstein.core.security.capability_delta import (
    GrantDelta,
    GrantDirection,
    compute_grant_delta,
)
from bernstein.core.security.permissions import DEFAULT_ROLE_PERMISSIONS, AgentPermissions


def test_widening_allowed_paths():
    base = DEFAULT_ROLE_PERMISSIONS["devops"]
    new_allowed = (*base.allowed_paths, ".github/workflows/*")
    new = AgentPermissions(
        allowed_paths=new_allowed,
        denied_paths=base.denied_paths,
        allowed_commands=base.allowed_commands,
        denied_commands=base.denied_commands,
    )
    delta = compute_grant_delta(base, new, role="devops", run_id="run-1")
    assert delta.is_widening is True
    assert len(delta.changes) == 1
    c = delta.changes[0]
    assert c.direction == GrantDirection.WIDENING
    assert c.axis == "allowed"
    assert c.path == ".github/workflows/*"


def test_narrowing_allowed_paths():
    base = AgentPermissions(allowed_paths=(".github/*", "Dockerfile"), denied_paths=())
    new = AgentPermissions(allowed_paths=("Dockerfile",), denied_paths=())
    delta = compute_grant_delta(base, new, role="devops", run_id="run-1")
    assert delta.is_widening is False
    assert len(delta.changes) == 1
    assert delta.changes[0].direction == GrantDirection.NARROWING
    assert delta.changes[0].axis == "allowed"


def test_removing_deny_is_widening():
    base = AgentPermissions(allowed_paths=(), denied_paths=(".sdd/*", "templates/roles/*"))
    new = AgentPermissions(allowed_paths=(), denied_paths=("templates/roles/*",))
    delta = compute_grant_delta(base, new, role="devops", run_id="run-1")
    assert delta.is_widening is True
    assert len(delta.changes) == 1
    assert delta.changes[0].direction == GrantDirection.WIDENING
    assert delta.changes[0].axis == "denied"
    assert delta.changes[0].path == ".sdd/*"


def test_adding_deny_is_narrowing():
    base = AgentPermissions(allowed_paths=(), denied_paths=())
    new = AgentPermissions(allowed_paths=(), denied_paths=("new_deny/*",))
    delta = compute_grant_delta(base, new, role="test", run_id="run-1")
    assert delta.is_widening is False
    assert len(delta.changes) == 1
    assert delta.changes[0].direction == GrantDirection.NARROWING
    assert delta.changes[0].axis == "denied"
    assert delta.changes[0].path == "new_deny/*"


def test_no_change_empty_changes():
    base = AgentPermissions(allowed_paths=("src/*",), denied_paths=(".sdd/*",))
    new = AgentPermissions(allowed_paths=("src/*",), denied_paths=(".sdd/*",))
    delta = compute_grant_delta(base, new, role="test", run_id="run")
    assert delta.is_widening is False
    assert len(delta.changes) == 0


def test_delta_hash_deterministic():
    base = AgentPermissions(allowed_paths=("src/*",), denied_paths=(".sdd/*",))
    new = AgentPermissions(allowed_paths=("src/*", "docs/*"), denied_paths=())
    delta1 = compute_grant_delta(base, new, role="test", run_id="run")
    delta2 = compute_grant_delta(base, new, role="test", run_id="run")
    assert delta1.delta_hash == delta2.delta_hash
    assert delta1.delta_hash.startswith("sha256:")

    # Change order of new paths (should be equivalent sets so delta identical)
    new2 = AgentPermissions(allowed_paths=("docs/*", "src/*"), denied_paths=())
    delta3 = compute_grant_delta(base, new2, role="test", run_id="run")
    assert delta1.delta_hash == delta3.delta_hash


def test_to_dict_from_dict_round_trip():
    base = AgentPermissions(allowed_paths=("src/*",), denied_paths=(".sdd/*",))
    new = AgentPermissions(allowed_paths=("src/*", "docs/*"), denied_paths=())
    delta = compute_grant_delta(base, new, role="test", run_id="run", timestamp_ns=123)

    d = delta.to_dict()
    restored = GrantDelta.from_dict(d)

    assert restored.role == delta.role
    assert restored.run_id == delta.run_id
    assert restored.timestamp_ns == delta.timestamp_ns
    assert restored.changes == delta.changes
    assert restored.delta_hash == delta.delta_hash


def test_motivating_scenario_devops():
    base = DEFAULT_ROLE_PERMISSIONS["devops"]
    # devops widens to include publish.yml explicitly
    new_allowed = set(base.allowed_paths) | {".github/workflows/publish.yml"}
    new = AgentPermissions(
        allowed_paths=tuple(new_allowed),
        denied_paths=base.denied_paths,
        allowed_commands=base.allowed_commands,
        denied_commands=base.denied_commands,
    )
    delta = compute_grant_delta(base, new, role="devops", run_id="run-1")
    assert delta.is_widening is True

    # We should have one WIDENING change
    widening_changes = [c for c in delta.changes if c.direction == GrantDirection.WIDENING]
    assert len(widening_changes) == 1
    assert widening_changes[0].path == ".github/workflows/publish.yml"
    assert widening_changes[0].axis == "allowed"
