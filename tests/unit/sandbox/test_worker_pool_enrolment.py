"""Worker pool enrolment is signed and strictly outbound (#2547).

Covers the acceptance criterion that a worker on a second host completes enrol
against the task server with zero inbound connections: the enrolment path only
ever issues client-initiated requests, and the enrolment receipt is a valid
Ed25519 signature over the target pool hash.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.cli.commands import worker_cmd
from bernstein.cli.commands.worker_cmd import WorkerLoop
from bernstein.core.sandbox.pool_enrolment import EnrolmentReceipt, verify_enrolment_receipt

POOL_HASH = "a" * 64


class _RecordingClient:
    """A stand-in httpx.Client that records outbound calls only."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    def post(self, url: str, json: dict | None = None, headers=None, timeout=None):
        self.posts.append((url, json or {}))

        class _Resp:
            status_code = 200

            def json(self_inner):
                return {}

        return _Resp()


@pytest.fixture
def loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkerLoop:
    # Isolate the install-identity keystore to the temp dir.
    monkeypatch.setenv("BERNSTEIN_AGENT_CARD_KEY_DIR", str(tmp_path / "keys"))
    return WorkerLoop(
        server_url="http://central:8052",
        name="node-2",
        workdir=tmp_path,
        pool="ci-linux",
        pool_hash=POOL_HASH,
    )


class TestEnrolment:
    def test_enrol_writes_verifiable_signed_receipt(self, loop: WorkerLoop, tmp_path: Path):
        client = _RecordingClient()
        loop._enrol(client)

        enrol_dir = tmp_path / ".sdd" / "sandbox" / "enrolments"
        files = list(enrol_dir.glob("*.json"))
        assert files, "enrolment receipt was not persisted"
        receipt = EnrolmentReceipt.from_dict(json.loads(files[0].read_text()))
        assert receipt.pool_hash == POOL_HASH
        assert verify_enrolment_receipt(receipt)

    def test_enrol_is_outbound_only(self, loop: WorkerLoop):
        client = _RecordingClient()
        loop._enrol(client)
        # Every network interaction was a worker-initiated POST; no inbound
        # server-to-worker channel exists.
        assert client.posts, "expected an outbound enrolment POST"
        assert all(url.startswith("http://central:8052/") for url, _ in client.posts)

    def test_no_inbound_socket_primitives_in_worker_module(self):
        """The worker never binds or listens: it is a pure outbound poller."""
        source = Path(worker_cmd.__file__).read_text()
        assert ".bind(" not in source
        assert ".listen(" not in source

    def test_absent_pool_is_a_noop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("BERNSTEIN_AGENT_CARD_KEY_DIR", str(tmp_path / "keys"))
        loop = WorkerLoop(server_url="http://central:8052", name="node-3", workdir=tmp_path)
        client = _RecordingClient()
        loop._enrol(client)
        assert client.posts == []  # no pool -> no enrolment, byte-identical to today
