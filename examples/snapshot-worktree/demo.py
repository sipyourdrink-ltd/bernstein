"""Read a linked worktree's snapshot from its temporary main repository."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from bernstein.core.git.git_basic import run_git
from bernstein.core.git.snapshot import SnapshotStore


def git(cwd: Path, *args: str) -> str:
    return run_git(list(args), cwd, check=True).stdout.rstrip("\n")


def main() -> None:
    # Keep Git settings from the calling checkout out of this demo process.
    for key in list(os.environ):
        if key.startswith("GIT_"):
            del os.environ[key]
    os.environ.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    with TemporaryDirectory() as directory:
        repo = Path(directory) / "main"
        linked = Path(directory) / "linked"
        repo.mkdir()
        git(repo, "init", "--quiet")
        git(repo, "config", "--local", "user.name", "github-actions[bot]")
        git(repo, "config", "--local", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        git(repo, "config", "--local", "commit.gpgsign", "false")

        filename = "note.txt"
        (repo / filename).write_text("initial\n", encoding="utf-8")
        git(repo, "add", filename)
        git(repo, "commit", "-m", "Create example file")
        git(repo, "worktree", "add", "--detach", str(linked))

        (repo / filename).write_text("main staged\n", encoding="utf-8")
        git(repo, "add", filename)
        (linked / filename).write_text("linked staged\n", encoding="utf-8")
        git(linked, "add", filename)
        (linked / filename).write_text("linked unstaged\n", encoding="utf-8")

        snapshot = SnapshotStore(linked).take(task_id="demo-task", label="before edit")
        main_index = git(repo, "show", f":{filename}")
        linked_index = git(linked, "show", f":{filename}")
        git(repo, "worktree", "remove", "--force", str(linked))

        saved = SnapshotStore(repo).get(snapshot.snapshot_id)
        assert saved == snapshot
        snapshot_content = git(repo, "show", f"{saved.tree_sha}:{filename}")
        assert (main_index, linked_index, snapshot_content) == ("main staged", "linked staged", "linked unstaged")
        print(
            json.dumps(
                {
                    "main_index": main_index,
                    "linked_index": linked_index,
                    "snapshot_content": snapshot_content,
                    "linked_worktree_removed": not linked.exists(),
                    "metadata_after_removal": {"task_id": saved.task_id, "label": saved.label},
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
