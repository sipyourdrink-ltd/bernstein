"""Tests for documentation generator."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.doc_generator import (
    DocEntry,
    Documentation,
    extract_docs_from_module,
    generate_docs_for_package,
)


class TestDocEntry:
    """Test DocEntry dataclass."""

    def test_entry_creation(self) -> None:
        """Test creating a doc entry."""
        entry = DocEntry(
            name="test_function",
            kind="function",
            docstring="Test function docstring",
            signature="def test_function() -> None",
            file_path="/test.py",
            line_number=10,
        )

        assert entry.name == "test_function"
        assert entry.kind == "function"
        assert entry.docstring == "Test function docstring"


class TestDocumentation:
    """Test Documentation dataclass."""

    def test_documentation_creation(self) -> None:
        """Test creating documentation."""
        docs = Documentation()

        assert docs.modules == []
        assert docs.classes == []
        assert docs.functions == []

    def test_to_markdown(self) -> None:
        """Test converting to Markdown."""
        docs = Documentation()
        docs.functions.append(
            DocEntry(
                name="test_func",
                kind="function",
                docstring="Test function",
                signature="def test_func() -> None",
            )
        )

        markdown = docs.to_markdown()

        assert "# API Documentation" in markdown
        assert "## Functions" in markdown
        assert "`test_func`" in markdown
        assert "Test function" in markdown


class TestExtractDocsFromModule:
    """Test extract_docs_from_module function."""

    def test_extract_from_test_module(self, tmp_path: Path) -> None:
        """Test extracting docs from a test module."""
        # Create a test module
        module_path = tmp_path / "test_module.py"
        module_path.write_text('''
"""Test module docstring."""

def test_function():
    """Test function docstring."""
    pass

class TestClass:
    """Test class docstring."""
    pass
''')

        docs = extract_docs_from_module(module_path)

        assert len(docs.modules) == 1
        assert "Test module docstring" in (docs.modules[0].docstring or "")
        assert len(docs.functions) == 1
        assert len(docs.classes) == 1

    def test_extract_from_empty_module(self, tmp_path: Path) -> None:
        """Test extracting from empty module."""
        module_path = tmp_path / "empty.py"
        module_path.write_text("")

        docs = extract_docs_from_module(module_path)

        assert len(docs.modules) == 0
        assert len(docs.functions) == 0

    def test_extract_from_nonexistent_file(self, tmp_path: Path) -> None:
        """Test extracting from non-existent file."""
        module_path = tmp_path / "nonexistent.py"

        docs = extract_docs_from_module(module_path)

        assert len(docs.modules) == 0


class TestGenerateDocsForPackage:
    """Test generate_docs_for_package function."""

    def test_generate_for_test_package(self, tmp_path: Path) -> None:
        """Test generating docs for a test package."""
        # Create test package
        package_dir = tmp_path / "testpkg"
        package_dir.mkdir()

        # Create module
        module_path = package_dir / "module.py"
        module_path.write_text('''
"""Module docstring."""

def func():
    """Function docstring."""
    pass
''')

        docs = generate_docs_for_package(package_dir)

        assert len(docs.functions) >= 1
        assert len(docs.modules) >= 1

    def test_generate_with_output_file(self, tmp_path: Path) -> None:
        """Test generating docs with output file."""
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()

        output_path = tmp_path / "docs.md"

        _docs = generate_docs_for_package(package_dir, output_path=output_path)

        assert output_path.exists()
        assert "# API Documentation" in output_path.read_text()
