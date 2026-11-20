"""Exercise the CI comparison against protoc's nested output layout."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = Path("scripts/generate_proto.sh")
PACKAGE = Path("src/bernstein/core/grpc_gen")


def test_proto_job_checks_with_pinned_generator() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(
        step for step in workflow["jobs"]["proto-drift"]["steps"] if step.get("name") == "Regenerate and compare"
    )
    assert step["run"] == f'uv run --with "grpcio-tools==1.83.0" {GENERATOR.as_posix()} --check'


@pytest.mark.skipif(sys.platform == "win32", reason="the proto workflow runs on Ubuntu")
@pytest.mark.parametrize("drift", ["proto", "missing", "extra"])
def test_proto_drift_requires_matching_shipped_files(tmp_path: Path, drift: str) -> None:
    generator = tmp_path / GENERATOR
    generator.parent.mkdir()
    shutil.copy(ROOT / GENERATOR, generator)
    schema = tmp_path / "proto/bernstein/v1/tasks.proto"
    schema.parent.mkdir(parents=True)
    schema.write_text("version 1")
    package = tmp_path / PACKAGE
    package.mkdir(parents=True)
    init = package / "__init__.py"
    init.write_text("# hand-written exports\n")

    # Exercise the same arguments and destination layout without requiring
    # grpcio-tools in the unit test environment.
    tools = tmp_path / "grpc_tools"
    tools.mkdir()
    (tools / "__init__.py").touch()
    (tools / "protoc.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "flags = dict(arg.split('=', 1) for arg in sys.argv[1:] if '=' in arg)\n"
        "schema = Path(sys.argv[-1])\n"
        "for flag, suffix in [('python', '_pb2.py'), ('pyi', '_pb2.pyi'), ('grpc_python', '_pb2_grpc.py')]:\n"
        "    package = Path(flags['--' + flag + '_out']) / 'bernstein/v1'\n"
        "    package.mkdir(parents=True, exist_ok=True)\n"
        "    (package / (schema.stem + suffix)).write_text('from bernstein.v1 import sibling\\n# ' + schema.read_text())\n"
    )
    (tmp_path / "python").symlink_to(sys.executable)
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}", "PYTHONPATH": str(tmp_path)}

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([generator, *args], env=env, capture_output=True, text=True, timeout=10, check=False)

    # The old generator passed the flat package as protoc's output root.
    # Check and repair must still work in that existing developer checkout.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"--python_out={package}",
            f"--pyi_out={package}",
            f"--grpc_python_out={package}",
            str(schema),
        ],
        env=env,
        check=True,
        timeout=10,
    )
    legacy = package / "bernstein/v1/tasks_pb2.py"
    original = legacy.read_bytes()
    handwritten = legacy.with_name("local_notes.py")
    handwritten.write_text("# local notes\n")

    # The normal developer command must write the flat files consumed by CI.
    run().check_returncode()
    stub = package / "tasks_pb2.pyi"
    assert stub.read_text() == "from . import sibling\n# version 1"
    run("--check").check_returncode()
    if drift == "proto":
        schema.write_text("version 2")
    elif drift == "missing":
        stub.unlink()
    else:
        (package / "retired_pb2.py").write_text("# obsolete")

    assert run("--check").returncode == 1
    run().check_returncode()
    run("--check").check_returncode()
    assert init.read_text() == "# hand-written exports\n"
    assert legacy.read_bytes() == original
    assert handwritten.read_text() == "# local notes\n"
