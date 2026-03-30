"""Quickstart command — zero-config demo of Bernstein orchestration."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import click
import httpx

from bernstein.cli.helpers import (
    SDD_DIRS,
    console,
    is_alive,
    print_banner,
)

_QUICKSTART_PORT = 8056
_QUICKSTART_GOAL = "Add input validation, error handling, and tests to the TODO API"

# Three focused tasks that showcase backend and QA agent roles.
_QUICKSTART_TASKS: list[dict[str, str]] = [
    {
        "filename": "1-input-validation.md",
        "content": (
            "# Add input validation to TODO API\n\n"
            "**Role:** backend\n"
            "**Priority:** 1\n\n"
            "In `app.py`, update the `create_todo` endpoint:\n"
            "- Check that the request body is valid JSON with a `title` field\n"
            "- Return HTTP 400 with `{\"error\": \"title is required\"}` if missing or empty\n"
            "- Return HTTP 400 with `{\"error\": \"invalid JSON\"}` if body is not valid JSON\n"
        ),
    },
    {
        "filename": "2-error-handling.md",
        "content": (
            "# Add 404 error handling for missing TODO items\n\n"
            "**Role:** backend\n"
            "**Priority:** 2\n\n"
            "In `app.py`, update `update_todo` and `delete_todo`:\n"
            "- Return HTTP 404 with `{\"error\": \"not found\"}` when `todo_id` does not exist\n"
            "- Do not allow an unhandled `KeyError` to propagate\n"
        ),
    },
    {
        "filename": "3-write-tests.md",
        "content": (
            "# Write pytest test suite for the TODO API\n\n"
            "**Role:** qa\n"
            "**Priority:** 3\n\n"
            "Create `tests/test_api.py` with pytest tests that cover:\n"
            "- GET /todos returns 200 and a list\n"
            "- POST /todos with valid data returns 201 and the created item\n"
            "- POST /todos with missing title returns 400\n"
            "- PATCH /todos/<id> with non-existent id returns 404\n"
            "- DELETE /todos/<id> with non-existent id returns 404\n"
            "Use the Flask test client (`app.test_client()`) as a pytest fixture.\n"
        ),
    },
]


def _setup_quickstart_project(project_dir: Path, adapter: str) -> None:
    """Copy quickstart example files and seed three backlog tasks.

    Args:
        project_dir: Destination directory (empty or temp dir).
        adapter: CLI adapter name written into workspace config.
    """
    import shutil as _shutil

    # Copy bundled examples/quickstart/ — go up 4 levels from this file to repo root
    examples_dir = Path(__file__).parent.parent.parent.parent / "examples" / "quickstart"
    if examples_dir.exists():
        _shutil.copytree(str(examples_dir), str(project_dir), dirs_exist_ok=True)
    else:
        # Inline fallback so the command works even when the examples/ dir is absent
        # (e.g. installed via pip without the examples tree).
        (project_dir / "app.py").write_text(
            '"""Minimal TODO API — intentionally missing validation, error handling, and tests."""\n\n'
            "from __future__ import annotations\n\n"
            "from dataclasses import dataclass, field\n\n"
            "from flask import Flask, jsonify, request\n\n"
            "app = Flask(__name__)\n\n\n"
            "@dataclass\n"
            "class TodoStore:\n"
            '    """In-memory store for TODO items."""\n\n'
            "    items: dict[int, dict[str, object]] = field(default_factory=dict)\n"
            "    next_id: int = 1\n\n\n"
            "store = TodoStore()\n\n\n"
            '@app.get("/todos")\n'
            "def list_todos() -> object:\n"
            "    return jsonify(list(store.items.values()))\n\n\n"
            '@app.post("/todos")\n'
            "def create_todo() -> object:\n"
            "    data = request.get_json()\n"
            '    todo = {"id": store.next_id, "title": data["title"], "done": False}\n'
            "    store.items[store.next_id] = todo\n"
            "    store.next_id += 1\n"
            "    return jsonify(todo), 201\n\n\n"
            '@app.patch("/todos/<int:todo_id>")\n'
            "def update_todo(todo_id: int) -> object:\n"
            "    todo = store.items[todo_id]\n"
            "    data = request.get_json()\n"
            "    todo.update(data)  # type: ignore[arg-type]\n"
            "    return jsonify(todo)\n\n\n"
            '@app.delete("/todos/<int:todo_id>")\n'
            "def delete_todo(todo_id: int) -> object:\n"
            "    del store.items[todo_id]\n"
            '    return "", 204\n\n\n'
            'if __name__ == "__main__":\n'
            "    app.run(debug=True)\n"
        )
        (project_dir / "requirements.txt").write_text("flask>=3.0.0\npytest>=8.0.0\n")

    # Create .sdd directory structure
    for d in SDD_DIRS:
        (project_dir / d).mkdir(parents=True, exist_ok=True)

    # Write workspace config
    config_path = project_dir / ".sdd" / "config.yaml"
    config_path.write_text(
        "# Bernstein quickstart workspace\n"
        f"server_port: {_QUICKSTART_PORT}\n"
        "max_workers: 2\n"
        "default_model: sonnet\n"
        "default_effort: normal\n"
        f"cli: {adapter}\n"
    )
    (project_dir / ".sdd" / "runtime" / ".gitignore").write_text("*.pid\n*.log\ntasks.jsonl\n")

    # Seed the three backlog tasks
    backlog_open = project_dir / ".sdd" / "backlog" / "open"
    for task in _QUICKSTART_TASKS:
        (backlog_open / task["filename"]).write_text(task["content"])


def _stop_quickstart_processes(project_dir: Path) -> None:
    """Terminate server, spawner, and watchdog started in project_dir.

    Args:
        project_dir: Quickstart project root whose .sdd/runtime/ holds PID files.
    """
    runtime_dir = project_dir / ".sdd" / "runtime"
    for pid_filename, _label in [
        ("watchdog.pid", "Watchdog"),
        ("spawner.pid", "Spawner"),
        ("server.pid", "Task server"),
    ]:
        pid_file = runtime_dir / pid_filename
        if not pid_file.exists():
            continue
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            continue
        if is_alive(pid):
            try:
                import signal as _signal

                os.kill(pid, _signal.SIGTERM)
            except OSError:
                pass
        pid_file.unlink(missing_ok=True)


def _print_quickstart_summary(
    project_dir: Path,
    server_url: str,
    elapsed_secs: float = 0.0,
    keep: bool = False,
) -> None:
    """Print final quickstart summary: tasks completed, cost, next steps.

    Args:
        project_dir: Quickstart project root.
        server_url: Base URL of the task server.
        elapsed_secs: Wall-clock seconds the orchestration took.
        keep: Whether the temp directory was kept.
    """
    from rich.table import Table

    tasks_data: list[dict[str, Any]] = []
    total_cost: float = 0.0
    try:
        resp = httpx.get(f"{server_url}/status", timeout=3.0)
        if resp.status_code == 200:
            payload = resp.json()
            tasks_data = payload.get("tasks", [])
            total_cost = payload.get("total_cost_usd", 0.0)
    except Exception:
        pass

    done = sum(1 for t in tasks_data if t.get("status") == "done")
    failed = sum(1 for t in tasks_data if t.get("status") == "failed")
    total = len(tasks_data)

    elapsed_str = f"{elapsed_secs:.0f}s" if elapsed_secs > 0 else "—"

    console.print("\n[bold cyan]── Quickstart Summary ────────────────────[/bold cyan]")

    table = Table(show_header=True, header_style="bold magenta", show_lines=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Tasks completed", f"[green]{done}[/green] / {total}")
    if failed:
        table.add_row("Tasks failed", f"[red]{failed}[/red]")
    table.add_row("Elapsed", elapsed_str)
    py_files = [p for p in project_dir.glob("**/*.py") if ".sdd" not in p.parts]
    table.add_row("Python files", str(len(py_files)))
    table.add_row("API cost", f"${total_cost:.4f}")
    console.print(table)

    console.print(f"\n[dim]Project directory:[/dim] {project_dir}")

    if keep:
        console.print(
            "[dim]  Inspect generated files:[/dim] "
            f"[bold]ls {project_dir}[/bold]"
        )
        console.print(
            "[dim]  Run the test suite:[/dim] "
            f"[bold]cd {project_dir} && pip install -r requirements.txt && pytest tests/ -q[/bold]"
        )
    else:
        console.print("[dim]  (directory removed — use [bold]--keep[/bold] to preserve it)[/dim]")

    console.print(
        "\n[dim]Next: initialise your own project with [bold]bernstein init[/bold] "
        "and describe your goal in [bold]bernstein.yaml[/bold].[/dim]"
    )
    console.print(
        f"\n[bold green]Completed {done} task{'s' if done != 1 else ''} in {elapsed_str}.[/bold green]"
    )


@click.command("quickstart")
@click.option(
    "--keep",
    is_flag=True,
    default=False,
    help="Preserve the temp directory after completion (default: clean up).",
)
@click.option(
    "--timeout",
    default=300,
    show_default=True,
    help="Maximum seconds to wait for all tasks to complete.",
)
@click.option(
    "--adapter",
    default=None,
    metavar="NAME",
    help="CLI adapter to use (auto-detected by default).",
)
def quickstart_cmd(keep: bool, timeout: int, adapter: str | None) -> None:
    """Zero-config demo: orchestrate 3 tasks on a Flask TODO API.

    \b
    Creates a temp project (Flask TODO API with gaps), seeds 3 tasks — input
    validation, error handling, and a pytest suite — then runs agents to
    complete them. No prior setup or bernstein.yaml required.

    \b
      bernstein quickstart              # run and clean up temp dir
      bernstein quickstart --keep       # keep temp dir for inspection
      bernstein quickstart --timeout 120
    """
    import shutil
    import tempfile

    from bernstein.cli.run_cmd import detect_available_adapter

    print_banner()

    detected = adapter or detect_available_adapter() or "mock"
    real_mode = detected != "mock"
    cost_estimate = "~$0.20" if real_mode else "$0.00 (mock)"

    console.print(
        f"\n[bold yellow]Cost estimate:[/bold yellow] {cost_estimate} (3 tasks)\n"
        f"[dim]Adapter: {detected}  |  Timeout: {timeout}s[/dim]"
    )

    project_dir = Path(tempfile.mkdtemp(prefix="bernstein-quickstart-"))
    console.print(f"\n[dim]Creating quickstart project in {project_dir}…[/dim]")

    _setup_quickstart_project(project_dir, detected)
    console.print("[green]✓[/green] Flask TODO API project created")
    console.print(
        "[green]✓[/green] 3 tasks seeded: input validation · error handling · pytest suite"
    )

    server_url = f"http://127.0.0.1:{_QUICKSTART_PORT}"
    orchestration_start = time.monotonic()

    try:
        console.print("\n[bold]Starting orchestration…[/bold]")
        from bernstein.core.bootstrap import bootstrap_from_goal

        bootstrap_from_goal(
            goal=_QUICKSTART_GOAL,
            workdir=project_dir,
            port=_QUICKSTART_PORT,
            cli=detected,
        )

        from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

        deadline = orchestration_start + timeout
        seen_done: set[str] = set()
        seen_failed: set[str] = set()

        console.print()
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as progress:
            poll_task = progress.add_task("Agents working…", total=None)

            while time.monotonic() < deadline:
                try:
                    resp = httpx.get(f"{server_url}/status", timeout=3.0)
                    if resp.status_code == 200:
                        payload = resp.json()
                        tasks_list: list[dict[str, Any]] = payload.get("tasks", [])
                        done_count = sum(1 for t in tasks_list if t.get("status") == "done")
                        failed_count = sum(1 for t in tasks_list if t.get("status") == "failed")
                        total_tasks = len(tasks_list)

                        for t in tasks_list:
                            tid = t.get("id", "")
                            title = (t.get("title") or "")[:60]
                            role = t.get("role", "agent")
                            if t.get("status") == "done" and tid not in seen_done:
                                seen_done.add(tid)
                                progress.console.print(
                                    f"  [green]✓[/green] [{role}] {title}"
                                )
                            elif t.get("status") == "failed" and tid not in seen_failed:
                                seen_failed.add(tid)
                                progress.console.print(
                                    f"  [red]✗[/red] [{role}] {title}"
                                )

                        progress.update(
                            poll_task,
                            description=(
                                f"Agents working… "
                                f"[green]{done_count}[/green]/{total_tasks} tasks done"
                                + (f"  [red]{failed_count} failed[/red]" if failed_count else "")
                            ),
                        )
                        if total_tasks > 0 and done_count + failed_count >= total_tasks:
                            break
                except Exception:
                    pass
                time.sleep(2)

        console.print("[green]✓[/green] Orchestration finished")

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
    except RuntimeError as exc:
        from bernstein.cli.errors import bootstrap_failed

        bootstrap_failed(exc).print()
    finally:
        _stop_quickstart_processes(project_dir)

    elapsed = time.monotonic() - orchestration_start
    _print_quickstart_summary(project_dir, server_url, elapsed_secs=elapsed, keep=keep)

    if not keep:
        import contextlib

        with contextlib.suppress(Exception):
            shutil.rmtree(project_dir, ignore_errors=True)
