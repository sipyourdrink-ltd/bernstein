"""Kubernetes operator for BernsteinPlan and BernsteinRun CRDs.

Watches custom resources and manages agent Jobs, bridging K8s-native
workflows to the Bernstein task server.  Requires the ``kubernetes``
package (``pip install bernstein[k8s]``).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

try:
    from kubernetes import client, config

    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False

logger = logging.getLogger(__name__)

CRD_GROUP = "bernstein.io"
CRD_VERSION = "v1"
PLAN_PLURAL = "bernsteinplans"
RUN_PLURAL = "bernsteinruns"


@dataclass(frozen=True)
class OperatorConfig:
    """Configuration for the Bernstein K8s operator."""

    namespace: str = ""
    server_url: str = "http://bernstein-server:8052"  # Local-only endpoint, HTTPS not applicable
    agent_image: str = "bernstein:latest"
    agent_image_pull_policy: str = "IfNotPresent"
    reconcile_interval_s: float = 10.0
    job_backoff_limit: int = 2
    job_ttl_after_finished: int = 600
    default_cpu_request: str = "500m"
    default_memory_request: str = "512Mi"
    default_cpu_limit: str = "2000m"
    default_memory_limit: str = "2Gi"
    auth_secret_name: str = "bernstein-auth"
    auth_secret_key: str = "auth-token"
    provider_keys_secret: str = ""


@dataclass
class _RunCounters:
    """Mutable counters for tracking run advancement."""

    completed: int = 0
    failed: int = 0
    active_jobs: int = 0
    all_done: bool = True


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _job_name(run_name: str, stage_idx: int, step_idx: int) -> str:
    return f"{run_name}-s{stage_idx}-t{step_idx}"


class PlanReconciler:
    """Reconciles BernsteinPlan resources — validates and updates status."""

    def __init__(self, crd_api: Any, cfg: OperatorConfig) -> None:
        self._api = crd_api
        self._cfg = cfg

    def reconcile(self, plan: dict[str, Any]) -> None:
        meta = plan["metadata"]
        spec = plan.get("spec", {})
        name = meta["name"]
        ns = meta["namespace"]
        stages = spec.get("stages", [])

        errors: list[str] = []
        if not stages:
            errors.append("spec.stages must contain at least one stage")
        for i, stage in enumerate(stages):
            if not stage.get("steps"):
                errors.append(f"stage[{i}] ({stage.get('name', '?')}) has no steps")
            for dep in stage.get("dependsOn", []):
                if dep not in {s.get("name") for s in stages}:
                    errors.append(f"stage[{i}] depends on unknown stage '{dep}'")

        total_steps = sum(len(s.get("steps", [])) for s in stages)
        phase = "Validated" if not errors else "Failed"

        status: dict[str, Any] = {
            "phase": phase,
            "totalStages": len(stages),
            "totalSteps": total_steps,
            "lastTransitionTime": _now_iso(),
        }
        if errors:
            status["validationErrors"] = errors

        self._patch_status(ns, name, status)
        logger.info("Plan %s/%s → %s (%d stages, %d steps)", ns, name, phase, len(stages), total_steps)

    def _patch_status(self, ns: str, name: str, status: dict[str, Any]) -> None:
        body = {"status": status}
        self._api.patch_namespaced_custom_object_status(CRD_GROUP, CRD_VERSION, ns, PLAN_PLURAL, name, body)


class RunReconciler:
    """Reconciles BernsteinRun resources — creates and monitors agent Jobs."""

    def __init__(
        self,
        crd_api: Any,
        batch_api: Any,
        core_api: Any,
        cfg: OperatorConfig,
    ) -> None:
        self._crd = crd_api
        self._batch = batch_api
        self._core = core_api
        self._cfg = cfg

    def reconcile(self, run: dict[str, Any]) -> None:
        meta = run["metadata"]
        spec = run.get("spec", {})
        status = run.get("status", {})
        name = meta["name"]
        ns = meta["namespace"]
        phase = status.get("phase", "Pending")

        if phase in ("Completed", "Failed", "Cancelled"):
            return

        plan_name = spec["planRef"]
        plan = self._get_plan(ns, plan_name)
        if plan is None:
            self._patch_status(
                ns,
                name,
                {
                    "phase": "Failed",
                    "conditions": [
                        {
                            "type": "PlanResolved",
                            "status": "False",
                            "reason": "PlanNotFound",
                            "message": f"BernsteinPlan '{plan_name}' not found",
                            "lastTransitionTime": _now_iso(),
                        }
                    ],
                },
            )
            return

        plan_status = plan.get("status", {})
        if plan_status.get("phase") != "Validated":
            return

        plan_spec = plan["spec"]
        stages = plan_spec.get("stages", [])

        if phase == "Pending":
            self._initialize_run(ns, name, stages)
            return

        self._advance_run(ns, name, run, plan_spec)

    def _get_plan(self, ns: str, name: str) -> dict[str, Any] | None:
        try:
            return self._crd.get_namespaced_custom_object(CRD_GROUP, CRD_VERSION, ns, PLAN_PLURAL, name)
        except Exception:
            return None

    def _initialize_run(self, ns: str, name: str, stages: list[dict[str, Any]]) -> None:
        total_steps = sum(len(s.get("steps", [])) for s in stages)
        stage_statuses = []
        for stage in stages:
            step_statuses = [
                {"goal": step["goal"], "phase": "Pending", "retries": 0} for step in stage.get("steps", [])
            ]
            stage_statuses.append(
                {
                    "name": stage["name"],
                    "phase": "Pending",
                    "steps": step_statuses,
                }
            )

        self._patch_status(
            ns,
            name,
            {
                "phase": "Running",
                "startTime": _now_iso(),
                "totalSteps": total_steps,
                "completedSteps": 0,
                "failedSteps": 0,
                "activeJobs": 0,
                "stages": stage_statuses,
            },
        )
        logger.info("Run %s/%s initialized with %d steps", ns, name, total_steps)

    def _process_running_step(
        self,
        ns: str,
        step_st: dict[str, Any],
        max_retries: int,
    ) -> tuple[int, int, int, bool]:
        """Handle a step in Running phase. Returns (completed, failed, active, stage_failed) deltas."""
        job_name = step_st.get("jobName", "")
        if not job_name:
            return 0, 0, 0, False
        job_phase = self._check_job(ns, job_name)
        if job_phase == "succeeded":
            step_st["phase"] = "Completed"
            step_st["completionTime"] = _now_iso()
            return 1, 0, 0, False
        if job_phase == "failed":
            retries = step_st.get("retries", 0)
            if retries < max_retries:
                step_st["retries"] = retries + 1
                step_st["phase"] = "Pending"
                step_st["error"] = "Job failed, retrying"
                return 0, 0, 0, False
            step_st["phase"] = "Failed"
            step_st["completionTime"] = _now_iso()
            step_st["error"] = "Max retries exceeded"
            return 0, 1, 0, True
        return 0, 0, 1, False

    def _launch_pending_step(
        self,
        ns: str,
        name: str,
        si: int,
        ti: int,
        plan_stages: list[dict[str, Any]],
        plan_spec: dict[str, Any],
        spec: dict[str, Any],
        step_st: dict[str, Any],
    ) -> None:
        """Launch a job for a Pending step."""
        plan_step = plan_stages[si]["steps"][ti]
        job_name_str = _job_name(name, si, ti)
        self._create_agent_job(ns, name, job_name_str, plan_step, plan_spec, spec)
        step_st["phase"] = "Running"
        step_st["jobName"] = job_name_str
        step_st["startTime"] = _now_iso()

    def _update_stage_phase(
        self, stage_st: dict[str, Any], stage_done: bool, stage_failed: bool, deps_met: bool
    ) -> None:
        """Update a stage's phase based on its steps' completion state."""
        if stage_done and not stage_failed:
            stage_st["phase"] = "Completed"
            stage_st["completionTime"] = _now_iso()
        elif stage_failed:
            stage_st["phase"] = "Failed"
            stage_st["completionTime"] = _now_iso()
        elif stage_st["phase"] == "Pending" and deps_met:
            stage_st["phase"] = "Running"
            stage_st["startTime"] = _now_iso()

    @staticmethod
    def _determine_run_phase(all_done: bool, failed: int, active_jobs: int, stages_status: list[dict[str, Any]]) -> str:
        """Compute overall run phase from aggregate step counts."""
        if all_done and failed == 0:
            return "Completed"
        if all_done or (failed > 0 and active_jobs == 0):
            has_pending = any(step.get("phase") == "Pending" for ss in stages_status for step in ss.get("steps", []))
            if not has_pending:
                return "Failed" if failed > 0 else "Completed"
        return "Running"

    def _advance_run(
        self,
        ns: str,
        name: str,
        run: dict[str, Any],
        plan_spec: dict[str, Any],
    ) -> None:
        status = run.get("status", {})
        stages_status = status.get("stages", [])
        plan_stages = plan_spec.get("stages", [])
        spec = run.get("spec", {})
        max_retries = spec.get("maxRetries", 2)

        counters = _RunCounters()

        for si, (plan_stage, stage_st) in enumerate(zip(plan_stages, stages_status, strict=False)):
            self._advance_stage(
                ns, name, si, plan_stage, stage_st, plan_stages, plan_spec, spec, max_retries, stages_status, counters
            )

        run_phase = self._determine_run_phase(counters.all_done, counters.failed, counters.active_jobs, stages_status)

        patch: dict[str, Any] = {
            "phase": run_phase,
            "completedSteps": counters.completed,
            "failedSteps": counters.failed,
            "activeJobs": counters.active_jobs,
            "stages": stages_status,
        }
        if run_phase in ("Completed", "Failed"):
            patch["completionTime"] = _now_iso()

        self._patch_status(ns, name, patch)

    def _advance_stage(
        self,
        ns: str,
        name: str,
        si: int,
        plan_stage: dict[str, Any],
        stage_st: dict[str, Any],
        plan_stages: list[dict[str, Any]],
        plan_spec: dict[str, Any],
        spec: dict[str, Any],
        max_retries: int,
        stages_status: list[dict[str, Any]],
        counters: _RunCounters,
    ) -> None:
        """Process a single stage within a run."""
        deps_met = self._deps_met(plan_stage.get("dependsOn", []), stages_status)
        if not deps_met:
            if stage_st["phase"] == "Pending":
                counters.all_done = False
            return

        stage_done = True
        stage_failed = False

        for ti, step_st in enumerate(stage_st.get("steps", [])):
            sd, sf = self._process_step(ns, name, si, ti, step_st, plan_stages, plan_spec, spec, max_retries, counters)
            if not sd:
                stage_done = False
            if sf:
                stage_failed = True

        self._update_stage_phase(stage_st, stage_done, stage_failed, deps_met)

    def _process_step(
        self,
        ns: str,
        name: str,
        si: int,
        ti: int,
        step_st: dict[str, Any],
        plan_stages: list[dict[str, Any]],
        plan_spec: dict[str, Any],
        spec: dict[str, Any],
        max_retries: int,
        counters: _RunCounters,
    ) -> tuple[bool, bool]:
        """Process a single step. Returns (is_done, is_failed)."""
        step_phase = step_st.get("phase", "Pending")

        if step_phase == "Completed":
            counters.completed += 1
            return True, False
        if step_phase == "Failed":
            counters.failed += 1
            return True, True

        counters.all_done = False

        if step_phase == "Running":
            c, f, a, sf = self._process_running_step(ns, step_st, max_retries)
            counters.completed += c
            counters.failed += f
            counters.active_jobs += a
            return False, sf

        if step_phase == "Pending":
            self._launch_pending_step(ns, name, si, ti, plan_stages, plan_spec, spec, step_st)
            counters.active_jobs += 1

        return False, False

    def _deps_met(self, deps: list[str], stages_status: list[dict[str, Any]]) -> bool:
        if not deps:
            return True
        name_to_status = {s["name"]: s.get("phase") for s in stages_status}
        return all(name_to_status.get(d) == "Completed" for d in deps)

    def _check_job(self, ns: str, job_name: str) -> str:
        try:
            job = self._batch.read_namespaced_job(job_name, ns)
        except Exception:
            return "unknown"
        if job.status.succeeded and job.status.succeeded > 0:
            return "succeeded"
        if job.status.failed and job.status.failed > 0:
            return "failed"
        return "running"

    def _create_agent_job(
        self,
        ns: str,
        run_name: str,
        job_name: str,
        step: dict[str, Any],
        plan_spec: dict[str, Any],
        run_spec: dict[str, Any],
    ) -> None:
        image = run_spec.get("image") or self._cfg.agent_image
        goal = step["goal"]
        role = step.get("role", "backend")
        model = step.get("model") or plan_spec.get("model", "claude-sonnet-4-20250514")
        effort = step.get("effort") or plan_spec.get("effort", "medium")
        cli_agent = plan_spec.get("cliAgent", "claude")

        resources = run_spec.get("resources", {})
        req_cpu = resources.get("requests", {}).get("cpu", self._cfg.default_cpu_request)
        req_mem = resources.get("requests", {}).get("memory", self._cfg.default_memory_request)
        lim_cpu = resources.get("limits", {}).get("cpu", self._cfg.default_cpu_limit)
        lim_mem = resources.get("limits", {}).get("memory", self._cfg.default_memory_limit)

        env = [
            client.V1EnvVar(
                name="BERNSTEIN_SERVER_URL",
                value=run_spec.get("serverUrl") or self._cfg.server_url,
            ),
            client.V1EnvVar(
                name="BERNSTEIN_AUTH_TOKEN",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=self._cfg.auth_secret_name,
                        key=self._cfg.auth_secret_key,
                    )
                ),
            ),
            client.V1EnvVar(name="BERNSTEIN_AGENT_ROLE", value=role),
            client.V1EnvVar(name="BERNSTEIN_TASK_GOAL", value=goal),
            client.V1EnvVar(name="BERNSTEIN_MODEL", value=model),
            client.V1EnvVar(name="BERNSTEIN_EFFORT", value=effort),
        ]

        if self._cfg.provider_keys_secret:
            for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
                env.append(
                    client.V1EnvVar(
                        name=key,
                        value_from=client.V1EnvVarSource(
                            secret_key_ref=client.V1SecretKeySelector(
                                name=self._cfg.provider_keys_secret,
                                key=key,
                                optional=True,
                            )
                        ),
                    )
                )

        container = client.V1Container(
            name="agent",
            image=image,
            image_pull_policy=self._cfg.agent_image_pull_policy,
            command=["bernstein", "agent", "--cli", cli_agent, "--role", role],
            env=env,
            resources=client.V1ResourceRequirements(
                requests={"cpu": req_cpu, "memory": req_mem},
                limits={"cpu": lim_cpu, "memory": lim_mem},
            ),
        )

        node_selector = run_spec.get("nodeSelector")

        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(
                name=job_name,
                namespace=ns,
                labels={
                    "app.kubernetes.io/name": "bernstein",
                    "app.kubernetes.io/component": "agent",
                    "bernstein.io/run": run_name,
                    "bernstein.io/role": role,
                },
                owner_references=[
                    client.V1OwnerReference(
                        api_version=f"{CRD_GROUP}/{CRD_VERSION}",
                        kind="BernsteinRun",
                        name=run_name,
                        uid="",  # Filled by K8s
                        controller=True,
                        block_owner_deletion=True,
                    )
                ],
            ),
            spec=client.V1JobSpec(
                backoff_limit=self._cfg.job_backoff_limit,
                ttl_seconds_after_finished=self._cfg.job_ttl_after_finished,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels={
                            "app.kubernetes.io/name": "bernstein",
                            "app.kubernetes.io/component": "agent",
                            "bernstein.io/run": run_name,
                        },
                    ),
                    spec=client.V1PodSpec(
                        containers=[container],
                        restart_policy="Never",
                        node_selector=node_selector,
                        security_context=client.V1PodSecurityContext(
                            run_as_non_root=True,
                            run_as_user=1000,
                            fs_group=1000,
                        ),
                    ),
                ),
            ),
        )

        try:
            self._batch.create_namespaced_job(ns, job)
            logger.info("Created agent Job %s/%s (role=%s)", ns, job_name, role)
        except client.ApiException as exc:
            if exc.status == 409:
                logger.debug("Job %s/%s already exists", ns, job_name)
            else:
                raise

    def _patch_status(self, ns: str, name: str, status: dict[str, Any]) -> None:
        body = {"status": status}
        self._crd.patch_namespaced_custom_object_status(CRD_GROUP, CRD_VERSION, ns, RUN_PLURAL, name, body)


def _reconcile_resources(
    crd_api: Any,
    ns: str,
    plural: str,
    reconciler: PlanReconciler | RunReconciler,
) -> None:
    """List and reconcile all resources of a given CRD type."""
    items = crd_api.list_namespaced_custom_object(CRD_GROUP, CRD_VERSION, ns, plural)
    for item in items.get("items", []):
        try:
            reconciler.reconcile(item)
        except Exception:
            logger.exception("Failed to reconcile %s %s", plural, item["metadata"]["name"])


class BernsteinOperator:
    """Main operator loop — watches CRDs and delegates to reconcilers."""

    def __init__(self, cfg: OperatorConfig | None = None) -> None:
        if not K8S_AVAILABLE:
            raise RuntimeError("kubernetes package not installed. Install with: pip install bernstein[k8s]")
        self._cfg = cfg or OperatorConfig()
        self._running = False

    def run(self) -> None:
        if os.getenv("KUBERNETES_SERVICE_HOST"):
            config.load_incluster_config()
        else:
            config.load_kube_config()

        crd_api = client.CustomObjectsApi()
        batch_api = client.BatchV1Api()
        core_api = client.CoreV1Api()

        plan_reconciler = PlanReconciler(crd_api, self._cfg)
        run_reconciler = RunReconciler(crd_api, batch_api, core_api, self._cfg)

        ns = self._cfg.namespace or os.getenv("POD_NAMESPACE", "default")
        self._running = True

        logger.info(
            "Bernstein operator started (namespace=%s, server=%s)",
            ns,
            self._cfg.server_url,
        )

        while self._running:
            try:
                _reconcile_resources(crd_api, ns, PLAN_PLURAL, plan_reconciler)
                _reconcile_resources(crd_api, ns, RUN_PLURAL, run_reconciler)
            except Exception:
                logger.exception("Reconcile loop error")

            time.sleep(self._cfg.reconcile_interval_s)

    def stop(self) -> None:
        self._running = False


def main() -> None:
    """Entry point for ``python -m bernstein.core.operator``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = OperatorConfig(
        namespace=os.getenv("POD_NAMESPACE", ""),
        # Local-only endpoint, HTTPS not applicable
        server_url=os.getenv("BERNSTEIN_SERVER_URL", "http://bernstein-server:8052"),
        agent_image=os.getenv("BERNSTEIN_AGENT_IMAGE", "bernstein:latest"),
        auth_secret_name=os.getenv("BERNSTEIN_AUTH_SECRET", "bernstein-auth"),
        provider_keys_secret=os.getenv("BERNSTEIN_PROVIDER_KEYS_SECRET", ""),
    )
    operator = BernsteinOperator(cfg)
    try:
        operator.run()
    except KeyboardInterrupt:
        operator.stop()


if __name__ == "__main__":
    main()
