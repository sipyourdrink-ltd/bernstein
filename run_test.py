from pathlib import Path
from bernstein.core.skills.lifecycle import LockEntry, _read_lock
import os

workdir = Path("tests/unit/skills/project")
print("CWD", os.getcwd())
try:
    print(_read_lock(workdir / "skills.lock"))
except Exception as e:
    print("error", e)
