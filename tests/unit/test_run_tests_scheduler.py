from pathlib import Path
from scripts.run_tests import split_memory_heavy, MEMORY_HEAVY_FILES

def test_split_memory_heavy_separates_heavy_files():
    files = [
        Path("tests/integration/test_standalone_receipt_verifier.py"),
        Path("tests/unit/test_foo.py"),
        Path("tests/unit/test_bar.py"),
    ]
    normal, heavy = split_memory_heavy(files)

    assert len(heavy) == 1
    assert heavy[0].name == "test_standalone_receipt_verifier.py"
    assert len(normal) == 2
    assert "test_standalone_receipt_verifier.py" not in [f.name for f in normal]

def test_no_heavy_files():
    files = [Path("tests/unit/test_foo.py")]
    normal, heavy = split_memory_heavy(files)
    assert len(heavy) == 0
    assert len(normal) == 1
