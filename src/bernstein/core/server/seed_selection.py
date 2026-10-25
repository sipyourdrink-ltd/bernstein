"""Child-local transport and validation of an explicitly selected seed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.config.seed_config import SeedError
from bernstein.core.config.seed_parser import _parse_seed

if TYPE_CHECKING:
    from pathlib import Path


def _apply_selected_seed(env: dict[str, str], seed_path: Path | None) -> None:
    """Validate a selected policy and overlay only the child's environment.

    An absent selection retains ordinary environment/workdir discovery. The
    path identifies the selected file, not an immutable config snapshot.
    """
    if seed_path is None:
        return
    try:
        selected = seed_path.resolve()
        # Revalidation must not apply this server's tuning to its supervisor.
        _parse_seed(selected, apply_tuning=False)
    except (SeedError, OSError, UnicodeError):
        # Parser diagnostics can contain YAML values. Restart logging must
        # expose the selected path without the seed's contents or cause chain.
        raise SeedError(f"Cannot load selected seed: {seed_path}") from None
    env["BERNSTEIN_SEED_PATH"] = str(selected)
