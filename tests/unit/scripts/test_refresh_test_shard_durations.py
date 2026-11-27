import importlib
from pathlib import Path
from zipfile import ZipFile

import pytest


def test_imports_repo_relative_shard_timings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    refresh = importlib.import_module("refresh_test_shard_durations")
    monkeypatch.setattr(refresh, "REPO_ROOT", tmp_path)

    test_root = tmp_path / "tests" / "unit"
    (test_root / "nested").mkdir(parents=True)
    measured = "tests/unit/nested/test_shared.py"
    legacy = "tests/unit/test_unique.py"
    for relative_path in (measured, "tests/unit/test_shared.py", legacy):
        (tmp_path / relative_path).touch()

    archive = tmp_path / "jobs.zip"
    with ZipFile(archive, "w") as logs:
        logs.writestr(
            "ubuntu-shard-1/job.txt",
            f"PASS [1/3] {measured} (2.0s)\nPASS [2/3] {measured} (4.0s)\nPASS [3/3] test_unique.py (5.0s)\n",
        )

    samples = refresh.harvest_basename_durations(archive)
    assert refresh.map_basenames_to_paths(samples, test_root) == {measured: 3.0, legacy: 5.0}
