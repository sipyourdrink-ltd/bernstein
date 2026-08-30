"""``bernstein review-receipt``: attested pull-request review receipts.

Issue #2296. Emits and verifies signed review receipts that bind the issue
body, the plan, the run journal head (every tool call), and the diff into one
artefact anchored in the review lineage spine:

    bernstein review-receipt emit   --pr <url> --repo <owner/repo> \
        --issue <issue.md> --plan <plan.md|-> --diff <pr.diff> \
        --journal-head <head> --verdict <verdict>
    bernstein review-receipt verify --pr <url> --issue <issue.md> --diff <pr.diff>

``verify`` recomputes ``issue_hash`` and ``diff_hash`` from the presented PR
inputs and checks the Ed25519 signature offline, proving the PR was reviewed
against the ticket without operator override. The tracker comment is a
projection of the receipt (short verdict + this verify command), never the
receipt body.

A separate top-level ``review-receipt`` group is used because the top-level
``review`` command is already a leaf command (manager-queue review / YAML
review pipeline); the receipt surface is additive and does not disturb it.
"""

from __future__ import annotations

import time
from pathlib import Path

import click

from bernstein.cli.helpers import console


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _lineage_root(workdir: Path) -> Path:
    return workdir / ".sdd" / "lineage"


def _identity_dir(workdir: Path) -> Path:
    return workdir / ".sdd" / "identity"


def _audit_dir(workdir: Path) -> Path:
    return workdir / ".sdd" / "audit"


def _read_text_or_stdin(value: str) -> str:
    if value == "-":
        import sys

        return sys.stdin.read()
    return Path(value).read_text(encoding="utf-8")


@click.group("review-receipt")
def review_receipt_group() -> None:
    """Emit and verify attested pull-request review receipts.

    \b
      bernstein review-receipt emit --pr <url> --repo o/r --issue i.md \
          --plan p.md --diff pr.diff --journal-head <head> --verdict approve
      bernstein review-receipt verify --pr <url> --issue i.md --diff pr.diff
    """


@review_receipt_group.command("emit")
@click.option("--pr", "pr_url", required=True, help="Pull request URL the receipt covers.")
@click.option("--repo", required=True, help="owner/repo slug.")
@click.option("--issue", "issue_file", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--plan", "plan_value", required=True, help="Plan file path, or '-' to read stdin.")
@click.option("--diff", "diff_file", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--journal-head", "journal_head", default="", help="Run journal Merkle head (every tool call).")
@click.option("--verdict", default="approve", show_default=True, help="Review verdict.")
@click.option("--task-id", "task_id", default="", help="Task the review is attributed to.")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def review_receipt_emit_cmd(
    pr_url: str,
    repo: str,
    issue_file: str,
    plan_value: str,
    diff_file: str,
    journal_head: str,
    verdict: str,
    task_id: str,
    workdir: str,
) -> None:
    """Bind issue + plan + tool calls + diff into a signed, anchored receipt.

    Exit code 0 on success.
    """
    from bernstein.core.review.receipt import emit_review_receipt, load_or_create_review_identity
    from bernstein.core.security.audit_chain import AuditChainStore, record_review_receipt

    root = Path(workdir).resolve()
    key = _load_hmac_key()
    private_pem, public_pem = load_or_create_review_identity(_identity_dir(root))

    issue_body = Path(issue_file).read_text(encoding="utf-8")
    plan = _read_text_or_stdin(plan_value)
    diff = Path(diff_file).read_bytes()

    receipt = emit_review_receipt(
        workdir=root,
        lineage_root=_lineage_root(root),
        hmac_key=key,
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        pr_url=pr_url,
        repo=repo,
        issue_body=issue_body,
        plan=plan,
        journal_head=journal_head,
        diff=diff,
        findings=(),
        verdict=verdict,
        task_id=task_id,
        timestamp=int(time.time()),
        resolution_hash="sha256:912abcebddc909bb61712cad73e12236d0128a53e9e7fcac0ac33c58df0ea804",
    )

    chain = AuditChainStore(_audit_dir(root), key=key)
    record_review_receipt(
        chain=chain,
        pr_url=receipt.pr_url,
        issue_hash=receipt.issue_hash,
        plan_hash=receipt.plan_hash,
        journal_head=receipt.journal_head,
        diff_hash=receipt.diff_hash,
        verdict=receipt.verdict,
        journal_entry_hash=receipt.journal_entry_hash,
    )

    console.print()
    console.print("[bold]Review receipt emit[/bold]")
    console.print(f"  issue_hash          {receipt.issue_hash}")
    console.print(f"  plan_hash           {receipt.plan_hash}")
    console.print(f"  journal_head        {receipt.journal_head}")
    console.print(f"  diff_hash           {receipt.diff_hash}")
    console.print(f"  journal_entry_hash  {receipt.journal_entry_hash}")
    console.print("[green]OK[/green] -- signed receipt anchored in the review spine.")


@review_receipt_group.command("verify")
@click.option("--pr", "pr_url", required=True, help="Pull request URL to verify.")
@click.option("--issue", "issue_file", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--diff", "diff_file", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--chain",
    is_flag=True,
    default=False,
    help="Verify every pass of a review contour, not just the single-pass receipt.",
)
@click.option(
    "--rules",
    "rules_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Rules file whose digest every pass must have been reviewed under (--chain only).",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def review_receipt_verify_cmd(
    pr_url: str,
    issue_file: str,
    diff_file: str,
    chain: bool,
    rules_file: str | None,
    workdir: str,
) -> None:
    """Prove offline that the PR's diff was reviewed against the issue.

    Recomputes ``issue_hash`` and ``diff_hash`` from the presented inputs and
    checks the Ed25519 signature and spine anchor. With ``--chain`` the whole
    fix-until-green sequence is walked: every pass must recompute, carry the
    previous pass's anchor, and name the same ruleset, and ``--diff`` is
    checked against the last pass. Exit codes: 0 = verified, 1 = no receipt /
    bad input, 2 = mismatch (tamper).
    """
    from bernstein.core.review.receipt import verify_review_receipt

    root = Path(workdir).resolve()
    issue_body = Path(issue_file).read_text(encoding="utf-8")
    diff = Path(diff_file).read_bytes()

    if chain:
        _verify_chain(root, pr_url=pr_url, issue_body=issue_body, diff=diff, rules_file=rules_file)

    result = verify_review_receipt(
        workdir=root,
        lineage_root=_lineage_root(root),
        hmac_key=_load_hmac_key(),
        pr_url=pr_url,
        issue_body=issue_body,
        diff=diff,
    )
    console.print()
    console.print(f"[bold]Review receipt verify[/bold] pr={pr_url}")
    if result.ok:
        console.print(f"  verdict {result.verdict}")
        console.print("[green]OK[/green] -- PR was reviewed against the ticket without operator override.")
        raise SystemExit(0)
    if result.receipt is None:
        console.print(f"[yellow]NO RECEIPT[/yellow] -- {result.reason}")
        raise SystemExit(1)
    console.print(f"[red]MISMATCH[/red] -- {result.reason}")
    raise SystemExit(2)


def _verify_chain(
    root: Path,
    *,
    pr_url: str,
    issue_body: str,
    diff: bytes,
    rules_file: str | None,
) -> None:
    """Walk every pass of a review contour and exit with its verdict."""
    from bernstein.core.quality.review_pipeline.ruleset import parse_ruleset
    from bernstein.core.review.receipt import verify_review_chain

    digest = None
    if rules_file is not None:
        digest = parse_ruleset(Path(rules_file).read_text(encoding="utf-8"), source=rules_file).digest

    result = verify_review_chain(
        workdir=root,
        lineage_root=_lineage_root(root),
        hmac_key=_load_hmac_key(),
        pr_url=pr_url,
        issue_body=issue_body,
        diff=diff,
        ruleset_digest=digest,
    )
    console.print()
    console.print(f"[bold]Review chain verify[/bold] pr={pr_url}")
    console.print(f"  passes  {result.passes}")
    if result.ok:
        console.print(f"  verdict {result.verdict}")
        console.print(f"  ruleset {result.ruleset_digest or '(none)'}")
        console.print("[green]OK[/green] -- every pass recomputed and the chain holds.")
        raise SystemExit(0)
    if result.passes == 0:
        console.print(f"[yellow]NO CHAIN[/yellow] -- {result.reason}")
        raise SystemExit(1)
    console.print(f"[red]MISMATCH[/red] -- {result.reason}")
    raise SystemExit(2)
