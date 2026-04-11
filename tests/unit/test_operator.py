"""Unit tests for the Kubernetes operator module."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestOperatorConfig:
    def test_default_config(self) -> None:
        from bernstein.core.operator import OperatorConfig

        cfg = OperatorConfig()
        assert cfg.namespace == ""
        assert cfg.server_url == "http://bernstein-server:8052"
        assert cfg.agent_image == "bernstein:latest"
        assert cfg.job_backoff_limit == 2
        assert cfg.job_ttl_after_finished == 600

    def test_custom_config(self) -> None:
        from bernstein.core.operator import OperatorConfig

        cfg = OperatorConfig(
            namespace="prod",
            server_url="http://custom:9999",
            agent_image="bernstein:v2",
        )
        assert cfg.namespace == "prod"
        assert cfg.server_url == "http://custom:9999"


class TestJobName:
    def test_format(self) -> None:
        from bernstein.core.operator import _job_name

        assert _job_name("my-run", 0, 0) == "my-run-s0-t0"
        assert _job_name("run-abc", 2, 5) == "run-abc-s2-t5"


class TestPlanReconciler:
    def test_validates_empty_stages(self) -> None:
        from bernstein.core.operator import OperatorConfig, PlanReconciler

        mock_api = MagicMock()
        reconciler = PlanReconciler(mock_api, OperatorConfig())

        plan = {
            "metadata": {"name": "test-plan", "namespace": "default"},
            "spec": {"stages": []},
        }
        reconciler.reconcile(plan)

        mock_api.patch_namespaced_custom_object_status.assert_called_once()
        call_args = mock_api.patch_namespaced_custom_object_status.call_args
        status = call_args[0][5]["status"]
        assert status["phase"] == "Failed"
        assert any("at least one stage" in e for e in status["validationErrors"])

    def test_validates_missing_steps(self) -> None:
        from bernstein.core.operator import OperatorConfig, PlanReconciler

        mock_api = MagicMock()
        reconciler = PlanReconciler(mock_api, OperatorConfig())

        plan = {
            "metadata": {"name": "test-plan", "namespace": "default"},
            "spec": {"stages": [{"name": "s1", "steps": []}]},
        }
        reconciler.reconcile(plan)

        call_args = mock_api.patch_namespaced_custom_object_status.call_args
        status = call_args[0][5]["status"]
        assert status["phase"] == "Failed"

    def test_validates_unknown_dependency(self) -> None:
        from bernstein.core.operator import OperatorConfig, PlanReconciler

        mock_api = MagicMock()
        reconciler = PlanReconciler(mock_api, OperatorConfig())

        plan = {
            "metadata": {"name": "test-plan", "namespace": "default"},
            "spec": {
                "stages": [
                    {
                        "name": "s1",
                        "dependsOn": ["nonexistent"],
                        "steps": [{"goal": "do thing"}],
                    }
                ]
            },
        }
        reconciler.reconcile(plan)

        call_args = mock_api.patch_namespaced_custom_object_status.call_args
        status = call_args[0][5]["status"]
        assert status["phase"] == "Failed"
        assert any("nonexistent" in e for e in status["validationErrors"])

    def test_valid_plan_succeeds(self) -> None:
        from bernstein.core.operator import OperatorConfig, PlanReconciler

        mock_api = MagicMock()
        reconciler = PlanReconciler(mock_api, OperatorConfig())

        plan = {
            "metadata": {"name": "test-plan", "namespace": "default"},
            "spec": {
                "stages": [
                    {
                        "name": "build",
                        "steps": [
                            {"goal": "implement feature"},
                            {"goal": "write tests"},
                        ],
                    },
                    {
                        "name": "test",
                        "dependsOn": ["build"],
                        "steps": [{"goal": "run integration tests"}],
                    },
                ]
            },
        }
        reconciler.reconcile(plan)

        call_args = mock_api.patch_namespaced_custom_object_status.call_args
        status = call_args[0][5]["status"]
        assert status["phase"] == "Validated"
        assert status["totalStages"] == 2
        assert status["totalSteps"] == 3


class TestRunReconciler:
    def _make_reconciler(self) -> tuple:
        from bernstein.core.operator import OperatorConfig, RunReconciler

        crd_api = MagicMock()
        batch_api = MagicMock()
        core_api = MagicMock()
        cfg = OperatorConfig(namespace="default")
        return RunReconciler(crd_api, batch_api, core_api, cfg), crd_api, batch_api

    def test_skips_completed_run(self) -> None:
        reconciler, crd_api, _ = self._make_reconciler()
        run = {
            "metadata": {"name": "r1", "namespace": "default"},
            "spec": {"planRef": "p1"},
            "status": {"phase": "Completed"},
        }
        reconciler.reconcile(run)
        crd_api.get_namespaced_custom_object.assert_not_called()

    def test_fails_when_plan_not_found(self) -> None:
        reconciler, crd_api, _ = self._make_reconciler()
        crd_api.get_namespaced_custom_object.side_effect = Exception("not found")

        run = {
            "metadata": {"name": "r1", "namespace": "default"},
            "spec": {"planRef": "missing-plan"},
            "status": {"phase": "Pending"},
        }
        reconciler.reconcile(run)

        crd_api.patch_namespaced_custom_object_status.assert_called_once()
        call_args = crd_api.patch_namespaced_custom_object_status.call_args
        status = call_args[0][5]["status"]
        assert status["phase"] == "Failed"

    def test_initializes_pending_run(self) -> None:
        reconciler, crd_api, _ = self._make_reconciler()

        plan = {
            "metadata": {"name": "p1", "namespace": "default"},
            "spec": {
                "stages": [
                    {"name": "s1", "steps": [{"goal": "task1"}, {"goal": "task2"}]},
                ]
            },
            "status": {"phase": "Validated"},
        }
        crd_api.get_namespaced_custom_object.return_value = plan

        run = {
            "metadata": {"name": "r1", "namespace": "default"},
            "spec": {"planRef": "p1"},
            "status": {"phase": "Pending"},
        }
        reconciler.reconcile(run)

        call_args = crd_api.patch_namespaced_custom_object_status.call_args
        status = call_args[0][5]["status"]
        assert status["phase"] == "Running"
        assert status["totalSteps"] == 2
        assert len(status["stages"]) == 1
        assert len(status["stages"][0]["steps"]) == 2


class TestBernsteinOperator:
    def test_raises_without_k8s(self) -> None:
        from bernstein.core.operator import K8S_AVAILABLE

        if K8S_AVAILABLE:
            pytest.skip("kubernetes package is installed")

        from bernstein.core.operator import BernsteinOperator

        with pytest.raises(RuntimeError, match="kubernetes"):
            BernsteinOperator()
