"""``bernstein artifact verify``: prove a non-coding artifact (issue #2608).

A non-coding task's output (report / dataset / action log / ops result) is
recorded as a signed, content-addressed lineage entry. ``artifact verify``
re-derives the canonical hash from the stored bytes, ties it to the signed
entry, and runs the lineage gate (Ed25519 signature + operator HMAC chain +
parent integrity). It fails - non-zero exit - on any post-hoc byte alteration
of the artifact or a removed lineage entry, so "the agent produced this exact
artifact" is a cryptographic check, not a trust exercise.

    bernstein artifact verify <task_id>

This is the singular ``artifact`` group; the plural ``artifacts`` group lists
agent-posted, journal-anchored task artifacts (issue #2553) and is unrelated.
"""

from __future__ import annotations

import os
from pathlib import Path

import click

from bernstein.cli.helpers import console


@click.group("artifact")
def artifact_group() -> None:
    """Verify a task's signed, content-addressed artifact.

    \b
      bernstein artifact verify <task_id>
    """


@artifact_group.command("verify")
@click.argument("task_id")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True, path_type=Path),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option(
    "--operator-secret-env",
    default="BERNSTEIN_OPERATOR_SECRET",
    show_default=True,
    help="Env var holding the operator HMAC secret. Falls back to the audit key.",
)
@click.option("--output-json", is_flag=True, help="Emit a JSON verdict instead of human text.")
def artifact_verify_cmd(task_id: str, workdir: Path, operator_secret_env: str, output_json: bool) -> None:
    """Re-derive TASK_ID's canonical hash and verify its signed lineage record.

    Exit codes: 0 = verified, 2 = tampered / missing / unverifiable.
    """
    import json
    import sys

    from bernstein.core.lineage.artifact_record import ARTIFACT_SINK_RELPATH, verify_artifact

    sdd = workdir.resolve() / ".sdd"
    sink_root = workdir.resolve() / ARTIFACT_SINK_RELPATH
    log_path = sdd / "lineage" / "log.jsonl"
    cards_dir = sdd / "agents"

    operator_secret = _resolve_operator_secret(operator_secret_env)

    result = verify_artifact(
        task_id=task_id,
        sink_root=sink_root,
        log_path=log_path,
        cards_dir=cards_dir,
        operator_secret=operator_secret,
    )

    if output_json:
        click.echo(
            json.dumps(
                {
                    "task_id": result.task_id,
                    "ok": result.ok,
                    "content_hash": result.content_hash,
                    "entry_hash": result.entry_hash,
                    "failures": result.failures,
                }
            )
        )
    elif result.ok:
        console.print(f"[green]VERIFIED[/green] task={task_id}")
        console.print(f"  content_hash  {result.content_hash}")
        console.print(f"  entry_hash    {result.entry_hash}")
    else:
        console.print(f"[red]TAMPERED[/red] task={task_id}")
        for failure in result.failures:
            console.print(f"  - {failure}")

    if not result.ok:
        sys.exit(2)


def _resolve_operator_secret(env_var: str) -> bytes | None:
    """Resolve the operator HMAC secret: env var first, then the audit key.

    Returns ``None`` only when neither source yields a key, in which case the
    HMAC leg of verification is skipped (signature + chain are still enforced).
    """
    secret = os.environ.get(env_var)
    if secret:
        return secret.encode("utf-8")
    try:
        from bernstein.core.security.audit import load_or_create_audit_key

        return load_or_create_audit_key()
    except Exception:  # pragma: no cover - defensive: never block verification on key IO
        return None


__all__ = ["artifact_group"]
