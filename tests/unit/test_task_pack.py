import os
import subprocess
import sys
from pathlib import Path


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
