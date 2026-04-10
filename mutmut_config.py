"""Mutmut configuration for Bernstein mutation testing."""


def init() -> None:
    """Configure mutmut."""


# Paths to mutate — critical modules where test effectiveness matters most
paths_to_mutate = [
    "src/bernstein/core/lifecycle.py",
    "src/bernstein/core/spawner.py",
    "src/bernstein/core/guardrails.py",
    "src/bernstein/core/models.py",
    "src/bernstein/core/task_store.py",
    "src/bernstein/core/config_schema.py",
    "src/bernstein/adapters/base.py",
]

# Test runner command
test_command = "python -m pytest tests/unit/ -x -q --no-header --override-ini=addopts="

# Tests directory
tests_dir = "tests/unit/"
