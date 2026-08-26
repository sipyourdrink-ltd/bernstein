"""Test to ensure no orphan modules exist in src/bernstein/core/tokens/."""

import ast
from pathlib import Path

def _get_token_modules() -> list[str]:
    """Return list of module names (without .py) in src/bernstein/core/tokens/."""
    token_dir = Path("src/bernstein/core/tokens")
    return [f.stem for f in token_dir.glob("*.py") if f.name != "__init__.py"]

def _is_module_used(module_name: str, src_files: list[Path]) -> bool:
    """Check if module is imported anywhere in src files."""
    for src_file in src_files:
        # Skip the module's own file
        if src_file == Path("src/bernstein/core/tokens") / f"{module_name}.py":
            continue
        try:
            tree = ast.parse(src_file.read_text())
        except SyntaxError:
            # If we can't parse, skip the file
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # Check for relative or absolute imports from the tokens package
                if node.module:
                    # Normalize module names to check for matches
                    parts = node.module.split('.')
                    # Check if the module is the last part of a tokens import
                    if len(parts) >= 2 and parts[-2:] == ['tokens', module_name]:
                        return True
                    # Also check for relative imports like from . import module_name
                    if node.level > 0 and node.module == '' and module_name in [alias.name for alias in node.names]:
                        return True
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith(f'.tokens.{module_name}'):
                        return True
    return False

def test_no_orphan_token_modules():
    """Fail if any module in src/bernstein/core/tokens/ is not used."""
    token_dir = Path("src/bernstein/core/tokens")
    module_names = _get_token_modules()
    # Collect all Python source files under src/
    src_files = list(Path("src").rglob("*.py"))
    orphans = []
    for module_name in module_names:
        if not _is_module_used(module_name, src_files):
            orphans.append(module_name)
    assert not orphans, f"Orphan token modules detected: {orphans}. These modules are not imported anywhere in the src tree and should be removed or marked for deletion."

if __name__ == "__main__":
    test_no_orphan_token_modules()