"""Tests for API backward compatibility checker.

Verifies that compare_signatures detects breaking changes in function/method
interfaces, and that the Bernstein task server public API contract is stable.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Unit tests for compare_signatures helper
# ---------------------------------------------------------------------------


def test_identical_signatures_are_compatible() -> None:
    """Two identical signatures produce no breaking changes."""
    from bernstein.core.api_compat import SignatureChange, compare_signatures

    def fn(a: int, b: str = "x") -> bool: ...

    result: list[SignatureChange] = compare_signatures(fn, fn)
    assert result == []


def test_adding_optional_parameter_is_compatible() -> None:
    """Adding a new parameter with a default value is backward-compatible."""
    from bernstein.core.api_compat import compare_signatures

    def before(a: int) -> None: ...
    def after(a: int, b: str = "new") -> None: ...

    changes = compare_signatures(before, after)
    assert changes == [], f"Expected no breaking changes, got: {changes}"


def test_removing_required_parameter_is_breaking() -> None:
    """Removing a required parameter is a breaking change."""
    from bernstein.core.api_compat import ChangeKind, compare_signatures

    def before(a: int, b: str) -> None: ...
    def after(a: int) -> None: ...

    changes = compare_signatures(before, after)
    breaking = [c for c in changes if c.kind == ChangeKind.REMOVED_REQUIRED_PARAM]
    assert len(breaking) == 1
    assert breaking[0].param_name == "b"


def test_adding_required_parameter_is_breaking() -> None:
    """Adding a new required parameter (no default) is a breaking change."""
    from bernstein.core.api_compat import ChangeKind, compare_signatures

    def before(a: int) -> None: ...
    def after(a: int, b: str) -> None: ...

    changes = compare_signatures(before, after)
    breaking = [c for c in changes if c.kind == ChangeKind.ADDED_REQUIRED_PARAM]
    assert len(breaking) == 1
    assert breaking[0].param_name == "b"


def test_making_param_required_is_breaking() -> None:
    """Changing a parameter from optional to required is a breaking change."""
    from bernstein.core.api_compat import ChangeKind, compare_signatures

    def before(a: int, b: str = "default") -> None: ...
    def after(a: int, b: str) -> None: ...  # b is now required

    changes = compare_signatures(before, after)
    breaking = [c for c in changes if c.kind == ChangeKind.PARAM_BECAME_REQUIRED]
    assert len(breaking) == 1
    assert breaking[0].param_name == "b"


def test_renaming_positional_param_is_breaking() -> None:
    """Renaming a positional parameter is a breaking change for keyword callers."""
    from bernstein.core.api_compat import ChangeKind, compare_signatures

    def before(old_name: int) -> None: ...
    def after(new_name: int) -> None: ...

    changes = compare_signatures(before, after)
    kinds = {c.kind for c in changes}
    # Rename shows up as removed old + added new
    assert ChangeKind.REMOVED_REQUIRED_PARAM in kinds or ChangeKind.ADDED_REQUIRED_PARAM in kinds


def test_no_breaking_change_when_making_param_optional() -> None:
    """Making a required parameter optional is backward-compatible."""
    from bernstein.core.api_compat import compare_signatures

    def before(a: int, b: str) -> None: ...
    def after(a: int, b: str = "default") -> None: ...

    changes = compare_signatures(before, after)
    assert changes == [], f"Making param optional should not break callers: {changes}"


# ---------------------------------------------------------------------------
# Contract tests: Bernstein task server public API fields
# ---------------------------------------------------------------------------


def test_task_create_required_fields_stable() -> None:
    """TaskCreate keeps the minimal required fields callers depend on."""
    from bernstein.core.server import TaskCreate

    fields = TaskCreate.model_fields
    # title and description are required (no default)
    assert "title" in fields, "TaskCreate must have 'title' field"
    assert "description" in fields, "TaskCreate must have 'description' field"
    assert fields["title"].is_required(), "TaskCreate.title must remain required"
    assert fields["description"].is_required(), "TaskCreate.description must remain required"


def test_task_response_fields_stable() -> None:
    """TaskResponse keeps the fields that all consumers expect."""
    from bernstein.core.server import TaskResponse

    required_fields = {"id", "title", "status", "role", "priority"}
    actual_fields = set(TaskResponse.model_fields)
    missing = required_fields - actual_fields
    assert not missing, f"TaskResponse is missing expected fields: {missing}"


def test_task_complete_request_result_summary_required() -> None:
    """TaskCompleteRequest.result_summary must remain required."""
    from bernstein.core.server import TaskCompleteRequest

    fields = TaskCompleteRequest.model_fields
    assert "result_summary" in fields
    assert fields["result_summary"].is_required(), "result_summary must not get a default value"


def test_heartbeat_request_fields_stable() -> None:
    """HeartbeatRequest retains role and status fields."""
    from bernstein.core.server import HeartbeatRequest

    fields = HeartbeatRequest.model_fields
    assert "role" in fields
    assert "status" in fields


def test_task_fail_request_fields_stable() -> None:
    """TaskFailRequest retains reason field with a default."""
    from bernstein.core.server import TaskFailRequest

    fields = TaskFailRequest.model_fields
    assert "reason" in fields
    # reason should have a default (empty string) — changing this to required is breaking
    assert not fields["reason"].is_required(), "TaskFailRequest.reason must keep its default"


def test_status_response_fields_stable() -> None:
    """StatusResponse keeps aggregate count fields."""
    from bernstein.core.server import StatusResponse

    required_fields = {"total", "open", "claimed", "done", "failed", "per_role"}
    missing = required_fields - set(StatusResponse.model_fields)
    assert not missing, f"StatusResponse is missing expected fields: {missing}"


# ---------------------------------------------------------------------------
# Parameter kind change detection (PARAM_BECAME_KEYWORD_ONLY,
# PARAM_BECAME_POSITIONAL_ONLY)
# ---------------------------------------------------------------------------


def test_positional_to_keyword_only_is_breaking() -> None:
    """Turning a positional-or-keyword param into keyword-only breaks positional callers."""
    from bernstein.core.api_compat import ChangeKind, compare_signatures

    def before(a: int, b: str) -> None: ...
    def after(a: int, *, b: str) -> None: ...  # b now keyword-only

    changes = compare_signatures(before, after)
    kinds = {c.kind for c in changes}
    assert ChangeKind.PARAM_BECAME_KEYWORD_ONLY in kinds
    breaking = [c for c in changes if c.kind == ChangeKind.PARAM_BECAME_KEYWORD_ONLY]
    assert breaking[0].param_name == "b"


def test_keyword_only_to_positional_only_is_breaking() -> None:
    """Turning a keyword-accessible param into positional-only breaks keyword callers."""
    from bernstein.core.api_compat import ChangeKind, compare_signatures

    # Python doesn't allow direct declaration like this in a simple def,
    # so we use inspect.Parameter directly to build synthetic signatures.
    import inspect

    # before: def f(a: int, b: str) — b is POSITIONAL_OR_KEYWORD
    before_params = [
        inspect.Parameter("a", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=int),
        inspect.Parameter("b", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str),
    ]
    before_sig = inspect.Signature(before_params)

    # after: def f(a: int, b: str, /) — b is POSITIONAL_ONLY
    after_params = [
        inspect.Parameter("a", inspect.Parameter.POSITIONAL_ONLY, annotation=int),
        inspect.Parameter("b", inspect.Parameter.POSITIONAL_ONLY, annotation=str),
    ]
    after_sig = inspect.Signature(after_params)

    # We test via a pair of callables whose __signature__ we can patch.
    class _Before:
        __signature__ = before_sig

    class _After:
        __signature__ = after_sig

    changes = compare_signatures(_Before, _After)
    kinds = {c.kind for c in changes}
    assert ChangeKind.PARAM_BECAME_POSITIONAL_ONLY in kinds


def test_adding_keyword_only_optional_param_is_compatible() -> None:
    """Adding a new keyword-only parameter with a default is backward-compatible."""
    from bernstein.core.api_compat import compare_signatures

    def before(a: int) -> None: ...
    def after(a: int, *, extra: str = "default") -> None: ...

    changes = compare_signatures(before, after)
    assert changes == [], f"Expected no breaking changes, got: {changes}"


def test_adding_keyword_only_required_param_is_breaking() -> None:
    """Adding a new keyword-only required parameter is a breaking change."""
    from bernstein.core.api_compat import ChangeKind, compare_signatures

    def before(a: int) -> None: ...
    def after(a: int, *, new_required: str) -> None: ...

    changes = compare_signatures(before, after)
    kinds = {c.kind for c in changes}
    assert ChangeKind.ADDED_REQUIRED_PARAM in kinds
    breaking = [c for c in changes if c.kind == ChangeKind.ADDED_REQUIRED_PARAM]
    assert breaking[0].param_name == "new_required"


def test_all_change_kinds_are_represented() -> None:
    """All ChangeKind variants are present in the enum."""
    from bernstein.core.api_compat import ChangeKind

    names = {ck.value for ck in ChangeKind}
    assert "removed_required_param" in names
    assert "added_required_param" in names
    assert "param_became_required" in names
    assert "param_became_keyword_only" in names
    assert "param_became_positional_only" in names
