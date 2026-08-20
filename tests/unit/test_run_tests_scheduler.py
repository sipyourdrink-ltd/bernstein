from pathlib import Path

from scripts.run_tests import split_memory_heavy


def test_split_memory_heavy_separates_heavy_files():
    files = [
        Path("tests/integration/test_standalone_receipt_verifier.py"),
        Path("tests/integration/volunteer/test_volunteer_sandbox_egress.py"),
        Path("tests/unit/test_foo.py"),
        Path("tests/unit/test_bar.py"),
    ]
    normal, heavy = split_memory_heavy(files)

    assert len(heavy) == 2
    assert "test_standalone_receipt_verifier.py" in [f.name for f in heavy]
    assert "test_volunteer_sandbox_egress.py" in [f.name for f in heavy]
    assert len(normal) == 2
    assert "test_standalone_receipt_verifier.py" not in [f.name for f in normal]


def test_no_heavy_files():
    files = [Path("tests/unit/test_foo.py")]
    normal, heavy = split_memory_heavy(files)
    assert len(heavy) == 0
    assert len(normal) == 1
