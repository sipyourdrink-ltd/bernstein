"""Apply a bounded autosync patch to a bare index and advance only the expected PR tip."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
from pathlib import PurePosixPath
from urllib.parse import quote

MIRRORS = {
    "AGENTS.md",
    "CLAUDE.md",
    "CONVENTIONS.md",
    ".aider.conf.yml",
    ".goosehints",
    ".github/copilot-instructions.md",
    "docs/sdd/module-map.md",
}


def validate_changes(raw_diff: bytes) -> None:
    """Only the generator can add mirrors. Ruff only edits existing regular files."""
    fields = raw_diff.removesuffix(b"\0").split(b"\0")
    for metadata, name in zip(fields[::2], fields[1::2], strict=True):
        old_mode, new_mode, _, _, status = metadata.removeprefix(b":").split()
        path = name.decode("utf-8")
        parts = PurePosixPath(path).parts
        mirror = path in MIRRORS or re.fullmatch(r"\.cursor/rules/[\w-]+\.mdc", path)
        source = path.endswith((".py", ".pyi", ".ipynb")) and not any(part.startswith(".") for part in parts)
        if not (mirror or source) or any(not char.isprintable() for char in path):
            raise ValueError("Autosync diff contains an unsupported path")
        if mirror and status == b"A" and old_mode == b"000000" and new_mode == b"100644":
            continue
        if status != b"M" or old_mode != new_mode or new_mode not in (b"100644", b"100755"):
            raise ValueError("Autosync may only modify regular files or add generated mirrors")


def current_target(remote: str, sha: str, ref: str) -> bool:
    """Recheck eligibility immediately before using the write credential."""
    repository = os.environ["GITHUB_REPOSITORY"]
    number = int(os.environ["PR_NUMBER"])
    pr = json.loads(subprocess.check_output(["gh", "api", f"repos/{repository}/pulls/{number}"]))
    head, base = pr["head"], pr["base"]
    if pr["state"] != "open" or any(label["name"] == "skip-autosync" for label in pr["labels"]):
        return False
    branch = ref.removeprefix("refs/heads/")
    if (
        head["repo"] is None
        or head["repo"]["full_name"] != repository
        or base["repo"]["full_name"] != repository
        or head["ref"] != branch
        or branch in {base["ref"], base["repo"]["default_branch"]}
        or head["sha"] != sha
    ):
        raise ValueError("PR head or target changed before publication")
    branch_url = f"repos/{repository}/branches/{quote(branch, safe='')}"
    try:
        info = json.loads(subprocess.check_output(["gh", "api", branch_url]))
    except subprocess.CalledProcessError:
        absent = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--refs", remote, ref], stdout=subprocess.DEVNULL, check=False
        )
        if absent.returncode == 2:
            return False
        raise
    if info["protected"] or info["commit"]["sha"] != sha:
        raise ValueError("PR head advanced or became protected before publication")
    return True


def publish(directory: str) -> None:
    token = os.environ.pop("BERNSTEIN_AUTOSYNC_TOKEN", "")
    if not token:
        raise ValueError("BERNSTEIN_AUTOSYNC_TOKEN is required for autosync pushes")
    patch = base64.b64decode(os.environ.pop("PATCH"), validate=True)
    if len(patch) > 65536:
        raise ValueError("Autosync diff exceeds 64 KiB")
    if not patch:
        print("No drift; nothing to amend.")
        return
    if not patch.startswith(b"diff --git ") or b"\0" in patch or b"\nGIT binary patch" in patch:
        raise ValueError("Autosync requires a text diff")
    sha = os.environ["PR_HEAD_SHA"]
    ref = f"refs/heads/{os.environ['PR_HEAD_REF']}"
    if not re.fullmatch("[0-9a-f]{40}", sha):
        raise ValueError("Invalid PR head SHA")
    remote = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}.git"
    git = ["git", "-C", directory, "-c", "core.hooksPath=/dev/null"]
    subprocess.run([*git, "check-ref-format", ref], check=True)
    # PR objects stay in a bare object store. No worktree, git hooks, project
    # configuration, local actions or checkout filters are used by this job.
    subprocess.run([*git, "init", "--bare", "--quiet"], check=True)
    subprocess.run([*git, "fetch", "--quiet", "--no-tags", "--depth=1", remote, sha], check=True)
    subprocess.run([*git, "read-tree", sha], check=True)
    subprocess.run([*git, "apply", "--cached", "--whitespace=nowarn", "-"], input=patch, check=True)
    changes = subprocess.check_output([*git, "diff", "--cached", "--raw", "-z", "--no-renames", sha])
    if not changes:
        raise ValueError("Nonempty autosync patch produced no changes")
    validate_changes(changes)
    tree = subprocess.check_output([*git, "write-tree"], text=True).strip()
    commit = subprocess.check_output(
        [
            *git,
            "-c",
            "user.name=github-actions[bot]",
            "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit-tree",
            tree,
            "-p",
            sha,
            "-m",
            "chore(auto): regenerate AGENTS.md mirrors + ruff format",
        ],
        text=True,
    ).strip()
    # The sole parent is the event SHA. The lease is an atomic compare-and-swap
    # and prevents a deleted ref from being recreated by an ordinary push.
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}",
    }
    if not current_target(remote, sha, ref):
        print("::notice::PR closed, opted out or branch deleted; skipping autosync.")
        return
    push = subprocess.run(
        [*git, "push", f"--force-with-lease={ref}:{sha}", remote, f"{commit}:{ref}"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if push.returncode:
        if "GH006" in push.stderr or "merge queue" in push.stderr.lower():
            print("::notice::Branch locked by the merge queue; skipping autosync.")
            return
        if (
            subprocess.run(
                [*git, "ls-remote", "--exit-code", "--refs", remote, ref], stdout=subprocess.DEVNULL, check=False
            ).returncode
            == 2
        ):
            print("::notice::Head branch deleted; skipping autosync.")
            return
        raise RuntimeError("Autosync push failed; the PR head may have advanced")
    print(f"Published autosync commit {commit}")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        publish(directory)
