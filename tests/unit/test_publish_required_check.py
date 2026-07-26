"""Unit tests for the required-context publisher.

The publisher exists so a required status context is never a side effect
of a job's fate. Branch protection folds every check-run of a required
name into its verdict and a later success does not clear an earlier
non-success, so the two properties pinned here are load-bearing:

    * one instance per head SHA - an existing instance is patched in
      place, never shadowed by a second POST, so a commit can never
      accumulate two contradictory verdicts, and
    * a closed conclusion set - only `success` and `failure` are writable,
      because `cancelled` is unrecoverable and `skipped`/`neutral` count as
      passing.

No network: the transport is injected.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from publish_required_check import (
    ALLOWED_CONCLUSIONS,
    conclusion_for_exit_code,
    existing_instances,
    main,
    publish,
)

REPO = "sipyourdrink-ltd/bernstein"
SHA = "8059528db8ac407b6f8232e425885f80d7560ffd"
NAME = "review-bot-ack"


class FakeTransport:
    """Records calls and replays a canned check-run listing."""

    def __init__(self, check_runs: list[dict[str, Any]] | None = None, new_id: int = 999) -> None:
        self._check_runs = check_runs or []
        self._new_id = new_id
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        # Check-run ids whose PATCH the server rejects, as the Actions
        # service may for instances it created itself.
        self.fail_patch_ids: set[int] = set()

    def __call__(self, method: str, url: str, body: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, url, body))
        if method == "GET":
            return {"check_runs": self._check_runs}
        if method == "POST":
            return {"id": self._new_id}
        run_id = int(url.rsplit("/", 1)[-1])
        if run_id in self.fail_patch_ids:
            raise RuntimeError(f"GitHub API PATCH {url} failed: 403 forbidden")
        return {"id": run_id}

    def methods(self) -> list[str]:
        return [m for m, _, _ in self.calls]


def _run(run_id: int, conclusion: str, slug: str = "github-actions") -> dict[str, Any]:
    return {"id": run_id, "conclusion": conclusion, "status": "completed", "app": {"slug": slug}}


# --------------------------------------------------------------------------
# Upsert: exactly one instance per head SHA
# --------------------------------------------------------------------------


def test_creates_a_single_instance_when_none_exists() -> None:
    transport = FakeTransport(check_runs=[], new_id=4242)
    ids = publish(transport, REPO, SHA, NAME, "success", "t", "s")

    assert ids == [4242]
    assert transport.methods() == ["GET", "POST"], "a fresh SHA takes exactly one POST"
    _, url, body = transport.calls[-1]
    assert url.endswith("/check-runs")
    assert body is not None
    assert body["name"] == NAME
    assert body["head_sha"] == SHA
    assert body["status"] == "completed"
    assert body["conclusion"] == "success"


def test_patches_the_existing_instance_instead_of_adding_a_second() -> None:
    transport = FakeTransport(check_runs=[_run(11, "failure")])
    ids = publish(transport, REPO, SHA, NAME, "success", "t", "s")

    assert ids == [11]
    assert "POST" not in transport.methods(), (
        "a second POST would leave two contradictory instances of a required context on one SHA"
    )
    method, url, body = transport.calls[-1]
    assert method == "PATCH"
    assert url.endswith("/check-runs/11")
    assert body is not None
    assert body["conclusion"] == "success"
    assert body["status"] == "completed"


def test_heals_every_stale_instance_on_a_poisoned_sha() -> None:
    """A SHA poisoned by the old job-name mechanism carries several
    unrecoverable instances. All of them are rewritten to the current
    verdict, so the fold over the required name is unambiguous.
    """
    transport = FakeTransport(
        check_runs=[_run(1, "cancelled"), _run(2, "skipped"), _run(3, "cancelled"), _run(4, "success")]
    )
    ids = publish(transport, REPO, SHA, NAME, "success", "t", "s")

    assert ids == [1, 2, 3, 4]
    assert transport.methods() == ["GET", "PATCH", "PATCH", "PATCH", "PATCH"]
    for _, _, body in transport.calls[1:]:
        assert body is not None
        assert body["conclusion"] == "success"


def test_falls_back_to_posting_when_no_stale_instance_is_writable() -> None:
    """Instances left by the old job-name mechanism belong to the Actions
    service. If it refuses the update, the head SHA must still get a fresh
    instance carrying the current verdict - refusing to publish would add
    an absent context to an already-blocked commit.
    """
    transport = FakeTransport(check_runs=[_run(1, "cancelled"), _run(2, "cancelled")], new_id=77)
    transport.fail_patch_ids = {1, 2}
    ids = publish(transport, REPO, SHA, NAME, "success", "t", "s")

    assert ids == [77]
    assert transport.methods() == ["GET", "PATCH", "PATCH", "POST"]


def test_a_single_successful_patch_suppresses_the_post() -> None:
    """A partially writable SHA must not gain an extra instance."""
    transport = FakeTransport(check_runs=[_run(1, "cancelled"), _run(2, "success")], new_id=77)
    transport.fail_patch_ids = {1}
    ids = publish(transport, REPO, SHA, NAME, "failure", "t", "s")

    assert ids == [2]
    assert "POST" not in transport.methods()


def test_ignores_instances_owned_by_another_app() -> None:
    """Branch protection pins the context to an app id. Instances owned by
    a different app cannot be patched with this token and are not ours to
    speak for.
    """
    transport = FakeTransport(check_runs=[_run(7, "success", slug="some-other-app")], new_id=8)
    ids = publish(transport, REPO, SHA, NAME, "failure", "t", "s")

    assert ids == [8]
    assert transport.methods() == ["GET", "POST"]


def test_existing_instances_filters_and_survives_a_malformed_listing() -> None:
    transport = FakeTransport(check_runs=[_run(1, "success"), "junk", {"no_id": True}])  # type: ignore[list-item]
    assert existing_instances(transport, REPO, SHA, NAME) == [1]

    empty = FakeTransport(check_runs=[])
    assert existing_instances(empty, REPO, SHA, NAME) == []


def test_query_scopes_to_the_context_name_and_sha() -> None:
    transport = FakeTransport(check_runs=[])
    publish(transport, REPO, SHA, NAME, "success", "t", "s")
    _, url, _ = transport.calls[0]
    assert f"/commits/{SHA}/check-runs" in url
    assert f"check_name={NAME}" in url


# --------------------------------------------------------------------------
# Fail closed: the conclusion set is closed
# --------------------------------------------------------------------------


def test_allowed_conclusions_excludes_the_unrecoverable_and_the_free_pass() -> None:
    assert set(ALLOWED_CONCLUSIONS) == {"success", "failure"}
    for banned in ("cancelled", "skipped", "neutral", "timed_out", "action_required", ""):
        assert banned not in ALLOWED_CONCLUSIONS


@pytest.mark.parametrize("bad", ["cancelled", "skipped", "neutral", "", "SUCCESS", "pass"])
def test_refuses_to_publish_a_conclusion_outside_the_closed_set(bad: str) -> None:
    transport = FakeTransport(check_runs=[])
    with pytest.raises(ValueError, match="refusing to publish"):
        publish(transport, REPO, SHA, NAME, bad, "t", "s")
    assert transport.calls == [], "a refused verdict must not touch the API at all"


@pytest.mark.parametrize(
    "code, expected",
    [(0, "success"), (1, "failure"), (2, "failure"), (127, "failure"), (-1, "failure")],
)
def test_exit_code_mapping_is_fail_closed(code: int, expected: str) -> None:
    """The gate exits 1 on an open finding and 2 on an internal error.
    Only a clean exit may publish success.
    """
    assert conclusion_for_exit_code(code) == expected


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_refuses_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    rc = main(["--repo", REPO, "--sha", SHA, "--name", NAME, "--conclusion", "success"])
    assert rc == 1


def test_cli_refuses_a_bad_conclusion_before_calling_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "x")

    def _boom(_token: str) -> Any:
        def _t(method: str, url: str, body: dict[str, Any] | None = None) -> Any:
            raise AssertionError("transport must not be used for a refused verdict")

        return _t

    monkeypatch.setattr("publish_required_check.urllib_transport", _boom)
    rc = main(["--repo", REPO, "--sha", SHA, "--name", NAME, "--conclusion", "cancelled"])
    assert rc == 1


def test_cli_reports_api_failure_as_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """An API failure leaves the context absent. Absent reads as blocked,
    so the job must fail loudly rather than exit clean.
    """
    monkeypatch.setenv("GH_TOKEN", "x")

    def _transport_factory(_token: str) -> Any:
        def _t(method: str, url: str, body: dict[str, Any] | None = None) -> Any:
            raise RuntimeError("GitHub API GET ... failed: 503")

        return _t

    monkeypatch.setattr("publish_required_check.urllib_transport", _transport_factory)
    rc = main(["--repo", REPO, "--sha", SHA, "--name", NAME, "--conclusion", "success"])
    assert rc == 2


def test_cli_publishes_and_exits_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "x")
    transport = FakeTransport(check_runs=[], new_id=5)
    monkeypatch.setattr("publish_required_check.urllib_transport", lambda _token: transport)
    rc = main(["--repo", REPO, "--sha", SHA, "--name", NAME, "--conclusion", "failure", "--summary", "why it failed"])
    assert rc == 0
    assert transport.methods() == ["GET", "POST"]
    _, _, body = transport.calls[-1]
    assert body is not None
    assert body["conclusion"] == "failure"
    assert body["output"]["summary"] == "why it failed"
