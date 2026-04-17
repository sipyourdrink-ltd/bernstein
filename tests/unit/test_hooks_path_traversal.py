"""Tests for audit-114: path-traversal hardening of the hooks receiver.

Covers both the low-level ``validate_session_id`` / ``_safe_child`` helpers
and the end-to-end ``POST /hooks/{session_id}`` route.  Every case that
arrives with an attacker-controlled ``session_id`` must either be rejected
with HTTP 400 before any filesystem access or be contained within the
intended base directory via ``Path.resolve().is_relative_to(base)``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from urllib.parse import quote

import pytest
from bernstein.core.hooks_receiver import (
    HookEvent,
    HookEventType,
    InvalidSessionIdError,
    _safe_child,
    touch_heartbeat,
    validate_session_id,
    write_hook_event,
    write_stop_marker,
)
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Unit-level: validate_session_id
# ---------------------------------------------------------------------------


class TestValidateSessionId:
    """The allowlist regex must reject every traversal shape we know of."""

    def test_plain_alphanumeric_accepted(self) -> None:
        assert validate_session_id("sess-001") == "sess-001"

    def test_underscore_and_digits_accepted(self) -> None:
        assert validate_session_id("agent_42_abc") == "agent_42_abc"

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(InvalidSessionIdError):
            validate_session_id("")

    def test_dotdot_rejected(self) -> None:
        with pytest.raises(InvalidSessionIdError):
            validate_session_id("..")

    def test_dotdot_with_separator_rejected(self) -> None:
        with pytest.raises(InvalidSessionIdError):
            validate_session_id("../etc/passwd")

    def test_forward_slash_rejected(self) -> None:
        with pytest.raises(InvalidSessionIdError):
            validate_session_id("foo/bar")

    def test_backslash_rejected(self) -> None:
        with pytest.raises(InvalidSessionIdError):
            validate_session_id("foo\\bar")

    def test_absolute_path_rejected(self) -> None:
        with pytest.raises(InvalidSessionIdError):
            validate_session_id("/etc/passwd")

    def test_null_byte_rejected(self) -> None:
        with pytest.raises(InvalidSessionIdError):
            validate_session_id("foo\x00bar")

    def test_dot_rejected(self) -> None:
        # A single dot would resolve to the base dir itself, so reject too.
        with pytest.raises(InvalidSessionIdError):
            validate_session_id(".")

    def test_whitespace_rejected(self) -> None:
        with pytest.raises(InvalidSessionIdError):
            validate_session_id("foo bar")

    def test_over_length_rejected(self) -> None:
        with pytest.raises(InvalidSessionIdError):
            validate_session_id("a" * 129)

    def test_non_string_rejected(self) -> None:
        with pytest.raises(InvalidSessionIdError):
            validate_session_id(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Unit-level: _safe_child containment + symlink escape
# ---------------------------------------------------------------------------


class TestSafeChild:
    """``_safe_child`` must contain every resolved path inside ``base``."""

    def test_valid_child_contained(self, tmp_path: Path) -> None:
        base = tmp_path / "hooks"
        base.mkdir()
        child = _safe_child(base, "sess-001", suffix=".jsonl")
        assert child.is_relative_to(base.resolve())
        assert child.name == "sess-001.jsonl"

    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        """A symlink under ``base`` pointing outside must be rejected.

        The file does not exist yet, but the *parent component* is a
        symlink so ``resolve()`` escapes. We build the exact path the
        receiver would build (base/session_id) and confirm containment
        fails.
        """
        base = tmp_path / "hooks"
        base.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        # Create a symlink INSIDE base pointing to outside.  The receiver
        # names children as ``base / session_id[+suffix]`` — if a prior
        # run placed a symlinked subdir with a valid-looking name, a
        # follow-up POST using that name would escape without containment
        # checks.
        link = base / "sess-escape"
        os.symlink(outside, link)

        with pytest.raises(InvalidSessionIdError):
            _safe_child(base, "sess-escape", suffix="")

    def test_base_itself_is_symlink_still_contained(self, tmp_path: Path) -> None:
        """If ``base`` is a symlink to a real dir, legitimate children
        must still be accepted after resolution."""
        real_base = tmp_path / "real_hooks"
        real_base.mkdir()
        base_link = tmp_path / "hooks_link"
        os.symlink(real_base, base_link)

        child = _safe_child(base_link, "sess-ok", suffix=".jsonl")
        assert child.is_relative_to(real_base.resolve())


# ---------------------------------------------------------------------------
# Unit-level: helper functions raise before touching disk
# ---------------------------------------------------------------------------


class TestReceiverHelpersRejectBadIds:
    """The public helpers must refuse to create files for bad IDs."""

    def _bad_event(self, session_id: str) -> HookEvent:
        return HookEvent(
            session_id=session_id,
            event_type=HookEventType.POST_TOOL_USE,
            raw_event_name="PostToolUse",
        )

    def test_write_hook_event_rejects_traversal(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidSessionIdError):
            write_hook_event(self._bad_event("../evil"), tmp_path)

    def test_write_stop_marker_rejects_absolute(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidSessionIdError):
            write_stop_marker("/etc/passwd", tmp_path)

    def test_touch_heartbeat_rejects_null_byte(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidSessionIdError):
            touch_heartbeat("foo\x00bar", tmp_path)


# ---------------------------------------------------------------------------
# End-to-end: POST /hooks/{session_id}
# ---------------------------------------------------------------------------


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path, tmp_path: Path):  # type: ignore[no-untyped-def]
    application = create_app(jsonl_path=jsonl_path)
    application.state.workdir = tmp_path  # type: ignore[attr-defined]
    return application


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


def _assert_rejected(response_status: int, response_body: dict[str, object]) -> None:
    assert response_status == 400, f"expected 400, got {response_status}: {response_body!r}"
    assert response_body["status"] == "error"
    assert "session_id" in str(response_body["detail"]).lower()


@pytest.mark.anyio
async def test_valid_session_id_returns_200(client: AsyncClient, tmp_path: Path) -> None:
    """Baseline: a clean session_id is accepted and writes its sidecar."""
    response = await client.post(
        "/hooks/sess-valid-001",
        json={"hook_event_name": "PostToolUse", "tool_name": "Bash"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    sidecar = tmp_path / ".sdd" / "runtime" / "hooks" / "sess-valid-001.jsonl"
    assert sidecar.exists()


@pytest.mark.anyio
async def test_dotdot_session_id_returns_400(client: AsyncClient, tmp_path: Path) -> None:
    """A literal ``..`` as session_id is rejected with 400, no sidecar created.

    httpx collapses ``/hooks/..`` to ``/hooks/`` before sending, so we
    percent-encode each dot to make sure the string survives transport
    and reaches the route parameter verbatim.  Starlette decodes the
    path component back to ``..`` before handing it to the handler.
    """
    response = await client.post(
        "/hooks/%2E%2E",
        json={"hook_event_name": "Stop"},
    )
    _assert_rejected(response.status_code, response.json())
    # Nothing should have been written outside the hooks tree.
    assert not (tmp_path / ".sdd" / "runtime" / "completed" / "..").exists()


@pytest.mark.anyio
async def test_absolute_path_session_id_returns_400(client: AsyncClient, tmp_path: Path) -> None:
    """An ``/etc/passwd``-shaped payload is never honoured as a write target.

    Starlette decodes percent-encoded ``/`` back into path separators, so
    the attacker's literal ``/etc/passwd`` cannot even reach the handler
    as a single route parameter — it either mis-routes (404) or, with
    backslashes that Starlette does not split, the handler rejects it
    with 400.  Both outcomes are security-equivalent: no filesystem
    write happens under the hooks base, and crucially nothing is
    written to ``/etc`` either.

    We assert the backslash form reaches the handler and gets 400, and
    separately that the forward-slash form never reaches the handler
    (so the 404 is fine — we just need to confirm no escape).
    """
    # Form 1: backslash-encoded absolute path survives Starlette routing
    # and hits the handler, which must reject it with 400.
    response = await client.post(
        "/hooks/" + quote("\\etc\\passwd", safe=""),
        json={"hook_event_name": "Stop"},
    )
    _assert_rejected(response.status_code, response.json())

    # Form 2: forward-slash absolute path — Starlette splits and 404s.
    # This is still a secure outcome; no file is written outside base.
    response2 = await client.post(
        "/hooks/" + quote("/etc/passwd", safe=""),
        json={"hook_event_name": "Stop"},
    )
    assert response2.status_code in (400, 404), f"absolute /etc/passwd must be rejected, got {response2.status_code}"
    assert not (tmp_path / "etc" / "passwd").exists()


@pytest.mark.anyio
async def test_url_encoded_dotdot_returns_400(client: AsyncClient, tmp_path: Path) -> None:
    """URL-encoded ``..`` decodes to ``..`` and is rejected with 400.

    The ``%2f`` suffix variant (``%2e%2e%2f``) is decoded by Starlette
    into ``../`` and then re-split, so it never reaches the handler as
    a single segment (404).  We cover both shapes — the bare ``%2e%2e``
    which the handler sees as ``..`` and rejects with 400, and the
    slash-suffixed form which Starlette fails to route at all (also
    secure).
    """
    # Bare dot-dot reaches the handler and must be rejected with 400.
    response = await client.post(
        "/hooks/%2e%2e",
        json={"hook_event_name": "Stop"},
    )
    _assert_rejected(response.status_code, response.json())

    # Slash-suffixed form is split by Starlette (404) or rejected (400)
    # — either outcome prevents filesystem writes outside the base.
    response2 = await client.post(
        "/hooks/%2e%2e%2f",
        json={"hook_event_name": "Stop"},
    )
    # Any non-2xx is acceptable: handler rejection (400), route miss
    # (404), or trailing-slash redirect (307) all prevent the write.
    assert response2.status_code in (307, 400, 404), (
        f"URL-encoded traversal must be rejected, got {response2.status_code}"
    )
    completed_dir = tmp_path / ".sdd" / "runtime" / "completed"
    if completed_dir.exists():
        assert not any(completed_dir.iterdir())


@pytest.mark.anyio
async def test_null_byte_session_id_returns_400(client: AsyncClient, tmp_path: Path) -> None:
    """Null byte in session_id is rejected (cannot be used to truncate filenames)."""
    encoded = quote("foo\x00bar", safe="")
    response = await client.post(
        f"/hooks/{encoded}",
        json={"hook_event_name": "PostToolUse"},
    )
    _assert_rejected(response.status_code, response.json())


@pytest.mark.anyio
async def test_symlink_escape_session_id_returns_400(client: AsyncClient, tmp_path: Path) -> None:
    """If a symlink under the hooks dir points outside, the defence in
    depth containment check still rejects the request with 400."""
    # Pre-create the hooks base and plant a symlink with a syntactically
    # valid session id name pointing at an outside directory.  A real
    # attacker would need to influence the hooks dir, but defence in
    # depth must still hold even under that assumption.
    hooks_dir = tmp_path / ".sdd" / "runtime" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside_target"
    outside.mkdir()
    # The receiver builds ``hooks_dir / f"{session_id}.jsonl"``.  We
    # plant a symlink at exactly that name pointing outside the base.
    link = hooks_dir / "sess-symlink.jsonl"
    os.symlink(outside / "stolen.jsonl", link)

    response = await client.post(
        "/hooks/sess-symlink",
        json={"hook_event_name": "PostToolUse", "tool_name": "Bash"},
    )
    _assert_rejected(response.status_code, response.json())
    # Nothing written to the outside target.
    assert not (outside / "stolen.jsonl").exists()


@pytest.mark.anyio
async def test_rejection_does_not_write_completion_marker(client: AsyncClient, tmp_path: Path) -> None:
    """Regression guard: a rejected request must not forge Stop markers.

    The raw payload ``../../SHUTDOWN`` encodes to ``%2F`` slashes which
    Starlette decodes back to path separators before dispatch, so the
    request never matches the route (404).  The security property we
    care about is the negative: no marker file anywhere in ``tmp_path``.
    """
    completed_dir = tmp_path / ".sdd" / "runtime" / "completed"
    response = await client.post(
        "/hooks/" + quote("../../SHUTDOWN", safe=""),
        json={"hook_event_name": "Stop"},
    )
    # 307/400/404 are all acceptable — none leave the filesystem dirty.
    assert response.status_code in (307, 400, 404), f"traversal payload must be rejected, got {response.status_code}"
    if completed_dir.exists():
        assert not any(completed_dir.iterdir())
    assert not (tmp_path / "SHUTDOWN").exists()
    assert not (tmp_path.parent / "SHUTDOWN").exists()
