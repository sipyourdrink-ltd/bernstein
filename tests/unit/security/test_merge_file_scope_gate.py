"""The signed ``allowed_files`` scope, enforced where a merge is accepted.

Issue #3914, decided in #3781: ``allowed_files`` is a signed field on an agent
credential that constrained nothing, while sitting beside ``permissions`` and
``task_ids`` and reading like both. The boundary lives at the acceptance gate
rather than at the individual write, so what it buys is containment - the
out-of-scope write still happens in the agent's own worktree, and does not
reach the repository.

Each test below names the property it protects, and each names a way the
result could be wrong.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bernstein.core.agents.spawner_merge import _file_scope_refusal, _incoming_change
from bernstein.core.identity.agent_jwt import AgentIdentityStore


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), capture_output=True, check=True)


def _repo_with_agent_branch(root: Path, session_id: str, files: dict[str, str]) -> None:
    """Commit ``files`` on ``agent/<session_id>``, branched off an empty main."""
    _run(["git", "init", "-b", "main"], root)
    _run(["git", "config", "user.email", "test@example.com"], root)
    _run(["git", "config", "user.name", "Test User"], root)
    _run(["git", "commit", "--allow-empty", "-m", "init"], root)

    _run(["git", "checkout", "-b", f"agent/{session_id}"], root)
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    _run(["git", "add", "-A"], root)
    _run(["git", "commit", "-m", "agent work"], root)
    _run(["git", "checkout", "main"], root)


def _session(session_id: str) -> Any:
    class _Stub:
        pass

    stub = _Stub()
    stub.id = session_id
    stub.task_ids = ["T-1"]
    return stub


def _mint(root: Path, session_id: str, allowed_files: list[str]) -> AgentIdentityStore:
    store = AgentIdentityStore(root / ".sdd" / "auth")
    store.create_identity(session_id, "backend", allowed_files=allowed_files)
    return store


def _refuse(root: Path, session_id: str) -> Any:
    return _file_scope_refusal(_session(session_id), root, f"agent/{session_id}")


def test_a_merge_touching_a_file_outside_the_signed_scope_is_refused(tmp_path: Path) -> None:
    """The boundary exists at all."""
    _repo_with_agent_branch(tmp_path, "s1", {"src/ok.py": "x\n", "infra/deploy.tf": "y\n"})
    _mint(tmp_path, "s1", ["src/**"])

    result = _refuse(tmp_path, "s1")

    assert result is not None
    assert result.success is False


def test_an_identity_with_an_empty_scope_merges_anything(tmp_path: Path) -> None:
    """The migration property. If this fails, the change is an outage for every
    identity in existence, because every one of them carries ``[]``."""
    _repo_with_agent_branch(tmp_path, "s2", {"anywhere/at/all.py": "x\n"})
    _mint(tmp_path, "s2", [])

    assert _refuse(tmp_path, "s2") is None


def test_a_session_with_no_identity_record_is_not_refused(tmp_path: Path) -> None:
    """Absence of a credential is absence of a restriction, not a denial."""
    _repo_with_agent_branch(tmp_path, "s3", {"infra/deploy.tf": "y\n"})
    # Another session holds a scope, so the store exists but says nothing here.
    _mint(tmp_path, "someone-else", ["src/**"])

    assert _refuse(tmp_path, "s3") is None


def test_the_refusal_names_the_paths_that_fell_outside_the_scope(tmp_path: Path) -> None:
    """A refusal that does not say why gets filed as a bug."""
    _repo_with_agent_branch(tmp_path, "s4", {"src/ok.py": "x\n", "infra/deploy.tf": "y\n"})
    _mint(tmp_path, "s4", ["src/**"])

    result = _refuse(tmp_path, "s4")

    assert result is not None
    assert "infra/deploy.tf" in result.error
    assert "src/**" in result.error
    assert "s4" in result.error
    # The in-scope file is not evidence of a refusal and must not be named.
    assert "src/ok.py" not in result.error


def test_a_double_star_pattern_covers_nested_paths_and_a_single_star_does_not(tmp_path: Path) -> None:
    """The ``fnmatch`` mistake, caught by a test rather than by a reviewer."""
    _repo_with_agent_branch(tmp_path, "s5", {"src/deep/nested.py": "x\n"})
    _mint(tmp_path, "s5", ["src/**"])
    assert _refuse(tmp_path, "s5") is None

    single = tmp_path / "single"
    single.mkdir()
    _repo_with_agent_branch(single, "s6", {"src/deep/nested.py": "x\n"})
    _mint(single, "s6", ["src/*"])

    result = _refuse(single, "s6")
    assert result is not None
    assert "src/deep/nested.py" in result.error


def test_a_scope_pattern_cannot_reach_outside_the_repository_root(tmp_path: Path) -> None:
    """``../``, absolute and drive-qualified forms are refused at creation, so
    they never become a signed scope."""
    store = AgentIdentityStore(tmp_path / ".sdd" / "auth")

    for index, pattern in enumerate(("../outside/**", "/etc/passwd", "C:/Windows/**", "~/.ssh/**", "")):
        with pytest.raises(ValueError, match="allowed_files"):
            store.create_identity(f"bad-{index}", "backend", allowed_files=[pattern])

    # Refused before anything is signed, so no credential exists for them.
    assert store.get("bad-0") is None


def test_a_pattern_stored_before_validation_existed_matches_nothing_rather_than_everything(
    tmp_path: Path,
) -> None:
    """The fail-open direction on an uninterpretable pattern is the dangerous
    one, so an unreadable scope admits nothing instead of everything."""
    _repo_with_agent_branch(tmp_path, "s7", {"src/ok.py": "x\n"})
    _mint(tmp_path, "s7", ["src/**"])

    # Rewrite the record the way it would have been written before creation
    # validated anything. Both copies move together: the identity and the
    # credential are compared on load and must agree.
    record = tmp_path / ".sdd" / "auth" / "agent_identities" / "s7.json"
    stored = json.loads(record.read_text(encoding="utf-8"))
    stored["allowed_files"] = [""]
    stored["credential"]["allowed_files"] = [""]
    record.write_text(json.dumps(stored), encoding="utf-8")

    result = _refuse(tmp_path, "s7")

    assert result is not None, "an uninterpretable scope must not widen to 'no scope'"
    assert "src/ok.py" in result.error


def test_a_rename_out_of_scope_is_refused_like_a_deletion(tmp_path: Path) -> None:
    """Moving a file into scope is still removing it from where it was.

    Rename detection reports only a rename's destination, so a scope check
    reading that list would see an in-scope path and admit a merge that
    deletes an out-of-scope one — passing the disguised removal while
    refusing the honest ``git rm``. Both orderings must land the same way.
    """
    _repo_with_agent_branch(tmp_path, "s10", {"infra/deploy.tf": "terraform\n"})
    _mint(tmp_path, "s10", ["src/**"])

    # Baseline: deleting it outright is already refused.
    deletion = _refuse(tmp_path, "s10")
    assert deletion is not None
    assert "infra/deploy.tf" in deletion.error

    # The same removal, disguised as a move into scope.
    moved = tmp_path / "moved"
    moved.mkdir()
    _run(["git", "init", "-b", "main"], moved)
    _run(["git", "config", "user.email", "test@example.com"], moved)
    _run(["git", "config", "user.name", "Test User"], moved)
    (moved / "infra").mkdir()
    (moved / "infra" / "deploy.tf").write_text("terraform\n", encoding="utf-8")
    _run(["git", "add", "-A"], moved)
    _run(["git", "commit", "-m", "init"], moved)

    _run(["git", "checkout", "-b", "agent/s11"], moved)
    (moved / "src").mkdir()
    _run(["git", "mv", "infra/deploy.tf", "src/deploy.tf"], moved)
    _run(["git", "commit", "-m", "rename"], moved)
    _run(["git", "checkout", "main"], moved)

    _mint(moved, "s11", ["src/**"])

    result = _refuse(moved, "s11")

    assert result is not None, "a rename out of scope must not pass a check a deletion fails"
    assert "infra/deploy.tf" in result.error


def test_a_refused_merge_leaves_the_branch_intact(tmp_path: Path) -> None:
    """Containment, not destruction: an operator has to be able to look at
    what was refused."""
    _repo_with_agent_branch(tmp_path, "s8", {"infra/deploy.tf": "y\n"})
    _mint(tmp_path, "s8", ["src/**"])

    before = subprocess.run(
        ["git", "rev-parse", "agent/s8"], cwd=str(tmp_path), capture_output=True, text=True, check=True
    ).stdout

    assert _refuse(tmp_path, "s8") is not None

    after = subprocess.run(
        ["git", "rev-parse", "agent/s8"], cwd=str(tmp_path), capture_output=True, text=True, check=True
    ).stdout
    assert before == after


def test_the_scope_check_runs_against_the_same_file_list_the_refusal_reports(tmp_path: Path) -> None:
    """One source of truth, so the message cannot describe a different set
    than the one that was judged."""
    _repo_with_agent_branch(tmp_path, "s9", {"a/one.py": "x\n", "b/two.py": "y\n", "c/three.py": "z\n"})
    _mint(tmp_path, "s9", ["a/**"])

    files, _ = _incoming_change(tmp_path, "agent/s9")
    result = _refuse(tmp_path, "s9")

    assert result is not None
    outside = {path for path in files if not path.startswith("a/")}
    assert outside, "the fixture must produce at least one out-of-scope path"
    for path in outside:
        assert path in result.error


def _truncate_record(record: Path) -> None:
    """A write that did not finish - a crash or a full disk mid-``_save``."""
    record.write_text('{"id": "s12", "role": "backend"', encoding="utf-8")


def _disagree_with_credential(record: Path) -> None:
    """One field edited, so the record now carries two answers to "scoped to what"."""
    stored = json.loads(record.read_text(encoding="utf-8"))
    stored["allowed_files"] = []
    record.write_text(json.dumps(stored), encoding="utf-8")


def _unparseable_enum(record: Path) -> None:
    """A value the record's own reader refuses, in a field that is not the scope."""
    stored = json.loads(record.read_text(encoding="utf-8"))
    stored["status"] = "not-a-real-status"
    record.write_text(json.dumps(stored), encoding="utf-8")


@pytest.mark.parametrize(
    "corrupt",
    [_truncate_record, _disagree_with_credential, _unparseable_enum],
    ids=["truncated", "scope-disagrees-with-credential", "unparseable-enum"],
)
def test_an_identity_record_that_does_not_deserialise_refuses_rather_than_widening(
    tmp_path: Path,
    corrupt: Any,
) -> None:
    """The fail-open direction again, one level up from the pattern.

    The store's shared reader answers ``None`` for a record it cannot parse
    and for a record that is not there, and only the second is the settled
    open default. A scope switched off by a truncated write - or by a
    one-field edit - would leave an operator believing an agent is bounded
    while every path merges. Refusing is recoverable; widening is silent.
    """
    _repo_with_agent_branch(tmp_path, "s12", {"infra/deploy.tf": "y\n"})
    _mint(tmp_path, "s12", ["src/**"])
    corrupt(tmp_path / ".sdd" / "auth" / "agent_identities" / "s12.json")

    result = _refuse(tmp_path, "s12")

    assert result is not None, "an unreadable record must not widen to 'no record'"
    assert result.success is False
    assert "s12" in result.error


def test_an_unreadable_identity_directory_refuses_rather_than_widening(tmp_path: Path) -> None:
    """A directory the gate cannot list makes every record look absent.

    ``get`` reports no identity either way, so a gate that trusted it would
    treat a store it cannot read as a store with nothing in it - the widest
    possible reading of the least trustworthy evidence.
    """
    _repo_with_agent_branch(tmp_path, "s13", {"infra/deploy.tf": "y\n"})
    _mint(tmp_path, "s13", ["src/**"])
    identities = tmp_path / ".sdd" / "auth" / "agent_identities"

    identities.chmod(0o000)
    try:
        if any(entry.name for entry in identities.iterdir()):  # pragma: no cover - root or a permissive fs
            pytest.skip("filesystem does not enforce directory permissions for this user")
    except OSError:
        pass
    try:
        result = _refuse(tmp_path, "s13")
    finally:
        identities.chmod(0o755)

    assert result is not None, "an unlistable store must not widen to 'no scope'"


def test_an_unreadable_scope_is_recorded_under_its_own_reason(tmp_path: Path) -> None:
    """Two different refusals, two different reasons in the refusal journal.

    An operator reading ``refused_merges.jsonl`` has to be able to tell "the
    agent went outside its scope" from "the scope itself is damaged": the
    first is the agent's problem and the second is the operator's.
    """
    _repo_with_agent_branch(tmp_path, "s14", {"infra/deploy.tf": "y\n"})
    _mint(tmp_path, "s14", ["src/**"])
    _truncate_record(tmp_path / ".sdd" / "auth" / "agent_identities" / "s14.json")

    assert _refuse(tmp_path, "s14") is not None

    journal = tmp_path / ".sdd" / "runtime" / "refused_merges.jsonl"
    reasons = [json.loads(line)["reason"] for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert reasons == ["allowed-files-unreadable"]


# ---------------------------------------------------------------------------
# The file list itself is evidence, and its absence is not "nothing to judge"
# ---------------------------------------------------------------------------


def _git_diff_fails(*, timeout: bool) -> Any:
    """Stand in for ``run_git`` so the file-list read fails and nothing else does.

    The two failures are the two the merge path actually sees: a non-zero
    ``git diff`` (the branch is gone, the object store is damaged), and the
    30-second timeout a very large diff hits while ``git merge`` itself still
    succeeds. Only the file-list call is broken, so a gate that reached the
    scope check on a half-read change would still look like it worked.
    """
    from bernstein.core.git_ops import run_git as real_run_git

    def _fake(args: list[str], cwd: Path, **kwargs: Any) -> Any:
        if args[:2] == ["diff", "--name-only"]:
            if timeout:
                raise subprocess.TimeoutExpired(cmd=["git", *args], timeout=30)
            return type("_Result", (), {"returncode": 128, "stdout": "", "stderr": "fatal: bad revision\n"})()
        return real_run_git(args, cwd, **kwargs)

    return _fake


@pytest.mark.parametrize("timeout", [False, True], ids=["non-zero-exit", "timed-out"])
def test_a_file_list_that_cannot_be_read_refuses_rather_than_admitting_everything(
    tmp_path: Path,
    timeout: bool,
) -> None:
    """The fail-open direction, one level below the scope.

    An empty file list reads as "no path fell outside the scope", which is
    the same answer a genuinely empty change gives. A merge whose file list
    timed out would land unjudged while the gate reported that it held.
    """
    _repo_with_agent_branch(tmp_path, "s15", {"infra/deploy.tf": "y\n"})
    _mint(tmp_path, "s15", ["src/**"])

    with patch("bernstein.core.git_ops.run_git", _git_diff_fails(timeout=timeout)):
        result = _refuse(tmp_path, "s15")

    assert result is not None, "an unreadable file list must not read as an empty one"
    assert result.success is False
    assert "s15" in result.error


def test_an_unreadable_file_list_is_recorded_under_its_own_reason(tmp_path: Path) -> None:
    """A third refusal, and a third reason.

    "The scope is damaged" and "the change could not be read" are repaired
    by different people: the first by the operator who owns the credential,
    the second by whoever owns the repository the diff would not come out of.
    """
    _repo_with_agent_branch(tmp_path, "s16", {"infra/deploy.tf": "y\n"})
    _mint(tmp_path, "s16", ["src/**"])

    with patch("bernstein.core.git_ops.run_git", _git_diff_fails(timeout=False)):
        assert _refuse(tmp_path, "s16") is not None

    journal = tmp_path / ".sdd" / "runtime" / "refused_merges.jsonl"
    reasons = [json.loads(line)["reason"] for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert reasons == ["allowed-files-diff-unreadable"]


def test_an_unreadable_file_list_is_inert_when_no_scope_was_set(tmp_path: Path) -> None:
    """The asymmetry to preserve.

    Only a failed read refuses, and only where a scope was declared. An
    identity with an empty scope declared no boundary, so there is nothing
    for an unreadable change to fall outside of - refusing there would turn
    every flaky ``git diff`` into a lost merge for every identity minted
    before the gate existed.
    """
    _repo_with_agent_branch(tmp_path, "s17", {"infra/deploy.tf": "y\n"})
    _mint(tmp_path, "s17", [])

    with patch("bernstein.core.git_ops.run_git", _git_diff_fails(timeout=True)):
        assert _refuse(tmp_path, "s17") is None


def test_a_genuinely_empty_change_is_still_admitted(tmp_path: Path) -> None:
    """The other half of the distinction being drawn.

    A branch that changes nothing reads back an empty file list, and that
    list is evidence rather than the absence of it.
    """
    _repo_with_agent_branch(tmp_path, "s18", {"src/ok.py": "x\n"})
    _run(["git", "checkout", "-b", "agent/s19"], tmp_path)
    _run(["git", "checkout", "main"], tmp_path)
    _mint(tmp_path, "s19", ["src/**"])

    assert _incoming_change(tmp_path, "agent/s19") == ([], "")
    assert _refuse(tmp_path, "s19") is None
