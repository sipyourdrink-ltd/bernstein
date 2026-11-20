"""Render the sticky observability comment posted on every PR.

Reads the JSON snapshot emitted by ``bernstein doctor observe --json``
and turns it into a compact Markdown block. Designed to run inside the
``pr-observability-summary.yml`` workflow but operator-runnable as well::

    bernstein doctor observe --json --no-persist > observe.json
    python scripts/observability/build_pr_summary.py \\
        --observe observe.json --pr 42 --branch feat/foo \\
        --out pr_summary.md

The output is intentionally terse: a one-row-per-backend status line, a
metrics table for backends that produced numeric rows, and a footer
pointing to the operator doc. Backends that soft-fail because their
credentials are not configured are folded into a single "skipped" line
so a fresh checkout does not generate noisy PR comments.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_STATUS_ICON = {
    "ok": "OK",
    "warn": "WARN",
    "fail": "FAIL",
    "skipped": "skipped",
    "error": "ERROR",
}


def _load_observe(path: Path) -> dict[str, Any]:
    """Return the parsed payload or a synthetic skipped payload on error."""

    if not path.exists():
        return {
            "summary": {"skipped": 0, "ok": 0, "warn": 0, "fail": 0, "error": 1},
            "backends": [],
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "summary": {"skipped": 0, "ok": 0, "warn": 0, "fail": 0, "error": 1},
            "backends": [],
            "_parse_error": str(exc),
        }


def _render_header(pr: str, branch: str, summary: dict[str, int]) -> list[str]:
    # Git branch names may contain backticks. Fence the complete literal.
    fence = "`" * (1 + max((len(run) for run in re.findall(r"`+", branch)), default=0))
    literal = f"{fence} {branch} {fence}" if "`" in branch else f"{fence}{branch}{fence}"
    line = (
        f"**bernstein doctor observe** for PR #{pr}"
        + (f" ({literal})" if branch else "")
        + f": ok={summary.get('ok', 0)}, warn={summary.get('warn', 0)}, "
        f"fail={summary.get('fail', 0)}, error={summary.get('error', 0)}, "
        f"skipped={summary.get('skipped', 0)}"
    )
    return [line, ""]


def _render_backend(backend: dict[str, Any]) -> list[str]:
    name = backend.get("backend", "?")
    status = backend.get("status", "?")
    detail = backend.get("detail", "")
    metrics = backend.get("metrics") or []

    icon = _STATUS_ICON.get(status, status)
    header = f"### {name} -- {icon}"
    if detail:
        header += f" ({detail})"
    if not metrics:
        return [header, ""]

    lines = [
        header,
        "",
        "| metric | value | delta | threshold | status |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for m in metrics:
        lines.append(
            "| {name} | {value} | {delta} | {threshold} | {status} |".format(
                name=m.get("name", ""),
                value=m.get("value", ""),
                delta=m.get("delta", "-"),
                threshold=m.get("threshold", "") or "-",
                status=m.get("threshold_status", ""),
            )
        )
    lines.append("")
    return lines


def _render_skipped_summary(skipped: list[str]) -> list[str]:
    if not skipped:
        return []
    return [
        "<details><summary>Skipped backends (credentials not configured)</summary>",
        "",
        "- " + "\n- ".join(skipped),
        "",
        "</details>",
        "",
    ]


def _build_markdown(payload: dict[str, Any], pr: str, branch: str) -> str:
    summary = payload.get("summary") or {}
    backends = payload.get("backends") or []
    lines = _render_header(pr=pr, branch=branch, summary=summary)
    skipped: list[str] = []
    for backend in backends:
        if backend.get("status") == "skipped":
            skipped.append(f"{backend.get('backend', '?')}: {backend.get('detail', '')}")
            continue
        lines.extend(_render_backend(backend))
    lines.extend(_render_skipped_summary(skipped))
    if payload.get("_parse_error"):
        lines.append(f"_observe JSON could not be parsed: {payload['_parse_error']}_")
        lines.append("")
    lines.append(
        "_See [docs/observability/unified-doctor.md]"
        "(/sipyourdrink-ltd/bernstein/blob/main/docs/observability/unified-doctor.md) "
        "for backend setup notes._"
    )
    return "\n".join(lines) + "\n"


def _publication_scalar(value: object) -> str:
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("Unexpected observation field type")
    # Preserve visible punctuation without letting a field add Markdown, HTML,
    # a mention, a table cell or a new line to the bot's comment.
    return "".join(c if c.isalnum() or c in " .,-" else f"&#{ord(c)};" for c in str(value) if c.isprintable())


def render_publication(raw: bytes, number: int, branch: str) -> bytes:
    """Render bounded observation data with the trusted comment template."""
    if len(raw) > 65536:
        raise ValueError("Observation exceeds 64 KiB")
    try:
        observed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        observed = {"summary": {"error": 1}, "_parse_error": str(exc)}

    def fields(row: dict, names: tuple[str, ...]) -> dict:
        return {key: _publication_scalar(row[key]) for key in names if key in row}

    clean: dict[str, Any] = {
        "summary": fields(observed.get("summary") or {}, ("ok", "warn", "fail", "error", "skipped")),
        "backends": [],
    }
    for backend in observed.get("backends") or []:
        row = fields(backend, ("backend", "status", "detail"))
        row["metrics"] = [
            fields(metric, ("name", "value", "delta", "threshold", "threshold_status"))
            for metric in backend.get("metrics") or []
        ]
        clean["backends"].append(row)
    if "_parse_error" in observed:
        clean["_parse_error"] = _publication_scalar(observed["_parse_error"])
    body = _build_markdown(clean, str(number), branch).encode()
    if len(body) > 48 * 1024:
        raise ValueError("Rendered comment exceeds 48 KiB")
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observe", required=True, type=Path, help="Path to observe JSON")
    parser.add_argument("--pr", required=True, help="Pull-request number")
    parser.add_argument("--branch", default="", help="Pull-request head branch")
    parser.add_argument("--out", required=True, type=Path, help="Output markdown path")
    args = parser.parse_args(argv)

    payload = _load_observe(args.observe)
    md = _build_markdown(payload, pr=args.pr, branch=args.branch)
    args.out.write_text(md, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
