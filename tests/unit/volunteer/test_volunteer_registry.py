"""Tests for the volunteer project registry browse logic."""

import json

import pytest

from bernstein.core.volunteer.registry import (
    HTTPResponse,
    browse_indexes,
)

#: A minimal valid volunteer manifest.
VALID_MANIFEST = json.dumps(
    {
        "version": 1,
        "license": "MIT",
        "gates": [["echo", "hello"]],
        "allowed_paths": [],
        "egress_allowlist": [],
        "sandbox": "container",
        "max_wall_clock_minutes": 60,
        "task_label": "volunteer-ok",
        "local_ok": True,
    }
).encode()

#: A manifest with a non-OSI license.
NON_OSI_MANIFEST = json.dumps(
    {
        "version": 1,
        "license": "Proprietary",
        "gates": [["echo", "hello"]],
        "allowed_paths": [],
        "egress_allowlist": [],
        "sandbox": "container",
        "max_wall_clock_minutes": 60,
        "task_label": "volunteer-ok",
        "local_ok": True,
    }
).encode()

#: A manifest for a project that does not accept local models.
NO_LOCAL_MANIFEST = json.dumps(
    {
        "version": 1,
        "license": "MIT",
        "gates": [["echo", "hello"]],
        "allowed_paths": [],
        "egress_allowlist": [],
        "sandbox": "container",
        "max_wall_clock_minutes": 120,
        "task_label": "volunteer-ok",
        "local_ok": False,
    }
).encode()

#: A manifest for a paused project (no longer accepting volunteer work).
PAUSED_MANIFEST = json.dumps(
    {
        "version": 1,
        "license": "MIT",
        "gates": [["echo", "hello"]],
        "allowed_paths": [],
        "egress_allowlist": [],
        "sandbox": "container",
        "max_wall_clock_minutes": 60,
        "task_label": "volunteer-ok",
        "local_ok": True,
        "status": "paused",
    }
).encode()


def _resolves_to(*addresses: str):
    """A resolver that answers with fixed addresses, so no DNS is needed."""
    return lambda _host: list(addresses)


class _FakeTransport:
    """Test double for HTTPTransport that returns canned responses."""

    def __init__(self) -> None:
        self.responses: dict[str, HTTPResponse] = {}
        self.call_count = 0

    def get(self, url: str, *, headers: dict[str, str]) -> HTTPResponse:
        self.call_count += 1
        if url in self.responses:
            return self.responses[url]
        return HTTPResponse(status=404, body=b"", etag=None)


def _make_index(projects: list[dict]) -> bytes:
    return json.dumps({"version": 1, "projects": projects}).encode()


def _manifest_url(repo_url: str, branch: str = "main") -> str:
    return f"{repo_url.rstrip('/')}/raw/{branch}/.bernstein/volunteer.json"


def test_two_indexes_with_overlapping_projects_merge_without_duplicates() -> None:
    transport = _FakeTransport()
    repo = "https://github.com/foo/bar"
    transport.responses[_manifest_url(repo)] = HTTPResponse(status=200, body=VALID_MANIFEST, etag=None)
    transport.responses["https://a.test/index.json"] = HTTPResponse(
        status=200,
        body=_make_index(
            [{"repo_url": repo, "default_branch": "main", "topics": [], "license": "MIT", "local_ok": True}]
        ),
        etag=None,
    )
    transport.responses["https://b.test/index.json"] = HTTPResponse(
        status=200,
        body=_make_index(
            [{"repo_url": repo, "default_branch": "main", "topics": [], "license": "MIT", "local_ok": True}]
        ),
        etag=None,
    )

    joinable, dropped = browse_indexes(
        ["https://a.test/index.json", "https://b.test/index.json"],
        transport=transport,
    )

    assert len(joinable) == 1
    assert joinable[0].repo_url == repo
    assert len(dropped) == 0


def test_a_project_with_a_non_osi_license_is_dropped_with_a_reason() -> None:
    transport = _FakeTransport()
    repo = "https://github.com/bad/license"
    transport.responses[_manifest_url(repo)] = HTTPResponse(status=200, body=NON_OSI_MANIFEST, etag=None)
    transport.responses["https://index.test/i.json"] = HTTPResponse(
        status=200,
        body=_make_index(
            [{"repo_url": repo, "default_branch": "main", "topics": [], "license": "Proprietary", "local_ok": True}]
        ),
        etag=None,
    )

    joinable, dropped = browse_indexes(["https://index.test/i.json"], transport=transport)

    assert len(joinable) == 0
    assert len(dropped) == 1
    assert dropped[0].repo_url == repo
    assert "license" in dropped[0].reason


def test_a_paused_project_is_dropped_from_browse() -> None:
    """A paused manifest still loads and validates, but browse skips it."""
    transport = _FakeTransport()
    repo = "https://github.com/paused/quiet"
    transport.responses[_manifest_url(repo)] = HTTPResponse(status=200, body=PAUSED_MANIFEST, etag=None)
    transport.responses["https://index.test/i.json"] = HTTPResponse(
        status=200,
        body=_make_index(
            [{"repo_url": repo, "default_branch": "main", "topics": [], "license": "MIT", "local_ok": True}]
        ),
        etag=None,
    )

    joinable, dropped = browse_indexes(["https://index.test/i.json"], transport=transport)

    assert len(joinable) == 0
    assert len(dropped) == 1
    assert dropped[0].repo_url == repo
    assert "status=paused" in dropped[0].reason


def test_a_project_with_no_reachable_manifest_is_dropped_with_a_reason() -> None:
    transport = _FakeTransport()
    repo = "https://github.com/missing/manifest"
    # No manifest response registered -> _FakeTransport returns 404
    transport.responses["https://index.test/i.json"] = HTTPResponse(
        status=200,
        body=_make_index(
            [{"repo_url": repo, "default_branch": "main", "topics": [], "license": "MIT", "local_ok": True}]
        ),
        etag=None,
    )

    joinable, dropped = browse_indexes(["https://index.test/i.json"], transport=transport)

    assert len(joinable) == 0
    assert len(dropped) == 1
    assert dropped[0].repo_url == repo
    assert "404" in dropped[0].reason


def test_size_language_local_ok_and_budget_filters_compose() -> None:
    transport = _FakeTransport()
    repo_a = "https://github.com/good/project"
    repo_b = "https://github.com/bad/project"

    transport.responses[_manifest_url(repo_a)] = HTTPResponse(status=200, body=VALID_MANIFEST, etag=None)
    transport.responses[_manifest_url(repo_b)] = HTTPResponse(status=200, body=NO_LOCAL_MANIFEST, etag=None)
    transport.responses["https://index.test/i.json"] = HTTPResponse(
        status=200,
        body=_make_index(
            [
                {
                    "repo_url": repo_a,
                    "default_branch": "main",
                    "topics": ["python", "size/s"],
                    "license": "MIT",
                    "local_ok": True,
                },
                {
                    "repo_url": repo_b,
                    "default_branch": "main",
                    "topics": ["go", "size/m"],
                    "license": "MIT",
                    "local_ok": False,
                },
            ]
        ),
        etag=None,
    )

    joinable, dropped = browse_indexes(
        ["https://index.test/i.json"],
        transport=transport,
        size="s",
        language="python",
        local_ok_only=True,
        budget_minutes=60,
    )

    assert len(joinable) == 1
    assert joinable[0].repo_url == repo_a
    # repo_b should be in dropped (local_ok=False, budget 120>60, language go != python, size/m != size/s)
    dropped_b = [d for d in dropped if d.repo_url == repo_b]
    assert len(dropped_b) == 1


def test_a_non_https_index_url_is_refused() -> None:
    transport = _FakeTransport()

    joinable, dropped = browse_indexes(
        ["http://example.test/index.json"],
        transport=transport,
    )

    assert len(joinable) == 0
    assert transport.call_count == 0
    assert len(dropped) == 1
    assert "URL scheme" in dropped[0].reason or "rejected" in dropped[0].reason


def test_browse_rejects_internal_index_url() -> None:
    """browse_indexes must reject index URLs that resolve to internal addresses."""
    from bernstein.core.volunteer.registry import _UrllibTransport

    transport = _UrllibTransport()

    joinable, dropped = browse_indexes(
        ["https://127.0.0.1/index.json"],
        transport=transport,
    )

    assert len(joinable) == 0
    assert len(dropped) == 1
    assert "internal address" in dropped[0].reason or "rejected" in dropped[0].reason


def test_browse_rejects_internal_manifest_url() -> None:
    """browse_indexes must reject manifest URLs pointing to internal addresses."""
    transport = _FakeTransport()
    repo = "https://github.com/foo/bar"

    transport.responses["https://index.test/index.json"] = HTTPResponse(
        status=200,
        body=_make_index(
            [{"repo_url": repo, "default_branch": "main", "topics": [], "license": "MIT", "local_ok": True}]
        ),
        etag=None,
    )
    transport.responses["https://github.com/foo/bar/raw/main/.bernstein/volunteer.json"] = HTTPResponse(
        status=404, body=b"", etag=None
    )

    joinable, dropped = browse_indexes(
        ["https://index.test/index.json"],
        transport=transport,
    )

    assert len(joinable) == 0
    assert len(dropped) == 1
    assert dropped[0].repo_url == repo
    assert "404" in dropped[0].reason


@pytest.mark.parametrize(
    "internal_ip",
    [
        "127.0.0.1",
        "::1",
        "169.254.169.254",
        "10.0.0.5",
        "192.168.1.100",
        "fe80::1",
        "fc00::1",
    ],
)
def test_browse_rejects_index_with_internal_repo_url(monkeypatch: pytest.MonkeyPatch, internal_ip: str) -> None:
    """browse_indexes must reject repo_urls that resolve to internal addresses."""

    def resolver(host: str) -> list[str]:
        if host == "internal.example":
            return [internal_ip]
        return ["93.184.216.34"]

    monkeypatch.setattr(
        "bernstein.core.security.url_allowlist._default_resolver",
        resolver,
    )

    transport = _FakeTransport()
    repo = "https://internal.example/repo"

    transport.responses["https://index.test/index.json"] = HTTPResponse(
        status=200,
        body=_make_index(
            [{"repo_url": repo, "default_branch": "main", "topics": [], "license": "MIT", "local_ok": True}]
        ),
        etag=None,
    )

    joinable, dropped = browse_indexes(
        ["https://index.test/index.json"],
        transport=transport,
    )

    assert len(joinable) == 0
    assert len(dropped) == 1
    assert dropped[0].repo_url == repo
    assert "internal address" in dropped[0].reason


@pytest.mark.parametrize(
    "internal_ip",
    [
        "127.0.0.1",
        "::1",
        "169.254.169.254",
    ],
)
def test_browse_rejects_rebinding_repo_url(monkeypatch: pytest.MonkeyPatch, internal_ip: str) -> None:
    """browse_indexes must reject repo_urls that resolve to mixed public+internal addresses."""

    def resolver(host: str) -> list[str]:
        if host == "rebind.example":
            return ["93.184.216.34", internal_ip]
        return ["93.184.216.34"]

    monkeypatch.setattr(
        "bernstein.core.security.url_allowlist._default_resolver",
        resolver,
    )

    transport = _FakeTransport()
    repo = "https://rebind.example/repo"

    transport.responses["https://index.test/index.json"] = HTTPResponse(
        status=200,
        body=_make_index(
            [{"repo_url": repo, "default_branch": "main", "topics": [], "license": "MIT", "local_ok": True}]
        ),
        etag=None,
    )

    joinable, dropped = browse_indexes(
        ["https://index.test/index.json"],
        transport=transport,
    )

    assert len(joinable) == 0
    assert len(dropped) == 1
    assert dropped[0].repo_url == repo
    assert "internal address" in dropped[0].reason


def test_browse_rejects_internal_index_url_via_resolver() -> None:
    """browse_indexes must reject index URLs pointing to internal addresses."""
    from bernstein.core.volunteer.registry import _UrllibTransport

    transport = _UrllibTransport()

    joinable, dropped = browse_indexes(
        ["https://127.0.0.1/index.json"],
        transport=transport,
    )

    assert len(joinable) == 0
    assert len(dropped) == 1
    assert "internal address" in dropped[0].reason


@pytest.mark.parametrize(
    "internal_ip",
    [
        "10.0.0.5",
        "172.16.0.100",
        "192.168.1.1",
        "fe80::1",
        "fc00::1",
    ],
)
def test_browse_rejects_index_with_internal_repo_various_ranges(
    monkeypatch: pytest.MonkeyPatch, internal_ip: str
) -> None:
    """browse_indexes must reject various internal IP ranges in repo URLs."""

    def resolver(host: str) -> list[str]:
        if host == "private.repo":
            return [internal_ip]
        return ["93.184.216.34"]

    monkeypatch.setattr(
        "bernstein.core.security.url_allowlist._default_resolver",
        resolver,
    )

    transport = _FakeTransport()
    repo = "https://private.repo/repo"

    transport.responses["https://index.test/index.json"] = HTTPResponse(
        status=200,
        body=_make_index(
            [{"repo_url": repo, "default_branch": "main", "topics": [], "license": "MIT", "local_ok": True}]
        ),
        etag=None,
    )

    joinable, dropped = browse_indexes(
        ["https://index.test/index.json"],
        transport=transport,
    )

    assert len(joinable) == 0
    assert len(dropped) == 1
    assert dropped[0].repo_url == repo
    assert "internal address" in dropped[0].reason


@pytest.mark.parametrize(
    "internal_ip",
    [
        "127.0.0.1",
        "::1",
        "169.254.169.254",
        "10.0.0.5",
        "192.168.1.100",
        "fe80::1",
        "fc00::1",
    ],
)
def test_browse_rejects_internal_manifest_url_via_repo(monkeypatch: pytest.MonkeyPatch, internal_ip: str) -> None:
    """browse_indexes must reject manifest URLs when repo resolves to internal address."""

    def resolver(host: str) -> list[str]:
        if host == "internal.example":
            return [internal_ip]
        return ["93.184.216.34"]

    monkeypatch.setattr(
        "bernstein.core.security.url_allowlist._default_resolver",
        resolver,
    )

    transport = _FakeTransport()
    repo = "https://internal.example/repo"

    transport.responses["https://index.test/index.json"] = HTTPResponse(
        status=200,
        body=_make_index(
            [{"repo_url": repo, "default_branch": "main", "topics": [], "license": "MIT", "local_ok": True}]
        ),
        etag=None,
    )

    joinable, dropped = browse_indexes(
        ["https://index.test/index.json"],
        transport=transport,
    )

    assert len(joinable) == 0
    assert len(dropped) == 1
    assert dropped[0].repo_url == repo
    assert "internal address" in dropped[0].reason


def test_browse_rejects_internal_manifest_url_ipv6_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """browse_indexes must reject IPv6 loopback in manifest URL."""

    def resolver(host: str) -> list[str]:
        if host == "localhost6.example":
            return ["::1"]
        return ["93.184.216.34"]

    monkeypatch.setattr(
        "bernstein.core.security.url_allowlist._default_resolver",
        resolver,
    )

    transport = _FakeTransport()
    repo = "https://localhost6.example/repo"

    transport.responses["https://index.test/index.json"] = HTTPResponse(
        status=200,
        body=_make_index(
            [{"repo_url": repo, "default_branch": "main", "topics": [], "license": "MIT", "local_ok": True}]
        ),
        etag=None,
    )

    joinable, dropped = browse_indexes(
        ["https://index.test/index.json"],
        transport=transport,
    )

    assert len(joinable) == 0
    assert len(dropped) == 1
    assert dropped[0].repo_url == repo
    assert "internal address" in dropped[0].reason


def test_browse_rejects_rebinding_manifest_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """browse_indexes must reject manifest URLs with mixed public+internal addresses."""

    def resolver(host: str) -> list[str]:
        if host == "rebind.example":
            return ["93.184.216.34", "127.0.0.1"]
        return ["93.184.216.34"]

    monkeypatch.setattr(
        "bernstein.core.security.url_allowlist._default_resolver",
        resolver,
    )

    transport = _FakeTransport()
    repo = "https://rebind.example/repo"

    transport.responses["https://index.test/index.json"] = HTTPResponse(
        status=200,
        body=_make_index(
            [{"repo_url": repo, "default_branch": "main", "topics": [], "license": "MIT", "local_ok": True}]
        ),
        etag=None,
    )

    joinable, dropped = browse_indexes(
        ["https://index.test/index.json"],
        transport=transport,
    )

    assert len(joinable) == 0
    assert len(dropped) == 1
    assert dropped[0].repo_url == repo
    assert "internal address" in dropped[0].reason
