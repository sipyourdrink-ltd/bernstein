"""Unit tests for GarakAdapter spawn/name/post_run_summary.

``garak`` requires a ``--target`` of the form ``<type>:<name>`` to do
anything meaningful. An empty or malformed target wastes compute and is
rejected before the subprocess starts. The tests below cover the command
construction, target validation, env isolation, and JSONL log parsing:
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.garak import GarakAdapter, _sha256_hex
from tests.unit._adapter_test_helpers import make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


def _spawn(adapter: GarakAdapter, tmp_path: Path, prompt: str, session_id: str):
    """Spawn against a mocked ``Popen`` and hand back the mock."""
    proc_mock = make_popen_mock(pid=900)
    with (
        patch("bernstein.adapters.garak.subprocess.Popen") as popen_cls,
        # _version_from_pip calls subprocess.getstatusoutput
        patch("bernstein.adapters.garak.subprocess.getstatusoutput", return_value=(0, "Version: 1.0.0")),
    ):
        popen_cls.return_value = proc_mock
        adapter.spawn(
            prompt=prompt,
            workdir=tmp_path,
            model_config=ModelConfig(model="sonnet", effort="high"),
            session_id=session_id,
        )
    return popen_cls


class TestGarakAdapterName:
    def test_name(self) -> None:
        assert GarakAdapter().name() == "garak"


class TestGarakAdapterSpawn:
    def test_spawn_builds_garak_command_with_target(self, tmp_path: Path) -> None:
        popen = _spawn(GarakAdapter(), tmp_path, "openai:gpt-4o", "garak-s1")
        garak_cmd = popen.call_args.args[0]
        assert "garak" in garak_cmd
        assert "--target" in garak_cmd
        target_idx = garak_cmd.index("--target")
        assert garak_cmd[target_idx + 1] == "openai:gpt-4o"
        assert "--report_filename" in garak_cmd

    def test_report_filename_contains_session_id(self, tmp_path: Path) -> None:
        popen = _spawn(GarakAdapter(), tmp_path, "openai:gpt-4o", "garak-session-xyz")
        garak_cmd = popen.call_args.args[0]
        idx = garak_cmd.index("--report_filename")
        report_path = garak_cmd[idx + 1]
        assert "garak-session-xyz" in report_path
        assert report_path.endswith(".jsonl")

    def test_extra_cli_flags_appended_after_target(self, tmp_path: Path) -> None:
        """Everything after ``--`` in the prompt is passed through verbatim."""
        popen = _spawn(GarakAdapter(), tmp_path, "openai:gpt-4o -- --probes dan,grandma", "garak-extra")
        garak_cmd = popen.call_args.args[0]
        assert "--probes" in garak_cmd
        probes_idx = garak_cmd.index("--probes")
        assert "dan,grandma" in garak_cmd[probes_idx + 1]

    def test_missing_colon_in_target_raises_before_spawn(self, tmp_path: Path) -> None:
        adapter = GarakAdapter()
        with patch("bernstein.adapters.garak.subprocess.Popen") as popen:
            with pytest.raises(ValueError, match="<type>:<name>"):
                adapter.spawn(
                    prompt="openai",  # no colon, no name
                    workdir=tmp_path,
                    model_config=ModelConfig(model="sonnet", effort="high"),
                    session_id="garak-bad-target",
                )
        popen.assert_not_called()

    def test_empty_target_type_raises_before_spawn(self, tmp_path: Path) -> None:
        adapter = GarakAdapter()
        with patch("bernstein.adapters.garak.subprocess.Popen") as popen:
            with pytest.raises(ValueError, match="non-empty"):
                adapter.spawn(
                    prompt=":gpt-4o",  # empty type
                    workdir=tmp_path,
                    model_config=ModelConfig(model="sonnet", effort="high"),
                    session_id="garak-empty-type",
                )
        popen.assert_not_called()

    def test_empty_target_name_raises_before_spawn(self, tmp_path: Path) -> None:
        adapter = GarakAdapter()
        with patch("bernstein.adapters.garak.subprocess.Popen") as popen:
            with pytest.raises(ValueError, match="non-empty"):
                adapter.spawn(
                    prompt="openai:",  # empty name
                    workdir=tmp_path,
                    model_config=ModelConfig(model="sonnet", effort="high"),
                    session_id="garak-empty-name",
                )
        popen.assert_not_called()

    def test_provider_credentials_isolated_per_target_type(self, tmp_path: Path) -> None:
        """Only the key matching the target type reaches the subprocess."""
        with patch("bernstein.adapters.garak.build_filtered_env", return_value={}) as build_env:
            _spawn(GarakAdapter(), tmp_path, "openai:gpt-4o", "garak-env")
        forwarded = set(build_env.call_args.args[0])
        assert "OPENAI_API_KEY" in forwarded
        # Other keys must not be forwarded unless they match the target type
        assert "HF_TOKEN" not in forwarded

    def test_huggingface_target_forwards_hf_token(self, tmp_path: Path) -> None:
        with patch("bernstein.adapters.garak.build_filtered_env", return_value={}) as build_env:
            _spawn(GarakAdapter(), tmp_path, "huggingface:meta-llama/Llama-2-7b", "garak-hf")
        forwarded = set(build_env.call_args.args[0])
        assert "HF_TOKEN" in forwarded

    def test_network_policy_enforced_before_spawning(self, tmp_path: Path) -> None:
        adapter = GarakAdapter()
        with (
            patch.object(GarakAdapter, "enforce_network_policy", side_effect=RuntimeError("blocked")),
            patch("bernstein.adapters.garak.subprocess.Popen") as popen,
            pytest.raises(RuntimeError, match="blocked"),
        ):
            adapter.spawn(
                prompt="openai:gpt-4o",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="garak-net",
            )
        popen.assert_not_called()

    def test_spawn_translates_missing_cli(self, tmp_path: Path) -> None:
        adapter = GarakAdapter()
        with (
            patch(
                "bernstein.adapters.garak.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            patch(
                "bernstein.adapters.garak.subprocess.getstatusoutput",
                return_value=(0, "Version: 1.0.0"),
            ),
        ):
            with pytest.raises(RuntimeError, match=r"garak not found.*github.com/NVIDIA/garak"):
                adapter.spawn(
                    prompt="openai:gpt-4o",
                    workdir=tmp_path,
                    model_config=ModelConfig(model="sonnet", effort="high"),
                    session_id="garak-missing",
                )

    def test_spawn_closes_stdin(self, tmp_path: Path) -> None:
        import subprocess

        popen = _spawn(GarakAdapter(), tmp_path, "openai:gpt-4o", "garak-stdin")
        assert popen.call_args.kwargs["stdin"] is subprocess.DEVNULL


class TestGarakAdapterPostRunSummary:
    def test_returns_empty_summary_when_report_absent(self, tmp_path: Path) -> None:
        adapter = GarakAdapter()
        summary = adapter.post_run_summary(
            session_id="garak-no-report",
            workdir=tmp_path,
        )
        assert summary["report_exists"] is False
        assert summary["attempt_count"] == 0
        assert summary["success_rate"] == 0.0
        assert summary["attempt_log_hash"] == ""

    def test_parses_jsonl_attempt_log(self, tmp_path: Path) -> None:
        # Filename uses underscores: garak_attempts_{session_id}.jsonl
        report_path = tmp_path / ".sdd" / "runtime" / "garak_attempts_garak-parse.jsonl"
        report_path.parent.mkdir(parents=True, exist_ok=True)

        # Simulate garak JSONL output with one pass, one fail, one error.
        entries = [
            {"status": "pass", "config": {"probes": ["dan", "grandma"], "target": "openai:gpt-4o"}},
            {"status": "fail", "config": {"probes": ["dan", "grandma"], "target": "openai:gpt-4o"}},
            {"status": "error", "config": {"probes": ["dan", "grandma"], "target": "openai:gpt-4o"}},
            {"status": "ok", "config": {}},  # ok counts as success
            {"status": "skip", "config": {}},  # skip counts as success
        ]
        report_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

        adapter = GarakAdapter()
        summary = adapter.post_run_summary(
            session_id="garak-parse",
            workdir=tmp_path,
        )
        assert summary["report_exists"] is True
        assert summary["attempt_count"] == 5
        assert summary["success_count"] == 3  # pass + ok + skip
        assert summary["fail_count"] == 1
        assert summary["error_count"] == 1
        assert summary["probe_set"] == ["dan", "grandma"]
        assert summary["target"] == "openai:gpt-4o"
        assert summary["attempt_log_hash"] != ""
        assert summary["success_rate"] == round(3 / 5, 4)

    def test_attempt_log_hash_changes_when_content_changes(self, tmp_path: Path) -> None:
        # First report file
        report_path1 = tmp_path / ".sdd" / "runtime" / "garak_attempts_garak-hash1.jsonl"
        report_path1.parent.mkdir(parents=True, exist_ok=True)
        report_path1.write_text('{"status": "pass"}\n', encoding="utf-8")

        adapter = GarakAdapter()
        hash1 = adapter.post_run_summary(session_id="garak-hash1", workdir=tmp_path)["attempt_log_hash"]

        # Second report file with different content
        report_path2 = tmp_path / ".sdd" / "runtime" / "garak_attempts_garak-hash2.jsonl"
        report_path2.write_text('{"status": "fail"}\n', encoding="utf-8")
        hash2 = adapter.post_run_summary(session_id="garak-hash2", workdir=tmp_path)["attempt_log_hash"]

        assert hash1 != hash2

    def test_skips_malformed_jsonl_lines(self, tmp_path: Path) -> None:
        report_path = tmp_path / ".sdd" / "runtime" / "garak_attempts_garak-bad.jsonl"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            '{"status": "pass"}',
            "NOT JSON AT ALL",
            "",
            '{"status": "fail"}',
        ]
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        adapter = GarakAdapter()
        summary = adapter.post_run_summary(session_id="garak-bad", workdir=tmp_path)
        # Only valid JSON lines counted.
        assert summary["attempt_count"] == 2
        assert summary["success_count"] == 1
        assert summary["fail_count"] == 1


class TestGarakSha256Hex:
    def test_sha256_hex_string(self) -> None:
        result = _sha256_hex("hello world")
        import hashlib

        expected = hashlib.sha256(b"hello world").hexdigest()
        assert result == expected

    def test_sha256_hex_bytes(self) -> None:
        result = _sha256_hex(b"hello world")
        import hashlib

        expected = hashlib.sha256(b"hello world").hexdigest()
        assert result == expected
