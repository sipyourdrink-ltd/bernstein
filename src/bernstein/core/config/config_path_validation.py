"""Validate file paths referenced in bernstein.yaml configuration.

This module provides startup validation for config-referenced paths:
context_files, agent_catalog, and other filesystem paths. If any path
does not exist, validation fails with a clear message before run starts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.seed import SeedConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PathValidationError:
    """A single path validation failure.

    Attributes:
        field: Config field name (e.g., "context_files", "agent_catalog").
        path: The path that failed validation.
        reason: Human-readable reason for the failure.
    """

    field: str
    path: str
    reason: str

    def __str__(self) -> str:
        return f"{self.field}: '{self.path}' {self.reason}"


@dataclass(frozen=True)
class PathValidationResult:
    """Result of validating all config paths.

    Attributes:
        errors: List of validation errors found.
    """

    errors: tuple[PathValidationError, ...]

    @property
    def ok(self) -> bool:
        """Return True if no validation errors."""
        return len(self.errors) == 0

    def format_errors(self) -> str:
        """Format all errors as a newline-separated string."""
        return "\n".join(f"  - {e}" for e in self.errors)


def validate_config_paths(seed: SeedConfig, workdir: Path) -> PathValidationResult:
    """Validate all file paths referenced in the seed configuration.

    Checks that:
    - All context_files exist and are readable files
    - agent_catalog (if set) exists and is a directory

    Args:
        seed: Validated seed configuration from bernstein.yaml.
        workdir: Project working directory for resolving relative paths.

    Returns:
        PathValidationResult with any errors found.
    """
    errors: list[PathValidationError] = []

    # Validate context_files
    for ctx_path in seed.context_files:
        full_path = Path(ctx_path)
        # Handle both absolute and relative paths
        if not full_path.is_absolute():
            full_path = workdir / ctx_path
        if not full_path.exists():
            errors.append(
                PathValidationError(
                    field="context_files",
                    path=ctx_path,
                    reason="does not exist",
                )
            )
        elif not full_path.is_file():
            errors.append(
                PathValidationError(
                    field="context_files",
                    path=ctx_path,
                    reason="is not a file",
                )
            )

    # Validate agent_catalog
    if seed.agent_catalog is not None:
        catalog_path = Path(seed.agent_catalog)
        # Handle both absolute and relative paths
        if not catalog_path.is_absolute():
            catalog_path = workdir / catalog_path
        if not catalog_path.exists():
            errors.append(
                PathValidationError(
                    field="agent_catalog",
                    path=seed.agent_catalog,
                    reason="does not exist",
                )
            )
        elif not catalog_path.is_dir():
            errors.append(
                PathValidationError(
                    field="agent_catalog",
                    path=seed.agent_catalog,
                    reason="is not a directory",
                )
            )

    return PathValidationResult(errors=tuple(errors))


def check_config_paths(seed: SeedConfig, workdir: Path) -> None:
    """Validate config paths and exit with clear error if any are missing.

    This is the main entry point for preflight path validation. Call this
    during startup before the orchestrator begins execution.

    Args:
        seed: Validated seed configuration from bernstein.yaml.
        workdir: Project working directory for resolving relative paths.

    Raises:
        SystemExit: If any config-referenced paths are missing or invalid.
    """
    from bernstein.cli.errors import BernsteinError, ExitCode, handle_cli_error

    result = validate_config_paths(seed, workdir)
    if not result.ok:
        raise handle_cli_error(
            BernsteinError(
                what="Config references invalid paths",
                why=f"The following paths in bernstein.yaml are missing or incorrect:\n{result.format_errors()}",
                fix="Create the missing files/directories or update bernstein.yaml to reference valid paths",
                exit_code=ExitCode.CONFIG,
            )
        )

    # Log success for debugging
    validated_count = len(seed.context_files) + (1 if seed.agent_catalog else 0)
    if validated_count > 0:
        logger.debug("Validated %d config path(s)", validated_count)
