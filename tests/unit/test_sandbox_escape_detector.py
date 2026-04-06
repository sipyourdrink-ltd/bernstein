"""Tests for SEC-012: Sandbox escape detection for containerized agents."""

from __future__ import annotations

from bernstein.core.sandbox_escape_detector import (
    BoundaryConfig,
    SandboxEscapeDetector,
    ViolationSeverity,
    ViolationType,
)


class TestBoundaryConfig:
    def test_defaults(self) -> None:
        config = BoundaryConfig()
        assert "/etc/shadow" in config.denied_paths
        assert config.max_processes == 50
        assert "nsenter" in config.denied_executables

    def test_custom_config(self) -> None:
        config = BoundaryConfig(
            allowed_paths=("/app/*",),
            denied_paths=("/secrets/*",),
            max_processes=10,
        )
        assert config.allowed_paths == ("/app/*",)
        assert config.max_processes == 10


class TestSandboxEscapeDetector:
    def test_denied_path_detected(self) -> None:
        detector = SandboxEscapeDetector()
        violations = detector.check_filesystem("agent-1", ["/etc/shadow"])
        assert len(violations) == 1
        assert violations[0].violation_type == ViolationType.FILESYSTEM_ESCAPE
        assert violations[0].severity == ViolationSeverity.CRITICAL

    def test_allowed_path_passes(self) -> None:
        detector = SandboxEscapeDetector()
        violations = detector.check_filesystem("agent-1", ["/workspace/src/main.py"])
        assert len(violations) == 0

    def test_path_outside_sandbox_warning(self) -> None:
        detector = SandboxEscapeDetector()
        violations = detector.check_filesystem("agent-1", ["/opt/secret/file"])
        assert len(violations) == 1
        assert violations[0].severity == ViolationSeverity.WARNING

    def test_docker_socket_denied(self) -> None:
        detector = SandboxEscapeDetector()
        violations = detector.check_filesystem("agent-1", ["/var/run/docker.sock"])
        assert len(violations) == 1
        assert violations[0].severity == ViolationSeverity.CRITICAL

    def test_unauthorized_host(self) -> None:
        detector = SandboxEscapeDetector()
        connections = [{"host": "evil.example.com", "port": 443}]
        violations = detector.check_network("agent-1", connections)
        assert any(v.violation_type == ViolationType.NETWORK_VIOLATION for v in violations)

    def test_allowed_host_passes(self) -> None:
        detector = SandboxEscapeDetector()
        connections = [{"host": "127.0.0.1", "port": 8052}]
        violations = detector.check_network("agent-1", connections)
        assert len(violations) == 0

    def test_unauthorized_port(self) -> None:
        detector = SandboxEscapeDetector()
        connections = [{"host": "127.0.0.1", "port": 22}]
        violations = detector.check_network("agent-1", connections)
        assert any(v.violation_type == ViolationType.NETWORK_VIOLATION for v in violations)

    def test_process_count_exceeded(self) -> None:
        detector = SandboxEscapeDetector(BoundaryConfig(max_processes=2))
        processes = [
            {"name": "bash", "pid": 1},
            {"name": "python", "pid": 2},
            {"name": "node", "pid": 3},
        ]
        violations = detector.check_processes("agent-1", processes)
        assert any(v.violation_type == ViolationType.PROCESS_VIOLATION for v in violations)

    def test_denied_executable(self) -> None:
        detector = SandboxEscapeDetector()
        processes = [{"name": "nsenter", "pid": 100}]
        violations = detector.check_processes("agent-1", processes)
        assert len(violations) == 1
        assert violations[0].severity == ViolationSeverity.CRITICAL

    def test_safe_executable_passes(self) -> None:
        detector = SandboxEscapeDetector()
        processes = [{"name": "python", "pid": 100}]
        violations = detector.check_processes("agent-1", processes)
        assert len(violations) == 0

    def test_unauthorized_mount(self) -> None:
        detector = SandboxEscapeDetector()
        violations = detector.check_mounts("agent-1", ["/host"])
        assert len(violations) == 1
        assert violations[0].violation_type == ViolationType.MOUNT_VIOLATION

    def test_allowed_mount(self) -> None:
        detector = SandboxEscapeDetector()
        violations = detector.check_mounts("agent-1", ["/workspace"])
        assert len(violations) == 0

    def test_violations_accumulated(self) -> None:
        detector = SandboxEscapeDetector()
        detector.check_filesystem("agent-1", ["/etc/shadow"])
        detector.check_filesystem("agent-1", ["/etc/passwd"])
        assert len(detector.violations) == 2

    def test_clear_violations(self) -> None:
        detector = SandboxEscapeDetector()
        detector.check_filesystem("agent-1", ["/etc/shadow"])
        detector.clear_violations()
        assert len(detector.violations) == 0

    def test_has_critical_violations(self) -> None:
        detector = SandboxEscapeDetector()
        assert not detector.has_critical_violations()
        detector.check_filesystem("agent-1", ["/etc/shadow"])
        assert detector.has_critical_violations()

    def test_multiple_paths_mixed(self) -> None:
        detector = SandboxEscapeDetector()
        violations = detector.check_filesystem(
            "agent-1",
            ["/workspace/main.py", "/etc/shadow", "/tmp/data.json"],
        )
        assert len(violations) == 1
