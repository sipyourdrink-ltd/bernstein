"""Configurable review checklist for pre-merge validation."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

ChecklistCategory = Literal["naming", "error_handling", "logging", "tests", "security", "performance", "documentation"]
_DEFAULT_CATEGORY: ChecklistCategory = "naming"


def _empty_checklist_items() -> list[ChecklistItem]:
    """Return a typed empty checklist item list."""
    return []


@dataclass
class ChecklistItem:
    """A single checklist item for code review."""

    id: str
    category: ChecklistCategory
    description: str
    required: bool = True
    auto_check: bool = False  # Can be automatically verified


@dataclass
class ReviewChecklist:
    """Configurable review checklist for pre-merge validation."""

    items: list[ChecklistItem] = field(default_factory=_empty_checklist_items)

    @classmethod
    def default(cls) -> ReviewChecklist:
        """Return default review checklist."""
        return cls(
            items=[
                ChecklistItem(
                    id="naming-001",
                    category="naming",
                    description="Function and variable names are descriptive and follow project conventions",
                    required=True,
                    auto_check=False,
                ),
                ChecklistItem(
                    id="error-001",
                    category="error_handling",
                    description="All exceptions are caught and handled appropriately",
                    required=True,
                    auto_check=False,
                ),
                ChecklistItem(
                    id="error-002",
                    category="error_handling",
                    description="Error messages are informative and actionable",
                    required=True,
                    auto_check=False,
                ),
                ChecklistItem(
                    id="logging-001",
                    category="logging",
                    description="Key operations are logged at appropriate levels",
                    required=True,
                    auto_check=False,
                ),
                ChecklistItem(
                    id="logging-002",
                    category="logging",
                    description="No sensitive data is logged",
                    required=True,
                    auto_check=False,
                ),
                ChecklistItem(
                    id="tests-001",
                    category="tests",
                    description="New code includes unit tests",
                    required=True,
                    auto_check=False,
                ),
                ChecklistItem(
                    id="tests-002",
                    category="tests",
                    description="Existing tests pass",
                    required=True,
                    auto_check=True,  # Can be auto-verified
                ),
                ChecklistItem(
                    id="security-001",
                    category="security",
                    description="No hardcoded credentials or secrets",
                    required=True,
                    auto_check=False,
                ),
                ChecklistItem(
                    id="security-002",
                    category="security",
                    description="Input validation is performed on user input",
                    required=True,
                    auto_check=False,
                ),
                ChecklistItem(
                    id="perf-001",
                    category="performance",
                    description="No obvious performance issues (e.g., N+1 queries)",
                    required=False,
                    auto_check=False,
                ),
                ChecklistItem(
                    id="docs-001",
                    category="documentation",
                    description="Public APIs are documented",
                    required=True,
                    auto_check=False,
                ),
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize checklist to dictionary."""
        return {
            "items": [
                {
                    "id": item.id,
                    "category": item.category,
                    "description": item.description,
                    "required": item.required,
                    "auto_check": item.auto_check,
                }
                for item in self.items
            ]
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewChecklist:
        """Deserialize checklist from dictionary."""
        raw_items = data.get("items", [])
        if not isinstance(raw_items, list):
            return cls()

        items: list[ChecklistItem] = []
        for raw_item in cast("list[object]", raw_items):
            if not isinstance(raw_item, Mapping):
                continue
            item_data = cast("Mapping[str, object]", raw_item)
            items.append(
                ChecklistItem(
                    id=str(item_data.get("id", "")),
                    category=_parse_category(item_data.get("category")),
                    description=str(item_data.get("description", "")),
                    required=bool(item_data.get("required", True)),
                    auto_check=bool(item_data.get("auto_check", False)),
                )
            )
        return cls(items=items)

    def load_from_file(self, path: Path) -> None:
        """Load checklist from YAML/JSON file."""
        if not path.exists():
            logger.warning("Checklist file not found: %s", path)
            return

        try:
            if path.suffix == ".json":
                data = json.loads(path.read_text(encoding="utf-8"))
            else:
                # Try YAML
                import yaml

                data = yaml.safe_load(path.read_text(encoding="utf-8"))

            loaded = self.from_dict(data)
            self.items = loaded.items
            logger.info("Loaded review checklist from %s", path)
        except Exception as exc:
            logger.error("Failed to load checklist: %s", exc)

    def save_to_file(self, path: Path) -> None:
        """Save checklist to YAML/JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_dict()

        if path.suffix == ".json":
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        else:
            import yaml

            path.write_text(yaml.dump(data), encoding="utf-8")

        logger.info("Saved review checklist to %s", path)


@dataclass
class ReviewResult:
    """Result of a review checklist check."""

    item_id: str
    passed: bool
    auto_checked: bool
    notes: str = ""


def run_review_checklist(
    workdir: Path,
    checklist: ReviewChecklist | None = None,
    diff_text: str = "",
) -> list[ReviewResult]:
    """Run review checklist against code changes.

    Args:
        workdir: Project working directory.
        checklist: Review checklist to use. Defaults to default checklist.
        diff_text: Git diff text for auto-checking.

    Returns:
        List of ReviewResult for each checklist item.
    """
    if checklist is None:
        checklist = ReviewChecklist.default()

    results: list[ReviewResult] = []

    for item in checklist.items:
        # Auto-check items that support it
        if item.auto_check and diff_text:
            passed, notes = _auto_check_item(item, diff_text, workdir)
            results.append(
                ReviewResult(
                    item_id=item.id,
                    passed=passed,
                    auto_checked=True,
                    notes=notes,
                )
            )
        else:
            # Manual check required
            results.append(
                ReviewResult(
                    item_id=item.id,
                    passed=False,  # Requires manual review
                    auto_checked=False,
                    notes="Requires manual review",
                )
            )

    return results


def _parse_category(value: object) -> ChecklistCategory:
    """Normalize a serialized checklist category value."""
    if value in {
        "naming",
        "error_handling",
        "logging",
        "tests",
        "security",
        "performance",
        "documentation",
    }:
        return cast("ChecklistCategory", value)
    return _DEFAULT_CATEGORY


def _auto_check_item(
    item: ChecklistItem,
    diff_text: str,
    workdir: Path,
) -> tuple[bool, str]:
    """Auto-check a single checklist item.

    Args:
        item: Checklist item to check.
        diff_text: Git diff text.
        workdir: Project working directory.

    Returns:
        Tuple of (passed, notes).
    """
    # Template: Implement auto-check logic for specific items
    # Examples:

    if item.id == "tests-002":
        # Check if test files were modified and tests pass
        if "test_" in diff_text or "_test." in diff_text:
            # Would run tests here
            return True, "Tests exist for changes"
        return False, "No test changes detected"

    if item.id == "logging-002":
        # Check for common sensitive data patterns in added lines
        sensitive_patterns = ["password", "secret", "api_key", "token"]
        for line in diff_text.split("\n"):
            if line.startswith("+"):
                for pattern in sensitive_patterns:
                    if pattern.lower() in line.lower():
                        return False, f"Potential sensitive data: {pattern}"
        return True, "No sensitive data detected"

    # Default: cannot auto-check
    return False, "Auto-check not implemented for this item"
