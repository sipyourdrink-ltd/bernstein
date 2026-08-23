"""Tests for the volunteer project registry browse logic."""

import json

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
