"""Doctor advisory for the live OTLP export path (#2526, Phase 4).

Export is off by default: with ``BERNSTEIN_OTEL_ENDPOINT`` unset the check is
an informational PASS (the local JSONL tracing path is active and nothing is
broken). With an endpoint configured the check confirms the optional
``opentelemetry-exporter-otlp-proto-grpc`` extra is importable -- PASS when it
is, WARN when it is missing, because a configured endpoint with no exporter
package silently drops every span. The advisory never fails the doctor run.
"""

from __future__ import annotations

from bernstein.cli.commands import doctor_cmd, status_cmd


class TestOtelExportDoctorCheck:
    def test_endpoint_unset_is_informational_pass(self, monkeypatch) -> None:
        monkeypatch.delenv("BERNSTEIN_OTEL_ENDPOINT", raising=False)
        result = doctor_cmd.check_otel_export_advisory()
        assert result["name"] == "OTel export"
        assert result["status"] == "PASS"
        assert result["fix"] == ""

    def test_endpoint_set_extra_present_passes(self, monkeypatch) -> None:
        monkeypatch.setenv("BERNSTEIN_OTEL_ENDPOINT", "http://otel-collector:4317")
        monkeypatch.setattr(doctor_cmd, "_otlp_exporter_extra_available", lambda: True)
        result = doctor_cmd.check_otel_export_advisory()
        assert result["status"] == "PASS"

    def test_endpoint_set_extra_missing_warns(self, monkeypatch) -> None:
        monkeypatch.setenv("BERNSTEIN_OTEL_ENDPOINT", "http://otel-collector:4317")
        monkeypatch.setattr(doctor_cmd, "_otlp_exporter_extra_available", lambda: False)
        result = doctor_cmd.check_otel_export_advisory()
        assert result["status"] == "WARN"
        assert "bernstein[otel]" in result["fix"]

    def test_blank_endpoint_is_treated_as_unset(self, monkeypatch) -> None:
        monkeypatch.setenv("BERNSTEIN_OTEL_ENDPOINT", "   ")
        # A blank endpoint means export is off; the missing extra is irrelevant.
        monkeypatch.setattr(doctor_cmd, "_otlp_exporter_extra_available", lambda: False)
        result = doctor_cmd.check_otel_export_advisory()
        assert result["status"] == "PASS"

    def test_check_is_wired_into_run_all_checks(self) -> None:
        names = {c["name"] for c in doctor_cmd.run_all_checks()}
        assert "OTel export" in names

    def test_check_is_wired_into_status_doctor(self, monkeypatch) -> None:
        monkeypatch.setenv("BERNSTEIN_OTEL_ENDPOINT", "http://otel-collector:4317")
        monkeypatch.setattr(doctor_cmd, "_otlp_exporter_extra_available", lambda: False)
        checks: list[dict[str, object]] = []
        status_cmd._doctor_check_otel_export(checks)
        row = next(c for c in checks if c["name"] == "OTel export")
        # Advisory only: never fails the doctor run, but the WARN detail stays visible.
        assert row["ok"] is True
        assert "WARNING" in str(row["detail"])
