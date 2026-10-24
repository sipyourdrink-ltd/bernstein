"""Deterministic cooperative cancellation regressions."""

import json
import signal
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from bernstein.core.agents import stop_reaper as mod
from bernstein.core.agents.stop_reaper import (
    StopEntry,
    StopEscalationPolicy,
    StopOutcome,
    StopReaper,
    StopTier,
    advance,
    heartbeat_age_s,
    reap_stop_requests,
)
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.tasks.models import AgentSession


class Ops:
    def __init__(self):
        self.running = True
        self.sent = []

    def alive(self, pgid):
        return self.running

    def send(self, pgid, sig):
        self.sent.append(sig)
        return True


def test_escalation_and_same_time_idempotence():
    ops = Ops()
    policy = StopEscalationPolicy(notify_grace_s=0, int_grace_s=0, term_grace_s=0)
    entry = StopEntry("t", "s", 987654, 0)
    entry = advance(entry, 1, policy, ops)
    assert entry.tier == StopTier.INT
    assert advance(entry, 1, policy, ops) == entry
    entry = advance(entry, 2, policy, ops)
    assert ops.sent == [signal.SIGINT, signal.SIGTERM]
    ops.running = False
    assert advance(entry, 3, policy, ops).outcome == StopOutcome.GONE


@pytest.mark.parametrize("pgid", [0, 1, -5, 444])
def test_unsafe(monkeypatch, pgid):
    monkeypatch.setattr(mod, "_own_pgid", lambda: 444)
    ops = Ops()
    result = advance(StopEntry("t", "s", pgid, 0), 100, StopEscalationPolicy(), ops)
    assert result.outcome == StopOutcome.REFUSED
    assert ops.sent == []


@pytest.mark.parametrize("die_at,expected", [(5, [signal.SIGINT]), (15, [signal.SIGINT, signal.SIGTERM])])
def test_grace_exact_times(die_at, expected):
    ops = Ops()
    entry = StopEntry("t", "s", 987654, 0)
    policy = StopEscalationPolicy()
    for now in range(die_at + 1):
        entry = advance(entry, now, policy, ops)
    assert ops.sent == expected
    ops.running = False
    entry = advance(entry, die_at + 1, policy, ops)
    assert entry.outcome == StopOutcome.GONE
    assert advance(entry, 1000, policy, ops) == entry


def test_kill_retries_bounded():
    ops = Ops()
    entry = StopEntry("t", "s", 987654, 0)
    policy = StopEscalationPolicy()
    for now in (5, 15, 30, 35, 40, 45, 100):
        entry = advance(entry, now, policy, ops)
    assert [row[1] for row in entry.signals] == [5, 15, 30, 35, 40]
    assert ops.sent == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL, signal.SIGKILL, signal.SIGKILL]
    assert entry.outcome == StopOutcome.UNVERIFIED


def test_failed_delivery_reprobes():
    class Failed(Ops):
        def send(self, pgid, sig):
            self.running = False
            return False

    entry = advance(StopEntry("t", "s", 987654, 0), 5, StopEscalationPolicy(), Failed())
    assert entry.outcome == StopOutcome.GONE
    assert entry.signals == [("SIGINT", 5, False)]


@pytest.mark.parametrize(
    "field,value",
    [
        ("notify_grace_s", -1),
        ("int_grace_s", float("nan")),
        ("term_grace_s", float("inf")),
        ("kill_verify_s", 0),
        ("kill_max_attempts", 0),
        ("heartbeat_interval_s", -1),
        ("heartbeat_stale_s", 5),
    ],
)
def test_policy_validation(field, value):
    assert replace(StopEscalationPolicy(), **{field: value}).validate()


def test_environment(caplog):
    policy = StopEscalationPolicy.from_env(
        {
            "BERNSTEIN_STOP_NOTIFY_GRACE_S": "2",
            "BERNSTEIN_STOP_INT_GRACE_S": "nan",
            "BERNSTEIN_STOP_TERM_GRACE_S": "inf",
            "BERNSTEIN_STOP_KILL_MAX_ATTEMPTS": "4",
            "BERNSTEIN_STOP_KILL_VERIFY_S": "bad",
        }
    )
    assert policy == StopEscalationPolicy(notify_grace_s=2, kill_max_attempts=4)
    assert len(caplog.records) == 3


def test_heartbeat(tmp_path, caplog):
    now = [0.0]
    wall = [100.0]
    reaper = StopReaper(tmp_path, clock=lambda: now[0], wall_clock=lambda: wall[0])
    reaper.track("t", "s", 23)
    assert reaper.track("t", "s2", 24)[1] is False
    reaper.beat(1, [])
    path = tmp_path / ".sdd/runtime/stop_reaper.heartbeat.json"
    first = path.read_bytes()
    now[0], wall[0] = 1, 101
    reaper.beat(1, [])
    assert path.read_bytes() == first
    now[0], wall[0] = 5, 105
    reaper.beat(1, [])
    assert heartbeat_age_s(tmp_path, 107) == 2
    replacement = StopReaper(tmp_path, clock=lambda: 50, wall_clock=lambda: 150)
    replacement.beat(1, [])
    assert json.loads(path.read_text())["gap_s"] == 45
    assert "gap" in caplog.text
    path.write_text("broken")
    assert heartbeat_age_s(tmp_path, 160) is None
    assert heartbeat_age_s(tmp_path / "missing", 160) is None


def driver(tmp_path, monkeypatch, sessions):
    from bernstein.core.agents import agent_lifecycle

    monkeypatch.setattr(agent_lifecycle, "_propagate_abort_to_children", lambda *a: None)
    monkeypatch.setattr(agent_lifecycle, "_release_file_ownership", lambda *a: None)
    now = [0.0]
    living = {s.id: True for s in sessions}
    sent, killed, shutdown, confirmed = [], [], [], []
    monkeypatch.setattr(mod.platform_compat, "kill_process_group", lambda pgid, sig: sent.append((pgid, sig)) or True)

    def kill(session):
        killed.append(session.id)
        session.status = "dead"

    client = SimpleNamespace(
        post=lambda url, **kw: confirmed.append((url, kw)) or httpx.Response(200, request=httpx.Request("POST", url))
    )
    reaper = StopReaper(tmp_path, clock=lambda: now[0], wall_clock=lambda: 100 + now[0])
    orch = SimpleNamespace(
        _workdir=tmp_path,
        _stop_reaper=reaper,
        _agents={s.id: s for s in sessions},
        _spawner=SimpleNamespace(kill=kill, check_alive=lambda s: living[s.id]),
        _signal_mgr=SimpleNamespace(write_shutdown=lambda *a, **kw: shutdown.append(a), clear_signals=lambda *a: None),
        _client=client,
        _config=SimpleNamespace(server_url="http://local"),
    )
    return orch, SimpleNamespace(reaped=[]), now, living, sent, killed, shutdown, confirmed


def session(sid="s", tasks=None, pgid=987654):
    return AgentSession(id=sid, role="worker", task_ids=tasks or ["t"], pgid=pgid, status="working")


def test_driver_observation_receipt(tmp_path, monkeypatch):
    orch, result, now, living, sent, killed, shutdown, confirmed = driver(tmp_path, monkeypatch, [session()])
    snapshot = {"stop_req": [SimpleNamespace(id="t", title="Task")]}
    reap_stop_requests(orch, result, snapshot)
    assert len(shutdown) == 1
    now[0] = 5
    reap_stop_requests(orch, result, snapshot)
    assert sent == [(987654, signal.SIGINT)] and not confirmed
    living["s"] = False
    reap_stop_requests(orch, result, snapshot)
    assert result.reaped == ["s"] and killed == ["s"] and len(confirmed) == 1
    chain = AuditChainStore(tmp_path / ".sdd/audit")
    assert chain.verify()[0]
    assert chain.query(event_type="process.reap_receipt")[0].details["reason"] == "stop_req"


def test_fallback_dead_status_is_not_observation(tmp_path, monkeypatch):
    orch, result, _now, living, _sent, killed, _shutdown, confirmed = driver(
        tmp_path, monkeypatch, [session(pgid=None)]
    )
    snapshot = {"stop_req": [SimpleNamespace(id="t", title="Task")]}
    for _ in range(3):
        reap_stop_requests(orch, result, snapshot)
    assert killed == ["s"] and not confirmed
    assert json.loads((tmp_path / ".sdd/runtime/stop_reaper.heartbeat.json").read_text())["pending"] == 1
    living["s"] = False
    reap_stop_requests(orch, result, snapshot)
    assert len(confirmed) == 1


def test_multiple_and_shared_sessions(tmp_path, monkeypatch):
    orch, result, now, living, sent, _killed, shutdown, confirmed = driver(
        tmp_path, monkeypatch, [session("a", ["t", "u"]), session("b", ["t"])]
    )
    snapshot = {"stop_req": [SimpleNamespace(id=i, title=i) for i in ["u", "t"]]}
    reap_stop_requests(orch, result, snapshot)
    now[0] = 5
    reap_stop_requests(orch, result, snapshot)
    assert len(sent) == 2 and len(shutdown) == 2
    living["a"] = False
    reap_stop_requests(orch, result, snapshot)
    assert [c[0] for c in confirmed] == ["http://local/tasks/u/cancel"]
    living["b"] = False
    reap_stop_requests(orch, result, {"stop_req": snapshot["stop_req"][1:]})
    assert result.reaped == ["a", "b"]
    assert confirmed[-1][0] == "http://local/tasks/t/cancel"


def test_no_sessions_sorted_and_http_retry(tmp_path, monkeypatch):
    orch, result, _now, _living, _sent, _killed, _shutdown, _confirmed = driver(tmp_path, monkeypatch, [])
    calls = []

    def post(url, **kw):
        calls.append(url)
        return httpx.Response(500 if len(calls) == 1 else 200, request=httpx.Request("POST", url))

    orch._client.post = post
    snapshot = {"stop_req": [SimpleNamespace(id=i, title=i) for i in ["z", "a"]]}
    reap_stop_requests(orch, result, snapshot)
    assert calls == ["http://local/tasks/a/cancel", "http://local/tasks/z/cancel"]
    reap_stop_requests(orch, result, {"stop_req": snapshot["stop_req"][1:]})
    assert calls[-1] == "http://local/tasks/a/cancel"


@pytest.mark.parametrize("unsafe", [True, False])
def test_fail_closed(tmp_path, monkeypatch, unsafe):
    orch, result, now, _living, sent, killed, _shutdown, confirmed = driver(
        tmp_path, monkeypatch, [session(pgid=0 if unsafe else 987654)]
    )
    snapshot = {"stop_req": [SimpleNamespace(id="t", title="Task")]}
    for tick in [0, 5, 15, 30, 35, 40, 45, 100]:
        now[0] = tick
        reap_stop_requests(orch, result, snapshot)
    assert not killed and not confirmed
    assert len(sent) == (0 if unsafe else 5)
    data = json.loads((tmp_path / ".sdd/runtime/stop_reaper.heartbeat.json").read_text())
    assert data["pending"] == 1 and data["unverified"] == ["t"]
    reap_stop_requests(orch, result, {"stop_req": []})
    assert not orch._stop_reaper.entries


@pytest.mark.parametrize("failure", ["receipt", "cleanup"])
def test_finalization_retries_before_confirm(tmp_path, monkeypatch, failure):
    from bernstein.core.agents import agent_lifecycle, spawner_core

    orch, result, _now, living, _sent, _killed, _shutdown, confirmed = driver(tmp_path, monkeypatch, [session()])
    living["s"] = False
    calls = []
    original = spawner_core.emit_process_reap_receipt

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("transient")
        if failure == "receipt":
            original(*args, **kwargs)

    if failure == "receipt":
        monkeypatch.setattr(spawner_core, "emit_process_reap_receipt", flaky)
    else:
        monkeypatch.setattr(agent_lifecycle, "_release_file_ownership", flaky)
    snapshot = {"stop_req": [SimpleNamespace(id="t", title="Task")]}
    reap_stop_requests(orch, result, snapshot)
    assert not confirmed and not result.reaped
    reap_stop_requests(orch, result, snapshot)
    assert len(confirmed) == 1 and result.reaped == ["s"]
    chain = AuditChainStore(tmp_path / ".sdd/audit")
    assert len(chain.query(event_type="process.reap_receipt")) == 1


def test_heartbeat_task_identity_change(tmp_path):
    reaper = StopReaper(tmp_path, clock=lambda: 0.0, wall_clock=lambda: 100.0)
    reaper.track("a", "s", 20)
    reaper.beat(1, [])
    path = tmp_path / ".sdd/runtime/stop_reaper.heartbeat.json"
    path.write_text("sentinel")
    reaper.forget("a")
    reaper.track("b", "s", 20)
    reaper.beat(1, [])
    assert json.loads(path.read_text())["pending"] == 1


def test_fallback_kill_failure_retries(tmp_path, monkeypatch):
    orch, result, _now, living, _sent, _killed, _shutdown, confirmed = driver(
        tmp_path, monkeypatch, [session(pgid=None)]
    )
    calls = []

    def flaky(session):
        calls.append(session.id)
        session.status = "dead"
        if len(calls) == 1:
            raise OSError("temporary")

    orch._spawner.kill = flaky
    snapshot = {"stop_req": [SimpleNamespace(id="t", title="Task")]}
    reap_stop_requests(orch, result, snapshot)
    reap_stop_requests(orch, result, snapshot)
    assert calls == ["s", "s"] and not confirmed
    living["s"] = False
    reap_stop_requests(orch, result, snapshot)
    assert result.reaped == ["s"] and len(confirmed) == 1


@pytest.mark.parametrize("previous,current,warns", [(0, 1, True), (1, 0, False)])
def test_stale_heartbeat_current_pending(tmp_path, caplog, previous, current, warns):
    path = tmp_path / ".sdd/runtime/stop_reaper.heartbeat.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"ts": 0, "pending": previous}))
    StopReaper(tmp_path, wall_clock=lambda: 100).beat(current, [])
    assert ("gap" in caplog.text) == warns


@pytest.mark.parametrize("payload", ["[]", "null", "42", '{"ts": "nan"}', '{"ts": "inf"}'])
def test_corrupt_heartbeat_shapes(tmp_path, payload):
    path = tmp_path / ".sdd/runtime/stop_reaper.heartbeat.json"
    path.parent.mkdir(parents=True)
    path.write_text(payload)
    assert heartbeat_age_s(tmp_path, 100) is None
    StopReaper(tmp_path).beat(1, [])


def test_disappearing_session_remains_pending(tmp_path, monkeypatch):
    orch, result, _now, living, _sent, _killed, _shutdown, confirmed = driver(
        tmp_path, monkeypatch, [session(pgid=None)]
    )
    snapshot = {"stop_req": [SimpleNamespace(id="t", title="Task")]}
    reap_stop_requests(orch, result, snapshot)
    orch._agents.clear()
    reap_stop_requests(orch, result, snapshot)
    assert not confirmed
    living["s"] = False
    reap_stop_requests(orch, result, snapshot)
    assert result.reaped == ["s"] and len(confirmed) == 1
    reap_stop_requests(orch, result, {"stop_req": []})
    assert not orch._stop_reaper._session_objects
