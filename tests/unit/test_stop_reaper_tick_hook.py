"""Keep cooperative cancellation connected to the guarded tick pipeline."""

import ast
from pathlib import Path


def test_tick_calls_stop_reaper_after_dead_agents_with_failure_guard():
    source = Path("src/bernstein/core/orchestration/orchestrator.py").read_text()
    tree = ast.parse(source)
    tick = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_tick_internal")
    calls = [node for node in ast.walk(tick) if isinstance(node, ast.Call)]
    stop_calls = [node for node in calls if isinstance(node.func, ast.Name) and node.func.id == "reap_stop_requests"]
    assert len(stop_calls) == 1, "Tick must drive the cooperative stop reaper exactly once"
    stop_call = stop_calls[0]
    dead_call = next(node for node in calls if isinstance(node.func, ast.Name) and node.func.id == "reap_dead_agents")
    assert dead_call.lineno < stop_call.lineno
    assert [ast.unparse(arg) for arg in stop_call.args] == ["self", "result", "tasks_by_status"]
    guards = [
        node
        for node in ast.walk(tick)
        if isinstance(node, ast.Try) and any(child is stop_call for stmt in node.body for child in ast.walk(stmt))
    ]
    assert guards, "Stop-reaper failure must not abort the tick"
    assert any(
        isinstance(handler.type, ast.Name)
        and handler.type.id == "Exception"
        and any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and ast.unparse(node.func) == "logger.warning"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "Stop reaper pass failed; STOP_REQ tasks stay pending"
            and any(keyword.arg == "exc_info" and ast.unparse(keyword.value) == "True" for keyword in node.keywords)
            for node in ast.walk(handler)
        )
        for guard in guards
        for handler in guard.handlers
    )
