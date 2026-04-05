"""Tests for the API backward-compatibility checker."""

from __future__ import annotations

from bernstein.core.api_compat_checker import (
    Addition,
    BreakingChange,
    ChangeType,
    CompatReport,
    check_compatibility,
)

# ---------------------------------------------------------------------------
# Removing a function → breaking
# ---------------------------------------------------------------------------


class TestRemovedFunction:
    def test_removed_public_function_is_breaking(self) -> None:
        old = """\
def greet(name: str) -> str:
    return f"Hello {name}"

def farewell(name: str) -> str:
    return f"Goodbye {name}"
"""
        new = """\
def greet(name: str) -> str:
    return f"Hello {name}"
"""
        report = check_compatibility(old, new, "mod.py")
        assert not report.is_compatible
        assert len(report.breaking_changes) == 1
        bc = report.breaking_changes[0]
        assert bc.change_type == ChangeType.REMOVED_FUNCTION
        assert bc.name == "farewell"
        assert bc.file == "mod.py"

    def test_removed_method_is_breaking(self) -> None:
        old = """\
class Service:
    def start(self) -> None: ...
    def stop(self) -> None: ...
"""
        new = """\
class Service:
    def start(self) -> None: ...
"""
        report = check_compatibility(old, new, "svc.py")
        assert not report.is_compatible
        assert len(report.breaking_changes) == 1
        bc = report.breaking_changes[0]
        assert bc.change_type == ChangeType.REMOVED_METHOD
        assert bc.name == "Service.stop"


# ---------------------------------------------------------------------------
# Removing a parameter → breaking
# ---------------------------------------------------------------------------


class TestRemovedParameter:
    def test_removed_parameter_is_breaking(self) -> None:
        old = """\
def connect(host: str, port: int, timeout: int) -> None: ...
"""
        new = """\
def connect(host: str, port: int) -> None: ...
"""
        report = check_compatibility(old, new, "net.py")
        assert not report.is_compatible
        assert len(report.breaking_changes) == 1
        bc = report.breaking_changes[0]
        assert bc.change_type == ChangeType.REMOVED_PARAMETER
        assert "timeout" in bc.description

    def test_removed_method_parameter_is_breaking(self) -> None:
        old = """\
class Client:
    def send(self, data: bytes, retries: int) -> None: ...
"""
        new = """\
class Client:
    def send(self, data: bytes) -> None: ...
"""
        report = check_compatibility(old, new, "client.py")
        assert not report.is_compatible
        bc = report.breaking_changes[0]
        assert bc.change_type == ChangeType.REMOVED_PARAMETER
        assert "retries" in bc.description


# ---------------------------------------------------------------------------
# Adding optional parameter → NOT breaking
# ---------------------------------------------------------------------------


class TestAddOptionalParam:
    def test_new_optional_param_is_not_breaking(self) -> None:
        old = """\
def fetch(url: str) -> bytes: ...
"""
        new = """\
def fetch(url: str, timeout: int = 30) -> bytes: ...
"""
        report = check_compatibility(old, new, "http.py")
        assert report.is_compatible
        assert len(report.breaking_changes) == 0

    def test_new_kwonly_optional_param_is_not_breaking(self) -> None:
        old = """\
def fetch(url: str) -> bytes: ...
"""
        new = """\
def fetch(url: str, *, timeout: int = 30) -> bytes: ...
"""
        report = check_compatibility(old, new, "http.py")
        assert report.is_compatible


# ---------------------------------------------------------------------------
# Changing private function → NOT breaking
# ---------------------------------------------------------------------------


class TestPrivateIgnored:
    def test_removed_private_function_not_breaking(self) -> None:
        old = """\
def _internal_helper(x: int) -> int:
    return x * 2

def public_api() -> None: ...
"""
        new = """\
def public_api() -> None: ...
"""
        report = check_compatibility(old, new, "mod.py")
        assert report.is_compatible

    def test_removed_private_method_not_breaking(self) -> None:
        old = """\
class Processor:
    def run(self) -> None: ...
    def _reset(self) -> None: ...
"""
        new = """\
class Processor:
    def run(self) -> None: ...
"""
        report = check_compatibility(old, new, "proc.py")
        assert report.is_compatible

    def test_private_class_removal_not_breaking(self) -> None:
        old = """\
class _InternalCache:
    pass

class PublicAPI:
    pass
"""
        new = """\
class PublicAPI:
    pass
"""
        report = check_compatibility(old, new, "cache.py")
        assert report.is_compatible


# ---------------------------------------------------------------------------
# Adding new function / class → NOT breaking
# ---------------------------------------------------------------------------


class TestAdditions:
    def test_new_function_is_not_breaking(self) -> None:
        old = """\
def existing() -> None: ...
"""
        new = """\
def existing() -> None: ...

def brand_new(x: int) -> str: ...
"""
        report = check_compatibility(old, new, "mod.py")
        assert report.is_compatible
        assert len(report.additions) == 1
        assert report.additions[0].name == "brand_new"
        assert report.additions[0].kind == "function"

    def test_new_class_is_not_breaking(self) -> None:
        old = """\
class Alpha:
    pass
"""
        new = """\
class Alpha:
    pass

class Beta:
    pass
"""
        report = check_compatibility(old, new, "mod.py")
        assert report.is_compatible
        assert any(a.name == "Beta" and a.kind == "class" for a in report.additions)

    def test_new_method_is_not_breaking(self) -> None:
        old = """\
class Service:
    def start(self) -> None: ...
"""
        new = """\
class Service:
    def start(self) -> None: ...
    def status(self) -> str: ...
"""
        report = check_compatibility(old, new, "svc.py")
        assert report.is_compatible
        assert any(a.name == "Service.status" and a.kind == "method" for a in report.additions)


# ---------------------------------------------------------------------------
# Removed class → breaking
# ---------------------------------------------------------------------------


class TestRemovedClass:
    def test_removed_class_is_breaking(self) -> None:
        old = """\
class Config:
    pass

class Server:
    pass
"""
        new = """\
class Server:
    pass
"""
        report = check_compatibility(old, new, "mod.py")
        assert not report.is_compatible
        assert len(report.breaking_changes) == 1
        bc = report.breaking_changes[0]
        assert bc.change_type == ChangeType.REMOVED_CLASS
        assert bc.name == "Config"


# ---------------------------------------------------------------------------
# Changed parameter type → breaking
# ---------------------------------------------------------------------------


class TestChangedParamType:
    def test_changed_type_annotation_is_breaking(self) -> None:
        old = """\
def process(data: str) -> None: ...
"""
        new = """\
def process(data: bytes) -> None: ...
"""
        report = check_compatibility(old, new, "mod.py")
        assert not report.is_compatible
        bc = report.breaking_changes[0]
        assert bc.change_type == ChangeType.CHANGED_PARAM_TYPE
        assert "str" in bc.description
        assert "bytes" in bc.description

    def test_no_annotation_to_annotation_not_breaking(self) -> None:
        """Adding a type annotation where there was none is not a breaking change."""
        old = """\
def process(data) -> None: ...
"""
        new = """\
def process(data: str) -> None: ...
"""
        report = check_compatibility(old, new, "mod.py")
        assert report.is_compatible


# ---------------------------------------------------------------------------
# Changed parameter position → breaking
# ---------------------------------------------------------------------------


class TestChangedParamPosition:
    def test_reordered_required_params_is_breaking(self) -> None:
        old = """\
def connect(host: str, port: int) -> None: ...
"""
        new = """\
def connect(port: int, host: str) -> None: ...
"""
        report = check_compatibility(old, new, "mod.py")
        assert not report.is_compatible
        # Both parameters moved, so two breaking changes
        position_changes = [
            bc for bc in report.breaking_changes if bc.change_type == ChangeType.CHANGED_PARAM_POSITION
        ]
        assert len(position_changes) == 2


# ---------------------------------------------------------------------------
# CompatReport dataclass
# ---------------------------------------------------------------------------


class TestCompatReport:
    def test_empty_report_is_compatible(self) -> None:
        report = CompatReport()
        assert report.is_compatible
        assert len(report.breaking_changes) == 0
        assert len(report.additions) == 0

    def test_report_with_only_additions_is_compatible(self) -> None:
        report = CompatReport(
            additions=[Addition(file="mod.py", name="new_func", kind="function")],
        )
        assert report.is_compatible

    def test_report_with_breaking_change_not_compatible(self) -> None:
        report = CompatReport(
            breaking_changes=[
                BreakingChange(
                    file="mod.py",
                    name="old_func",
                    change_type=ChangeType.REMOVED_FUNCTION,
                    description="removed",
                )
            ],
        )
        assert not report.is_compatible


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_sources_no_changes(self) -> None:
        report = check_compatibility("", "", "empty.py")
        assert report.is_compatible

    def test_syntax_error_in_old_source(self) -> None:
        report = check_compatibility("def broken(:", "def fine() -> None: ...", "bad.py")
        assert report.is_compatible  # gracefully returns empty report

    def test_syntax_error_in_new_source(self) -> None:
        report = check_compatibility("def fine() -> None: ...", "def broken(:", "bad.py")
        assert report.is_compatible

    def test_async_functions_tracked(self) -> None:
        old = """\
async def handler(request: str) -> str: ...
"""
        new = """\
pass
"""
        report = check_compatibility(old, new, "api.py")
        assert not report.is_compatible
        assert report.breaking_changes[0].change_type == ChangeType.REMOVED_FUNCTION
        assert report.breaking_changes[0].name == "handler"
