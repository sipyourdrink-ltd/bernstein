"""Auto-generate documentation from codebase and docstrings."""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DocEntry:
    """Single documentation entry."""

    name: str
    kind: str  # "module", "class", "function", "method"
    docstring: str | None
    signature: str | None = None
    file_path: str | None = None
    line_number: int | None = None


@dataclass
class Documentation:
    """Generated documentation collection."""

    modules: list[DocEntry] = field(default_factory=list)
    classes: list[DocEntry] = field(default_factory=list)
    functions: list[DocEntry] = field(default_factory=list)

    def to_markdown(self) -> str:
        """Convert documentation to Markdown format."""
        lines = ["# API Documentation", "", "Auto-generated from source code.", ""]

        # Modules
        if self.modules:
            lines.append("## Modules")
            lines.append("")
            for module in self.modules:
                lines.append(f"### `{module.name}`")
                lines.append("")
                if module.docstring:
                    lines.append(module.docstring)
                    lines.append("")

        # Classes
        if self.classes:
            lines.append("## Classes")
            lines.append("")
            for cls in self.classes:
                lines.append(f"### `{cls.name}`")
                lines.append("")
                if cls.docstring:
                    lines.append(cls.docstring)
                    lines.append("")
                if cls.signature:
                    lines.append(f"```python")
                    lines.append(cls.signature)
                    lines.append(f"```")
                    lines.append("")

        # Functions
        if self.functions:
            lines.append("## Functions")
            lines.append("")
            for func in self.functions:
                lines.append(f"### `{func.name}`")
                lines.append("")
                if func.docstring:
                    lines.append(func.docstring)
                    lines.append("")
                if func.signature:
                    lines.append(f"```python")
                    lines.append(func.signature)
                    lines.append(f"```")
                    lines.append("")

        return "\n".join(lines)


def extract_docs_from_module(module_path: Path) -> Documentation:
    """Extract documentation from a Python module.

    Args:
        module_path: Path to Python module.

    Returns:
        Documentation with extracted entries.
    """
    docs = Documentation()

    try:
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return docs

    module_name = module_path.stem

    # Module docstring
    module_doc = ast.get_docstring(tree)
    if module_doc:
        docs.modules.append(
            DocEntry(
                name=module_name,
                kind="module",
                docstring=module_doc,
                file_path=str(module_path),
            )
        )

    # Extract classes and functions
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            class_doc = ast.get_docstring(node)
            docs.classes.append(
                DocEntry(
                    name=node.name,
                    kind="class",
                    docstring=class_doc,
                    file_path=str(module_path),
                    line_number=node.lineno,
                )
            )

        elif isinstance(node, ast.FunctionDef):
            func_doc = ast.get_docstring(node)
            # Get signature
            try:
                sig = inspect.signature(node)
                signature = f"def {node.name}{sig}"
            except (ValueError, TypeError):
                signature = None

            docs.functions.append(
                DocEntry(
                    name=node.name,
                    kind="function",
                    docstring=func_doc,
                    signature=signature,
                    file_path=str(module_path),
                    line_number=node.lineno,
                )
            )

    return docs


def generate_docs_for_package(
    package_dir: Path,
    output_path: Path | None = None,
) -> Documentation:
    """Generate documentation for entire package.

    Args:
        package_dir: Path to package directory.
        output_path: Optional path to save Markdown output.

    Returns:
        Documentation with all extracted entries.
    """
    all_docs = Documentation()

    # Find all Python modules
    for py_file in package_dir.glob("*.py"):
        if py_file.name.startswith("_"):
            continue

        module_docs = extract_docs_from_module(py_file)
        all_docs.modules.extend(module_docs.modules)
        all_docs.classes.extend(module_docs.classes)
        all_docs.functions.extend(module_docs.functions)

    # Save to file if requested
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(all_docs.to_markdown())

    return all_docs
