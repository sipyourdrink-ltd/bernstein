"""`tuning.agent` must reach the code that judges liveness and sends signals.

Finding X (2026-09-03). `.sdd/runtime/config_snapshot.json` carried
`liveness_grace_s: 600` and `escalation_sigterm_s: 1200`, yet `spawner.log`
recorded, for four separate healthy agents::

    liveness_judgment: ... pid_alive=True heartbeat_age_s=125.0 log_age_s=133.9
        grace_s=90 verdict=DEAD (no fresh signal)
    Sent SIGTERM to agent adversary-5f4b2ed3 (PID 99072): heartbeat stale for 125s

Two import-time bindings caused it: `_ORPHAN_LIVENESS_GRACE_S = 90.0` was read
as a bare module constant, and `EscalationThresholds` bound `AGENT.escalation_*`
as class-definition-time defaults. `defaults.override` (the path bernstein.yaml's
`tuning:` block takes) REBINDS the singleton rather than mutating it, and runs
long after these modules import - so the tuning reached the config snapshot and
nothing that kills.

Follow-up finding 1 (same class, same run): `tasks/models.py` kept the same stale
`AGENT` import, so `OrchestratorConfig.heartbeat_timeout_s` and
`heartbeat_starting_timeout_s` resolved their `default_factory` against the frozen
shipped values. The run configured `heartbeat_starting_timeout_s: 900` and the
config still reported 300, so a judge slow to its first heartbeat kept the shipped
window and could be reaped mid-first-turn.

Every test drives `defaults.override` rather than monkeypatching a consuming
module, so a re-introduced import-time snapshot fails them.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.heartbeat import check_stale_agents
from bernstein.core.models import AgentHeartbeat, AgentSession, ModelConfig

from bernstein.core import defaults
from bernstein.core.agents.agent_lifecycle import _orphan_liveness_grace_s, _probe_liveness_signals
from bernstein.core.agents.agent_signals import AgentSignalManager
from bernstein.core.agents.heartbeat_escalation import EscalationThresholds
from bernstein.core.observability.watchdog import collect_watchdog_findings
from bernstein.core.tasks.models import OrchestratorConfig

#: The `tuning.agent` block the acceptance run actually shipped.
_MEASURED_TUNING = {
    "liveness_grace_s": 600.0,
    "escalation_warn_s": 600.0,
    "escalation_sigusr1_s": 900.0,
    "escalation_sigterm_s": 1200.0,
    "escalation_sigkill_s": 1500.0,
}


@pytest.fixture(autouse=True)
def _restore_defaults() -> Iterator[None]:
    """Tuning singletons are process-global; put them back after each test."""
    yield
    defaults.reset()


def _session(*, pid: int | None = 999_999, spawn_ts: float) -> AgentSession:
    return AgentSession(
        id="adversary-1",
        role="adversary",
        task_ids=["T-1"],
        status="working",
        spawn_ts=spawn_ts,
        pid=pid,
        model_config=ModelConfig("sonnet", "high"),
    )


def _orch(workdir: Path, session: AgentSession, *, timeout_s: float = 120.0) -> SimpleNamespace:
    return SimpleNamespace(
        _agents={session.id: session},
        _signal_mgr=AgentSignalManager(workdir),
        _spawner=MagicMock(),
        _workdir=workdir,
        _config=SimpleNamespace(heartbeat_enabled=True, heartbeat_timeout_s=timeout_s),
    )


def _age_heartbeat_file(workdir: Path, session_id: str, *, age_s: float) -> None:
    """Age the heartbeat FILE's mtime, not just the timestamp inside it.

    `_probe_liveness_signals` reads the file's mtime as an independent liveness
    signal, so a freshly-written heartbeat carrying an old payload timestamp
    still looks fresh to it. Without this the reaper's verdict below would be an
    artifact of the fixture rather than a consequence of the configured grace.
    """
    path = workdir / ".sdd" / "runtime" / "heartbeats" / f"{session_id}.json"
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))


def _write_log(workdir: Path, session_id: str, *, age_s: float) -> Path:
    log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("still streaming a long turn\n", encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(log_path, (stamp, stamp))
    return log_path


class TestLivenessGraceIsConfigurable:
    def test_unconfigured_grace_is_the_shipped_90s(self) -> None:
        assert _orphan_liveness_grace_s() == 90.0

    def test_a_raised_grace_is_honoured(self) -> None:
        defaults.override("agent", {"liveness_grace_s": 600.0})
        assert _orphan_liveness_grace_s() == 600.0

    def test_the_shipped_90s_stays_a_floor(self) -> None:
        """The 90s minimum is measured ground truth for double-forked runners;
        lowering the tunable must not make this probe judge agents dead sooner."""
        defaults.override("agent", {"liveness_grace_s": 10.0})
        assert _orphan_liveness_grace_s() == 90.0


class TestLivenessJudgmentUsesTheConfiguredGrace:
    """The measured case: a log 134s old under `liveness_grace_s: 600`."""

    def test_unconfigured_a_134s_old_log_is_judged_dead(self, tmp_path: Path) -> None:
        session = _session(pid=None, spawn_ts=time.time() - 800)
        orch = _orch(tmp_path, session)
        _write_log(tmp_path, session.id, age_s=133.9)

        result = _probe_liveness_signals(orch, session, time.time())

        assert result["has_fresh_signal"] is False
        assert result["verdict"].startswith("DEAD")

    def test_with_the_configured_grace_the_same_agent_is_alive(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        defaults.override("agent", {"liveness_grace_s": 600.0})
        session = _session(pid=None, spawn_ts=time.time() - 800)
        orch = _orch(tmp_path, session)
        _write_log(tmp_path, session.id, age_s=133.9)

        with caplog.at_level("INFO", logger="bernstein.core.agents.agent_lifecycle"):
            result = _probe_liveness_signals(orch, session, time.time())

        assert result["has_fresh_signal"] is True
        assert result["verdict"].startswith("ALIVE")
        # The judgment line is the operator's only diagnostic: it must report
        # the grace it actually applied, not the shipped constant.
        assert "grace_s=600" in caplog.text
        assert "grace_s=90 " not in caplog.text


class TestEscalationThresholdsAreConfigurable:
    def test_unconfigured_thresholds_are_the_shipped_tiers(self) -> None:
        assert EscalationThresholds() == EscalationThresholds(
            warn_s=60.0, sigusr1_s=90.0, sigterm_s=120.0, sigkill_s=150.0
        )

    def test_tuning_applied_after_import_reaches_the_thresholds(self) -> None:
        defaults.override("agent", _MEASURED_TUNING)
        assert EscalationThresholds() == EscalationThresholds(
            warn_s=600.0, sigusr1_s=900.0, sigterm_s=1200.0, sigkill_s=1500.0
        )

    def test_explicit_thresholds_still_win(self) -> None:
        defaults.override("agent", _MEASURED_TUNING)
        assert EscalationThresholds(warn_s=0.0, sigusr1_s=0.0, sigterm_s=42.0, sigkill_s=99.0).sigterm_s == 42.0


class TestCheckStaleAgentsHonoursTheConfiguredSigtermTier:
    """`check_stale_agents` armed the ladder from `heartbeat_timeout_s` alone."""

    def test_unconfigured_a_125s_silence_still_gets_sigterm(self, tmp_path: Path) -> None:
        """Default behaviour must be byte-identical: 125s > the 120s tier."""
        now = time.time()
        session = _session(spawn_ts=now - 300)
        orch = _orch(tmp_path, session, timeout_s=120.0)
        orch._signal_mgr.write_heartbeat(session.id, AgentHeartbeat(timestamp=now - 125, status="working"))
        _write_log(tmp_path, session.id, age_s=134.0)  # stale past the 90s grace

        with patch("bernstein.core.platform_compat.kill_process_group") as kpg:
            check_stale_agents(orch)

        assert kpg.called, "the shipped ladder must keep firing at 125s"
        assert orch._heartbeat_escalation_ladder.thresholds.sigterm_s == 120.0

    def test_a_raised_sigterm_tier_spares_the_same_agent(self, tmp_path: Path) -> None:
        """The measured kill: 125s of silence under `escalation_sigterm_s: 1200`."""
        defaults.override("agent", _MEASURED_TUNING)
        now = time.time()
        session = _session(spawn_ts=now - 300)
        orch = _orch(tmp_path, session, timeout_s=120.0)
        orch._signal_mgr.write_heartbeat(session.id, AgentHeartbeat(timestamp=now - 125, status="working"))
        _write_log(tmp_path, session.id, age_s=134.0)

        with patch("bernstein.core.platform_compat.kill_process_group") as kpg:
            check_stale_agents(orch)

        assert not kpg.called, "a raised escalation tier must move the SIGTERM deadline"
        ladder = orch._heartbeat_escalation_ladder
        assert ladder.thresholds.sigterm_s == 1200.0
        assert ladder.thresholds.sigkill_s == 1500.0

    def test_it_is_a_floor_and_never_shortens_a_longer_heartbeat_timeout(self, tmp_path: Path) -> None:
        """An explicitly long `heartbeat_timeout_s` is not clipped back to the tier."""
        defaults.override("agent", _MEASURED_TUNING)
        now = time.time()
        session = _session(spawn_ts=now - 3000)
        orch = _orch(tmp_path, session, timeout_s=2400.0)
        orch._signal_mgr.write_heartbeat(session.id, AgentHeartbeat(timestamp=now - 125, status="working"))
        _write_log(tmp_path, session.id, age_s=134.0)

        with patch("bernstein.core.platform_compat.kill_process_group"):
            check_stale_agents(orch)

        assert orch._heartbeat_escalation_ladder.thresholds.sigterm_s == 2400.0

    def test_lowering_the_tier_below_the_shipped_default_changes_nothing(self, tmp_path: Path) -> None:
        """Only a raise is honoured; this must never signal an agent sooner."""
        defaults.override("agent", {"escalation_sigterm_s": 30.0, "escalation_sigkill_s": 60.0})
        now = time.time()
        session = _session(spawn_ts=now - 300)
        orch = _orch(tmp_path, session, timeout_s=120.0)
        orch._signal_mgr.write_heartbeat(session.id, AgentHeartbeat(timestamp=now - 60, status="working"))
        _write_log(tmp_path, session.id, age_s=134.0)

        with patch("bernstein.core.platform_compat.kill_process_group") as kpg:
            check_stale_agents(orch)

        assert not kpg.called
        assert orch._heartbeat_escalation_ladder.thresholds.sigterm_s == 120.0


class TestOrchestratorConfigResolvesAgentTuning:
    """`OrchestratorConfig`'s heartbeat fields are `default_factory` reads of AGENT."""

    def test_unconfigured_fields_are_the_shipped_values(self) -> None:
        config = OrchestratorConfig()
        assert config.heartbeat_timeout_s == 120
        assert config.heartbeat_starting_timeout_s == 300

    def test_a_configured_starting_timeout_reaches_the_config(self) -> None:
        """The measured case: `heartbeat_starting_timeout_s: 900` resolved to 300."""
        defaults.override("agent", {"heartbeat_starting_timeout_s": 900.0})
        assert OrchestratorConfig().heartbeat_starting_timeout_s == 900

    def test_a_configured_heartbeat_stale_window_reaches_the_config(self) -> None:
        defaults.override("agent", {"heartbeat_stale_s": 300.0})
        assert OrchestratorConfig().heartbeat_timeout_s == 300

    def test_the_tier1_watchdog_judges_a_slow_first_turn_by_the_configured_window(self, tmp_path: Path) -> None:
        """The field's only consumer: `observability.watchdog` reads it off the
        config and judges a `starting`-phase agent against it. A judge 500s into
        its first turn is inside a configured 900s window (severity "high") but
        past the shipped 300s one (severity "critical") - and the finding's own
        detail line names the window it applied, so the receipt is checkable."""
        defaults.override("agent", {"heartbeat_starting_timeout_s": 900.0})
        now = time.time()
        session = _session(spawn_ts=now - 500)
        orch = _orch(tmp_path, session)
        orch._config = OrchestratorConfig()
        orch._latest_tasks_by_id = {}
        orch._signal_mgr.write_heartbeat(
            session.id, AgentHeartbeat(timestamp=now - 500, status="starting", phase="starting")
        )
        # Stale past the 90s liveness grace, so the log signal cannot suppress
        # the incident and the starting window is what actually decides.
        _write_log(tmp_path, session.id, age_s=200.0)

        findings = {f.key: f for f in collect_watchdog_findings(orch)}
        finding = findings[f"heartbeat:{session.id}:T-1"]
        assert "timeout=900s" in finding.detail
        assert finding.severity == "high"


class TestTier1WatchdogHonoursTheLivenessGrace:
    """`observability.watchdog` was the last kill-adjacent module judging by the
    90s snapshot. With `liveness_grace_s: 600` configured, an agent quiet for
    100-150s (routine for a v5 judge) was ALIVE to `agent_lifecycle`'s reaper and
    STALE to the tier-1 watchdog at the same instant.

    Every assertion here is on the decision the value produces - a heartbeat
    finding raised or suppressed - never on the field itself."""

    def _watchdog_findings(self, tmp_path: Path, *, heartbeat_age_s: float, log_age_s: float) -> dict[str, object]:
        now = time.time()
        session = _session(spawn_ts=now - heartbeat_age_s - 300)
        orch = _orch(tmp_path, session)
        orch._latest_tasks_by_id = {}
        orch._signal_mgr.write_heartbeat(
            session.id, AgentHeartbeat(timestamp=now - heartbeat_age_s, status="working", phase="implementing")
        )
        _write_log(tmp_path, session.id, age_s=log_age_s)
        return {f.key: f for f in collect_watchdog_findings(orch)}

    def test_unconfigured_a_134s_quiet_agent_is_flagged_stale(self, tmp_path: Path) -> None:
        """Shipped behaviour must not move: 134s > the 90s grace, so no suppression."""
        findings = self._watchdog_findings(tmp_path, heartbeat_age_s=500.0, log_age_s=134.0)
        assert any(k.startswith("heartbeat:") for k in findings), findings

    def test_the_configured_grace_suppresses_the_same_finding(self, tmp_path: Path) -> None:
        """The v5 case: a judge quiet 134s under `liveness_grace_s: 600` is alive."""
        defaults.override("agent", {"liveness_grace_s": 600.0})
        findings = self._watchdog_findings(tmp_path, heartbeat_age_s=500.0, log_age_s=134.0)
        assert not any(k.startswith("heartbeat:") for k in findings), findings

    def test_the_suppression_cap_still_bounds_it(self, tmp_path: Path) -> None:
        """A raised grace is not an exemption: past `liveness_suppression_cap_s`
        of heartbeat silence the log mtime is output noise and the finding is
        raised anyway (issue #3058)."""
        defaults.override("agent", {"liveness_grace_s": 600.0})
        findings = self._watchdog_findings(tmp_path, heartbeat_age_s=1000.0, log_age_s=134.0)
        assert any(k.startswith("heartbeat:") for k in findings), findings

    def test_a_raised_suppression_cap_is_honoured_too(self, tmp_path: Path) -> None:
        """The cap is read off the same snapshot, so it needs its own receipt."""
        defaults.override("agent", {"liveness_grace_s": 600.0, "liveness_suppression_cap_s": 1800.0})
        findings = self._watchdog_findings(tmp_path, heartbeat_age_s=1000.0, log_age_s=134.0)
        assert not any(k.startswith("heartbeat:") for k in findings), findings

    def test_the_reaper_and_the_watchdog_now_agree(self, tmp_path: Path) -> None:
        """The disagreement that made this worth fixing: same agent, same instant,
        two modules. `_probe_liveness_signals` said ALIVE while the watchdog
        raised a staleness finding."""
        defaults.override("agent", {"liveness_grace_s": 600.0})
        now = time.time()
        session = _session(pid=None, spawn_ts=now - 800)
        orch = _orch(tmp_path, session)
        orch._latest_tasks_by_id = {}
        orch._signal_mgr.write_heartbeat(
            session.id, AgentHeartbeat(timestamp=now - 500, status="working", phase="implementing")
        )
        _write_log(tmp_path, session.id, age_s=134.0)

        _age_heartbeat_file(tmp_path, session.id, age_s=500.0)

        reaper_says_alive = _probe_liveness_signals(orch, session, time.time())["has_fresh_signal"]
        watchdog_flags_stale = any(k.startswith("heartbeat:") for k in {f.key for f in collect_watchdog_findings(orch)})
        assert reaper_says_alive is True
        assert watchdog_flags_stale is False
