"""GitLab CI trigger source adapters — normalize GitLab webhook payloads into TriggerEvents."""

from __future__ import annotations

import time
from typing import Any

from bernstein.core.models import TriggerEvent


def normalize_pipeline(payload: dict[str, Any], sender: str, repo: str) -> TriggerEvent:
    """Normalize a GitLab pipeline webhook payload into a TriggerEvent.

    Args:
        payload: Raw GitLab pipeline webhook JSON payload.
        sender: GitLab username that triggered the pipeline.
        repo: Repository path (namespace/project).

    Returns:
        Normalized TriggerEvent.
    """
    pipeline = payload.get("object_attributes", {})
    status = pipeline.get("status", "unknown")
    ref = pipeline.get("ref", "")
    pipeline_url = pipeline.get("url", "")
    pipeline_id = pipeline.get("id", 0)
    sha = pipeline.get("sha", "")

    return TriggerEvent(
        source="gitlab_pipeline",
        timestamp=time.time(),
        raw_payload=payload,
        repo=repo,
        branch=ref,
        sha=sha,
        sender=sender,
        message=f"Pipeline {pipeline_id} {status}",
        metadata={
            "status": status,
            "pipeline_id": pipeline_id,
            "pipeline_url": pipeline_url,
            "ref": ref,
        },
    )


def normalize_job(payload: dict[str, Any], sender: str, repo: str) -> TriggerEvent:
    """Normalize a GitLab job webhook payload into a TriggerEvent.

    Args:
        payload: Raw GitLab job webhook JSON payload.
        sender: GitLab username that triggered the job.
        repo: Repository path (namespace/project).

    Returns:
        Normalized TriggerEvent.
    """
    build = payload.get("build_name", "unknown")
    stage = payload.get("build_stage", "unknown")
    status = payload.get("build_status", "unknown")
    ref = payload.get("build_ref", "")
    sha = payload.get("commit", {}).get("sha", "")
    pipeline_id = payload.get("pipeline_id", 0)
    job_url = payload.get("build_url", "")

    return TriggerEvent(
        source="gitlab_job",
        timestamp=time.time(),
        raw_payload=payload,
        repo=repo,
        branch=ref,
        sha=sha,
        sender=sender,
        message=f"Job '{build}' ({stage}) {status}",
        metadata={
            "status": status,
            "build_name": build,
            "build_stage": stage,
            "pipeline_id": pipeline_id,
            "job_url": job_url,
        },
    )
