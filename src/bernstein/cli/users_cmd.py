"""Bernstein users — manage RBAC users for the task server."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TypedDict, cast

import click
from rich.console import Console
from rich.table import Table

console = Console()
logger = logging.getLogger(__name__)

USERS_DIR = Path(".sdd/auth/users")


class UserRecord(TypedDict):
    """Serialized RBAC user record stored under ``.sdd/auth/users``."""

    id: str
    email: str
    display_name: str
    role: str
    groups: list[str]
    created_at: float
    active: bool


def _ensure_users_dir() -> Path:
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    return USERS_DIR


def _load_users() -> list[UserRecord]:
    """Load all user JSON files from the users directory."""
    users_dir = _ensure_users_dir()
    users: list[UserRecord] = []
    for f in sorted(users_dir.glob("*.json")):
        try:
            data = cast("UserRecord", json.loads(f.read_text()))
            users.append(data)
        except (json.JSONDecodeError, OSError):
            logger.warning("Skipping invalid user file: %s", f)
    return users


def _save_user(user: UserRecord) -> None:
    """Save a user to the users directory."""
    users_dir = _ensure_users_dir()
    filepath = users_dir / f"{user['id']}.json"
    filepath.write_text(json.dumps(user, indent=2))


@click.group("users")
def users_group() -> None:
    """Manage RBAC users (add, remove, list)."""


@users_group.command("list")
def list_users() -> None:
    """List all registered users."""
    users = _load_users()
    if not users:
        console.print("[dim]No users registered.[/dim]")
        return

    table = Table(title="Bernstein Users")
    table.add_column("ID", style="dim")
    table.add_column("Email", style="cyan")
    table.add_column("Name")
    table.add_column("Role", style="green")

    for u in users:
        table.add_row(
            u.get("id", "?")[:12],
            u.get("email", ""),
            u.get("display_name", ""),
            u.get("role", "viewer"),
        )

    console.print(table)


@users_group.command("add")
@click.argument("email")
@click.option(
    "--role",
    type=click.Choice(["admin", "operator", "viewer"]),
    default="viewer",
    help="Role to assign.",
)
@click.option("--name", default="", help="Display name.")
def add_user(email: str, role: str, name: str) -> None:
    """Add a new user with the given email and role."""
    import hashlib
    import time

    user_id = hashlib.sha256(email.encode()).hexdigest()[:16]

    # Check for duplicate
    users_dir = _ensure_users_dir()
    filepath = users_dir / f"{user_id}.json"
    if filepath.exists():
        console.print(f"[yellow]⚠[/yellow] User with email [bold]{email}[/bold] already exists.")
        return

    user: UserRecord = {
        "id": user_id,
        "email": email,
        "display_name": name or email.split("@")[0],
        "role": role,
        "groups": [],
        "created_at": time.time(),
        "active": True,
    }
    _save_user(user)
    console.print(f"[green]✓[/green] Added user [bold]{email}[/bold] with role [cyan]{role}[/cyan]")


@users_group.command("remove")
@click.argument("email")
def remove_user(email: str) -> None:
    """Remove a user by email."""
    import hashlib

    user_id = hashlib.sha256(email.encode()).hexdigest()[:16]
    users_dir = _ensure_users_dir()
    filepath = users_dir / f"{user_id}.json"

    if not filepath.exists():
        console.print(f"[red]✗[/red] User [bold]{email}[/bold] not found.")
        return

    filepath.unlink()
    console.print(f"[green]✓[/green] Removed user [bold]{email}[/bold]")
