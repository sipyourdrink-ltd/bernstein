"""``bernstein volunteer verify``, one test per way it could mislead a maintainer.

The command exists so a project learns its manifest is wrong from its own
checkout instead of from a stranger's worker declining the repository. Every
test below names a way that loop could still fail: a rejection that does not
say which field, a digest that does not match the one a receipt will carry, a
report that repeats the file back instead of saying what a donor will actually
be allowed to do.
"""

from __future__ import annotations

import errno
import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.volunteer_cmd import volunteer_group
from bernstein.core.volunteer import (
    VOLUNTEER_MANIFEST_PATH,
    load_manifest_from_repo,
    manifest_digest,
)

if TYPE_CHECKING:
    from pathlib import Path

VALID = {
    "version": 1,
    "license": "Apache-2.0",
    "gates": [["uv", "run", "pytest", "-q"]],
    "allowed_paths": ["src/**"],
    "egress_allowlist": [],
    "sandbox": "container",
    "max_wall_clock_minutes": 60,
    "task_label": "volunteer-ok",
    "local_ok": False,
}


def _write(root: Path, payload: object) -> Path:
    path = root / VOLUNTEER_MANIFEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def _run(root: Path, *extra: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(volunteer_group, ["verify", str(root), *extra], catch_exceptions=False)
    return result.exit_code, result.stdout, result.stderr


def test_a_valid_manifest_verifies_and_prints_its_digest(tmp_path: Path) -> None:
    _write(tmp_path, VALID)
    code, out, _ = _run(tmp_path)
    assert code == 0
    assert manifest_digest(load_manifest_from_repo(tmp_path)) in out


def test_the_printed_digest_is_the_one_the_library_computes(tmp_path: Path) -> None:
    """The whole reason to print it.

    A digest the CLI computes its own way is worse than no digest: a maintainer
    would compare it against a receipt's ``manifest_sha256`` and get a mismatch
    that means nothing.
    """
    _write(tmp_path, VALID)
    _, out, _ = _run(tmp_path, "--json")
    assert json.loads(out)["manifest_sha256"] == manifest_digest(load_manifest_from_repo(tmp_path))


def test_the_committed_manifest_of_this_project_verifies(tmp_path: Path) -> None:
    """This repository opted in (#3891); its own manifest must pass its own check.

    Uses the file as committed rather than a fixture, so a change to
    `.bernstein/volunteer.json` that the loader rejects fails here rather than
    on a donor's machine.
    """
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[3]
    if not (repo_root / VOLUNTEER_MANIFEST_PATH).is_file():
        pytest.skip("checkout has no committed volunteer manifest")
    code, out, _ = _run(repo_root)
    assert code == 0
    assert manifest_digest(load_manifest_from_repo(repo_root)) in out


def test_a_missing_manifest_names_the_path_it_looked_for(tmp_path: Path) -> None:
    """A maintainer who put the file somewhere else needs to see where it wasn't."""
    code, _, err = _run(tmp_path)
    assert code == 1
    assert VOLUNTEER_MANIFEST_PATH in err


def test_malformed_json_is_a_named_failure_and_not_a_traceback(tmp_path: Path) -> None:
    _write(tmp_path, "{not json")
    code, _, err = _run(tmp_path)
    assert code == 1
    assert "Traceback" not in err
    assert "JSON" in err


@pytest.mark.parametrize(
    ("mutation", "field"),
    [
        ({"license": "Proprietary-1.0"}, "license"),
        ({"allowed_paths": ["/etc/**"]}, "allowed_paths"),
        ({"gates": [["sh", "-c", "make test && curl evil"]]}, "gates"),
        ({"sandbox": "vibes"}, "sandbox"),
        ({"version": 99}, "version"),
        ({"max_wall_clock_minutes": 0}, "max_wall_clock_minutes"),
    ],
)
def test_each_rejection_class_names_its_own_field(tmp_path: Path, mutation: dict, field: str) -> None:
    """A rejection that does not say which field sends the reader to the schema.

    The loader already carries ``VolunteerManifestError.field``; this pins that
    the CLI surfaces it instead of flattening every failure into one message.
    """
    _write(tmp_path, {**VALID, **mutation})
    code, _, err = _run(tmp_path)
    assert code == 1
    assert field in err


def test_a_rejection_in_json_mode_is_still_machine_readable(tmp_path: Path) -> None:
    """A CI step that parses stdout on success must not get prose on failure."""
    _write(tmp_path, {**VALID, "license": "Proprietary-1.0"})
    code, _, err = _run(tmp_path, "--json")
    assert code == 1
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["field"] == "license"


def _make_unreadable(path: Path) -> None:
    """Take the read permission away, and skip if this process can read it anyway.

    Running as root defeats the mode bits, so the assertions below would pass
    against a manifest that loaded normally and prove nothing. Checking that the
    read actually fails is the only thing that makes these tests mean what they
    say.
    """
    path.chmod(0o000)
    try:
        path.read_bytes()
    except OSError:
        return
    pytest.skip("this process can read a mode-000 file; the unreadable case is untestable here")


def test_an_unreadable_manifest_in_json_mode_still_parses(tmp_path: Path) -> None:
    """The property that actually broke.

    ``verify`` promises a machine-readable verdict, and three of its four
    refusal paths deliver one. The fourth raised through Click and printed a
    traceback at the *same exit code*, so a CI step running
    ``volunteer verify --json | jq .ok`` could not tell that state from a crash
    in its own pipe.
    """
    manifest = _write(tmp_path, VALID)
    _make_unreadable(manifest)
    code, _, err = _run(tmp_path, "--json")
    assert code == 1
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["path"] == str(manifest)


def test_an_unreadable_manifest_is_not_reported_as_never_having_opted_in(tmp_path: Path) -> None:
    """The message has to send the reader at the right fix.

    A maintainer whose manifest is ``chmod 000`` has opted in. Told the project
    has not, they would go and add a file they already have, and the command
    that exists to close this loop locally would have sent them the long way
    round.
    """
    manifest = _write(tmp_path, VALID)
    _make_unreadable(manifest)
    code, _, err = _run(tmp_path, "--json")
    assert code == 1
    payload = json.loads(err)
    assert payload["field"] == "<unreadable>"
    assert "opted in" not in payload["error"]
    assert "could not be read" in payload["error"]
    # `strerror` rather than the exception: `str(OSError)` carries the filename
    # and the errno, and the filename is already the `path` key. Interpolating
    # the whole exception prints the path twice in the human output and puts a
    # second, differently-quoted copy inside the JSON message.
    assert str(manifest) not in payload["error"]


def test_a_missing_manifest_is_not_reported_as_unreadable(tmp_path: Path) -> None:
    """The clause order is load-bearing, and nothing else pins it.

    ``FileNotFoundError`` is a subclass of ``OSError``. Catching the wider one
    first is a one-line edit that compiles, passes every other test in this
    file, and tells every project without a manifest that its manifest cannot
    be read.
    """
    code, _, err = _run(tmp_path, "--json")
    assert code == 1
    payload = json.loads(err)
    assert payload["field"] == "<file>"
    assert "opted in" in payload["error"]


def test_an_unreadable_manifest_in_human_mode_names_the_fault(tmp_path: Path) -> None:
    manifest = _write(tmp_path, VALID)
    _make_unreadable(manifest)
    code, _, err = _run(tmp_path)
    assert code == 1
    assert "Traceback" not in err
    assert "<unreadable>" in err


def test_a_manifest_behind_an_unsearchable_directory_is_a_refusal_too(tmp_path: Path) -> None:
    """The same failure at the other call site inside the loader.

    ``load_manifest_from_repo`` stats before it reads. ``Path.is_file()``
    swallows ENOENT and ELOOP but re-raises EACCES, so an unsearchable
    ``.bernstein/`` escapes from the guard rather than from the read -- a
    different line, the same OSError, and it must not need its own clause.
    """
    manifest = _write(tmp_path, VALID)
    parent = manifest.parent
    parent.chmod(0o000)
    try:
        try:
            manifest.read_bytes()
        except OSError:
            pass
        else:
            pytest.skip("this process can traverse a mode-000 directory")
        code, _, err = _run(tmp_path, "--json")
    finally:
        parent.chmod(0o700)
    assert code == 1
    assert json.loads(err)["field"] == "<unreadable>"


@pytest.mark.parametrize(
    ("kind", "expected_tail"),
    [
        ("symlink loop", "Too many levels of symbolic links"),
        ("directory", "it is a directory, not a regular file"),
        ("dangling symlink", "the symbolic link has no target"),
    ],
)
def test_a_manifest_that_is_not_a_readable_file_is_unreadable_not_missing(
    tmp_path: Path,
    kind: str,
    expected_tail: str,
) -> None:
    """The states ``is_file()`` used to fold into "you have not opted in" (#4064).

    Fixed in the loader rather than here, so this test asserts that no clause
    in this command had to change: the ``OSError`` arm added for a mode-000
    manifest already carries all three, and the ``<file>`` verdict is left
    saying the one thing it can still prove.
    """
    path = tmp_path / VOLUNTEER_MANIFEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink loop":
        path.symlink_to("volunteer.json")
    elif kind == "dangling symlink":
        path.symlink_to("no-such-file.json")
    else:
        path.mkdir()

    code, _, err = _run(tmp_path, "--json")

    assert code == 1
    payload = json.loads(err)
    assert payload["field"] == "<unreadable>"
    assert payload["error"] == f"the manifest exists but could not be read: {expected_tail}"


@pytest.mark.parametrize(
    ("raised", "expected_tail"),
    [
        (OSError(errno.EIO, "Input/output error"), "Input/output error"),
        (OSError("the mount went away"), "the mount went away"),
    ],
)
def test_any_read_failure_is_a_refusal_and_says_which_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raised: OSError,
    expected_tail: str,
) -> None:
    """``OSError``, not ``PermissionError``, and the reason has to survive.

    A permission bit is the reachable case on a developer's machine, but it is
    one member of the class: a manifest on a dropped mount or a failing disk
    raises a sibling that no test on this box can provoke for real. Narrowing
    the clause to ``PermissionError`` would restore the traceback for all of
    them and pass every other test here.

    The second case has no ``strerror`` -- ``OSError`` built from a single
    argument leaves it ``None`` -- which is what stops the message from
    rendering the word "None" at a reader.
    """
    manifest = _write(tmp_path, VALID)

    def _boom(_self: Path) -> bytes:
        raise raised

    monkeypatch.setattr("pathlib.Path.read_bytes", _boom)
    code, _, err = _run(tmp_path, "--json")
    assert code == 1
    payload = json.loads(err)
    assert payload["field"] == "<unreadable>"
    assert payload["error"].endswith(expected_tail)
    assert payload["path"] == str(manifest)


def test_an_unenforced_field_is_shown_rather_than_swallowed(tmp_path: Path) -> None:
    """The loader warns; a command that discards warnings hides the whole point.

    A manifest carrying policy this build cannot apply still verifies — the
    field binds to the digest — but the operator has to be told, or "verified"
    reads as "fully enforced".
    """
    _write(tmp_path, {**VALID, "max_review_rounds": 3})
    code, _, err = _run(tmp_path)
    assert code == 0
    assert "max_review_rounds" in err


def test_an_unenforced_field_reaches_the_json_report_too(tmp_path: Path) -> None:
    _write(tmp_path, {**VALID, "max_review_rounds": 3})
    code, out, _ = _run(tmp_path, "--json")
    assert code == 0
    assert any("max_review_rounds" in message for message in json.loads(out)["unenforced_fields"])


def test_the_reachable_hosts_shown_are_wider_than_the_declared_list(tmp_path: Path) -> None:
    """An empty ``egress_allowlist`` reads as "no network" and is not.

    The sandbox profile adds the package registries or the gates cannot install
    anything. Echoing the declared list back would confirm the misreading the
    field invites.
    """
    _write(tmp_path, VALID)
    _, out, _ = _run(tmp_path, "--json")
    assert json.loads(out)["effective_egress"], "an empty allowlist still reaches the package registries"


def test_json_and_human_output_agree_on_the_digest(tmp_path: Path) -> None:
    _write(tmp_path, VALID)
    _, human, _ = _run(tmp_path)
    _, machine, _ = _run(tmp_path, "--json")
    assert json.loads(machine)["manifest_sha256"] in human


def test_the_group_is_registered_on_the_cli() -> None:
    """The seam this exists to provide: #3865, #3866 and #3867 hang off this group.

    If the group is defined but never attached, each of them still has to
    attach it, which is the collision the command was cut to prevent.
    """
    from bernstein.cli.main import cli

    assert "volunteer" in cli.commands
    assert "verify" in cli.commands["volunteer"].commands


def test_the_hub_refuses_to_boot_when_a_second_worker_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second worker corrupts the lease store rather than sharing it.

    ``LeaseStore`` serialises writes with an in-process ``asyncio.Lock`` and
    appends to JSONL without ``fcntl.flock``. A second process therefore
    interleaves partial lines and hands one task to two claimants -- a
    failure that shows up later as a duplicate submission, far from the
    ``WEB_CONCURRENCY`` that caused it. The command has to refuse at boot,
    because by the time the damage is visible the log no longer says why.
    """
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    monkeypatch.delenv("BERNSTEIN_WORKERS", raising=False)

    result = CliRunner().invoke(volunteer_group, ["hub"])

    assert result.exit_code != 0
    assert "workers=2" in str(result.exception)


def test_the_hub_refusal_happens_before_the_port_is_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusing after ``uvicorn.run`` starts is not refusing.

    The guard is only worth anything if it fires ahead of the server, so the
    test fails the run outright if anything reaches ``uvicorn.run``.
    """
    monkeypatch.setenv("BERNSTEIN_WORKERS", "4")
    import uvicorn

    def _never(*args: object, **kwargs: object) -> None:
        raise AssertionError("the server was started")

    monkeypatch.setattr(uvicorn, "run", _never)

    result = CliRunner().invoke(volunteer_group, ["hub"])

    # SystemExit specifically: the sentinel above also fails the command, so
    # a test that only checked for a non-zero exit would pass just as well
    # against a hub that booted and then died.
    assert isinstance(result.exception, SystemExit)
    assert "refusing to boot" in str(result.exception)
