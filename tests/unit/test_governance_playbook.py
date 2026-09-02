"""Tests for governance playbook schema and validation (#4979)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bernstein.core.governance.playbook import (
    Ceiling,
    GovernanceClause,
    GovernancePlaybook,
    PlaybookSchema,
    PlaybookValidationError,
    Surface,
    load_playbook,
    load_playbook_from_text,
)

# ---------------------------------------------------------------------------
# Surface tests
# ---------------------------------------------------------------------------


def test_surface_valid() -> None:
    """Valid surface passes validation."""
    s = Surface(
        surface_id="prod-api",
        kind="api_endpoint",
        selector="api.production/*",
        description="Production API",
    )
    assert s.surface_id == "prod-api"
    assert s.kind == "api_endpoint"


def test_surface_id_must_match_pattern() -> None:
    """surface_id must match slug pattern."""
    with pytest.raises(ValueError) as exc:
        Surface(surface_id="Prod-API", kind="k", selector="s")
    assert "surface_id" in str(exc.value)


def test_surface_id_cannot_start_with_digit() -> None:
    with pytest.raises(ValueError):
        Surface(surface_id="1prod", kind="k", selector="s")


# ---------------------------------------------------------------------------
# Ceiling tests
# ---------------------------------------------------------------------------


def test_ceiling_valid() -> None:
    """Valid ceiling passes validation."""
    c = Ceiling(
        ceiling_id="max-cost",
        kind="budget",
        limit="100.00 USD",
        description="Max cost budget",
    )
    assert c.ceiling_id == "max-cost"


def test_ceiling_id_must_match_pattern() -> None:
    with pytest.raises(ValueError) as exc:
        Ceiling(ceiling_id="Max-Cost", kind="k", limit="l")
    assert "ceiling_id" in str(exc.value)


# ---------------------------------------------------------------------------
# GovernanceClause tests
# ---------------------------------------------------------------------------


def test_clause_valid() -> None:
    """Valid clause passes validation."""
    c = GovernanceClause(
        surface_ref="prod-api",
        ceiling_ref="max-cost",
        reason="API calls must not exceed production budget",
    )
    assert c.surface_ref == "prod-api"
    assert c.ceiling_ref == "max-cost"


def test_clause_reason_must_be_non_empty() -> None:
    with pytest.raises(ValueError):
        GovernanceClause(surface_ref="s", ceiling_ref="c", reason="")


def test_clause_reason_max_length() -> None:
    with pytest.raises(ValueError):
        GovernanceClause(surface_ref="s", ceiling_ref="c", reason="x" * 1025)


# ---------------------------------------------------------------------------
# GovernancePlaybook tests
# ---------------------------------------------------------------------------


def test_playbook_minimal() -> None:
    """Minimal playbook with only required fields."""
    p = GovernancePlaybook(
        playbook_id="pb-001",
        name="Test",
        description="A test playbook",
    )
    assert p.playbook_id == "pb-001"
    assert p.version == "1.0.0"
    assert p.surfaces == []
    assert p.ceilings == []
    assert p.clauses == []


def test_playbook_version_validation() -> None:
    """Version must look like semver."""
    with pytest.raises(ValueError):
        GovernancePlaybook(playbook_id="p", name="n", description="d", version="invalid")

    # Valid versions
    GovernancePlaybook(playbook_id="p", name="n", description="d", version="1.0")
    GovernancePlaybook(playbook_id="p", name="n", description="d", version="1.0.0")
    GovernancePlaybook(playbook_id="p", name="n", description="d", version="1.0.0-alpha")


def test_playbook_id_must_match_pattern() -> None:
    with pytest.raises(ValueError):
        GovernancePlaybook(playbook_id="Invalid", name="n", description="d")


# ---------------------------------------------------------------------------
# PlaybookSchema validation tests
# ---------------------------------------------------------------------------


def test_validate_passes_for_valid_playbook() -> None:
    """Schema validation passes for a well-formed playbook."""
    playbook = GovernancePlaybook(
        playbook_id="pb-001",
        name="Production limits",
        description="Budget and access controls",
        surfaces=[
            Surface(surface_id="prod-api", kind="api_endpoint", selector="api.production/*"),
            Surface(surface_id="prod-db", kind="database", selector="db.production.main"),
        ],
        ceilings=[
            Ceiling(ceiling_id="max-cost", kind="budget", limit="100.00 USD"),
            Ceiling(ceiling_id="rate-limit", kind="rate", limit="100 req/min"),
        ],
        clauses=[
            GovernanceClause(
                surface_ref="prod-api",
                ceiling_ref="max-cost",
                reason="API calls must not exceed production budget",
            ),
            GovernanceClause(
                surface_ref="prod-api",
                ceiling_ref="rate-limit",
                reason="Rate limit to prevent overload",
            ),
        ],
    )

    schema = PlaybookSchema()
    schema.validate(playbook)  # Should not raise


def test_validate_fails_on_unknown_surface_ref() -> None:
    """Validation fails when surface_ref doesn't resolve."""
    playbook = GovernancePlaybook(
        playbook_id="pb-001",
        name="Test",
        description="Test",
        surfaces=[Surface(surface_id="prod-api", kind="k", selector="s")],
        ceilings=[Ceiling(ceiling_id="max-cost", kind="k", limit="l")],
        clauses=[
            GovernanceClause(
                surface_ref="unknown-surface",
                ceiling_ref="max-cost",
                reason="Reason",
            ),
        ],
    )

    schema = PlaybookSchema()
    with pytest.raises(PlaybookValidationError) as exc:
        schema.validate(playbook)

    errors = exc.value.errors
    assert any("surface_ref" in field for field, _ in errors)
    assert any("unknown-surface" in msg for _, msg in errors)


def test_validate_fails_on_unknown_ceiling_ref() -> None:
    """Validation fails when ceiling_ref doesn't resolve."""
    playbook = GovernancePlaybook(
        playbook_id="pb-001",
        name="Test",
        description="Test",
        surfaces=[Surface(surface_id="prod-api", kind="k", selector="s")],
        ceilings=[Ceiling(ceiling_id="max-cost", kind="k", limit="l")],
        clauses=[
            GovernanceClause(
                surface_ref="prod-api",
                ceiling_ref="unknown-ceiling",
                reason="Reason",
            ),
        ],
    )

    schema = PlaybookSchema()
    with pytest.raises(PlaybookValidationError) as exc:
        schema.validate(playbook)

    errors = exc.value.errors
    assert any("ceiling_ref" in field for field, _ in errors)
    assert any("unknown-ceiling" in msg for _, msg in errors)


def test_validate_fails_on_duplicate_surface_id() -> None:
    """Validation fails on duplicate surface_id."""
    playbook = GovernancePlaybook(
        playbook_id="pb-001",
        name="Test",
        description="Test",
        surfaces=[
            Surface(surface_id="prod-api", kind="k", selector="s"),
            Surface(surface_id="prod-api", kind="k2", selector="s2"),
        ],
        ceilings=[],
        clauses=[],
    )

    schema = PlaybookSchema()
    with pytest.raises(PlaybookValidationError) as exc:
        schema.validate(playbook)

    errors = exc.value.errors
    assert any("duplicate surface_id" in msg for _, msg in errors)


def test_validate_fails_on_duplicate_ceiling_id() -> None:
    """Validation fails on duplicate ceiling_id."""
    playbook = GovernancePlaybook(
        playbook_id="pb-001",
        name="Test",
        description="Test",
        surfaces=[],
        ceilings=[
            Ceiling(ceiling_id="max-cost", kind="k", limit="l"),
            Ceiling(ceiling_id="max-cost", kind="k2", limit="l2"),
        ],
        clauses=[],
    )

    schema = PlaybookSchema()
    with pytest.raises(PlaybookValidationError) as exc:
        schema.validate(playbook)

    errors = exc.value.errors
    assert any("duplicate ceiling_id" in msg for _, msg in errors)


def test_validate_fails_on_empty_reason() -> None:
    """Validation fails when clause has empty reason."""
    # Pydantic field validator catches this, but let's test the schema too
    playbook = GovernancePlaybook(
        playbook_id="pb-001",
        name="Test",
        description="Test",
        surfaces=[Surface(surface_id="prod-api", kind="k", selector="s")],
        ceilings=[Ceiling(ceiling_id="max-cost", kind="k", limit="l")],
        clauses=[
            GovernanceClause(
                surface_ref="prod-api",
                ceiling_ref="max-cost",
                reason="   ",  # whitespace only
            ),
        ],
    )

    schema = PlaybookSchema()
    with pytest.raises(PlaybookValidationError) as exc:
        schema.validate(playbook)

    errors = exc.value.errors
    assert any("reason" in field for field, _ in errors)


def test_validate_multiple_errors_collected() -> None:
    """All validation errors are collected, not just the first."""
    playbook = GovernancePlaybook(
        playbook_id="pb-001",
        name="Test",
        description="Test",
        surfaces=[
            Surface(surface_id="prod-api", kind="k", selector="s"),
            Surface(surface_id="prod-api", kind="k2", selector="s2"),  # duplicate
        ],
        ceilings=[
            Ceiling(ceiling_id="max-cost", kind="k", limit="l"),
            Ceiling(ceiling_id="max-cost", kind="k2", limit="l2"),  # duplicate
        ],
        clauses=[
            GovernanceClause(
                surface_ref="unknown-surface",
                ceiling_ref="unknown-ceiling",
                reason="Reason",
            ),
        ],
    )

    schema = PlaybookSchema()
    with pytest.raises(PlaybookValidationError) as exc:
        schema.validate(playbook)

    errors = exc.value.errors
    # Should have errors for: duplicate surface, duplicate ceiling, unknown surface ref, unknown ceiling ref
    assert len(errors) >= 4


# ---------------------------------------------------------------------------
# Loader tests
# ---------------------------------------------------------------------------


def test_load_playbook_from_text_valid_yaml() -> None:
    """Load playbook from valid YAML text."""
    yaml_text = """
playbook_id: pb-001
name: "Production limits"
description: "Budget and access controls"
version: "1.0.0"
surfaces:
  - surface_id: prod-api
    kind: api_endpoint
    selector: "api.production/*"
ceilings:
  - ceiling_id: max-cost
    kind: budget
    limit: "100.00 USD"
clauses:
  - surface_ref: prod-api
    ceiling_ref: max-cost
    reason: "API calls must not exceed production budget"
"""
    playbook = load_playbook_from_text(yaml_text)
    assert playbook.playbook_id == "pb-001"
    assert len(playbook.surfaces) == 1
    assert len(playbook.ceilings) == 1
    assert len(playbook.clauses) == 1


def test_load_playbook_from_text_malformed_yaml() -> None:
    """Load fails on malformed YAML."""
    with pytest.raises(PlaybookValidationError) as exc:
        load_playbook_from_text("invalid: yaml: : :")

    errors = exc.value.errors
    assert any("yaml" in field for field, _ in errors)


def test_load_playbook_from_text_not_a_mapping() -> None:
    """Load fails when YAML is not a mapping."""
    with pytest.raises(PlaybookValidationError) as exc:
        load_playbook_from_text("- item1\n- item2")

    errors = exc.value.errors
    assert any("structure" in field for field, _ in errors)


def test_load_playbook_valid_file(tmp_path) -> None:
    """Load playbook from file path."""
    playbook_yaml = """
playbook_id: pb-002
name: "File test"
description: "Loaded from file"
version: "2.0.0"
surfaces: []
ceilings: []
clauses: []
"""
    file_path = tmp_path / "playbook.yaml"
    file_path.write_text(playbook_yaml)

    playbook = load_playbook(file_path)
    assert playbook.playbook_id == "pb-002"
    assert playbook.version == "2.0.0"


def test_load_playbook_missing_file(tmp_path) -> None:
    """Load fails when file doesn't exist."""
    with pytest.raises(PlaybookValidationError) as exc:
        load_playbook(tmp_path / "nonexistent.yaml")

    errors = exc.value.errors
    assert any("file" in field for field, _ in errors)


# ---------------------------------------------------------------------------
# PlaybookValidationError tests
# ---------------------------------------------------------------------------


def test_playbook_validation_error_to_json() -> None:
    """PlaybookValidationError.to_json returns structured errors."""
    err = PlaybookValidationError(
        [
            ("surfaces.foo", "duplicate surface_id 'foo'"),
            ("clauses.0.reason", "reason field must be non-empty"),
        ]
    )

    json_data = err.to_json()
    assert isinstance(json_data, list)
    assert len(json_data) == 2
    assert json_data[0]["field"] == "surfaces.foo"
    assert json_data[0]["message"] == "duplicate surface_id 'foo'"
    assert json_data[1]["field"] == "clauses.0.reason"


# ---------------------------------------------------------------------------
# CLI validation command tests
# ---------------------------------------------------------------------------


def test_cli_validate_valid_playbook(tmp_path) -> None:
    """CLI validate command exits 0 for valid playbook."""
    from click.testing import CliRunner

    from bernstein.cli.commands.governance_cmd import govern_group

    playbook_yaml = """
playbook_id: pb-001
name: "Test"
description: "Valid playbook"
version: "1.0.0"
surfaces:
  - surface_id: prod-api
    kind: api_endpoint
    selector: "api.production/*"
ceilings:
  - ceiling_id: max-cost
    kind: budget
    limit: "100.00 USD"
clauses:
  - surface_ref: prod-api
    ceiling_ref: max-cost
    reason: "API calls must not exceed production budget"
"""
    file_path = tmp_path / "playbook.yaml"
    file_path.write_text(playbook_yaml)

    runner = CliRunner()
    result = runner.invoke(govern_group, ["validate", str(file_path)])

    assert result.exit_code == 0
    assert "OK" in result.output


def test_cli_validate_invalid_playbook(tmp_path) -> None:
    """CLI validate command exits 1 for invalid playbook with JSON errors."""
    from click.testing import CliRunner

    from bernstein.cli.commands.governance_cmd import govern_group

    playbook_yaml = """
playbook_id: pb-001
name: "Test"
description: "Invalid playbook"
version: "1.0.0"
surfaces:
  - surface_id: prod-api
    kind: api_endpoint
    selector: "api.production/*"
ceilings: []
clauses:
  - surface_ref: prod-api
    ceiling_ref: unknown-ceiling
    reason: "Reason"
"""
    file_path = tmp_path / "playbook.yaml"
    file_path.write_text(playbook_yaml)

    runner = CliRunner()
    result = runner.invoke(govern_group, ["validate", str(file_path)])

    assert result.exit_code == 1
    assert "VALIDATION FAILED" in result.output
    assert "unknown-ceiling" in result.output


def test_cli_validate_malformed_yaml(tmp_path) -> None:
    """CLI validate handles malformed YAML gracefully."""
    from click.testing import CliRunner

    from bernstein.cli.commands.governance_cmd import govern_group

    file_path = tmp_path / "playbook.yaml"
    file_path.write_text("invalid: yaml: : :")

    runner = CliRunner()
    result = runner.invoke(govern_group, ["validate", str(file_path)])

    assert result.exit_code == 1
    assert "VALIDATION FAILED" in result.output


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_clause_refs_must_match_id_pattern() -> None:
    """surface_ref and ceiling_ref must match the same ID pattern."""
    with pytest.raises(ValueError):
        GovernanceClause(surface_ref="Invalid-Ref", ceiling_ref="c", reason="r")

    with pytest.raises(ValueError):
        GovernanceClause(surface_ref="s", ceiling_ref="Invalid-Ref", reason="r")


def test_playbook_with_many_surfaces_and_ceilings() -> None:
    """Playbook handles multiple surfaces and ceilings correctly."""
    surfaces = [Surface(surface_id=f"s{i}", kind="k", selector="sel") for i in range(10)]
    ceilings = [Ceiling(ceiling_id=f"c{i}", kind="k", limit="lim") for i in range(5)]
    clauses = [
        GovernanceClause(surface_ref=f"s{i % 10}", ceiling_ref=f"c{i % 5}", reason=f"Reason {i}") for i in range(20)
    ]

    playbook = GovernancePlaybook(
        playbook_id="pb-many",
        name="Many",
        description="Many surfaces and ceilings",
        surfaces=surfaces,
        ceilings=ceilings,
        clauses=clauses,
    )

    schema = PlaybookSchema()
    schema.validate(playbook)  # Should not raise


def test_playbook_extra_fields_forbidden() -> None:
    """Extra fields on models are forbidden."""
    with pytest.raises(ValidationError):
        Surface(surface_id="s", kind="k", selector="s", extra_field="oops")

    with pytest.raises(ValidationError):
        Ceiling(ceiling_id="c", kind="k", limit="l", extra_field="oops")

    with pytest.raises(ValidationError):
        GovernanceClause(surface_ref="s", ceiling_ref="c", reason="r", extra_field="oops")

    with pytest.raises(ValidationError):
        GovernancePlaybook(playbook_id="p", name="n", description="d", extra_field="oops")
