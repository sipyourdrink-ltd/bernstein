"""``bernstein volunteer verify``, one test per way it could mislead a maintainer.

The command exists so a project learns its manifest is wrong from its own
checkout instead of from a stranger's worker declining the repository. Every
test below names a way that loop could still fail: a rejection that does not
say which field, a digest that does not match the one a receipt will carry, a
report that repeats the file back instead of saying what a donor will actually
be allowed to do.
"""

from __future__ import annotations

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
