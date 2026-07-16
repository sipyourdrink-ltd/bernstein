#!/usr/bin/env python
"""Pull open GlitchTip issues and emit eval-case input records.

The GlitchTip server speaks the Sentry-compatible API. This scraper
lists open ``is:unresolved`` issues across every project in the org,
fetches the latest event with stacktrace metadata for each, and writes
one JSON record per unique issue under
``.sdd/reports/glitchtip_events/`` for the ``IncidentSynthesizer`` to
ingest on its next pass.

Pipeline
--------
1. Read ``GLITCHTIP_API_TOKEN`` / ``BERNSTEIN_GLITCHTIP_TOKEN``,
   ``GLITCHTIP_BASE_URL`` / ``BERNSTEIN_GLITCHTIP_BASE_URL``,
   ``GLITCHTIP_ORG_SLUG`` / ``BERNSTEIN_GLITCHTIP_ORG`` from env. There
   is **no** hardcoded host: the base URL must come from env (or a
   DSN-derived host) so the shipped package never reaches a specific
   server. When the token is missing the scraper exits 0 with a one-line
   notice; downstream integration tests skip rather than fail.
2. List unresolved issues via ``GET /api/0/organizations/<org>/issues/``
   with ``query=is:unresolved`` and follow ``Link: next`` pagination
   until exhausted.
3. For each issue, fetch ``GET /api/0/issues/<id>/events/latest/`` to
   retrieve the exception type, message, and deepest in-app stack
   frame. Network errors on individual events are tolerated: the
   record is still emitted with empty stacktrace fields.
4. Apply the wiring-probe allow-list filter so the two administrative
   smoke issues seeded during initial wiring do not become eval cases.
5. Deduplicate against existing JSON records in the output directory
   **and** existing eval-case YAMLs under
   ``src/bernstein/eval/cases/incidents/`` keyed on the
   ``glitchtip-issue:<issue_id>`` source-incident slug.
6. Emit one JSON file per unique issue using the
   :class:`GlitchTipIncident` schema in
   :mod:`bernstein.eval.incident_synthesizer`. The bernstein package is
   imported lazily so the script also runs from an air-gapped checkout.

Usage::

    GLITCHTIP_API_TOKEN=... GLITCHTIP_BASE_URL=https://errors.example.com \\
        python scripts/scrape_glitchtip_events.py \\
        --org bernstein \\
        --out .sdd/reports/glitchtip_events

A ``--dry-run`` flag prints the JSON records to stdout without
touching the filesystem.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import urllib.parse
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Try to import the dataclass and HTTP helper from the bernstein package
# so the scraper and the synthesizer never drift. Fall back to local
# definitions when the package is not on PYTHONPATH (air-gapped use).
try:
    from bernstein.eval.incident_synthesizer import GlitchTipIncident
except Exception:  # pragma: no cover - import fallback for air-gap
    GlitchTipIncident = None  # type: ignore[assignment,misc]

try:
    from bernstein.core.observability.glitchtip_insights import (
        ENV_GLITCHTIP_BASE_URL,
        ENV_GLITCHTIP_ORG,
        ENV_GLITCHTIP_TOKEN,
        GlitchTipHTTPError,
        _resolve_base_url,
    )
except Exception:  # pragma: no cover - import fallback for air-gap
    ENV_GLITCHTIP_TOKEN = "BERNSTEIN_GLITCHTIP_TOKEN"
    ENV_GLITCHTIP_BASE_URL = "BERNSTEIN_GLITCHTIP_BASE_URL"
    ENV_GLITCHTIP_ORG = "BERNSTEIN_GLITCHTIP_ORG"

    class GlitchTipHTTPError(RuntimeError):  # type: ignore[no-redef]
        """Raised when the underlying HTTP call cannot complete."""

    _resolve_base_url = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("scrape_glitchtip_events")

# Workflow-conventional env var aliases. The scraper accepts both the
# ``BERNSTEIN_GLITCHTIP_*`` names used by the runtime code and the
# ``GLITCHTIP_*`` names used by the GitHub Actions secret store; the
# latter are checked second so an explicit BERNSTEIN_* override wins.
ENV_TOKEN_ALIAS = "GLITCHTIP_API_TOKEN"
ENV_BASE_URL_ALIAS = "GLITCHTIP_BASE_URL"
ENV_ORG_ALIAS = "GLITCHTIP_ORG_SLUG"

DEFAULT_ORG_SLUG = "bernstein"
DEFAULT_TIMEOUT = 10.0
DEFAULT_PAGE_LIMIT = 100
MAX_PAGES = 20

# Administrative smoke issues seeded during initial wiring. Add new
# titles here when an operator-driven probe needs to be filtered out
# of the regression-case stream. Matching is case-insensitive and on
# a substring basis so trivial variants ("smoke from operator
# finalisation v2") also slip through.
DEFAULT_WIRING_PROBE_ALLOW_LIST: tuple[str, ...] = (
    "glitchtip insights wiring probe",
    "glitchtip smoke from operator finalisation",
)

# Fields lifted from a Sentry-protocol stacktrace frame. ``in_app`` is
# the canonical filter for "user code" vs library/runtime frames.
_IN_APP_KEY = "in_app"


def _read_env(env: dict[str, str] | None) -> tuple[str | None, str | None, str]:
    """Resolve token, base URL, and org slug from env.

    Precedence:
      1. ``BERNSTEIN_GLITCHTIP_*`` overrides win when set (these are the
         names used by the runtime code so an operator using both
         surfaces does not have to define the variable twice).
      2. ``GLITCHTIP_*`` aliases used by GitHub Actions secret store.
      3. For the base URL: ``BERNSTEIN_GLITCHTIP_BASE_URL`` env var,
         then a derived host from one of the DSN env vars
         (``BERNSTEIN_GLITCHTIP_DSN`` / ``BERNSTEIN_TELEMETRY_DSN`` /
         ``GLITCHTIP_DSN``) via :func:`_resolve_base_url`.

    Returns ``(token, base_url, org_slug)`` with each element ``None``
    when it cannot be resolved.
    """
    source = env if env is not None else os.environ.copy()
    token = source.get(ENV_GLITCHTIP_TOKEN) or source.get(ENV_TOKEN_ALIAS) or None
    base_url = source.get(ENV_GLITCHTIP_BASE_URL) or source.get(ENV_BASE_URL_ALIAS) or None
    if base_url:
        base_url = base_url.rstrip("/")
    elif _resolve_base_url is not None:
        # Fall back to DSN-derived host via the shared helper. We pass a
        # copy of the env so the helper's BERNSTEIN_GLITCHTIP_BASE_URL
        # lookup does not race the alias read above.
        base_url = _resolve_base_url(dict(source))
    org_slug = source.get(ENV_GLITCHTIP_ORG) or source.get(ENV_ORG_ALIAS) or DEFAULT_ORG_SLUG
    return token, base_url, org_slug


def _default_http_get(url: str, token: str, timeout: float) -> tuple[int, Any, dict[str, str]]:
    """Issue a GET and return ``(status, parsed_json, headers)``.

    Mirrors the auth header and soft-fail shape of
    :func:`bernstein.core.observability.glitchtip_insights._http_get`,
    but additionally returns response headers because the pagination
    ``Link`` header is required here and the bernstein helper drops it.
    """
    import httpx

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise GlitchTipHTTPError(str(exc)) from exc
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    # httpx.Headers behaves like a case-insensitive dict; we flatten to
    # str:str so the parser does not depend on the httpx type.
    flat_headers = {k.lower(): v for k, v in resp.headers.items()}
    return resp.status_code, payload, flat_headers


_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"\s*;\s*results="true"')


def _parse_link_header(value: str) -> str | None:
    """Return the ``next`` URL from a Sentry-protocol ``Link`` header.

    The Sentry / GlitchTip Link header carries cursor URLs in the form::

        <https://host/api/0/...?cursor=...>; rel="next"; results="true"; ...

    Only the next link with ``results="true"`` is followed; ``false``
    indicates the cursor is empty so we stop.
    """
    if not value:
        return None
    match = _LINK_NEXT_RE.search(value)
    return match.group(1) if match else None


def _same_host(candidate: str, base_url: str) -> bool:
    """Return True when ``candidate`` targets the configured host over HTTP(S).

    The pagination ``next`` URL is server-supplied and is followed with
    the Bearer token attached. Pinning it to the configured base-URL host
    stops a hostile or compromised server from redirecting the cursor at
    an attacker-controlled endpoint and exfiltrating the token (SSRF).
    """
    try:
        parsed = urllib.parse.urlparse(candidate)
        base = urllib.parse.urlparse(base_url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return bool(parsed.netloc) and parsed.netloc.lower() == base.netloc.lower()


def list_unresolved_issues(
    base_url: str,
    token: str,
    org_slug: str,
    *,
    http_get: Callable[..., tuple[int, Any, dict[str, str]]],
    timeout: float = DEFAULT_TIMEOUT,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    max_pages: int = MAX_PAGES,
) -> list[dict[str, Any]]:
    """Return every unresolved issue across all projects in the org.

    Pagination is by the Sentry-protocol ``Link: rel="next"`` header.
    We cap at :data:`MAX_PAGES` to keep one run bounded; an org with
    more than ``MAX_PAGES * page_limit`` open issues should be triaged
    by hand, not by a daily cron.
    """
    issues: list[dict[str, Any]] = []
    url: str | None = f"{base_url}/api/0/organizations/{org_slug}/issues/?query=is:unresolved&limit={page_limit}"
    pages = 0
    while url and pages < max_pages:
        pages += 1
        status, payload, headers = http_get(url, token, timeout)
        if status < 200 or status >= 300:
            raise GlitchTipHTTPError(f"HTTP {status} from {url}")
        if not isinstance(payload, list):
            raise GlitchTipHTTPError(f"non-list payload from {url}")
        for raw in payload:
            if isinstance(raw, dict):
                issues.append(raw)
        next_url = _parse_link_header(headers.get("link", ""))
        # Only follow the next cursor when it points back at the
        # configured host. A next link to any other host is dropped so the
        # Bearer token is never sent off-host (SSRF guard).
        if next_url and not _same_host(next_url, base_url):
            logger.warning("dropping cross-host pagination link; stopping pagination")
            next_url = None
        url = next_url
    return issues


def fetch_latest_event(
    base_url: str,
    token: str,
    issue_id: str,
    *,
    http_get: Callable[..., tuple[int, Any, dict[str, str]]],
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any] | None:
    """Return the latest event for an issue or ``None`` on lookup failure.

    Failures here are intentionally tolerated: the issue list payload
    alone carries enough metadata to emit a useful eval case, and the
    event lookup is a best-effort enrichment.
    """
    url = f"{base_url}/api/0/issues/{issue_id}/events/latest/"
    try:
        status, payload, _ = http_get(url, token, timeout)
    except GlitchTipHTTPError as exc:
        logger.debug("event lookup failed for issue %s: %s", issue_id, exc)
        return None
    if status < 200 or status >= 300:
        logger.debug("event lookup returned HTTP %s for issue %s", status, issue_id)
        return None
    return payload if isinstance(payload, dict) else None


def _extract_exception_from_event(event: dict[str, Any]) -> tuple[str, str, str, int]:
    """Pull ``(type, value, top_frame_path, top_frame_line)`` out of an event.

    The Sentry-protocol event payload has either::

        "exception": {"values": [{"type": "...", "value": "...", "stacktrace": {...}}]}

    or the older form::

        "entries": [{"type": "exception", "data": {"values": [...]}}]

    We support both and pick the *deepest* in-app frame (Sentry orders
    frames oldest to newest, so the last in-app frame is the call site
    most operators want to inspect first).
    """
    exception_values: list[Any] = []
    raw_exception: Any = event.get("exception")
    if isinstance(raw_exception, dict):
        values = raw_exception.get("values")
        if isinstance(values, list):
            exception_values = values
    if not exception_values:
        for entry in event.get("entries") or []:
            if isinstance(entry, dict) and entry.get("type") == "exception":
                data = entry.get("data") or {}
                if isinstance(data, dict):
                    values = data.get("values")
                    if isinstance(values, list):
                        exception_values = values
                        break
    if not exception_values:
        return "", "", "", 0

    head = exception_values[0]
    if not isinstance(head, dict):
        return "", "", "", 0

    exc_type = str(head.get("type") or "")
    exc_value = str(head.get("value") or "")

    stacktrace = head.get("stacktrace")
    if not isinstance(stacktrace, dict):
        return exc_type, exc_value, "", 0
    frames_raw = stacktrace.get("frames")
    if not isinstance(frames_raw, list):
        return exc_type, exc_value, "", 0

    # Frames are oldest-first; iterate in reverse to pick the deepest
    # in-app frame.
    for frame in reversed(frames_raw):
        if not isinstance(frame, dict):
            continue
        if frame.get(_IN_APP_KEY) is False:
            continue
        path = str(frame.get("filename") or frame.get("abs_path") or "")
        line_raw = frame.get("lineno") or 0
        try:
            line = int(line_raw) if not isinstance(line_raw, bool) else 0
        except (TypeError, ValueError):
            line = 0
        if path:
            return exc_type, exc_value, path, line
    return exc_type, exc_value, "", 0


def _extract_tag(event: dict[str, Any], key: str) -> str:
    """Return the value of a Sentry-protocol tag, or empty string."""
    for tag in event.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        if tag.get("key") == key:
            return str(tag.get("value") or "")
    direct = event.get(key)
    return str(direct) if isinstance(direct, str) else ""


def _is_wiring_probe(title: str, allow_list: tuple[str, ...]) -> bool:
    """Return True when ``title`` matches a wiring-probe filter substring.

    Matching is case-insensitive on a substring basis so trivial
    variants ("glitchtip smoke from operator finalisation v2") are
    still filtered.
    """
    needle = title.lower()
    return any(probe.lower() in needle for probe in allow_list)


def build_record(
    issue: dict[str, Any],
    *,
    base_url: str,
    token: str,
    http_get: Callable[..., tuple[int, Any, dict[str, str]]],
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any] | None:
    """Convert one raw issue + its latest event into a JSON record.

    Returns ``None`` when the issue lacks an id (defensive: a malformed
    API response with no id slot would otherwise emit an empty file).
    """
    issue_id = str(issue.get("id") or "").strip()
    if not issue_id:
        return None

    title = str(issue.get("title") or "")
    project = issue.get("project") or {}
    project_slug = ""
    if isinstance(project, dict):
        project_slug = str(project.get("slug") or "")

    event = fetch_latest_event(base_url, token, issue_id, http_get=http_get, timeout=timeout)
    exc_type, exc_value, top_path, top_line = "", "", "", 0
    environment = ""
    release = ""
    if event is not None:
        exc_type, exc_value, top_path, top_line = _extract_exception_from_event(event)
        environment = _extract_tag(event, "environment")
        release = _extract_tag(event, "release")

    # Trim long values so neither the JSON nor the YAML stage blows out
    # the synthesizer's prompt budget.
    exc_value = exc_value.strip()
    if len(exc_value) > 600:
        exc_value = exc_value[:600] + "..."

    count_raw: Any = issue.get("count") or 0
    try:
        event_count = int(count_raw) if not isinstance(count_raw, bool) else 0
    except (TypeError, ValueError):
        event_count = 0

    record_obj = {
        "glitchtip_issue_id": issue_id,
        "project_slug": project_slug,
        "exception_type": exc_type,
        "exception_value": exc_value,
        "top_frame_path": top_path,
        "top_frame_line": top_line,
        "first_seen": str(issue.get("firstSeen") or ""),
        "last_seen": str(issue.get("lastSeen") or ""),
        "event_count": event_count,
        "environment": environment,
        "release": release,
        "title": title,
    }

    if GlitchTipIncident is not None:
        # Round-trip through the dataclass so the scraper and synthesizer
        # never drift on field names. ``asdict`` returns a plain dict;
        # we drop the dataclass-only ``title`` field downstream because
        # the synthesizer keeps it in the record for the wiring-probe
        # filter only.
        incident = GlitchTipIncident(
            issue_id=record_obj["glitchtip_issue_id"],
            project_slug=record_obj["project_slug"],
            exception_type=record_obj["exception_type"],
            exception_value=record_obj["exception_value"],
            top_frame_path=record_obj["top_frame_path"],
            top_frame_line=record_obj["top_frame_line"],
            first_seen=record_obj["first_seen"],
            last_seen=record_obj["last_seen"],
            event_count=record_obj["event_count"],
            environment=record_obj["environment"],
            release=record_obj["release"],
            title=record_obj["title"],
        )
        # Use ``glitchtip_issue_id`` rather than ``issue_id`` in the JSON
        # so the field name matches the documented schema even when the
        # dataclass attribute differs.
        as_dict = asdict(incident)
        as_dict["glitchtip_issue_id"] = as_dict.pop("issue_id")
        return as_dict
    return record_obj


def existing_keys(out_dir: Path, cases_dir: Path | None) -> set[str]:
    """Return ``glitchtip-issue:<id>`` keys already on disk.

    Two sources are merged:

    * The scraper's own ``out_dir`` (so re-running the script is a
      pure no-op).
    * Emitted YAML cases under ``cases_dir`` (so a previous synth pass
      that has already promoted a record into a YAML case is also
      treated as covered).
    """
    keys: set[str] = set()
    if out_dir.is_dir():
        for path in out_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            issue_id = raw.get("glitchtip_issue_id")
            if isinstance(issue_id, str) and issue_id:
                keys.add(f"glitchtip-issue:{issue_id}")
    if cases_dir and cases_dir.is_dir():
        for path in cases_dir.glob("inc-*.yaml"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.startswith("source_incident:"):
                        raw_val = line.split(":", 1)[1].strip()
                        if len(raw_val) >= 2 and raw_val[0] == '"' and raw_val[-1] == '"':
                            raw_val = raw_val[1:-1]
                        if raw_val.startswith("glitchtip-issue:"):
                            keys.add(raw_val)
                        break
            except OSError:
                continue
    return keys


def emit_record(record: dict[str, Any], out_dir: Path) -> Path:
    """Write a record under ``out_dir`` and return the path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    issue_id = str(record["glitchtip_issue_id"])
    # Sanitise the issue id for the filename: GlitchTip ids are numeric
    # in the wild but we accept arbitrary strings defensively.
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", issue_id)
    path = out_dir / f"issue-{safe}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return path


def run(
    *,
    out_dir: Path,
    cases_dir: Path | None,
    dry_run: bool,
    wiring_probe_allow_list: tuple[str, ...] = DEFAULT_WIRING_PROBE_ALLOW_LIST,
    env: dict[str, str] | None = None,
    http_get: Callable[..., tuple[int, Any, dict[str, str]]] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> int:
    """Drive one scrape pass. Returns the number of records emitted.

    Exits 0 (and emits zero records) when the API token is not
    configured. This is the "graceful exit when unconfigured" branch
    referenced by the integration tests.
    """
    token, base_url, org_slug = _read_env(env)
    if not token:
        logger.warning(
            "%s / %s not set; scraper exits 0 with no output",
            ENV_GLITCHTIP_TOKEN,
            ENV_TOKEN_ALIAS,
        )
        return 0
    if not base_url:
        logger.warning(
            "%s / %s not set and no DSN host available; scraper exits 0",
            ENV_GLITCHTIP_BASE_URL,
            ENV_BASE_URL_ALIAS,
        )
        return 0

    getter = http_get or _default_http_get
    try:
        issues = list_unresolved_issues(
            base_url,
            token,
            org_slug,
            http_get=getter,
            timeout=timeout,
        )
    except GlitchTipHTTPError as exc:
        logger.warning("issue list unreachable; scraper exits 0: %s", exc)
        return 0

    seen = existing_keys(out_dir, cases_dir)
    emitted = 0
    for issue in issues:
        title = str(issue.get("title") or "")
        if _is_wiring_probe(title, wiring_probe_allow_list):
            logger.info("skipping wiring-probe issue: %s", title)
            continue
        record = build_record(
            issue,
            base_url=base_url,
            token=token,
            http_get=getter,
            timeout=timeout,
        )
        if record is None:
            continue
        key = f"glitchtip-issue:{record['glitchtip_issue_id']}"
        if key in seen:
            continue
        if dry_run:
            print(json.dumps(record, sort_keys=True))
        else:
            path = emit_record(record, out_dir)
            logger.info("emitted %s", path)
        seen.add(key)
        emitted += 1
    return emitted


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(".sdd/reports/glitchtip_events"),
        help="Output directory for emitted JSON records.",
    )
    parser.add_argument(
        "--cases-dir",
        type=Path,
        default=Path("src/bernstein/eval/cases/incidents"),
        help="Existing YAML cases dir (consulted for dedup only).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print records to stdout instead of writing files.",
    )
    parser.add_argument(
        "--wiring-probe",
        action="append",
        default=None,
        help=(
            "Title substring to filter out as an administrative wiring probe. "
            "Repeatable. Defaults to the built-in allow-list."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    allow_list: tuple[str, ...] = tuple(args.wiring_probe) if args.wiring_probe else DEFAULT_WIRING_PROBE_ALLOW_LIST
    count = run(
        out_dir=args.out,
        cases_dir=args.cases_dir,
        dry_run=args.dry_run,
        wiring_probe_allow_list=allow_list,
    )
    logger.info("scraper finished; %d new record(s)", count)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
