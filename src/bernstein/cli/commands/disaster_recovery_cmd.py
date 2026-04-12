"""Disaster recovery: backup and restore .sdd/ state."""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console


@click.group()
def dr_group() -> None:
    """Disaster recovery: backup and restore commands."""


@dr_group.command("backup")
@click.option(
    "--to",
    "dest",
    type=click.Path(path_type=Path),
    required=True,
    help="Destination path for the backup archive.",
)
@click.option(
    "--encrypt",
    is_flag=True,
    default=False,
    help="Encrypt the backup with a symmetric key.",
)
@click.option(
    "--password",
    default=None,
    help="Password for encryption (default: generate random key).",
)
@click.option(
    "--sdd",
    "sdd_dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to .sdd/ directory (default: .sdd/ in current dir).",
)
def dr_backup_cmd(
    dest: Path,
    encrypt: bool,
    password: str | None,
    sdd_dir: Path | None,
) -> None:
    """Backup persistent .sdd/ state to a file.

    Examples::

        bernstein dr backup --to ./backup.tar.gz
        bernstein dr backup --to ./backup.tar.gz --encrypt
        bernstein dr backup --to ./backup.tar.gz --encrypt --password mysecret
    """
    from bernstein.core.disaster_recovery import backup_sdd

    sdd = sdd_dir or Path(".sdd")
    if not sdd.is_dir():
        console.print(f"[red]Error:[/red] .sdd/ directory not found: {sdd}")
        raise SystemExit(1)

    console.print(f"[bold]Backing up[/bold] {sdd} [bold]to[/bold] {dest}...")
    if encrypt:
        console.print("[cyan]Encryption enabled[/cyan]")

    result = backup_sdd(sdd, dest, encrypt=encrypt, password=password)

    console.print("[green]Backup complete![/green]")
    console.print(f"  Path: {result['path']}")
    console.print(f"  Size: {result['size_bytes']} bytes")
    console.print(f"  Files: {result['file_count']}")
    console.print(f"  SHA256: {result['sha256'][:16]}...")


@dr_group.command("restore")
@click.option(
    "--from",
    "source",
    type=click.Path(path_type=Path),
    required=True,
    help="Source backup archive path.",
)
@click.option(
    "--decrypt",
    is_flag=True,
    default=False,
    help="Decrypt the backup before restoring.",
)
@click.option(
    "--password",
    default=None,
    help="Password for decryption.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="List backup contents without extracting.",
)
@click.option(
    "--sdd",
    "sdd_dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to .sdd/ directory (default: .sdd/ in current dir).",
)
def dr_restore_cmd(
    source: Path,
    decrypt: bool,
    password: str | None,
    dry_run: bool,
    sdd_dir: Path | None,
) -> None:
    """Restore .sdd/ state from a backup file.

    Examples::

        bernstein dr restore --from ./backup.tar.gz
        bernstein dr restore --from ./backup.tar.gz --decrypt
        bernstein dr restore --from ./backup.tar.gz --dry-run
    """
    from bernstein.core.disaster_recovery import restore_sdd

    sdd = sdd_dir or Path(".sdd")

    if dry_run:
        console.print(f"[bold]Dry run[/bold] — listing contents of {source}:")
    else:
        console.print(f"[bold]Restoring[/bold] {source} [bold]into[/bold] {sdd}...")
        if decrypt:
            console.print("[cyan]Decryption enabled[/cyan]")

    result = restore_sdd(source, sdd, decrypt=decrypt, password=password, dry_run=dry_run)

    console.print(f"  Files: {result['files_restored']}")
    console.print(f"  Source: {result['source']}")
    console.print(f"  SHA256: {result['sha256'][:16]}...")
    if "files" in result:
        console.print(f"\n[cyan]Files in backup:[/cyan]\n{result['files']}")

    if not dry_run:
        console.print("[green]Restore complete![/green]")


cli = dr_group
