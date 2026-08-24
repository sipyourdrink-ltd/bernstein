from bernstein.core.security.capability_delta import GrantDirection
from bernstein.core.security.surface_grant_delta import (
    SurfaceGrantChange,
    SurfaceGrantDelta,
    compute_surface_grant_delta,
    is_permission_bearing_surface,
)


def test_is_permission_bearing_surface():
    assert is_permission_bearing_surface(".github/workflows/ci.yml")
    assert is_permission_bearing_surface(".github/workflows/release.yaml")
    assert is_permission_bearing_surface("Dockerfile")
    assert is_permission_bearing_surface("docker-compose.yaml")
    assert is_permission_bearing_surface("docker-compose.yml")
    assert is_permission_bearing_surface("docker/Dockerfile")
    assert not is_permission_bearing_surface("src/foo.py")
    assert not is_permission_bearing_surface("README.md")
    assert not is_permission_bearing_surface(".github/CODEOWNERS")


def test_non_surface_returns_none():
    assert compute_surface_grant_delta("src/foo.py", "a", "b") is None


def test_identical_content_is_neutral():
    content = "name: ci\non: push\n"
    delta = compute_surface_grant_delta(".github/workflows/ci.yml", content, content)
    assert delta is not None
    assert delta.direction == GrantDirection.UNCHANGED
    assert delta.changes == ()
    assert delta.is_widening is False


def test_comment_only_change_is_neutral():
    old = "name: ci\n# old comment\non: push\n"
    new = "name: ci\n# new comment\non: push\n"
    delta = compute_surface_grant_delta(".github/workflows/ci.yml", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.UNCHANGED
    assert delta.changes == ()


def test_permissions_scope_widening():
    old = "name: ci\non: push\npermissions:\n  contents: read\n"
    new = "name: ci\non: push\npermissions:\n  contents: write\n"
    delta = compute_surface_grant_delta(".github/workflows/ci.yml", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.WIDENING
    assert delta.is_widening is True
    assert any(c.axis == "permissions" and c.direction == GrantDirection.WIDENING for c in delta.changes)


def test_permissions_scope_narrowing():
    old = "name: ci\non: push\npermissions:\n  contents: write\n"
    new = "name: ci\non: push\npermissions:\n  contents: read\n"
    delta = compute_surface_grant_delta(".github/workflows/ci.yml", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.NARROWING
    assert all(c.direction == GrantDirection.NARROWING for c in delta.changes)


def test_adding_permission_scope_is_widening():
    old = "name: ci\non: push\npermissions:\n  contents: read\n"
    new = "name: ci\non: push\npermissions:\n  contents: read\n  issues: write\n"
    delta = compute_surface_grant_delta(".github/workflows/ci.yml", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.WIDENING
    assert any(c.detail and "added permission scope issues" in c.detail for c in delta.changes)


def test_removing_permission_scope_is_narrowing():
    old = "name: ci\non: push\npermissions:\n  contents: read\n  issues: write\n"
    new = "name: ci\non: push\npermissions:\n  contents: read\n"
    delta = compute_surface_grant_delta(".github/workflows/ci.yml", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.NARROWING
    assert any(c.detail and "removed permission scope issues" in c.detail for c in delta.changes)


def test_commenting_out_permissions_widens_to_default():
    # Default is {contents: read, pull-requests: read, issues: read}; the
    # commented block only granted contents: read, so the effective default is
    # wider.
    old = "name: ci\non: push\npermissions:\n  contents: read\n"
    new = "name: ci\non: push\n# permissions:\n#   contents: read\n"
    delta = compute_surface_grant_delta(".github/workflows/ci.yml", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.WIDENING


def test_commenting_out_default_permissions_is_neutral():
    old = "name: ci\non: push\npermissions:\n  contents: read\n  pull-requests: read\n  issues: read\n"
    new = "name: ci\non: push\n# permissions:\n#   contents: read\n#   pull-requests: read\n#   issues: read\n"
    delta = compute_surface_grant_delta(".github/workflows/ci.yml", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.UNCHANGED


def test_job_level_permissions_widening():
    old = (
        "name: ci\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    permissions:\n      contents: read\n    steps:\n      - run: echo hi\n"
    )
    new = (
        "name: ci\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    permissions:\n      contents: write\n    steps:\n      - run: echo hi\n"
    )
    delta = compute_surface_grant_delta(".github/workflows/ci.yml", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.WIDENING
    assert any(c.axis == "job:build.permissions" for c in delta.changes)


def test_action_ref_pinned_to_floating_is_widening():
    old = (
        "name: ci\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
    )
    new = (
        "name: ci\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: actions/checkout@v7\n"
    )
    delta = compute_surface_grant_delta(".github/workflows/ci.yml", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.WIDENING
    assert any(c.axis == "action_ref" and c.direction == GrantDirection.WIDENING for c in delta.changes)


def test_action_ref_floating_to_pinned_is_narrowing():
    old = (
        "name: ci\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: actions/checkout@v7\n"
    )
    new = (
        "name: ci\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
    )
    delta = compute_surface_grant_delta(".github/workflows/ci.yml", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.NARROWING
    assert any(c.axis == "action_ref" and c.direction == GrantDirection.NARROWING for c in delta.changes)


def test_new_secret_reference_is_widening():
    old = "name: ci\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
    new = (
        "name: ci\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: echo hi\n        env:\n          TOKEN: ${{ secrets.DEPLOY_TOKEN }}\n"
    )
    delta = compute_surface_grant_delta(".github/workflows/ci.yml", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.WIDENING
    assert any(c.axis == "secret_ref" and c.new_value == "DEPLOY_TOKEN" for c in delta.changes)


def test_yaml_anchor_permissions_resolved():
    old = (
        "name: ci\non: push\njobs:\n  build:\n    permissions: &perms\n      contents: read\n"
        "    steps:\n      - run: echo hi\n"
    )
    new = (
        "name: ci\non: push\njobs:\n  build:\n    permissions: &perms\n      contents: write\n"
        "    steps:\n      - run: echo hi\n"
    )
    delta = compute_surface_grant_delta(".github/workflows/ci.yml", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.WIDENING


def test_dockerfile_user_to_root_is_widening():
    old = "FROM python:3.12\nUSER appuser\n"
    new = "FROM python:3.12\nUSER root\n"
    delta = compute_surface_grant_delta("Dockerfile", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.WIDENING
    assert any(c.axis == "dockerfile_user" and c.direction == GrantDirection.WIDENING for c in delta.changes)


def test_dockerfile_user_to_non_root_is_narrowing():
    old = "FROM python:3.12\nUSER root\n"
    new = "FROM python:3.12\nUSER appuser\n"
    delta = compute_surface_grant_delta("Dockerfile", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.NARROWING
    assert any(c.axis == "dockerfile_user" and c.direction == GrantDirection.NARROWING for c in delta.changes)


def test_dockerfile_cap_add_is_widening():
    old = "FROM python:3.12\nRUN apt-get update\n"
    new = "FROM python:3.12\nRUN apt-get update && docker run --cap-add=NET_ADMIN /bin/app\n"
    delta = compute_surface_grant_delta("Dockerfile", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.WIDENING
    assert any(c.axis == "container_caps" and c.new_value == "NET_ADMIN" for c in delta.changes)


def test_dockerfile_privileged_is_widening():
    old = "FROM python:3.12\nRUN echo hi\n"
    new = "FROM python:3.12\nRUN --privileged echo hi\n"
    delta = compute_surface_grant_delta("Dockerfile", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.WIDENING
    assert any(c.axis == "container_caps" and c.new_value == "privileged" for c in delta.changes)


def test_dockerfile_comment_only_is_neutral():
    old = "FROM python:3.12\n# old comment\nUSER appuser\n"
    new = "FROM python:3.12\n# new comment\nUSER appuser\n"
    delta = compute_surface_grant_delta("Dockerfile", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.UNCHANGED


def test_compose_cap_add_is_widening():
    old = "services:\n  app:\n    image: nginx\n"
    new = "services:\n  app:\n    image: nginx\n    cap_add:\n      - NET_ADMIN\n"
    delta = compute_surface_grant_delta("docker-compose.yaml", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.WIDENING
    assert any(c.axis == "container_caps" and c.new_value == "NET_ADMIN" for c in delta.changes)


def test_compose_cap_drop_is_narrowing():
    old = "services:\n  app:\n    image: nginx\n"
    new = "services:\n  app:\n    image: nginx\n    cap_drop:\n      - ALL\n"
    delta = compute_surface_grant_delta("docker-compose.yml", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.NARROWING
    assert any(c.axis == "container_caps" and c.direction == GrantDirection.NARROWING for c in delta.changes)


def test_compose_privileged_is_widening():
    old = "services:\n  app:\n    image: nginx\n"
    new = "services:\n  app:\n    image: nginx\n    privileged: true\n"
    delta = compute_surface_grant_delta("docker-compose.yaml", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.WIDENING
    assert any(c.axis == "container_caps" and c.new_value == "true" for c in delta.changes)


def test_compose_user_to_root_is_widening():
    old = 'services:\n  app:\n    image: nginx\n    user: "1000:1000"\n'
    new = "services:\n  app:\n    image: nginx\n    user: root\n"
    delta = compute_surface_grant_delta("docker-compose.yaml", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.WIDENING
    assert any(c.axis == "dockerfile_user" and c.direction == GrantDirection.WIDENING for c in delta.changes)


def test_mixed_changes_are_widening():
    # One widening (contents read->write) and one narrowing (removed issues
    # scope) -> overall widening, because any widening change wins.
    old = "name: ci\non: push\npermissions:\n  contents: read\n  issues: write\n"
    new = "name: ci\non: push\npermissions:\n  contents: write\n"
    delta = compute_surface_grant_delta(".github/workflows/ci.yml", old, new)
    assert delta is not None
    assert delta.direction == GrantDirection.WIDENING
    assert delta.is_widening is True
    assert len(delta.changes) >= 2


def test_delta_dataclass_shape():
    change = SurfaceGrantChange(
        axis="permissions",
        direction=GrantDirection.WIDENING,
        old_value="contents: read",
        new_value="contents: write",
        detail="widened",
    )
    delta = SurfaceGrantDelta(
        path=".github/workflows/ci.yml",
        direction=GrantDirection.WIDENING,
        changes=(change,),
    )
    assert delta.is_widening is True
    assert delta.changes[0].axis == "permissions"
