"""``bernstein volunteer``: the donor-and-project surface for volunteer work (#3919).

A project opts in by committing ``.bernstein/volunteer.json``. A donor's worker
reads that file and refuses anything it does not permit. Until this group
existed the file had no reader on the CLI at all: the first feedback a
maintainer got about a bad manifest was a stranger's worker declining their
repository, which is the worst place to learn that ``allowed_paths`` has a typo.

``bernstein volunteer verify`` closes that loop locally::

    bernstein volunteer verify                 # this checkout
    bernstein volunteer verify /path/to/repo   # somewhere else
    bernstein volunteer verify --json          # for a CI step

The digest it prints is the point of the command, not decoration. It is the
same content address a result receipt binds itself to, so a maintainer learns
here what their submissions will be checked against, and anyone can re-derive
it from the committed file.

Only verbs whose backing code exists live here. An absent subcommand is honest;
one that prints "not implemented yet" is a promise the code has not made.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from bernstein.core.volunteer import VolunteerManifest
    from bernstein.core.volunteer.clean_room import CleanRoomResult


@click.group("volunteer")
def volunteer_group() -> None:
    """Volunteer-worker surfaces: donate agent capacity to opt-in projects.

    A project declares its policy in `.bernstein/volunteer.json`; see
    `docs/reference/volunteer-manifest.md` for the schema.
    """


@volunteer_group.command("verify")
@click.argument(
    "repo_root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    required=False,
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def verify_cmd(repo_root: Path, as_json: bool) -> None:
    """Validate a project's volunteer manifest and print its digest.

    Exits non-zero on any rejection, naming the field at fault rather than
    raising a traceback at a maintainer who is editing a config file.
    """
    from bernstein.core.volunteer import (
        VOLUNTEER_MANIFEST_PATH,
        UnenforcedManifestFieldWarning,
        VolunteerManifestError,
        effective_egress,
        load_manifest_from_repo,
    )

    manifest_path = repo_root / VOLUNTEER_MANIFEST_PATH

    # Unenforced fields are a warning from the loader, not a return value.
    # Catching them here is what turns "your build is older than this policy"
    # from a stderr line into part of the report.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UnenforcedManifestFieldWarning)
        try:
            manifest = load_manifest_from_repo(repo_root)
        except FileNotFoundError:
            _fail(
                "the project has not opted in to volunteer work",
                field="<file>",
                as_json=as_json,
                path=manifest_path,
            )
            return
        # Must stay below the FileNotFoundError clause: that is a subclass of
        # OSError, and above it every project without a manifest would be told
        # its manifest is unreadable. Its own clause rather than a widening of
        # the one above, because the two send the reader to different fixes --
        # "you have not opted in" points a maintainer whose file is chmod 000 at
        # adding a file they already have.
        except OSError as exc:
            _fail(
                f"the manifest exists but could not be read: {exc.strerror or exc}",
                field="<unreadable>",
                as_json=as_json,
                path=manifest_path,
            )
            return
        except VolunteerManifestError as exc:
            _fail(str(exc).split(": ", 1)[-1], field=exc.field, as_json=as_json, path=manifest_path)
            return

    unenforced = [str(w.message) for w in caught if issubclass(w.category, UnenforcedManifestFieldWarning)]

    if as_json:
        click.echo(json.dumps(_report(manifest, manifest_path, effective_egress(manifest), unenforced), indent=2))
        return

    _print_report(manifest, manifest_path, effective_egress(manifest), unenforced)


@volunteer_group.command("browse")
@click.option(
    "--index",
    "index_urls",
    multiple=True,
    help="HTTPS URL of a volunteer index JSON document. Can be repeated.",
)
@click.option("--size", default=None, help="Filter by size label (e.g., 's', 'm').")
@click.option("--language", default=None, help="Filter by language topic.")
@click.option("--local-ok", "local_ok_only", is_flag=True, help="Only show projects that accept local models.")
@click.option("--budget", "budget_minutes", type=int, default=None, help="Max wall-clock minutes you will provide.")
@click.option("--verbose", is_flag=True, help="Show dropped projects with reasons.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def browse_cmd(
    index_urls: tuple[str, ...],
    size: str | None,
    language: str | None,
    local_ok_only: bool,
    budget_minutes: int | None,
    verbose: bool,
    as_json: bool,
) -> None:
    """Browse opt-in volunteer projects from one or more indexes.

    Fetches configured indexes (HTTPS only), merges and deduplicates by repo,
    validates each project's ``.bernstein/volunteer.json``, and filters by
    your donor preferences.

    \b
    Examples::

        bernstein volunteer browse --index https://example.test/index.json
        bernstein volunteer browse --index https://a.test/i.json --index https://b.test/i.json \\
            --local-ok --language python
        bernstein volunteer browse --verbose
    """
    from bernstein.core.volunteer.registry import browse_indexes

    if not index_urls:
        _fail("no index URLs provided; use --index at least once", field="--index", as_json=as_json, path=Path("<cli>"))
        return

    joinable, dropped = browse_indexes(
        list(index_urls),
        size=size,
        language=language,
        local_ok_only=local_ok_only,
        budget_minutes=budget_minutes,
    )

    if as_json:
        payload = {
            "joinable": [
                {
                    "repo_url": r.repo_url,
                    "default_branch": r.default_branch,
                    "manifest_url": r.manifest_url,
                    "manifest_sha256": r.digest,
                    "license": r.manifest.license,
                    "local_ok": r.manifest.local_ok,
                    "max_wall_clock_minutes": r.manifest.max_wall_clock_minutes,
                    "task_label": r.manifest.task_label,
                    "topics": list(r.topics),
                }
                for r in joinable
            ],
            "dropped": [{"repo_url": d.repo_url, "reason": d.reason} for d in dropped] if verbose else [],
        }
        click.echo(json.dumps(payload, indent=2))
        return

    if not joinable:
        click.echo("No joinable projects found.")
    else:
        for r in joinable:
            click.echo(f"  {r.repo_url}")
            click.echo(f"    digest     {r.digest}")
            click.echo(f"    license    {r.manifest.license}")
            click.echo(f"    local ok   {'yes' if r.manifest.local_ok else 'no'}")
            click.echo(f"    wall clock {r.manifest.max_wall_clock_minutes} min")
            click.echo(f"    task label {r.manifest.task_label}")
            if r.topics:
                click.echo(f"    topics     {', '.join(r.topics)}")

    if verbose and dropped:
        click.echo("\nDropped:")
        for d in dropped:
            click.echo(f"  {d.repo_url}: {d.reason}")


@volunteer_group.command("budget")
@click.option("--budget-tasks", type=click.IntRange(min=0), default=None, help="Total tasks you authorize.")
@click.option(
    "--budget-hours",
    type=click.FloatRange(min=0),
    default=None,
    help="Total wall-clock hours you authorize.",
)
@click.option("--budget-tokens", type=click.IntRange(min=0), default=None, help="Estimated token total you authorize.")
@click.option("--max-size", type=click.Choice(["xs", "s", "m"]), default=None, help="Largest task size to offer.")
@click.option(
    "--local-only/--allow-api-adapters",
    default=None,
    help="Restrict adapter selection to profiles with local-model support.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override the persistent donor budget config path.",
)
@click.option(
    "--ledger",
    "ledger_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Override the persistent donor budget ledger path.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def budget_cmd(
    budget_tasks: int | None,
    budget_hours: float | None,
    budget_tokens: int | None,
    max_size: str | None,
    local_only: bool | None,
    config_path: Path | None,
    ledger_path: Path | None,
    as_json: bool,
) -> None:
    """Set or inspect the donor budget that persists across volunteer runs."""
    from bernstein.core.volunteer.budget import (
        DEFAULT_BUDGET_CONFIG_PATH,
        DEFAULT_LEDGER_PATH,
        BudgetConfigError,
        BudgetLedgerError,
        VolunteerBudget,
        budget_line_items,
        load_budget_config,
        load_ledger,
        save_budget_config,
        with_budget_overrides,
    )

    config = config_path or DEFAULT_BUDGET_CONFIG_PATH
    ledger = ledger_path or DEFAULT_LEDGER_PATH
    try:
        persisted = load_budget_config(config)
        effective = with_budget_overrides(
            persisted,
            max_tasks=budget_tasks,
            max_hours=budget_hours,
            max_tokens=budget_tokens,
            max_size=max_size,
            local_only=local_only,
        )
        supplied = any(value is not None for value in (budget_tasks, budget_hours, budget_tokens, max_size, local_only))
        consumption = load_ledger(ledger)
        if supplied:
            save_budget_config(effective, config)
    except (BudgetConfigError, BudgetLedgerError, OSError) as error:
        raise click.ClickException(str(error)) from error

    line_items = budget_line_items(effective, consumption)
    payload = {
        "budget": effective.to_dict(),
        "ledger": consumption.to_dict(),
        "line_items": line_items,
        "config_path": str(config),
        "ledger_path": str(ledger),
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo("Volunteer donor budget")
    for item in line_items:
        authorized = item["authorized"] if item["authorized"] is not None else "unlimited"
        remaining_value = item["remaining"] if item["remaining"] is not None else "unlimited"
        click.echo(
            f"  {item['dimension']}: used {item['used']}, reserved {item['reserved']}, "
            f"authorized {authorized}, remaining {remaining_value} {item['unit']}"
        )
    if effective == VolunteerBudget() and persisted is None:
        click.echo("  No limits configured.")


def _report(
    manifest: VolunteerManifest,
    path: Path,
    egress: tuple[str, ...],
    unenforced: list[str],
) -> dict[str, Any]:
    """The verdict as a record, keyed the way a receipt keys it.

    ``manifest_sha256`` matches the field name the sandbox refusal record and
    the result bundle already use, so a caller can join on it without a lookup
    table.
    """
    return {
        "ok": True,
        "path": str(path),
        "manifest_sha256": manifest.digest,
        "manifest": manifest.to_canonical_dict(),
        "effective_egress": list(egress),
        "unenforced_fields": unenforced,
    }


def _print_report(
    manifest: VolunteerManifest,
    path: Path,
    egress: tuple[str, ...],
    unenforced: list[str],
) -> None:
    click.echo(f"✓ {path}")
    click.echo(f"  digest              {manifest.digest}")
    click.echo(f"  license             {manifest.license}")
    click.echo(f"  sandbox             {manifest.sandbox}")
    click.echo(f"  wall clock          {manifest.max_wall_clock_minutes} min")
    click.echo(f"  task label          {manifest.task_label}")
    click.echo(f"  local models ok     {'yes' if manifest.local_ok else 'no'}")
    click.echo(f"  allowed paths       {', '.join(manifest.allowed_paths) or 'repo-wide'}")
    for index, gate in enumerate(manifest.gates):
        click.echo(f"  gate {index + 1:<15}{gate}")
    # An empty `egress_allowlist` reads as "no network", and it is not: the
    # sandbox profile adds the package registries or the gates cannot install
    # anything. Printing what a donor will actually be able to reach is the
    # difference between a policy a maintainer wrote and one they understood.
    click.echo(f"  reachable hosts     {', '.join(egress)}")
    for message in unenforced:
        click.echo(f"! {message}", err=True)


def _fail(message: str, *, field: str, as_json: bool, path: Path) -> None:
    if as_json:
        click.echo(
            json.dumps({"ok": False, "path": str(path), "field": field, "error": message}, indent=2),
            err=True,
        )
    else:
        click.echo(f"✗ {path}", err=True)
        click.echo(f"  {field}: {message}", err=True)
    raise SystemExit(1)


@volunteer_group.command("verify-bundle")
@click.argument(
    "bundle_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--repo",
    "repo_root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    help="Local clone that already holds the bundle's attested base commit.",
)
@click.option(
    "--receipt-out",
    "receipt_out",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Sign and write a clean-room verification receipt to this path.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def verify_bundle_cmd(bundle_path: Path, repo_root: Path, receipt_out: Path | None, as_json: bool) -> None:
    """Re-run a result bundle's gates in a fresh, detached clean-room worktree.

    Checks out the bundle's attested base commit into an isolated worktree
    that shares nothing with the run being checked, applies the attested
    patch, re-executes every gate the checkout's own manifest declares, and
    compares exit codes and log digests against what the bundle attests.
    Exits non-zero when the clean room does not reproduce the bundle.

    Always builds (but does not sign) a clean-room verification receipt so
    its digest can be printed alongside the report -- the same "the digest
    is the point, not decoration" reasoning ``verify`` already documents.
    Pass ``--receipt-out`` to also sign it, with this machine's persistent
    volunteer worker key, and write it to disk.
    """
    from cryptography.exceptions import InvalidKey
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    from bernstein.core.security.result_receipt_bundle import GENESIS_ANCHOR, ChainLink, load_bundle
    from bernstein.core.volunteer.clean_room import (
        build_clean_room_receipt,
        clean_room_receipt_from_result,
        verify_in_clean_room,
        write_clean_room_receipt,
    )
    from bernstein.core.volunteer.lease_store import DEFAULT_WORKER_KEY_PATH, load_or_create_worker_key

    envelope = load_bundle(bundle_path)

    # The key that verifies the bundle's OWN signature is read from the
    # bundle itself: at this stage nothing external has told this command
    # whose bundle it is. That is safe rather than circular -- an attacker
    # who rewrites this field also has to forge the signature over it, and
    # `verify_in_clean_room`'s first step (verify_result_bundle) is exactly
    # what catches that.
    claimed_worker = envelope.statement.get("predicate", {}).get("bundle", {}).get("worker", {})
    claimed_pem = claimed_worker.get("public_key_pem", "") if isinstance(claimed_worker, dict) else ""
    try:
        bundle_public_key = load_pem_public_key(claimed_pem.encode("ascii"))
    except (ValueError, TypeError, InvalidKey) as exc:
        _fail(
            f"bundle names an unreadable worker public key: {exc}",
            field="worker.public_key_pem",
            as_json=as_json,
            path=bundle_path,
        )
        return
    if not isinstance(bundle_public_key, Ed25519PublicKey):
        _fail(
            "bundle's worker public key is not Ed25519",
            field="worker.public_key_pem",
            as_json=as_json,
            path=bundle_path,
        )
        return

    result = verify_in_clean_room(envelope, repo_root=repo_root, public_key=bundle_public_key)

    # Relative to repo_root, not the process's own CWD: --repo already lets
    # an operator name a checkout other than the one they are standing in,
    # and the persistent worker identity should live inside that checkout
    # either way, matching where every other volunteer command's .sdd/
    # state lives relative to the project it belongs to.
    verifier_key = load_or_create_worker_key(repo_root / DEFAULT_WORKER_KEY_PATH)
    verifier_public_key = verifier_key.public_key()
    from bernstein.core.security.audit_dsse import export_public_key_pem, keyid_from_public_key

    receipt = clean_room_receipt_from_result(
        result,
        # This command does not walk a prior on-disk receipt to continue a
        # chain across invocations -- every run here starts a fresh,
        # length-1 chain. Continuity across runs is the submission path's
        # concern (a separate issue), built on the same ChainLink type.
        chain=ChainLink(anchor=GENESIS_ANCHOR, length=1),
        verifier_keyid=keyid_from_public_key(verifier_public_key),
        verifier_public_key_pem=export_public_key_pem(verifier_public_key).decode("ascii"),
    )

    if receipt_out is not None:
        receipt_envelope = build_clean_room_receipt(receipt, signing_key=verifier_key)
        write_clean_room_receipt(receipt_envelope, receipt_out)

    if as_json:
        payload = result.to_dict()
        payload["receipt_digest"] = receipt.digest
        payload["receipt_path"] = str(receipt_out) if receipt_out is not None else None
        click.echo(json.dumps(payload, indent=2))
    else:
        _print_clean_room_report(result, receipt_digest=receipt.digest, receipt_path=receipt_out)

    if not result.passed:
        raise SystemExit(1)


def _print_clean_room_report(result: CleanRoomResult, *, receipt_digest: str, receipt_path: Path | None) -> None:
    if result.passed:
        click.echo(f"✓ clean room reproduced bundle {result.bundle_digest}")
    else:
        click.echo(f"✗ clean room did NOT reproduce bundle {result.bundle_digest}", err=True)
    click.echo(f"  patch applied       {'yes' if result.patch_applied else 'no'}")
    if result.refusal_reason is not None:
        click.echo(f"  refusal             {result.refusal_reason}: {result.refusal_detail}", err=True)
    for divergence in result.outcome_divergences:
        click.echo(
            f"  outcome divergence  {divergence.command!r}: "
            f"attested exit {divergence.attested_exit_code}, got {divergence.actual_exit_code}",
            err=True,
        )
    for divergence in result.log_only_divergences:
        click.echo(f"  log-only divergence {divergence.command!r}: same exit code, output text differs", err=True)
    click.echo(f"  receipt digest      {receipt_digest}")
    if receipt_path is not None:
        click.echo(f"  receipt written to  {receipt_path}")


@volunteer_group.command("hub")
@click.option("--host", default="127.0.0.1", help="Host to bind to.")
@click.option("--port", type=int, default=8053, help="Port to bind to.")
@click.option(
    "--lease-store",
    "lease_store_path",
    default=None,
    help="Path to the JSONL lease store file.",
)
def hub_cmd(host: str, port: int, lease_store_path: str | None) -> None:
    """Serve the volunteer hub HTTP interface.

    The hub exposes endpoints for workers to enroll, claim, heartbeat,
    submit, and release tasks, plus its own task board for work that
    originates here rather than mirroring a git-forge issue.  See
    :func:`bernstein.core.volunteer.hub_app.build_hub_app` for the API surface.

    The board's log is a sibling of the lease log, since the two are one hub's
    state and are torn down together.

    .. note:: The lease store is single-process only. Do not run with
       ``uvicorn --workers N>1`` or multiple replicas.

    Raises:
        SystemExit: Via ``preflight_multi_worker_guard`` when the operator
            asks for more than one worker. ``LeaseStore`` serialises writes
            with an in-process ``asyncio.Lock`` and appends to JSONL without
            ``fcntl.flock``, so a second worker interleaves partial lines and
            hands the same task to two claimants.
    """
    from bernstein.core.server.server_app import preflight_multi_worker_guard

    preflight_multi_worker_guard()

    try:
        import uvicorn
    except ImportError:
        raise click.ClickException(
            "uvicorn is required for the volunteer hub; install with `uv pip install uvicorn`"
        ) from None

    from bernstein.core.volunteer.budget import BudgetConfigError, load_budget_config
    from bernstein.core.volunteer.hub_app import build_hub_app
    from bernstein.core.volunteer.lease_store import LeaseStore
    from bernstein.core.volunteer.task_board import TaskBoard

    if lease_store_path is None:
        lease_store_path = ".sdd/runtime/volunteer/leases.jsonl"

    try:
        donor_budget = load_budget_config()
    except (BudgetConfigError, OSError) as error:
        raise click.ClickException(str(error)) from error

    lease_log = Path(lease_store_path)
    store = LeaseStore(lease_log, budget=donor_budget)
    board = TaskBoard(lease_log.parent / "tasks.jsonl")
    app = build_hub_app(store, task_board=board)
    click.echo(f"Bernstein volunteer hub listening on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
