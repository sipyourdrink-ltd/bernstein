"""Project manifest for the volunteer-workers program (``.bernstein/volunteer.json``).

A public project opts into receiving volunteer work by committing one file.
Everything downstream reads it: discovery renders it, the sandbox profile
enforces its egress list, the task runner enforces its ``allowed_paths``, and
the clean-room verifier re-runs its ``gates``.

Why the manifest is content-addressed
-------------------------------------

A volunteer submission arrives as an ordinary fork PR carrying a signed
receipt bundle.  The bundle attests which gates ran and what they produced,
but that attestation is worth nothing on its own: a volunteer who picks their
own weaker gates can produce a receipt that verifies perfectly and proves
nothing about the project's actual acceptance bar.

So the receipt binds to the policy, not just to the run.
:func:`manifest_digest` is the value a receipt bundle carries as
``manifest_sha256``.  A maintainer recomputes it from the manifest at the
commit the submission names; equal digests mean the volunteer ran the policy
this project declared, and a mismatch is a refusal with no judgement call in
it.  Strip the digest and the manifest degrades from a trust anchor into a
configuration file.

Two consequences follow, and both are load-bearing:

*Canonical over parsed, not raw over bytes.*  The digest covers the
normalised policy, not the file's bytes.  Reindenting the JSON or reordering
its keys must not invalidate every outstanding receipt, because neither
changes what the project declared.

*Unknown fields are carried, never dropped.*  A field this loader does not
recognise is preserved verbatim in :attr:`VolunteerManifest.extensions` and
participates in the digest.  Dropping it would let a project add a
policy-tightening field that older workers silently ignore while still
producing a digest that matches -- a downgrade with a valid-looking receipt
stapled to it.  Tolerating a field and ignoring it are different things; this
loader does the first and never the second.

Invariant for future schema versions
------------------------------------

Once a field ships, its normalisation may not change.  Two loaders that both
accept a manifest must serialise it identically or the same policy would
produce two digests.  A change to how a field is normalised is a breaking
change and takes a new entry in :data:`SUPPORTED_SCHEMA_VERSIONS`; a purely
additive field does not, because unknown fields already round-trip.

Gates are argv, never shell strings
-----------------------------------

``gates`` entries are argument vectors (``["uv", "run", "pytest", "-q"]``),
and a bare string is refused with an error that says so.  Two reasons, in
order of weight:

1. The command originates in a repository the donor does not control.  Handing
   attacker-influenceable text to a shell is the exfiltration path this whole
   program exists to close; there is no quoting discipline that makes it safe
   in general.
2. The clean-room re-run (#3871) must execute the *same* command the original
   run did.  A shell string is re-parsed by whatever shell each side happens
   to have, so "same string" does not imply "same execution" across two
   machines.  An argv is the command.

The cost is real and worth naming: a project whose gate is a pipeline has to
put the pipeline in a script and name the script.  That is the intended
trade -- the script is then part of the repository, reviewable and hashed
along with everything else.
"""

from __future__ import annotations

import errno
import hashlib
import json
import stat
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.path_scope import ScopePatternError
from bernstein.core.path_scope import paths_outside_scope as _paths_outside_scope
from bernstein.core.path_scope import validate_repo_relative_pattern as _validate_repo_relative_pattern

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

#: Repository-relative location of the manifest.  A project opts in by
#: committing this path; existence of the file in the project's own repository
#: is what proves control of the project.
VOLUNTEER_MANIFEST_PATH = ".bernstein/volunteer.json"

#: Schema versions this loader accepts.  An unknown version is refused
#: outright -- unlike an unknown *field*, an unknown *version* means the
#: document's shape may have changed under fields this loader thinks it
#: understands.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

#: Sandbox isolation levels a project may demand as its minimum.
SANDBOX_LEVELS = ("microvm", "container")

#: Default issue label marking a task as open to volunteers.
DEFAULT_TASK_LABEL = "volunteer-ok"

#: Upper bound on a single task's wall clock, in minutes.  A donor may set a
#: tighter budget; no project may demand more than a day of someone's machine.
MAX_WALL_CLOCK_MINUTES = 1440

#: OSI-approved SPDX identifiers the program accepts.  The program is
#: open-source only (umbrella decision 1): public code means every input and
#: every receipt is publicly auditable and there is no confidential codebase to
#: leak.  The list is deliberately an allowlist of identifiers in real use
#: rather than the full SPDX register -- an unlisted-but-legitimate license is
#: a one-line PR with a human looking at it.
OSI_APPROVED_LICENSES = frozenset(
    {
        "0BSD",
        "AGPL-3.0-only",
        "AGPL-3.0-or-later",
        "Apache-2.0",
        "Artistic-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "BSL-1.0",
        "CDDL-1.0",
        "CECILL-2.1",
        "ECL-2.0",
        "EPL-1.0",
        "EPL-2.0",
        "EUPL-1.2",
        "GPL-2.0-only",
        "GPL-2.0-or-later",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "ISC",
        "LGPL-2.1-only",
        "LGPL-2.1-or-later",
        "LGPL-3.0-only",
        "LGPL-3.0-or-later",
        "MIT",
        "MIT-0",
        "MPL-2.0",
        "MS-PL",
        "NCSA",
        "OSL-3.0",
        "PostgreSQL",
        "Python-2.0",
        "Unlicense",
        "UPL-1.0",
        "Zlib",
    }
)

#: Valid values for the manifest status field.
STATUS_VALUES = ("active", "paused")

_KNOWN_FIELDS = frozenset(
    {
        "version",
        "license",
        "gates",
        "allowed_paths",
        "egress_allowlist",
        "sandbox",
        "max_wall_clock_minutes",
        "task_label",
        "local_ok",
        "status",
    }
)

#: Characters that betray a shell string where an argv was required.
_SHELL_METACHARACTERS = frozenset("|&;<>$`\\\n")


class UnenforcedManifestFieldWarning(UserWarning):
    """A manifest declares policy fields this build does not know about.

    Not an error: unknown fields are how the schema grows without breaking
    older workers, and they still bind to the digest.  It is a warning because
    the donor is running under a policy their build can only partly apply.
    """


class VolunteerManifestError(ValueError):
    """A manifest could not be loaded.

    Carries the offending field so a caller can point at it without parsing
    the message.  ``field`` is ``"<document>"` when the failure is the
    document as a whole (not JSON, not an object).
    """

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(f"{field}: {message}")


@dataclass(frozen=True, slots=True)
class GateCommand:
    """One acceptance command the project requires, as an argument vector.

    Attributes:
        argv: Program and arguments, executed without a shell.
    """

    argv: tuple[str, ...]

    def __str__(self) -> str:
        return " ".join(self.argv)


@dataclass(frozen=True, slots=True)
class VolunteerManifest:
    """A project's declared volunteer policy.

    Attributes:
        version: Schema version; one of :data:`SUPPORTED_SCHEMA_VERSIONS`.
        license: OSI-approved SPDX identifier for the project.
        gates: Commands that must pass before a submission may be opened.
        allowed_paths: Repository-relative globs a patch may touch.  Empty
            means repo-wide.
        egress_allowlist: Hostnames the sandbox may reach on top of the
            package-registry set the sandbox profile defines.  Empty means the
            gates need no network beyond that set.
        sandbox: Minimum isolation the project accepts.
        max_wall_clock_minutes: Per-task wall-clock ceiling.
        task_label: Issue label marking a task as open to volunteers.
        local_ok: Whether tasks are generally solvable by local models.
        extensions: Fields this loader did not recognise, preserved verbatim.
            They participate in :attr:`digest`.
    """

    version: int
    license: str
    gates: tuple[GateCommand, ...]
    allowed_paths: tuple[str, ...]
    egress_allowlist: tuple[str, ...]
    sandbox: str
    max_wall_clock_minutes: int
    task_label: str
    local_ok: bool
    status: str
    extensions: Mapping[str, Any]

    @property
    def is_active(self) -> bool:
        """Whether the project is currently accepting volunteer work.

        A paused manifest still loads, validates, and digests, so an older
        worker can keep producing receipts against the same policy digest;
        discovery (the browse view) is the place a paused project drops out
        of the donor's view.
        """
        return self.status == "active"

    def to_canonical_dict(self) -> dict[str, Any]:
        """The normalised policy, as the digest sees it.

        Unknown fields are merged back at the top level under their original
        keys, so a loader that understands a field and one that does not
        serialise the same document.
        """
        payload: dict[str, Any] = {
            "version": self.version,
            "license": self.license,
            "gates": [list(gate.argv) for gate in self.gates],
            "allowed_paths": list(self.allowed_paths),
            "egress_allowlist": list(self.egress_allowlist),
            "sandbox": self.sandbox,
            "max_wall_clock_minutes": self.max_wall_clock_minutes,
            "task_label": self.task_label,
            "local_ok": self.local_ok,
            "status": self.status,
        }
        payload.update(self.extensions)
        return payload

    @property
    def digest(self) -> str:
        """SHA-256 of the canonical form, as ``manifest_sha256`` carries it."""
        return manifest_digest(self)

    def paths_outside_scope(self, paths: Iterable[str]) -> tuple[str, ...]:
        """Return the patch paths this project's ``allowed_paths`` does not admit.

        The refusal names these, so a contributor is told which files broke the
        scope rather than that something did.  An empty ``allowed_paths`` admits
        everything, which is what a project that never declared one carries.

        Args:
            paths: Repository-relative paths, as ``git diff --name-only`` prints
                them.

        Returns:
            The offending paths in input order, empty when the patch is in scope.
        """
        return _paths_outside_scope(paths, self.allowed_paths)


def canonical_manifest_bytes(manifest: VolunteerManifest) -> bytes:
    """Serialise a manifest to its canonical, byte-stable form.

    Matches the discipline in
    :func:`bernstein.core.security.audit_dsse._canonical_json` -- keys sorted
    at every depth, no insignificant whitespace -- so a digest computed here
    is comparable with the rest of the receipt substrate.
    """
    return json.dumps(
        _sort_recursive(manifest.to_canonical_dict()),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def manifest_digest(manifest: VolunteerManifest) -> str:
    """The manifest's content address: 64 lowercase hex characters."""
    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


def load_manifest(source: str | bytes) -> VolunteerManifest:
    """Parse and validate a manifest document.

    Parsing is all-or-nothing: either a fully-validated manifest comes back or
    :class:`VolunteerManifestError` is raised naming the field at fault.  No
    partially-populated object is ever produced, because a half-applied policy
    is more dangerous than no policy.

    Raises:
        VolunteerManifestError: The document is not a JSON object, declares an
            unsupported version, or any field fails validation.
    """
    text = source.decode("utf-8") if isinstance(source, bytes) else source
    try:
        raw = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VolunteerManifestError("<document>", f"not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise VolunteerManifestError("<document>", f"expected a JSON object, got {type(raw).__name__}")

    version = _load_version(raw)
    return VolunteerManifest(
        version=version,
        license=_load_license(raw),
        gates=_load_gates(raw),
        allowed_paths=_load_allowed_paths(raw),
        egress_allowlist=_load_egress_allowlist(raw),
        sandbox=_load_sandbox(raw),
        max_wall_clock_minutes=_load_wall_clock(raw),
        task_label=_load_task_label(raw),
        local_ok=_load_local_ok(raw),
        status=_load_status(raw),
        extensions=_load_extensions(raw),
    )


def _not_a_regular_file(mode: int) -> str:
    """Name the shape found at the manifest path, for the refusal message."""
    if stat.S_ISDIR(mode):
        return "a directory"
    if stat.S_ISFIFO(mode):
        return "a fifo"
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
        return "a device file"
    return "of an unrecognised type"


def load_manifest_from_repo(repo_root: Path) -> VolunteerManifest:
    """Load ``.bernstein/volunteer.json`` from a checked-out repository.

    "The project has not opted in" is a claim about the *project*, so it is
    made only when nothing exists at the path at all.  Every other filesystem
    state -- a symlink loop, a directory, an unreadable file, a
    ``.bernstein`` that is itself a regular file -- is a manifest that exists
    and could not be read, and raises :class:`OSError` instead, which is what
    lets a caller tell the two apart (#4064).

    :meth:`Path.is_file` could not make that distinction.  It swallows every
    errno in ``pathlib._IGNORED_ERRNOS`` -- ``ENOENT``, ``ENOTDIR``, ``EBADF``,
    ``ELOOP`` -- and answers ``False`` for anything that is not a regular file,
    so five states arrived at one message asserting something false about the
    project.  That is worse than a traceback: exit 1 and a well-formed
    ``"the project has not opted in"`` sends a maintainer whose file is a
    broken symlink off to add a file they already have.

    The synthesised errors carry ``EINVAL`` deliberately.  ``ENOENT`` is the
    errno that describes a dangling symlink, but ``OSError(ENOENT, ...)``
    *constructs a* :class:`FileNotFoundError` -- the errno-to-subclass map runs
    in ``OSError.__new__`` -- which would reintroduce this bug through the fix
    for it.

    Raises:
        FileNotFoundError: Nothing exists at the path; the project has not
            opted in.
        OSError: Something exists at the path and is not a readable regular
            file.
        VolunteerManifestError: The manifest was read but does not validate.
    """
    path = repo_root / VOLUNTEER_MANIFEST_PATH
    absent = f"{path} does not exist; the project has not opted in to volunteer work"
    try:
        mode = path.stat().st_mode
    except FileNotFoundError as exc:
        # ENOENT from stat() is ambiguous: either nothing is at the path, or a
        # symlink is and its target is gone.  lstat separates them, and only
        # the first is a project that never opted in.  An lstat that fails for
        # any other reason propagates, because that is not an absence either.
        try:
            path.lstat()
        except FileNotFoundError:
            raise FileNotFoundError(absent) from exc
        raise OSError(errno.EINVAL, "the symbolic link has no target", str(path)) from exc
    if not stat.S_ISREG(mode):
        # Refused before the open() rather than after: a fifo at this path
        # would otherwise block the read until someone wrote to it.
        raise OSError(errno.EINVAL, f"it is {_not_a_regular_file(mode)}, not a regular file", str(path))
    return load_manifest(path.read_bytes())


# ---------------------------------------------------------------------------
# Field loaders
# ---------------------------------------------------------------------------


def _load_version(raw: dict[str, Any]) -> int:
    if "version" not in raw:
        raise VolunteerManifestError("version", "required")
    value = raw["version"]
    # bool is an int subclass; `true` is not a schema version.
    if not isinstance(value, int) or isinstance(value, bool):
        raise VolunteerManifestError("version", f"expected an integer, got {type(value).__name__}")
    if value not in SUPPORTED_SCHEMA_VERSIONS:
        supported = ", ".join(str(v) for v in sorted(SUPPORTED_SCHEMA_VERSIONS))
        raise VolunteerManifestError("version", f"unsupported schema version {value}; this build accepts {supported}")
    return value


def _load_license(raw: dict[str, Any]) -> str:
    value = _require_str(raw, "license")
    if value not in OSI_APPROVED_LICENSES:
        raise VolunteerManifestError(
            "license",
            f"{value!r} is not in the accepted OSI-approved set; the volunteer program is open-source only",
        )
    return value


def _load_gates(raw: dict[str, Any]) -> tuple[GateCommand, ...]:
    value = raw.get("gates")
    if not isinstance(value, list):
        raise VolunteerManifestError("gates", f"expected a list, got {type(value).__name__}")
    if not value:
        raise VolunteerManifestError(
            "gates",
            "must name at least one command; a project with no acceptance gate cannot verify a submission",
        )
    gates: list[GateCommand] = []
    for index, entry in enumerate(value):
        gates.append(_load_gate(entry, index))
    return tuple(gates)


def _load_gate(entry: Any, index: int) -> GateCommand:
    field = f"gates[{index}]"
    if isinstance(entry, str):
        raise VolunteerManifestError(
            field,
            f"expected an argv list, got a string {entry!r}; write it as "
            f"{json.dumps(entry.split())} -- gate commands run without a shell",
        )
    if not isinstance(entry, list):
        raise VolunteerManifestError(field, f"expected an argv list, got {type(entry).__name__}")
    if not entry:
        raise VolunteerManifestError(field, "argv is empty")
    argv: list[str] = []
    for position, token in enumerate(entry):
        if not isinstance(token, str):
            raise VolunteerManifestError(f"{field}[{position}]", f"expected a string, got {type(token).__name__}")
        if not token:
            raise VolunteerManifestError(f"{field}[{position}]", "empty argument")
        offending = _SHELL_METACHARACTERS.intersection(token)
        if offending:
            raise VolunteerManifestError(
                f"{field}[{position}]",
                f"contains shell metacharacters {''.join(sorted(offending))!r}; gate commands run without a shell, "
                "so put the pipeline in a script in the repository and name the script here",
            )
        argv.append(token)
    return GateCommand(argv=tuple(argv))


def _load_allowed_paths(raw: dict[str, Any]) -> tuple[str, ...]:
    value = raw.get("allowed_paths", [])
    if not isinstance(value, list):
        raise VolunteerManifestError("allowed_paths", f"expected a list, got {type(value).__name__}")
    paths: list[str] = []
    for index, entry in enumerate(value):
        field = f"allowed_paths[{index}]"
        if not isinstance(entry, str):
            raise VolunteerManifestError(field, f"expected a string, got {type(entry).__name__}")
        paths.append(_validate_repo_relative(entry, field))
    return tuple(paths)


def _validate_repo_relative(entry: str, field: str) -> str:
    """Refuse anything that could name a file outside the checkout.

    The rules live in :func:`~bernstein.core.path_scope.validate_repo_relative_pattern`
    because an agent credential's ``allowed_files`` declares the same kind of
    scope (#3914); only the error type differs, so that is all this adds.
    """
    try:
        return _validate_repo_relative_pattern(entry)
    except ScopePatternError as exc:
        raise VolunteerManifestError(field, str(exc)) from exc


def _load_egress_allowlist(raw: dict[str, Any]) -> tuple[str, ...]:
    value = raw.get("egress_allowlist", [])
    if not isinstance(value, list):
        raise VolunteerManifestError("egress_allowlist", f"expected a list, got {type(value).__name__}")
    hosts: list[str] = []
    for index, entry in enumerate(value):
        field = f"egress_allowlist[{index}]"
        if not isinstance(entry, str):
            raise VolunteerManifestError(field, f"expected a string, got {type(entry).__name__}")
        hosts.append(_validate_host(entry, field))
    return tuple(hosts)


def _validate_host(entry: str, field: str) -> str:
    """Accept a bare hostname only.

    No scheme, no path, no wildcard.  The sandbox turns this list into deny-all
    plus these names; a wildcard would hand back the egress surface the profile
    exists to remove, and a URL would leave the sandbox guessing which part of
    it was the host.
    """
    if not entry:
        raise VolunteerManifestError(field, "empty host")
    if "://" in entry:
        raise VolunteerManifestError(field, f"{entry!r} is a URL; name the host only")
    if "/" in entry:
        raise VolunteerManifestError(field, f"{entry!r} contains a path; name the host only")
    if "*" in entry:
        raise VolunteerManifestError(
            field,
            f"{entry!r} is a wildcard; list each host explicitly so the deny-all default keeps its meaning",
        )
    if any(character.isspace() for character in entry):
        raise VolunteerManifestError(field, f"{entry!r} contains whitespace")
    if entry != entry.lower():
        raise VolunteerManifestError(field, f"{entry!r} must be lowercase so two spellings cannot hash differently")
    return entry


def _load_sandbox(raw: dict[str, Any]) -> str:
    value = _require_str(raw, "sandbox")
    if value not in SANDBOX_LEVELS:
        levels = ", ".join(SANDBOX_LEVELS)
        raise VolunteerManifestError("sandbox", f"{value!r} is not one of: {levels}")
    return value


def _load_wall_clock(raw: dict[str, Any]) -> int:
    if "max_wall_clock_minutes" not in raw:
        raise VolunteerManifestError("max_wall_clock_minutes", "required")
    value = raw["max_wall_clock_minutes"]
    if not isinstance(value, int) or isinstance(value, bool):
        raise VolunteerManifestError("max_wall_clock_minutes", f"expected an integer, got {type(value).__name__}")
    if value < 1:
        raise VolunteerManifestError("max_wall_clock_minutes", f"must be at least 1, got {value}")
    if value > MAX_WALL_CLOCK_MINUTES:
        raise VolunteerManifestError(
            "max_wall_clock_minutes",
            f"must be at most {MAX_WALL_CLOCK_MINUTES}, got {value}",
        )
    return value


def _load_task_label(raw: dict[str, Any]) -> str:
    value = raw.get("task_label", DEFAULT_TASK_LABEL)
    if not isinstance(value, str):
        raise VolunteerManifestError("task_label", f"expected a string, got {type(value).__name__}")
    if not value:
        raise VolunteerManifestError("task_label", "empty label")
    if len(value) > 50:
        raise VolunteerManifestError("task_label", f"longer than 50 characters ({len(value)})")
    if any(character.isspace() and character != " " for character in value):
        raise VolunteerManifestError("task_label", f"{value!r} contains a control character")
    return value


def _load_local_ok(raw: dict[str, Any]) -> bool:
    value = raw.get("local_ok", False)
    if not isinstance(value, bool):
        raise VolunteerManifestError("local_ok", f"expected a boolean, got {type(value).__name__}")
    return value


def _load_status(raw: dict[str, Any]) -> str:
    value = raw.get("status", "active")
    if not isinstance(value, str):
        raise VolunteerManifestError("status", f"expected a string, got {type(value).__name__}")
    if value not in STATUS_VALUES:
        raise VolunteerManifestError("status", f"{value!r} is not one of: {', '.join(STATUS_VALUES)}")
    return value


def _load_extensions(raw: dict[str, Any]) -> Mapping[str, Any]:
    """Keep fields this loader does not know, verbatim.

    They round-trip into :meth:`VolunteerManifest.to_canonical_dict` and so
    into the digest.  A project that adds a policy-tightening field gets a
    different digest even from workers too old to enforce it, which is what
    stops an old worker producing a receipt that looks like compliance with a
    policy it never read.

    The load also warns, because carrying a field is not enforcing it: a donor
    whose build is older than the project's manifest is running under a policy
    it can only partly apply, and that is worth saying out loud.
    """
    extensions = {key: value for key, value in sorted(raw.items()) if key not in _KNOWN_FIELDS}
    if extensions:
        warnings.warn(
            f"{VOLUNTEER_MANIFEST_PATH} declares fields this build does not enforce: "
            f"{', '.join(extensions)}. They are carried into the manifest digest, so receipts stay "
            "comparable, but a newer worker may apply policy this one cannot.",
            UnenforcedManifestFieldWarning,
            stacklevel=4,
        )
    return extensions


def _require_str(raw: dict[str, Any], field: str) -> str:
    if field not in raw:
        raise VolunteerManifestError(field, "required")
    value = raw[field]
    if not isinstance(value, str):
        raise VolunteerManifestError(field, f"expected a string, got {type(value).__name__}")
    if not value:
        raise VolunteerManifestError(field, "empty")
    return value


def _sort_recursive(value: Any) -> Any:
    """Order dict keys at every depth so serialisation is byte-stable."""
    if isinstance(value, dict):
        return {key: _sort_recursive(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sort_recursive(item) for item in value]
    return value
