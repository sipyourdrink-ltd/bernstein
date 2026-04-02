"""Unit tests for layered guardrail bypass logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
import pytest
from bernstein.core.guardrails import run_guardrails, GuardrailsConfig
from bernstein.core.models import Task

def test_run_guardrails_bypass_ask(tmp_path: Path):
    # Mock diff that triggers a scope violation (ASK)
    # Scope violation happens in check_scope
    diff = "diff --git a/out_of_scope.py b/out_of_scope.py\n+new content"
    task = Task(id="T1", title="test", role="backend", description="test", owned_files=["src/"])
    config = GuardrailsConfig(secrets=False, file_permissions=False, license_scan=False)
    
    # Without bypass: should be flagged (passed=False, blocked=False)
    results = run_guardrails(diff, task, config, tmp_path, bypass_enabled=False)
    scope_res = next(r for r in results if r.check == "scope_enforcement")
    assert scope_res.passed is False
    assert scope_res.blocked is False

    # With bypass: should be [BYPASSED] (passed=True)
    results = run_guardrails(diff, task, config, tmp_path, bypass_enabled=True)
    scope_res = next(r for r in results if r.check == "scope_enforcement")
    assert scope_res.passed is True
    assert "[BYPASSED]" in scope_res.detail

def test_run_guardrails_bypass_immune_stays_blocked(tmp_path: Path):
    # Mock diff that triggers an immune path violation
    diff = "diff --git a/bernstein.yaml b/bernstein.yaml\n+new content"
    task = Task(id="T1", title="test", role="backend", description="test")
    config = GuardrailsConfig(secrets=False, file_permissions=False, license_scan=False)
    
    # With bypass enabled: IMMUNE check must STILL fail and block
    results = run_guardrails(diff, task, config, tmp_path, bypass_enabled=True)
    immune_res = next(r for r in results if r.check == "immune_path_enforcement")
    assert immune_res.passed is False
    assert immune_res.blocked is True
    assert "[BYPASSED]" not in immune_res.detail
