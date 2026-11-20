"""Resolve an eligible PR from GitHub metadata and validate its read-job artifact."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

# -I excludes both the invocation directory and PYTHONPATH. Import only our
# sibling renderer from the default-branch checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from observability.build_pr_summary import render_publication

LIMIT = 64 * 1024
POLICY = ".github/workflows/pr-policy.yml"
SUMMARY = ".github/workflows/pr-observability-summary.yml"
BOTS = {"dependabot[bot]", "renovate[bot]", "github-actions[bot]", "bernstein[bot]", "bernstein-orchestrator[bot]"}


def api(path: str) -> Any:
    return json.loads(subprocess.check_output(["gh", "api", path]))


def artifact(repo: str, run: dict, name: str, member: str) -> bytes | None:
    listing = api(f"repos/{repo}/actions/runs/{run['id']}/artifacts?name={name}-{run['run_attempt']}")
    if listing["total_count"] == 0:
        return None  # An ineligible PR never uploads a result.
    (item,) = listing["artifacts"]
    if item["expired"] or item["size_in_bytes"] > LIMIT + 4096:
        raise ValueError("Expired or oversized publication artifact")
    url = f"repos/{repo}/actions/artifacts/{int(item['id'])}/zip"
    with subprocess.Popen(["gh", "api", url], stdout=subprocess.PIPE) as fetch:
        assert fetch.stdout is not None
        compressed = fetch.stdout.read(LIMIT + 4097)
        if len(compressed) > LIMIT + 4096:
            fetch.kill()
            raise ValueError("Oversized publication archive")
        if fetch.wait() != 0:
            raise RuntimeError("Artifact download failed")
    return read_member(compressed, member)


def read_member(compressed: bytes, member: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(compressed)) as archive:
        (entry,) = archive.infolist()
        if entry.filename != member or entry.file_size > LIMIT:
            raise ValueError("Unexpected artifact member or size")
        if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ValueError("Unsupported artifact compression")
        # Never extract paths supplied by the producer into the workspace.
        with archive.open(entry) as stream:
            return stream.read(LIMIT + 1)


def prepare(event: dict) -> dict[str, str]:
    run, repo = event["workflow_run"], event["repository"]
    if run["conclusion"] != "success" or run["head_repository"]["id"] != repo["id"]:
        return {}
    if run["path"] not in (SUMMARY, POLICY):
        return {}
    summary = run["path"] == SUMMARY
    repository = repo["full_name"]
    sha = run["head_sha"]
    if not re.fullmatch("[0-9a-f]{40}", sha):
        raise ValueError("Invalid triggering SHA")
    if run["event"] == "workflow_dispatch":
        if not summary or run["head_branch"] != repo["default_branch"]:
            return {}
        # A tag can share a branch name. Require history from the default branch.
        comparison = api(f"repos/{repository}/compare/{sha}...{quote(repo['default_branch'], safe='')}")
        if comparison["status"] not in {"ahead", "identical"}:
            return {}
        match = re.fullmatch(r"PR observability summary for ([1-9][0-9]*)", run["display_title"])
        if not match:
            raise ValueError("Dispatch requires a numeric PR number")
        number = int(match[1])
        pr = api(f"repos/{repository}/pulls/{number}")
        branch = ""
    elif run["event"] == "pull_request" and len(run["pull_requests"]) == 1:
        (association,) = run["pull_requests"]
        number = int(association["number"])
        if not summary and run["display_title"] not in {
            f"PR policy for {number} ({action})" for action in ("opened", "synchronize", "ready_for_review")
        }:
            return {}
        pr = api(f"repos/{repository}/pulls/{number}")
        head = pr["head"]
        if (
            pr["state"] != "open"
            or head["repo"] is None
            or head["repo"]["id"] != repo["id"]
            or head["ref"] != run["head_branch"]
            or association["head"]["sha"] != sha
        ):
            return {}
        labels = {label["name"] for label in pr["labels"]}
        branch = head["ref"]
        if summary and "deep-review" not in labels:
            return {}
        if not summary and (pr["user"]["login"] in BOTS or "skip-autosync" in labels):
            return {}
    else:
        return {}

    data = artifact(
        repository,
        run,
        "pr-observations" if summary else "pr-autosync",
        "observe.json" if summary else "autosync.patch",
    )
    if data is None or (not summary and not data):
        return {}
    if branch and pr["head"]["sha"] != sha:
        if summary:
            return {}
        raise ValueError("The PR head advanced during autosync")
    if summary:
        body = render_publication(data, number, branch)
        return {
            "kind": "comment",
            "number": str(number),
            "sha": sha if branch else "",
            "data": base64.b64encode(body).decode("ascii"),
        }
    if branch in (repo["default_branch"], pr["base"]["ref"]):
        raise ValueError("Autosync cannot update the default or target branch")
    subprocess.run(["git", "check-ref-format", f"refs/heads/{branch}"], check=True)
    if api(f"repos/{repository}/branches/{quote(branch, safe='')}")["protected"]:
        raise ValueError("Autosync cannot update a protected branch")
    return {
        "kind": "autosync",
        "number": str(number),
        "ref": branch,
        "sha": sha,
        "data": base64.b64encode(data).decode("ascii"),
    }


if __name__ == "__main__":
    result = prepare(json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes()))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for key, value in result.items():
            print(f"{key}={value}", file=output)
