# Commands — bernstein

`declared` = written in a task runner or manifest. `inferred` = deduced from config; agent-docs never executes commands to confirm.

| ID | Command | Kind | Description | Anchor |
|---|---|---|---|---|
| bernstein | `bernstein` | declared | console entry point | `pyproject.toml:342` |
| bernstein-bench | `bernstein-bench` | declared | console entry point | `pyproject.toml:344` |
| bernstein-worker | `bernstein-worker` | declared | console entry point | `pyproject.toml:343` |
| verify-audit-receipt | `verify-audit-receipt` | declared | console entry point | `pyproject.toml:345` |
| lint | `ruff check .` | inferred | ruff configured | `pyproject.toml:552` |
| test | `pytest` | inferred | pytest configured | `pyproject.toml:796` |
| typecheck | `mypy .` | inferred | mypy configured | `pyproject.toml:672` |
