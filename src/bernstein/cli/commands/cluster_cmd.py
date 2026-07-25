"""``bernstein cluster``: cluster lifecycle helpers (mTLS bootstrap, topology)."""

from __future__ import annotations

import datetime
import json
import os
import time
from pathlib import Path
from typing import Any, cast

import click
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from bernstein.cli.helpers import SERVER_URL, auth_headers, console, is_json, print_json

DEFAULT_CLUSTER_DIR = Path.home() / ".bernstein" / "cluster"
KEY_SIZE = 4096
SERIAL_BITS = 64
CA_VALID_DAYS = 3650
LEAF_VALID_DAYS = 825


@click.group("cluster")
def cluster_group() -> None:
    """Cluster lifecycle helpers.

    \b
      bernstein cluster bootstrap-ca   # generate self-signed CA + server/node certs
    """


def _build_name(common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Bernstein Cluster"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def _generate_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)


def _write_pem(path: Path, data: bytes, *, mode: int) -> None:
    path.write_bytes(data)
    os.chmod(path, mode)


def _build_ca(out_dir: Path) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = _generate_key()
    public_key = key.public_key()
    name = _build_name("Bernstein Self-Signed CA")
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=CA_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(public_key),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(public_key),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    _write_pem(out_dir / "ca.crt", cert.public_bytes(serialization.Encoding.PEM), mode=0o644)
    _write_pem(
        out_dir / "ca.key",
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        mode=0o600,
    )
    return cert, key


def _issue_leaf(
    ca_cert: x509.Certificate,
    ca_key: rsa.RSAPrivateKey,
    out_dir: Path,
    *,
    name_prefix: str,
    common_name: str,
    san_dns: list[str],
    is_server: bool,
) -> None:
    key = _generate_key()
    public_key = key.public_key()
    now = datetime.datetime.now(datetime.UTC)
    eku = (
        x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH, x509.ExtendedKeyUsageOID.CLIENT_AUTH])
        if is_server
        else x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.CLIENT_AUTH])
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(_build_name(common_name))
        .issuer_name(ca_cert.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=LEAF_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(eku, critical=False)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(d) for d in san_dns]),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(public_key),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    _write_pem(out_dir / f"{name_prefix}.crt", cert.public_bytes(serialization.Encoding.PEM), mode=0o644)
    _write_pem(
        out_dir / f"{name_prefix}.key",
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        mode=0o600,
    )


@cluster_group.command("bootstrap-ca")
@click.option(
    "--out-dir",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Directory to write cert artifacts. Default: ~/.bernstein/cluster/",
)
@click.option(
    "--server-cn",
    "server_cn",
    default="bernstein-central",
    help="Common name + primary DNS SAN for the server cert.",
)
@click.option(
    "--node-cn",
    "node_cn",
    default="bernstein-node",
    help="Common name for the node (worker) cert template.",
)
@click.option(
    "--server-san",
    "server_san",
    multiple=True,
    help="Additional DNS SANs for the server cert (repeat for multiple).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite existing artifacts in --out-dir.",
)
def bootstrap_ca(
    out_dir: Path | None,
    server_cn: str,
    node_cn: str,
    server_san: tuple[str, ...],
    force: bool,
) -> None:
    """Generate a self-signed CA, server cert, and node cert template.

    Writes ``ca.crt``, ``ca.key``, ``server.crt``, ``server.key``,
    ``node.crt``, and ``node.key`` to ``--out-dir`` (default
    ``~/.bernstein/cluster/``). Private keys are written 0600.

    This is a self-hosted, self-signed CA - appropriate for internal
    clusters on infrastructure you control. For production deployments
    use your existing PKI (step-ca, cert-manager, Vault, etc.).
    """
    target = out_dir or DEFAULT_CLUSTER_DIR
    target = target.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)

    existing = [p for p in ("ca.crt", "ca.key", "server.crt", "node.crt") if (target / p).exists()]
    if existing and not force:
        console.print(
            f"[red]Refusing to overwrite existing artifacts in {target}: {existing}.[/red] "
            "Re-run with --force to replace them."
        )
        raise SystemExit(1)

    ca_cert, ca_key = _build_ca(target)
    sans = [server_cn, *server_san, "localhost"]
    _issue_leaf(
        ca_cert,
        ca_key,
        target,
        name_prefix="server",
        common_name=server_cn,
        san_dns=list(dict.fromkeys(sans)),
        is_server=True,
    )
    _issue_leaf(
        ca_cert,
        ca_key,
        target,
        name_prefix="node",
        common_name=node_cn,
        san_dns=[node_cn],
        is_server=False,
    )

    console.print(f"[green]Wrote cluster mTLS artifacts to[/green] {target}")
    console.print(
        "\n[bold]Next steps:[/bold]\n"
        f"  1. On the central node, point ClusterConfig.tls at {target}/server.crt + server.key + ca.crt.\n"
        f"  2. Distribute {target}/ca.crt + node.crt + node.key to each worker (out-of-band, e.g. scp).\n"
        f"  3. Set ClusterConfig.tls.verify_mode='required' on both sides.\n"
        f"  4. Restart the central server and workers.\n\n"
        "[yellow]Warning:[/yellow] this is a self-signed CA suitable for internal clusters only. "
        "For production, use your own CA / step-ca / cert-manager."
    )


# ---------------------------------------------------------------------------
# cluster status / nodes - topology visibility (issue #2874)
# ---------------------------------------------------------------------------


def _format_heartbeat_age(last_heartbeat: float, now: float) -> str:
    """Render heartbeat staleness as a compact age string.

    ``never`` when the node has not heartbeated (persisted-but-offline nodes
    load with ``last_heartbeat == 0``); otherwise seconds/minutes/hours since
    the last heartbeat was received.
    """
    if not last_heartbeat or last_heartbeat <= 0:
        return "never"
    age = max(0, int(now - last_heartbeat))
    if age < 60:
        return f"{age}s"
    if age < 3600:
        return f"{age // 60}m {age % 60}s"
    hours, remainder = divmod(age, 3600)
    return f"{hours}h {remainder // 60}m"


def _node_display_fields(node: dict[str, Any], now: float) -> dict[str, str]:
    """Project a cluster-status node dict into rendered table cells.

    ``claimed`` is the node's self-reported ``active_agents`` - the count of
    tasks it has claimed and is actively running (see the worker heartbeat).
    ``adapter`` comes from the node's advertised labels, falling back to ``-``.
    """
    capacity = node.get("capacity", {}) or {}
    labels = node.get("labels", {}) or {}
    try:
        last_heartbeat = float(node.get("last_heartbeat") or 0.0)
    except (TypeError, ValueError):
        last_heartbeat = 0.0
    return {
        "id": str(node.get("id", "")),
        "name": str(node.get("name", "")) or "-",
        "status": str(node.get("status", "")),
        "adapter": str(labels.get("adapter") or "-"),
        "heartbeat": _format_heartbeat_age(last_heartbeat, now),
        "claimed": str(int(capacity.get("active_agents", 0) or 0)),
        "slots": f"{int(capacity.get('available_slots', 0) or 0)}/{int(capacity.get('max_agents', 0) or 0)}",
    }


def _fetch_cluster_status(server_url: str) -> dict[str, Any]:
    """Fetch the cluster status summary from the running task server."""
    try:
        resp = httpx.get(f"{server_url}/cluster/status", timeout=5.0, headers=auth_headers())
        resp.raise_for_status()
    except httpx.ConnectError:
        console.print("[red]Cannot connect to task server.[/red]")
        console.print(f"[dim]Is it running at {server_url}?[/dim]")
        raise SystemExit(1) from None
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]Server error:[/red] {exc.response.status_code}")
        raise SystemExit(1) from None
    data = resp.json()
    if not isinstance(data, dict):
        console.print("[red]Unexpected response format from server.[/red]")
        raise SystemExit(1)
    return data


def _render_nodes_table(nodes: list[dict[str, Any]], now: float) -> None:
    """Render the registered-node table, or a hint when the registry is empty."""
    if not nodes:
        console.print("[dim]No nodes registered.[/dim]")
        return
    from rich.table import Table

    table = Table(title="Cluster Nodes", show_lines=False, header_style="bold cyan")
    table.add_column("Node ID", style="dim", min_width=12)
    table.add_column("Name", min_width=8)
    table.add_column("Status", min_width=8)
    table.add_column("Adapter", min_width=8)
    table.add_column("Heartbeat", justify="right")
    table.add_column("Claimed", justify="right")
    table.add_column("Slots", justify="right")
    for node in nodes:
        fields = _node_display_fields(node, now)
        status = fields["status"]
        status_style = "green" if status == "online" else "yellow" if status != "offline" else "dim"
        table.add_row(
            fields["id"],
            fields["name"],
            f"[{status_style}]{status}[/{status_style}]",
            fields["adapter"],
            fields["heartbeat"],
            fields["claimed"],
            fields["slots"],
        )
    console.print(table)


@cluster_group.command("nodes")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON instead of a table.")
@click.option("--server-url", "server_url", default=None, help="Central server URL (default: BERNSTEIN_SERVER_URL).")
def cluster_nodes(as_json: bool, server_url: str | None) -> None:
    """List registered cluster nodes with heartbeat age and claimed-task counts."""
    data = _fetch_cluster_status(server_url or SERVER_URL)
    nodes = data.get("nodes", []) if isinstance(data.get("nodes"), list) else []
    if as_json or is_json():
        print_json(nodes)
        return
    _render_nodes_table(nodes, time.time())


@cluster_group.command("status")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON instead of a table.")
@click.option("--server-url", "server_url", default=None, help="Central server URL (default: BERNSTEIN_SERVER_URL).")
def cluster_status_cmd(as_json: bool, server_url: str | None) -> None:
    """Show cluster topology: online/offline counts and the node table."""
    data = _fetch_cluster_status(server_url or SERVER_URL)
    if as_json or is_json():
        print_json(data)
        return
    console.print(
        f"[bold]Cluster[/bold]  topology={data.get('topology', '?')}  "
        f"nodes={data.get('online_nodes', 0)}/{data.get('total_nodes', 0)} online"
    )
    console.print(
        f"[dim]capacity: {data.get('active_agents', 0)} active / "
        f"{data.get('available_slots', 0)} free / {data.get('total_capacity', 0)} total slots[/dim]"
    )
    nodes = data.get("nodes", []) if isinstance(data.get("nodes"), list) else []
    _render_nodes_table(nodes, time.time())


# ---------------------------------------------------------------------------
# ``bernstein cluster claims`` -- the signed MESH claim journal (#2558)
# ---------------------------------------------------------------------------


def _resolve_claim_journal_path(workdir: str, journal_path: str | None) -> Path:
    """Return the claim-journal path to read, without touching the network.

    An explicit ``--journal`` wins so an operator can verify a journal copied
    off a machine that is gone; otherwise the conventional path under the
    project's ``.sdd/`` is used.
    """
    if journal_path:
        return Path(journal_path).expanduser().resolve()
    from bernstein.core.orchestration.tracker_pipeline import default_claim_journal_path

    return default_claim_journal_path(Path(workdir).resolve() / ".sdd")


def _read_claim_receipts(path: Path) -> list[dict[str, Any]]:
    """Return the raw receipt dicts on disk, in journal order."""
    if not path.exists():
        raise click.ClickException(
            f"no claim journal at {path}\n"
            "The journal is written only by the MESH topology; set cluster.topology to 'mesh', "
            "or pass --journal to point at one.",
        )
    receipts: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped:
            receipts.append(json.loads(stripped))
    return receipts


@cluster_group.group("claims")
def cluster_claims_group() -> None:
    """Read and verify the signed MESH claim journal.

    \b
      bernstein cluster claims log      # every receipt, in chain order
      bernstein cluster claims head     # the current head hash
      bernstein cluster claims verify   # offline replay: links, signatures, anchors

    Every subcommand reads the journal file directly. None of them contacts a
    server, so the coordination history stays readable when the fleet is down.
    """


@cluster_claims_group.command("log")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON instead of a table.")
@click.option("--journal", "journal_path", default=None, help="Path to the claim journal (default: .sdd/cluster/).")
@click.option("--workdir", default=".", help="Project directory holding .sdd/.")
@click.option("--limit", default=0, type=int, help="Show only the last N receipts (0 = all).")
def cluster_claims_log(as_json: bool, journal_path: str | None, workdir: str, limit: int) -> None:
    """List every claim receipt in chain order."""
    from rich.table import Table

    path = _resolve_claim_journal_path(workdir, journal_path)
    receipts = _read_claim_receipts(path)
    if limit > 0:
        receipts = receipts[-limit:]
    if as_json or is_json():
        print_json(receipts)
        return
    table = Table(title=f"Claim journal ({path})", show_lines=False, header_style="bold cyan")
    table.add_column("#", justify="right")
    table.add_column("kind")
    table.add_column("key")
    table.add_column("claimer")
    table.add_column("node")
    table.add_column("entry_hash")
    for index, receipt in enumerate(receipts):
        key = f"{receipt.get('tracker', '')}/{receipt.get('ticket_id', '')}/{receipt.get('role', '')}"
        kind = str(receipt.get("kind", "?"))
        style = "red" if kind == "fork" else ("yellow" if kind == "supersede" else "green")
        table.add_row(
            str(index),
            f"[{style}]{kind}[/{style}]",
            key,
            str(receipt.get("claimer_id", "")),
            str(receipt.get("node_id", "")),
            str(receipt.get("entry_hash", ""))[:23],
        )
    console.print(table)


@cluster_claims_group.command("head")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON instead of text.")
@click.option("--journal", "journal_path", default=None, help="Path to the claim journal (default: .sdd/cluster/).")
@click.option("--workdir", default=".", help="Project directory holding .sdd/.")
def cluster_claims_head(as_json: bool, journal_path: str | None, workdir: str) -> None:
    """Print the journal head hash and entry count."""
    from bernstein.core.lineage.tracker_audit import GENESIS_PREV_HASH

    path = _resolve_claim_journal_path(workdir, journal_path)
    receipts = _read_claim_receipts(path)
    head = str(receipts[-1]["entry_hash"]) if receipts else GENESIS_PREV_HASH
    if as_json or is_json():
        print_json({"head": head, "entries": len(receipts), "journal": str(path)})
        return
    console.print(f"[bold]head[/bold]    {head}")
    console.print(f"[dim]entries {len(receipts)}  journal {path}[/dim]")


@cluster_claims_group.command("verify")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON instead of text.")
@click.option("--journal", "journal_path", default=None, help="Path to the claim journal (default: .sdd/cluster/).")
@click.option("--workdir", default=".", help="Project directory holding .sdd/.")
@click.option(
    "--check-anchors/--no-check-anchors",
    default=True,
    help="Also confirm every receipt is anchored in the local HMAC audit chain.",
)
def cluster_claims_verify(as_json: bool, journal_path: str | None, workdir: str, check_anchors: bool) -> None:
    """Replay the journal offline and report the head, tamper, and forks.

    Runs with no live nodes: it needs the journal file and, for the anchor
    check, the local audit chain. Confirms every ``prev_entry_hash`` link,
    every recomputed ``entry_hash``, every Ed25519 node signature, and every
    audit-chain anchor, then prints the head hash.

    \b
    Exit codes:
      0  intact, no fork
      1  integrity failure -- the failing entry index is printed
      2  intact but forked -- the divergence entry index is printed
    """
    from bernstein.core.orchestration.tracker_pipeline import ClaimJournal
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.server.dashboard_tokens import resolve_dashboard_hmac_key

    root = Path(workdir).resolve()
    sdd_dir = root / ".sdd"
    path = _resolve_claim_journal_path(workdir, journal_path)
    if not path.exists():
        _read_claim_receipts(path)  # raises the actionable ClickException

    chain: AuditChainStore | None = None
    if check_anchors and (sdd_dir / "audit").exists():
        chain = AuditChainStore(sdd_dir / "audit", key=resolve_dashboard_hmac_key(sdd_dir))

    # Verification is a pure read: the KMS adapter is never asked to sign, so
    # a journal can be verified on a machine that holds no signing key at all.
    journal = ClaimJournal(path, kms_adapter=cast("Any", None), node_id="verifier")
    result = journal.verify(chain=chain)

    payload = {
        "ok": result.ok,
        "clean": result.clean,
        "head": result.head,
        "entries": result.entry_count,
        "bad_index": result.bad_index,
        "failures": result.failures,
        "anchors_checked": result.anchors_checked,
        "forks": [
            {
                "divergence_index": fork.divergence_index,
                "entry_hash": fork.entry_hash,
                "local_head": fork.local_head,
                "observed_by": fork.observed_by,
            }
            for fork in result.forks
        ],
    }
    if as_json or is_json():
        print_json(payload)
    elif not result.ok:
        console.print(f"[bold red]FAILED[/bold red] at entry index {result.bad_index}")
        for failure in result.failures:
            console.print(f"  [red]{failure}[/red]")
    else:
        anchor_note = "links + signatures + audit anchors" if result.anchors_checked else "links + signatures"
        console.print(f"[bold green]OK[/bold green] {result.entry_count} entries verified ({anchor_note})")
        console.print(f"[bold]head[/bold] {result.head}")
        for fork in result.forks:
            console.print(
                f"[bold red]FORK[/bold red] at divergence entry index {fork.divergence_index}: "
                f"{fork.entry_hash} did not extend {fork.local_head} (observed by {fork.observed_by})"
            )

    if not result.ok:
        raise SystemExit(1)
    if result.forks:
        raise SystemExit(2)
