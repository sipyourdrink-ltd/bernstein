"""Check that logging preserves request scope and never prints bearer values."""

from starlette.requests import Request

from bernstein.core.server.access_log import extract_remote_ip, extract_request_actor, extract_tenant_id


def test_claimed_tenant_is_for_logging_only() -> None:
    request = Request({"type": "http", "headers": [(b"x-tenant-id", b" claimed ")]})

    assert extract_tenant_id(request) == "claimed"
    assert not hasattr(request.state, "tenant_id")
    request.state.tenant_id = " bound "
    assert extract_tenant_id(request) == "bound"
    assert request.state.tenant_id == " bound "


def test_actor_label_uses_state_and_does_not_return_header() -> None:
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer test-token")]})

    assert extract_request_actor(request) == "authenticated"
    request.state.auth_actor = " operator "
    assert extract_request_actor(request) == "operator"


def test_blank_tenant_and_missing_client_have_stable_labels() -> None:
    request = Request({"type": "http", "headers": [(b"x-tenant-id", b"  ")]})

    assert extract_tenant_id(request) == "default"
    assert extract_request_actor(request) == "anonymous"
    assert extract_remote_ip(request) == "unknown"
