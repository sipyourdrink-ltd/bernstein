"""GitLab Issues tracker adapter.

Implements the ``AbstractTrackerAdapter`` contract on top of the public
GitLab REST API. Follows the same pattern as the GitHub Projects v2
adapter (see ``github_projects_adapter.py``).

Auth:
- Personal Access Token via the ``GITLAB_TOKEN`` env var by default; a
  custom env var name can be set on the config.

Instance:
- ``GITLAB_URL`` env var overrides the default ``https://gitlab.com``
  base URL for self-hosted instances.

Capabilities:
- ``pull_open_tickets``: paginate the project's open issues, optionally
  filtered by labels and assignee, and emit normalised ``Ticket`` objects.
- ``add_comment``: post a note on an issue.
- ``transition``: encode workflow state as labels. Atomically swaps an
  old status label for a new one via ``add_labels`` and ``remove_labels``
  on a single ``PUT /projects/:id/issues/:iid`` call.

Rate-limit handling:
- Token-bucket per-instance (cooperative, advisory).
- ``RateLimited`` raised with ``retry_after`` derived from the
  ``Retry-After`` or ``RateLimit-Reset`` headers.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import Iterator

try:  # pragma: no cover - optional dep already present in project deps
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from bernstein.core.trackers.contract import (
    AbstractTrackerAdapter,
    CommentResult,
    RateLimited,
    RoutingHint,
    Ticket,
    TrackerError,
    TrackerUnavailable,
    TransitionResult,
)
from bernstein.core.trackers.synthetic import probe_body, probe_title

logger = logging.getLogger(__name__)

DEFAULT_GITLAB_URL = "https://gitlab.com"
DEFAULT_PAGE_SIZE = 50
GITLAB_API_PATH = "/api/v4"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitLabConfig:
    """Configuration for a GitLab Issues adapter instance.

    Attributes:
        project_id_or_path: Numeric project id or URL-path (e.g.
            ``my-group/my-project``). Both forms work; the adapter
            URL-encodes path-style ids.
        instance_url: Base URL of the GitLab instance. Defaults to the
            value of the ``GITLAB_URL`` environment variable, or
            ``https://gitlab.com`` when that env var is unset.
        token_env: Environment variable name holding a Personal Access
            Token. Defaults to ``GITLAB_TOKEN``.
        label_filter_pull: Optional list of labels the adapter requires
            when listing open issues. Acts as an AND-filter.
        assignee_filter_pull: Optional assignee username filter applied
            when listing open issues.
        state_label_map: Mapping from canonical status names to GitLab
            labels (e.g. ``{"claim": "state:claimed", "ready":
            "state:ready", "failed": "state:failed"}``). The adapter
            assumes all label values share a common prefix so it can
            strip the previous state-label cleanly on ``transition``.
        cli_choice_label_prefix: Optional prefix that identifies a CLI
            choice label (e.g. ``cli:``). The first matching label on a
            pulled issue is surfaced via ``routing_hint.cli``.
        page_size: REST per-page size when listing.
        rate_limit_min_interval: Minimum seconds between API calls per
            adapter instance (cheap client-side throttle).
    """

    project_id_or_path: str
    instance_url: str | None = None
    token_env: str = "GITLAB_TOKEN"
    label_filter_pull: tuple[str, ...] = ()
    assignee_filter_pull: str | None = None
    state_label_map: dict[str, str] = field(default_factory=dict)
    cli_choice_label_prefix: str | None = None
    page_size: int = DEFAULT_PAGE_SIZE
    rate_limit_min_interval: float = 0.0


# ---------------------------------------------------------------------------
# Token / URL resolution
# ---------------------------------------------------------------------------


def _resolve_token(config: GitLabConfig) -> str:
    """Resolve the auth token for a REST call.

    Reads ``config.token_env`` from the environment (default
    ``GITLAB_TOKEN``).

    Raises:
        TrackerUnavailable: if the env var is unset or empty.
    """
    env_var = config.token_env or "GITLAB_TOKEN"
    token = os.environ.get(env_var, "")
    if not token:
        msg = (
            f"GitLab adapter: no token available (env var '{env_var}' is empty); "
            "set a personal access token to authenticate."
        )
        raise TrackerUnavailable(msg)
    return token


def _resolve_base_url(config: GitLabConfig) -> str:
    """Resolve the GitLab instance base URL.

    Precedence:
        1. ``config.instance_url`` when set.
        2. ``GITLAB_URL`` environment variable.
        3. ``https://gitlab.com``.
    """
    base = config.instance_url or os.environ.get("GITLAB_URL") or DEFAULT_GITLAB_URL
    return base.rstrip("/")


def _project_segment(config: GitLabConfig) -> str:
    """Return the URL-encoded project identifier for use in API paths."""
    raw = config.project_id_or_path
    if raw.isdigit():
        return raw
    return quote(raw, safe="")


# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------


class _TokenBucket:
    """Minimal cooperative token-bucket throttle.

    Identical contract to the GitHub adapter's bucket. ``min_interval ==
    0`` disables the throttle. Server-side limits and ``Retry-After``
    headers remain the hard enforcement boundary.
    """

    def __init__(self, min_interval: float) -> None:
        self._min_interval = max(0.0, min_interval)
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def acquire(self, *, now: float | None = None, sleep: Any = time.sleep) -> None:
        if self._min_interval <= 0.0:
            return
        now = time.monotonic() if now is None else now
        with self._lock:
            wait = self._next_allowed - now
            if wait > 0:
                sleep(wait)
                now = now + wait
            self._next_allowed = now + self._min_interval


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class GitLabAdapter(AbstractTrackerAdapter):
    """GitLab Issues adapter.

    The adapter is intentionally thin: every call is a single REST
    round-trip. State is encoded as labels because GitLab Issues lack a
    real workflow status field.

    Parameters:
        config: Adapter configuration.
        http_client: Optional pre-built ``httpx.Client`` (tests pass a
            mocked transport via ``respx``).
        token_provider: Optional callable returning the auth token. The
            default reads from ``_resolve_token(config)`` on each call.
    """

    name = "gitlab"

    def __init__(
        self,
        config: GitLabConfig,
        *,
        http_client: Any | None = None,
        token_provider: Any | None = None,
    ) -> None:
        if httpx is None:  # pragma: no cover - dep is declared in pyproject
            msg = "httpx is required for GitLabAdapter"
            raise RuntimeError(msg)
        self._config = config
        self._client: Any = http_client or httpx.Client(timeout=30.0)
        self._owns_client = http_client is None
        self._token_provider = token_provider or (lambda: _resolve_token(config))
        self._base_url = _resolve_base_url(config)
        self._project_segment = _project_segment(config)
        self._bucket = _TokenBucket(config.rate_limit_min_interval)

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Release the underlying HTTP client if we own it."""
        if self._owns_client:
            try:
                self._client.close()
            except Exception:  # pragma: no cover - best effort
                logger.debug("ignoring error while closing httpx client", exc_info=True)

    def __enter__(self) -> GitLabAdapter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- public API ---------------------------------------------------------

    def pull_open_tickets(
        self,
        filter: dict[str, Any] | None = None,
    ) -> Iterator[Ticket]:
        """Yield open issues for the configured project as ``Ticket`` objects.

        ``filter`` keys:
            - ``labels``: iterable of label names; ANDed with the config
              filter and with the GitLab server.
            - ``assignee``: assignee username; overrides
              ``config.assignee_filter_pull`` for this call.
        """
        filter_dict = filter or {}
        labels: list[str] = list(self._config.label_filter_pull)
        extra_labels = filter_dict.get("labels")
        if extra_labels:
            labels.extend(str(lab) for lab in extra_labels)
        assignee = filter_dict.get("assignee", self._config.assignee_filter_pull)

        per_page = max(1, min(100, self._config.page_size))
        page = 1
        while True:
            params: dict[str, Any] = {
                "state": "opened",
                "per_page": per_page,
                "page": page,
            }
            if labels:
                params["labels"] = ",".join(labels)
            if assignee:
                params["assignee_username"] = assignee

            data = self._request(
                "GET",
                f"/projects/{self._project_segment}/issues",
                params=params,
            )
            if not isinstance(data, list):
                return
            for raw in data:
                ticket = self._issue_to_ticket(raw)
                if ticket is not None:
                    yield ticket
            if len(data) < per_page:
                return
            page += 1

    def add_comment(
        self,
        ticket_id: str,
        body: str,
        *,
        idempotency_key: str | None = None,
    ) -> CommentResult:
        """Post a note on the issue identified by ``ticket_id``.

        ``ticket_id`` is the issue ``iid`` (project-scoped internal id),
        which is what the adapter emits from ``pull_open_tickets`` as
        ``Ticket.id``.
        """
        path = f"/projects/{self._project_segment}/issues/{ticket_id}/notes"
        payload = {"body": body}
        headers = self._idempotency_headers(idempotency_key)
        result = self._request("POST", path, json=payload, headers=headers)
        comment_id = ""
        if isinstance(result, dict):
            raw_id = result.get("id")
            if raw_id is not None:
                comment_id = str(raw_id)
        return CommentResult(comment_id=comment_id, ticket_id=ticket_id)

    def transition(
        self,
        ticket_id: str,
        status_id: str,
        *,
        idempotency_key: str | None = None,
        etag: str | None = None,
    ) -> TransitionResult:
        """Atomically swap the state label on issue ``ticket_id``.

        ``status_id`` may be either:
            - A canonical key in ``config.state_label_map`` (resolved
              to the mapped label name).
            - A bare GitLab label name (used as-is when no mapping is
              configured).

        The previous state-label (if any) is removed in the same call by
        listing every other label in the config map under
        ``remove_labels``.
        """
        new_label = self._config.state_label_map.get(status_id, status_id)
        remove_labels = [
            label
            for key, label in self._config.state_label_map.items()
            if key != status_id and label and label != new_label
        ]
        path = f"/projects/{self._project_segment}/issues/{ticket_id}"
        payload: dict[str, Any] = {"add_labels": new_label}
        if remove_labels:
            payload["remove_labels"] = ",".join(remove_labels)
        headers = self._idempotency_headers(idempotency_key)
        if etag:
            headers["If-Match"] = etag
        self._request("PUT", path, json=payload, headers=headers)
        return TransitionResult(
            ticket_id=ticket_id,
            new_status=status_id,
            etag=None,
        )

    # -- synthetic-transaction probe ----------------------------------------
    #
    # GitLab is the reference probe implementation: a free-tier or
    # self-hosted project is the lowest-friction throwaway target of the
    # built-in adapters, and issue create/delete are plain REST calls on
    # the same ``_request`` path the hot-path methods already use, so the
    # probe exercises the adapter's real transport, auth and error
    # mapping rather than a side channel.

    def create_probe_ticket(self, marker: str) -> str:
        """Create a throwaway issue titled with ``marker``; return its iid."""
        payload = {"title": probe_title(marker), "description": probe_body(marker)}
        data = _as_row(self._request("POST", f"/projects/{self._project_segment}/issues", json=payload))
        iid = data.get("iid")
        if iid is None:
            msg = f"GitLab did not return an iid for the synthetic issue {marker!r}"
            raise TrackerUnavailable(msg)
        return str(iid)

    def find_probe_tickets(self, marker: str) -> tuple[str, ...]:
        """Return the iids of every issue whose title carries ``marker``.

        Searches every state, not just ``opened``: a probe run transitions
        its entity before deleting it, and a tracker that closes on
        transition must not hide the leftover from the next run's sweep.
        """
        rows = _as_rows(
            self._request(
                "GET",
                f"/projects/{self._project_segment}/issues",
                params={"search": marker, "in": "title", "state": "all", "per_page": 100},
            )
        )
        # GitLab's ``search`` is fuzzy; re-check the marker client-side so a
        # near-miss never becomes a delete target.
        return tuple(
            str(row["iid"]) for row in rows if row.get("iid") is not None and marker in str(row.get("title") or "")
        )

    def delete_probe_ticket(self, ticket_id: str, marker: str) -> None:
        """Delete ``ticket_id``, refusing any issue whose title lacks ``marker``."""
        data = _as_row(self._request("GET", f"/projects/{self._project_segment}/issues/{ticket_id}"))
        title = str(data.get("title") or "")
        if marker not in title:
            msg = f"GitLab issue {ticket_id} does not carry the synthetic marker {marker!r}; refusing to delete it."
            raise TrackerError(msg)
        self._request("DELETE", f"/projects/{self._project_segment}/issues/{ticket_id}")

    # -- internals ----------------------------------------------------------

    def _idempotency_headers(self, key: str | None) -> dict[str, str]:
        if not key:
            return {}
        return {"Idempotency-Key": key}

    def _issue_to_ticket(self, raw: dict[str, Any]) -> Ticket | None:
        iid = raw.get("iid")
        if iid is None:
            return None
        labels_raw = raw.get("labels") or []
        labels = tuple(str(lab) for lab in labels_raw if lab)
        status = self._status_from_labels(labels)
        cli_choice = self._cli_from_labels(labels)
        return Ticket(
            id=str(iid),
            external_url=str(raw.get("web_url") or ""),
            title=str(raw.get("title") or ""),
            body=str(raw.get("description") or ""),
            status=status,
            labels=labels,
            etag=None,
            routing_hint=RoutingHint(cli=cli_choice),
            raw={
                "id": raw.get("id"),
                "project_id": raw.get("project_id"),
                "state": raw.get("state"),
                "assignee": (raw.get("assignee") or {}).get("username")
                if isinstance(raw.get("assignee"), dict)
                else None,
            },
        )

    def _status_from_labels(self, labels: tuple[str, ...]) -> str:
        """Infer the canonical status from labels when a map is configured."""
        if not self._config.state_label_map:
            return ""
        reverse = {label: key for key, label in self._config.state_label_map.items() if label}
        for label in labels:
            mapped = reverse.get(label)
            if mapped:
                return mapped
        return ""

    def _cli_from_labels(self, labels: tuple[str, ...]) -> str | None:
        prefix = self._config.cli_choice_label_prefix
        if not prefix:
            return None
        for label in labels:
            if label.startswith(prefix):
                return label[len(prefix) :] or None
        return None

    # -- HTTP --------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        self._bucket.acquire()
        token = self._token_provider()
        merged_headers: dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if headers:
            merged_headers.update(headers)
        url = f"{self._base_url}{GITLAB_API_PATH}{path}"
        try:
            response = self._client.request(
                method,
                url,
                params=params,
                json=json,
                headers=merged_headers,
            )
        except Exception as exc:  # pragma: no cover - network errors
            msg = f"GitLab transport error: {exc}"
            raise TrackerUnavailable(msg) from exc

        status_code = getattr(response, "status_code", 0)
        if status_code == 429:
            retry_after = _parse_retry_after(response)
            msg = f"GitLab rate-limited (status={status_code})"
            raise RateLimited(msg, retry_after=retry_after)
        if status_code == 403 and _looks_like_rate_limit(response):
            # GitLab occasionally signals rate limiting via HTTP 403 with
            # ``RateLimit-*`` / ``Retry-After`` headers. Treat those as
            # transient rate limits; plain 403s remain permission errors.
            retry_after = _parse_retry_after(response)
            msg = f"GitLab rate-limited (status={status_code})"
            raise RateLimited(msg, retry_after=retry_after)
        if status_code >= 500:
            msg = f"GitLab server error (status={status_code})"
            raise TrackerUnavailable(msg)
        if status_code >= 400:
            body = _safe_text(response)
            msg = f"GitLab HTTP {status_code}: {body[:200]}"
            raise TrackerUnavailable(msg)

        if status_code == 204 or not getattr(response, "content", b""):
            return None
        try:
            return response.json()
        except Exception as exc:  # pragma: no cover - malformed JSON
            msg = f"GitLab returned non-JSON: {exc}"
            raise TrackerUnavailable(msg) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_row(payload: Any) -> dict[str, Any]:
    """Normalise a decoded JSON payload into an object row (empty when it is not one)."""
    if isinstance(payload, dict):
        return cast("dict[str, Any]", payload)
    return {}


def _as_rows(payload: Any) -> list[dict[str, Any]]:
    """Normalise a decoded JSON payload into a list of object rows."""
    if not isinstance(payload, list):
        return []
    return [cast("dict[str, Any]", item) for item in cast("list[Any]", payload) if isinstance(item, dict)]


def _looks_like_rate_limit(response: Any) -> bool:
    """Return True if a 403 response carries GitLab rate-limit signals.

    Standard 403 (Forbidden) is an auth or permission failure; the
    operator should not auto-retry. GitLab does, however, occasionally
    return 403 specifically for rate limiting and surfaces it via
    ``Retry-After`` or ``RateLimit-*`` headers. Only those count.
    """
    headers = getattr(response, "headers", {}) or {}
    if not hasattr(headers, "get"):
        return False
    if headers.get("Retry-After"):
        return True
    return any(headers.get(key) is not None for key in ("RateLimit-Reset", "RateLimit-Remaining", "RateLimit-Limit"))


def _parse_retry_after(response: Any) -> float | None:
    """Best-effort ``Retry-After`` / ``RateLimit-Reset`` parser."""
    headers = getattr(response, "headers", {}) or {}
    retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
    if retry_after:
        try:
            return float(retry_after)
        except (TypeError, ValueError):
            return None
    reset = headers.get("RateLimit-Reset") if hasattr(headers, "get") else None
    if reset:
        try:
            reset_epoch = float(reset)
            return max(0.0, reset_epoch - time.time())
        except (TypeError, ValueError):
            return None
    return None


def _safe_text(response: Any) -> str:
    try:
        return str(getattr(response, "text", ""))
    except Exception:  # pragma: no cover
        return ""
