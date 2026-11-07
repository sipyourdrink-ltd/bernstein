"""On-disk layout of one deep-research run.

A deep-research agent's unit of work is a report and the sources it read, not
a commit. The runner writes three files into the run directory:

* ``report.md`` - the report, as the agent produced it;
* ``sources.json`` - the distinct source URLs the agent opened, sorted;
* ``run.json`` - the run record: agent, timing, terminal state, and the
  sha256 of the other two files' bytes.

The record binds the report to the source list, so an edit to either after
the run is detected by recomputing the digests (:func:`verify_run`).

Standard library only, and no ``bernstein`` import: the runners import this
module by path from inside the agent's own interpreter, where bernstein is not
installed.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA = "deep-research-run/v1"
REPORT_FILE = "report.md"
SOURCES_FILE = "sources.json"
RUN_FILE = "run.json"

STATE_OK = "ok"
STATE_INCONCLUSIVE = "inconclusive"
STATE_DRIVER_FAILURE = "driver_failure"
STATES = (STATE_OK, STATE_INCONCLUSIVE, STATE_DRIVER_FAILURE)


def canonical_sources(urls: Any) -> list[str]:
    """Distinct, stripped, sorted URLs; blanks dropped."""
    return sorted({str(u).strip() for u in urls if u is not None and str(u).strip()})


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_run(
    out_dir: Path,
    *,
    agent: str,
    report: str,
    sources: Any,
    started_at: float,
    finished_at: float,
    state: str,
    detail: str = "",
) -> dict[str, Any]:
    """Write the three files and return the run record.

    An empty report is recorded as inconclusive whatever ``state`` says: a run
    that produced nothing to read did not succeed.
    """
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / REPORT_FILE
    sources_path = out_dir / SOURCES_FILE
    report_path.write_text(report, encoding="utf-8")
    urls = canonical_sources(sources)
    sources_path.write_text(json.dumps(urls, indent=2) + "\n", encoding="utf-8")
    if state == STATE_OK and not report.strip():
        state = STATE_INCONCLUSIVE
        detail = detail or "the agent returned an empty report"
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "agent": agent,
        "state": state,
        "detail": detail,
        "started_at": started_at,
        "finished_at": finished_at,
        "wall_seconds": round(finished_at - started_at, 3),
        "sources_count": len(urls),
        "report_sha256": _sha256(report_path),
        "sources_sha256": _sha256(sources_path),
    }
    (out_dir / RUN_FILE).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def verify_run(out_dir: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Read the run record and check it against the files beside it.

    Returns the record (``None`` when there is none to read) and the list of
    discrepancies, each naming the file or field it concerns.
    """
    try:
        loaded: Any = json.loads((out_dir / RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"{RUN_FILE}: unreadable ({exc.__class__.__name__})"]
    if not isinstance(loaded, dict):
        return None, [f"{RUN_FILE}: not a {SCHEMA} record"]
    record = cast("dict[str, Any]", loaded)
    if record.get("schema") != SCHEMA:
        return None, [f"{RUN_FILE}: not a {SCHEMA} record"]
    errors: list[str] = []
    for field, name in (("report_sha256", REPORT_FILE), ("sources_sha256", SOURCES_FILE)):
        path = out_dir / name
        if not path.is_file():
            errors.append(f"{name}: missing")
        elif _sha256(path) != record.get(field):
            errors.append(
                f"{name}: bytes do not match the digest in {RUN_FILE} (report or sources edited after the run)"
            )
    if not errors:
        urls = json.loads((out_dir / SOURCES_FILE).read_text(encoding="utf-8"))
        if urls != canonical_sources(urls) or len(urls) != record.get("sources_count"):
            errors.append(f"{SOURCES_FILE}: not the canonical list the record counts")
    if record.get("state") not in STATES:
        errors.append(f"{RUN_FILE}: unknown state {record.get('state')!r}")
    return record, errors
