"""Tests for adapter-side capture of provider context mutations (issue #2507).

The claude adapter's wrapper script and stream parser are the observation
surface: provider-side context rewrites arrive as stream-json ``system``
events. These tests pin that the signals are surfaced, persisted to the
per-session sidecar, and exposed through the adapter contract, and that
adapters without an observation surface declare themselves blind.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from bernstein.adapters.base import (
    MUTATION_OBSERVABILITY_DECLARED_BLIND,
    MUTATION_OBSERVABILITY_OBSERVED,
    CLIAdapter,
)
from bernstein.adapters.claude import ClaudeCodeAdapter
from bernstein.adapters.claude_stream_parser import (
    PROVIDER_MUTATION_SUBTYPES,
    ClaudeStreamParser,
    StreamEventType,
)
from bernstein.adapters.claude_wrapper_script import build_wrapper_script
from bernstein.adapters.registry import iter_adapter_specs


class TestStreamParserMutationSignals:
    def test_compact_boundary_system_event_is_a_mutation_signal(self) -> None:
        parser = ClaudeStreamParser()
        line = json.dumps(
            {
                "type": "system",
                "subtype": "compact_boundary",
                "compact_metadata": {"trigger": "auto", "pre_tokens": 90000},
            }
        )
        events = parser.feed_line(line + "\n")

        assert len(events) == 1
        assert events[0].event_type == StreamEventType.PROVIDER_STATE_MUTATION
        assert events[0].data["kind"] == "compact_boundary"
        assert events[0].data["detail"]["compact_metadata"]["trigger"] == "auto"
        assert parser.state.provider_mutations == [events[0].data]

    def test_plain_system_events_are_not_mutation_signals(self) -> None:
        parser = ClaudeStreamParser()
        line = json.dumps({"type": "system", "subtype": "init", "message": "ready"})
        events = parser.feed_line(line + "\n")

        assert len(events) == 1
        assert events[0].event_type == StreamEventType.SYSTEM
        assert parser.state.provider_mutations == []

    def test_all_declared_subtypes_recognised(self) -> None:
        parser = ClaudeStreamParser()
        for subtype in sorted(PROVIDER_MUTATION_SUBTYPES):
            events = parser.feed_line(json.dumps({"type": "system", "subtype": subtype}) + "\n")
            assert events[0].event_type == StreamEventType.PROVIDER_STATE_MUTATION
        assert len(parser.state.provider_mutations) == len(PROVIDER_MUTATION_SUBTYPES)


class TestWrapperMutationSidecar:
    def _run_wrapper(self, script: str, ndjson: str) -> None:
        subprocess.run(
            [sys.executable, "-c", script],
            input=ndjson.encode("utf-8"),
            capture_output=True,
            check=True,
            timeout=30,
        )

    def test_wrapper_writes_mutation_sidecar(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "session.jsonl"
        script = build_wrapper_script(mutation_path=str(sidecar))
        ndjson = (
            json.dumps({"type": "system", "subtype": "init"})
            + "\n"
            + json.dumps(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "compact_metadata": {"trigger": "auto", "pre_tokens": 12345},
                }
            )
            + "\n"
            + json.dumps({"type": "result", "result": "done"})
            + "\n"
        )
        self._run_wrapper(script, ndjson)

        rows = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1
        assert rows[0]["kind"] == "compact_boundary"
        assert rows[0]["detail"]["compact_metadata"]["pre_tokens"] == 12345

    def test_wrapper_without_mutation_path_writes_nothing(self, tmp_path: Path) -> None:
        script = build_wrapper_script()
        assert "provider_state" not in script
        ndjson = json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n"
        self._run_wrapper(script, ndjson)
        assert list(tmp_path.iterdir()) == []


class TestAdapterContract:
    def test_base_adapter_is_declared_blind(self) -> None:
        assert CLIAdapter.provider_mutation_observability == MUTATION_OBSERVABILITY_DECLARED_BLIND

    def test_base_adapter_observes_nothing(self, tmp_path: Path) -> None:
        class _Blind(CLIAdapter):
            def spawn(self, **kwargs: object) -> object:  # type: ignore[override]
                raise NotImplementedError

            def is_alive(self, pid: int) -> bool:
                return False

            def kill(self, pid: int) -> object:
                raise NotImplementedError

            def name(self) -> str:
                return "blind"

        assert _Blind().observed_provider_mutations(tmp_path, "s-1") == []

    def test_claude_adapter_declares_observed(self) -> None:
        assert ClaudeCodeAdapter.provider_mutation_observability == MUTATION_OBSERVABILITY_OBSERVED

    def test_claude_reads_mutation_sidecar(self, tmp_path: Path) -> None:
        sidecar_dir = tmp_path / ".sdd" / "runtime" / "provider_state"
        sidecar_dir.mkdir(parents=True)
        rows = [
            {"kind": "compact_boundary", "detail": {"trigger": "auto"}},
            {"kind": "context_edit", "detail": {}},
            {"not-a-signal": True},
        ]
        (sidecar_dir / "s-9.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n",
            encoding="utf-8",
        )

        signals = ClaudeCodeAdapter().observed_provider_mutations(tmp_path, "s-9")
        assert signals == [
            {"kind": "compact_boundary", "detail": {"trigger": "auto"}},
            {"kind": "context_edit", "detail": {}},
        ]

    def test_claude_missing_sidecar_returns_empty(self, tmp_path: Path) -> None:
        assert ClaudeCodeAdapter().observed_provider_mutations(tmp_path, "missing") == []

    def test_registry_conformance_observed_adapters_override_capture(self) -> None:
        """Adapters declaring observed capability must implement the capture hook.

        Declared-blind adapters must NOT ship a partial capture surface: the
        capability record in the journal is only trustworthy when the two
        declarations agree.
        """
        for name, spec in iter_adapter_specs():
            cls = spec if isinstance(spec, type) else type(spec)
            capability = getattr(cls, "provider_mutation_observability", MUTATION_OBSERVABILITY_DECLARED_BLIND)
            overrides = "observed_provider_mutations" in vars(cls)
            if capability == MUTATION_OBSERVABILITY_OBSERVED:
                assert overrides, f"adapter {name!r} declares observed but does not override capture"
            else:
                assert not overrides, f"adapter {name!r} overrides capture but declares {capability!r}"
