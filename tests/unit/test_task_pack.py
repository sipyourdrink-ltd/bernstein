import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bernstein.core.tasks.task_pack import PackEntry, TaskContextPack


def test_a_pack_rebuilds_byte_identically(tmp_path: Path):
    script = tmp_path / "build_pack.py"
    script.write_text("""
import sys
from bernstein.core.tasks.task_pack import TaskContextPack, PackEntry

pack = TaskContextPack(entries=[
    PackEntry(path="src/main.py", sha256="12345"),
    PackEntry(path="README.md", sha256="abcde")
])
sys.stdout.buffer.write(pack.canonical_bytes())
    """)

    # Ensure the subprocess can find the 'src' directory
    src_dir = str(Path(__file__).resolve().parent.parent.parent / "src")

    env1 = os.environ.copy()
    env1["PYTHONHASHSEED"] = "1"
    env1["PYTHONPATH"] = src_dir

    env2 = os.environ.copy()
    env2["PYTHONHASHSEED"] = "2"
    env2["PYTHONPATH"] = src_dir

    # Run scripts using the exact same python executable (sys.executable)
    run1 = subprocess.run([sys.executable, str(script)], capture_output=True, env=env1, check=True)
    run2 = subprocess.run([sys.executable, str(script)], capture_output=True, env=env2, check=True)

    assert run1.stdout == run2.stdout
    assert b"12345" in run1.stdout


def test_entry_order_does_not_change_bytes():
    a = TaskContextPack(entries=[PackEntry(path="a.py", sha256="1"), PackEntry(path="b.py", sha256="2")])
    b = TaskContextPack(entries=[PackEntry(path="b.py", sha256="2"), PackEntry(path="a.py", sha256="1")])
    assert a.canonical_bytes() == b.canonical_bytes()


def test_duplicate_paths_are_rejected():
    pack = TaskContextPack(entries=[PackEntry(path="a.py", sha256="1"), PackEntry(path="a.py", sha256="2")])
    with pytest.raises(ValueError, match="Duplicate path"):
        pack.canonical_bytes()


def test_canonical_bytes_keeps_the_to_dict_envelope():
    """Sorting must not change the shape the pack serialises to.

    ``to_dict`` is the pack's declared projection. Hashing a different
    shape would leave a consumer that writes ``to_dict`` and verifies
    ``canonical_bytes`` comparing two encodings of the same pack - and the
    existing rebuild test cannot catch it, because it compares two fresh
    builds that would both carry the new shape.
    """
    pack = TaskContextPack(entries=[PackEntry(path="b.py", sha256="2"), PackEntry(path="a.py", sha256="1")])
    assert json.loads(pack.canonical_bytes()) == {
        "entries": [{"path": "a.py", "sha256": "1"}, {"path": "b.py", "sha256": "2"}],
    }
