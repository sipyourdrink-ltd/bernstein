"""``bernstein review-annotation``: inspect operator annotation anchors.

Issue #3456. An operator annotation is bound to the diff bytes it targets, not
to a line number, so that a later rebase either still resolves it or provably
does not. This surface is read-only and offline: ``derive`` prints the
canonical anchor for a range of a file, ``resolve`` reports where those bytes
are in the file now, or that they are gone. Neither writes anything.

    bernstein review-annotation derive --file src/x.py --start-line 12 \
        --end-line 14 --comment "rename this"
    bernstein review-annotation resolve --anchor anchor.json --file src/x.py

``resolve`` exits non-zero on an orphaned anchor so a script can tell the two
outcomes apart without parsing prose; the JSON payload carries the reason code
either way.

A separate top-level ``review-annotation`` group is used because the top-level
``review`` command is already a leaf command (manager-queue review / YAML
review pipeline), the same reason ``review-receipt`` is its own group.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.core.review.annotation_anchor import AnnotationAnchor, derive_anchor, resolve_anchor


def _emit(payload: dict[str, object]) -> None:
    click.echo(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


@click.group("review-annotation")
def review_annotation_group() -> None:
    """Derive and resolve content-addressed anchors for review annotations.

    \b
      bernstein review-annotation derive --file src/x.py --start-line 12 \
          --end-line 14 --comment "rename this"
      bernstein review-annotation resolve --anchor anchor.json --file src/x.py
    """


@review_annotation_group.command("derive")
@click.option(
    "--file",
    "file_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="File whose bytes the annotation targets, as rendered to the operator.",
)
@click.option("--start-line", "start_line", required=True, type=int, help="First annotated line (1-based, inclusive).")
@click.option("--end-line", "end_line", required=True, type=int, help="Last annotated line (1-based, inclusive).")
@click.option("--comment", required=True, help="Comment text the anchor binds a digest of.")
def derive_cmd(file_path: str, start_line: int, end_line: int, comment: str) -> None:
    """Print the canonical anchor binding a comment to a range of bytes."""
    blob = Path(file_path).read_bytes()
    try:
        anchor = derive_anchor(blob_bytes=blob, start_line=start_line, end_line=end_line, comment=comment)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    payload = anchor.to_canonical_dict()
    payload["anchor_digest"] = anchor.anchor_digest()
    _emit(payload)


@review_annotation_group.command("resolve")
@click.option(
    "--anchor",
    "anchor_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="JSON anchor as printed by 'review-annotation derive'.",
)
@click.option(
    "--file",
    "file_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="File to resolve the anchor against, in its current state.",
)
def resolve_cmd(anchor_path: str, file_path: str) -> None:
    """Report where the anchored bytes are now, or that they are orphaned."""
    try:
        anchor = AnnotationAnchor.from_dict(json.loads(Path(anchor_path).read_text(encoding="utf-8")))
    except (KeyError, TypeError, ValueError) as exc:
        raise click.ClickException(f"not a usable annotation anchor: {exc}") from exc
    resolution = resolve_anchor(anchor, Path(file_path).read_bytes())
    payload = resolution.to_dict()
    payload["anchor_digest"] = anchor.anchor_digest()
    _emit(payload)
    if resolution.status != "resolved":
        raise SystemExit(1)
