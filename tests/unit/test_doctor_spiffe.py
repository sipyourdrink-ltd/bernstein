"""Doctor preflight for the SPIFFE workload-API credential path (issue #2516).

With the ``spiffe`` extra absent the check is an informational PASS: the
default Ed25519 identity path is active and nothing is broken. With the extra
present and a reachable Workload API socket the check reports the SVID path
green; with the extra present but the socket unset or missing it warns.
"""

from __future__ import annotations

import socket as _socket

from bernstein.cli.commands import doctor_cmd


class TestSpiffeDoctorCheck:
    def test_extra_absent_is_informational_pass(self, monkeypatch) -> None:
        monkeypatch.setattr(doctor_cmd, "_spiffe_extra_available", lambda: False)
        result = doctor_cmd.check_spiffe_workload_api()
        assert result["name"] == "SPIFFE workload API"
        assert result["status"] == "PASS"
        assert "extra" in result["detail"].lower()

    def test_extra_present_socket_unset_warns(self, monkeypatch) -> None:
        monkeypatch.setattr(doctor_cmd, "_spiffe_extra_available", lambda: True)
        monkeypatch.delenv("SPIFFE_ENDPOINT_SOCKET", raising=False)
        result = doctor_cmd.check_spiffe_workload_api()
        assert result["status"] == "WARN"

    def test_extra_present_socket_missing_warns(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(doctor_cmd, "_spiffe_extra_available", lambda: True)
        monkeypatch.setenv("SPIFFE_ENDPOINT_SOCKET", f"unix://{tmp_path}/nope.sock")
        result = doctor_cmd.check_spiffe_workload_api()
        assert result["status"] == "WARN"

    def test_extra_present_socket_reachable_passes(self, monkeypatch) -> None:
        import os
        import tempfile

        # AF_UNIX paths are capped near 104 chars; the session scratchpad
        # tmp_path is longer, so bind under a short dedicated temp dir.
        sock_dir = tempfile.mkdtemp(prefix="bstn-spf-", dir="/tmp")
        sock_path = os.path.join(sock_dir, "agent.sock")
        srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        srv.bind(sock_path)
        srv.listen(1)
        try:
            monkeypatch.setattr(doctor_cmd, "_spiffe_extra_available", lambda: True)
            monkeypatch.setenv("SPIFFE_ENDPOINT_SOCKET", f"unix://{sock_path}")
            result = doctor_cmd.check_spiffe_workload_api()
            assert result["status"] == "PASS"
            assert "SVID" in result["detail"] or "reachable" in result["detail"].lower()
        finally:
            srv.close()
            os.unlink(sock_path)
            os.rmdir(sock_dir)

    def test_check_is_wired_into_run_all_checks(self) -> None:
        names = {c["name"] for c in doctor_cmd.run_all_checks()}
        assert "SPIFFE workload API" in names
