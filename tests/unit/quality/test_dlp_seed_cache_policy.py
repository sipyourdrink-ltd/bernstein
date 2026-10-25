"""Effective seed policy must participate in persisted DLP verdict identity."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import TYPE_CHECKING

import pytest
from bernstein.core.models import Complexity, Scope, Task
from bernstein.core.seed import parse_seed

from bernstein.core.quality import gate_runner
from bernstein.core.quality.gate_pipeline import GatePipelineStep
from bernstein.core.quality.gate_runner import GateRunner
from bernstein.core.quality.quality_gates import QualityGatesConfig

if TYPE_CHECKING:
    from pathlib import Path


_LICENSE = "SPDX-" + "License-Identifier: GPL-3.0-only\n"
_SSN = 'ssn = "' + "123" + '-45-6789"\n'
_INTERNAL = 'url = "https://private.review.invalid"\n'
_PREFIXED_SSN = 'ssn = "REVIEW-' + "123" + '-45-6789"\n'
_STEP = GatePipelineStep(name="dlp_scan", required=True, condition="always")


def _config(path: Path, values: dict[str, object]) -> QualityGatesConfig:
    path.write_text(json.dumps({"goal": "DLP policy test", "quality_gates": values}), encoding="utf-8")
    config = parse_seed(path).quality_gates
    assert config is not None
    return config


@pytest.mark.parametrize(
    ("permissive", "strict", "content"),
    [
        ({"dlp_check_license_violations": False}, {"dlp_check_license_violations": True}, _LICENSE),
        ({"dlp_block_license_violations": False}, {"dlp_block_license_violations": True}, _LICENSE),
        ({"dlp_check_regulated_data": False}, {"dlp_check_regulated_data": True}, _SSN),
        ({"dlp_block_regulated_data": False}, {"dlp_block_regulated_data": True}, _SSN),
        (
            {"dlp_check_proprietary_data": False, "dlp_block_proprietary_data": True},
            {"dlp_check_proprietary_data": True, "dlp_block_proprietary_data": True},
            'host = "service.corp"\n',
        ),
        (
            {"dlp_block_proprietary_data": False},
            {"dlp_block_proprietary_data": True},
            'host = "service.corp"\n',
        ),
        (
            {"dlp_internal_url_patterns": [], "dlp_block_proprietary_data": True},
            {"dlp_internal_url_patterns": ["private.review.invalid"], "dlp_block_proprietary_data": True},
            _INTERNAL,
        ),
        ({"dlp_ignore_paths": ["source.txt"]}, {"dlp_ignore_paths": []}, _LICENSE),
        ({"dlp_allowlist_prefixes": ["REVIEW"]}, {"dlp_allowlist_prefixes": []}, _PREFIXED_SSN),
    ],
    ids=[
        "check-license",
        "block-license",
        "check-regulated",
        "block-regulated",
        "check-proprietary",
        "block-proprietary",
        "internal-patterns",
        "ignored-paths",
        "allowlist-prefixes",
    ],
)
def test_tightened_dlp_seed_policy_rescans_a_persisted_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    permissive: dict[str, object],
    strict: dict[str, object],
    content: str,
) -> None:
    # Only base-ref discovery is replaced: no repository or subprocess is needed.
    monkeypatch.setattr(gate_runner, "_effective_base_ref", lambda *_: "main")
    weak_config = _config(tmp_path / "weak.yaml", permissive)
    strict_config = _config(tmp_path / "strict.yaml", strict)
    task = Task(
        id="dlp-policy",
        title="Check effective DLP policy",
        description="Synthetic scanner fixtures",
        role="backend",
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
    )
    (tmp_path / "source.txt").write_text(content, encoding="utf-8")

    first = asyncio.run(GateRunner(weak_config, tmp_path).run_gate(_STEP, task, tmp_path, ["source.txt"]))
    assert first.status == "pass" and not first.blocked and not first.cached
    assert (tmp_path / ".sdd" / "caching" / "gate_cache.json").is_file()

    # A fresh actual scan proves the stricter policy should block these bytes.
    uncached = GateRunner(dataclasses.replace(strict_config, cache_enabled=False), tmp_path)
    control = asyncio.run(uncached.run_gate(_STEP, task, tmp_path, ["source.txt"]))
    assert control.status == "fail" and control.blocked and not control.cached

    fresh_reader = GateRunner(strict_config, tmp_path)
    result = asyncio.run(fresh_reader.run_gate(_STEP, task, tmp_path, ["source.txt"]))
    assert result.status == "fail" and result.blocked and not result.cached

    # Unchanged policy still reuses its own verdict rather than disabling caching.
    repeated = asyncio.run(GateRunner(strict_config, tmp_path).run_gate(_STEP, task, tmp_path, ["source.txt"]))
    assert repeated.cached and repeated.blocked


def test_dlp_seed_defaults_match_config_without_sharing_lists(tmp_path: Path) -> None:
    first = _config(tmp_path / "first.yaml", {})
    second = _config(tmp_path / "second.yaml", {})
    defaults = QualityGatesConfig()
    for field in dataclasses.fields(defaults):
        if not field.name.startswith("dlp_"):
            continue
        value = getattr(first, field.name)
        assert value == getattr(defaults, field.name)
        if isinstance(value, list):
            assert value is not getattr(second, field.name)


@pytest.mark.parametrize("enabled", [False, True])
def test_dlp_seed_switch_controls_the_implicit_required_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    monkeypatch.setattr(gate_runner, "_effective_base_ref", lambda *_: "main")
    config = _config(tmp_path / "seed.yaml", {"dlp_scan": enabled})
    steps = [step for step in GateRunner(config, tmp_path)._resolve_pipeline() if step.name == "dlp_scan"]
    assert bool(steps) is enabled
    assert all(step.required for step in steps)
