"""Execute the API-only label reconciliation without GitHub credentials."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/pr-labels.yml").read_text())


def test_reconciliation_uses_existing_trusted_label_event_and_actions_token() -> None:
    doc = _workflow()
    assert set(doc.get("on", doc.get(True))) == {"pull_request_target"}
    job = doc["jobs"]["quorum-after-labels"]
    assert job["needs"] == "label"
    assert job["permissions"] == {"actions": "write", "checks": "read", "pull-requests": "read"}
    assert all("run" not in step and "checkout" not in step.get("uses", "") for step in job["steps"])
    assert "github-token" not in job["steps"][-1]["with"]
    rerun = yaml.safe_load((ROOT / ".github/workflows/quorum-rerun.yml").read_text())
    assert "workflow_run" not in rerun.get("on", rerun.get(True))


@pytest.mark.parametrize(
    "case",
    [
        "success",
        "failure",
        "wait",
        "absent",
        "timeout",
        "closed",
        "new-head",
        "read-error",
        "post-error",
        "many-runs",
        "duplicate",
        "foreign-app",
        "bad-url",
    ],
)
def test_label_reconciliation_fails_closed_and_reruns_successes(case: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the github-script body")
    script = _workflow()["jobs"]["quorum-after-labels"]["steps"][-1]["with"]["script"]
    harness = r"""
const scenario = JSON.parse(process.argv[1]);
const script = JSON.parse(process.argv[2]);
const calls = [];
let queries = 0;
const context = {repo: {owner: 'owner', repo: 'repo'}, payload: {pull_request: {number: 42, head: {sha: 'a'.repeat(40)}}}};
const core = {info() {}};
process.env.GITHUB_SERVER_URL = 'https://github.com';
global.setTimeout = callback => callback();
const github = {
  rest: {
    pulls: {get: async args => {
      calls.push(['get', args]);
      if (scenario === 'read-error') throw Error('HTTP 503');
      return {data: {state: scenario === 'closed' ? 'closed' : 'open',
        head: {sha: (scenario === 'new-head' ? 'b' : 'a').repeat(40)}}};
    }},
    checks: {listForRef: 'listForRef'},
    actions: {reRunWorkflow: async args => {
      calls.push(['rerun', args]);
      if (scenario === 'post-error') throw Error('HTTP 403');
    }},
  },
  paginate: async (method, args) => {
    calls.push(['checks', args]);
    queries++;
    if (method !== 'listForRef' || args.filter !== 'all' || args.ref !== 'a'.repeat(40)) throw Error('wrong lookup');
    if (scenario === 'absent') return [];
    return Array.from({length: scenario === 'many-runs' ? 13 : scenario === 'duplicate' ? 2 : 1}, (_, i) => ({
      app: {slug: scenario === 'foreign-app' ? 'untrusted' : 'github-actions'},
      status: scenario === 'timeout' || (scenario === 'wait' && queries === 1) ? 'in_progress' : 'completed',
      conclusion: scenario === 'failure' ? 'failure' : 'success',
      html_url: `https://${scenario === 'bad-url' ? 'evil.invalid' : 'github.com'}/owner/repo/actions/runs/${scenario === 'duplicate' ? 987 : 987 + i}/job/1`,
    }));
  },
};
(async () => {
  let error = null;
  try { await new (Object.getPrototypeOf(async function(){}).constructor)('github', 'context', 'core', script)(github, context, core); }
  catch (e) { error = e.message; }
  console.log(JSON.stringify({error, calls, queries}));
})();
"""
    proc = subprocess.run(
        [node, "-e", harness, json.dumps(case), json.dumps(script)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    result = json.loads(proc.stdout)
    failures = {"absent", "timeout", "read-error", "post-error", "many-runs", "foreign-app", "bad-url"}
    assert bool(result["error"]) == (case in failures), result
    posts = [args for name, args in result["calls"] if name == "rerun"]
    if case in {"success", "failure", "wait", "duplicate", "post-error"}:
        assert posts == [{"owner": "owner", "repo": "repo", "run_id": 987}]
    else:
        assert not posts
    if case in {"timeout", "absent", "foreign-app"}:
        assert result["queries"] == 60
    if case == "wait":
        assert result["queries"] == 2
