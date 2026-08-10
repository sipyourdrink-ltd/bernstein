"""Contracts for the release-version RPM spec renderer (issue #3325).

The spec checked into ``packaging/rpm/bernstein.spec`` carries whatever
version was last committed. The release chain has to publish the *tag*
version, so the renderer is the single place that binds the two together;
if it silently kept the committed version the RPM channel would keep
shipping a stale package while every job stayed green.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "build_copr_srpm.py"
SPEC = REPO_ROOT / "packaging" / "rpm" / "bernstein.spec"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_copr_srpm", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod() -> ModuleType:
    assert SCRIPT.exists(), f"{SCRIPT} must exist"
    return _load_module()


@pytest.fixture(scope="module")
def spec_text() -> str:
    return SPEC.read_text(encoding="utf-8")


def test_rpm_version_strips_the_tag_prefix(mod: ModuleType) -> None:
    assert mod.rpm_version("v3.13.0") == "3.13.0"
    assert mod.rpm_version("3.13.0") == "3.13.0"


def test_rpm_version_uses_the_rpm_prerelease_separator(mod: ModuleType) -> None:
    """RPM forbids ``-`` in ``Version:``; ``~`` sorts before the final release."""
    assert mod.rpm_version("v3.13.0-rc1") == "3.13.0~rc1"


@pytest.mark.parametrize("bad", ["", "v", "vnext", "3.13.0 0", "3.13.0/etc"])
def test_rpm_version_rejects_values_rpm_cannot_carry(mod: ModuleType, bad: str) -> None:
    with pytest.raises(ValueError, match="version"):
        mod.rpm_version(bad)


def test_pypi_version_strips_the_tag_prefix(mod: ModuleType) -> None:
    assert mod.pypi_version("v3.13.0") == "3.13.0"
    assert mod.pypi_version("3.13.0") == "3.13.0"


def test_pypi_version_keeps_the_index_prerelease_separator(mod: ModuleType) -> None:
    """RPM spells a pre-release ``3.13.0~rc1``; PyPI serves it as ``3.13.0-rc1``.

    The spec installs ``bernstein==%%{pypi_version}`` into the packaged venv,
    so the binding must keep the spelling the index resolves.
    """
    assert mod.pypi_version("v3.13.0-rc1") == "3.13.0-rc1"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "v",
        "vnext",
        "3.13.0 0",
        "3.13.0/etc",
        # Shapes a character whitelist would wave through but pip cannot
        # resolve: the SRPM would render fine and fail in the remote builder.
        "3.13.0..1",
        "1.0--rc1",
        "1.0+foo+bar",
        "1.0+local",
        "3.13.0-",
        # An epoch cannot be carried by `Version:`, so accepting one here would
        # advertise a capability `render_spec` cannot honour.
        "1!2.0",
    ],
)
def test_pypi_version_rejects_values_pip_cannot_resolve(mod: ModuleType, bad: str) -> None:
    with pytest.raises(ValueError, match="version"):
        mod.pypi_version(bad)


@pytest.mark.parametrize(
    "good",
    ["3.13.0", "3.13.0-rc1", "3.13.0rc1", "3.13.0.post1", "3.13.0.dev1"],
)
def test_pypi_version_accepts_the_release_grammar(mod: ModuleType, good: str) -> None:
    assert mod.pypi_version(good) == good


def test_render_spec_binds_the_spec_to_the_release_version(mod: ModuleType, spec_text: str) -> None:
    """The rendered spec must declare the release version, not the committed one."""
    rendered = mod.render_spec(spec_text, "v3.13.0", date(2026, 8, 1))

    version_lines = [line for line in rendered.splitlines() if line.startswith("Version:")]
    assert version_lines == ["Version:        3.13.0"]


def test_render_spec_binds_the_packaged_payload_to_the_release_version(mod: ModuleType, spec_text: str) -> None:
    """``%install`` fetches ``bernstein==%%{pypi_version}`` into the venv.

    Leaving that bound to the committed value would build a package whose
    payload is a different release than its own metadata claims - the exact
    defect the venv spec replaced (#3558).
    """
    rendered = mod.render_spec(spec_text, "v3.13.0", date(2026, 8, 1))

    payload_lines = [line for line in rendered.splitlines() if line.startswith("%global pypi_version")]
    assert payload_lines == ["%global pypi_version 3.13.0"]


def test_render_spec_binds_both_spellings_of_a_prerelease(mod: ModuleType, spec_text: str) -> None:
    """One release, two version grammars: RPM's ``~`` and PyPI's ``-``."""
    rendered = mod.render_spec(spec_text, "v3.13.0-rc1", date(2026, 8, 1))

    assert "Version:        3.13.0~rc1" in rendered
    assert "%global pypi_version 3.13.0-rc1" in rendered


def test_render_spec_fails_loudly_on_a_spec_without_the_payload_binding(mod: ModuleType) -> None:
    with pytest.raises(ValueError, match="pypi_version"):
        mod.render_spec("Name: bernstein\nVersion: 0.0.0\n", "v3.13.0", date(2026, 8, 1))


def test_render_spec_records_the_release_in_the_changelog(mod: ModuleType, spec_text: str) -> None:
    rendered = mod.render_spec(spec_text, "v3.13.0", date(2026, 8, 1))
    body = rendered.split("%changelog\n", 1)[1]

    # 2026-08-01 is a Saturday; a wrong weekday makes rpmbuild warn.
    assert body.splitlines()[0].startswith("* Sat Aug 01 2026 ")
    # The entry must carry the spec's own `Release:` number, not a hardcoded 1:
    # a changelog naming a different EVR than the package builds is a lie about
    # what was shipped.
    assert body.splitlines()[0].endswith(" - 3.13.0-2")
    assert "- Release 3.13.0" in body


def test_render_spec_changelog_release_tracks_the_release_field(mod: ModuleType) -> None:
    spec = "Name: bernstein\nVersion: 0.0.0\nRelease: 7%{?dist}\n%global pypi_version 0.0.0\n"
    rendered = mod.render_spec(spec, "v3.13.0", date(2026, 8, 1))

    assert rendered.split("%changelog\n", 1)[1].splitlines()[0].endswith(" - 3.13.0-7")


def test_render_spec_is_deterministic_and_idempotent(mod: ModuleType, spec_text: str) -> None:
    """Re-rendering the same release must not stack duplicate changelog entries."""
    once = mod.render_spec(spec_text, "v3.13.0", date(2026, 8, 1))
    twice = mod.render_spec(once, "v3.13.0", date(2026, 8, 1))

    assert once == mod.render_spec(spec_text, "v3.13.0", date(2026, 8, 1))
    assert twice == once


def test_render_spec_keeps_the_previous_changelog_history(mod: ModuleType, spec_text: str) -> None:
    rendered = mod.render_spec(spec_text, "v3.13.0", date(2026, 8, 1))

    assert "1.4.11-1" in rendered.split("%changelog\n", 1)[1]


def test_render_spec_fails_loudly_on_a_spec_without_a_version(mod: ModuleType) -> None:
    with pytest.raises(ValueError, match="Version:"):
        mod.render_spec("Name: bernstein\n", "v3.13.0", date(2026, 8, 1))


def test_committed_spec_changelog_dates_are_real_weekdays(spec_text: str) -> None:
    """A wrong weekday in ``%changelog`` makes every rpmbuild emit a warning."""
    import re

    stamp = re.compile(r"^\* (\w{3}) (\w{3}) (\d{2}) (\d{4}) ")
    months = {
        "Jan": 1,
        "Feb": 2,
        "Mar": 3,
        "Apr": 4,
        "May": 5,
        "Jun": 6,
        "Jul": 7,
        "Aug": 8,
        "Sep": 9,
        "Oct": 10,
        "Nov": 11,
        "Dec": 12,
    }
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

    entries = [match for line in spec_text.splitlines() if (match := stamp.match(line))]
    assert entries, "spec must keep a %changelog"
    for match in entries:
        day_name, month_name, day, year = match.groups()
        actual = date(int(year), months[month_name], int(day))
        assert day_name == weekdays[actual.weekday()], f"bogus changelog date: {match.group(0)!r}"


def test_every_version_the_pypi_check_accepts_can_also_be_rendered(mod: ModuleType) -> None:
    """The two version checks must agree on what a release can be.

    ``pypi_version`` accepting a spelling that ``rpm_version`` rejects would
    advertise support the renderer cannot honour: the value passes the helper
    and then raises out of ``render_spec`` before anything is bound.
    """
    for candidate in ("3.13.0", "3.13.0-rc1", "3.13.0rc1", "3.13.0.post1", "3.13.0.dev1"):
        assert mod.pypi_version(candidate)
        assert mod.rpm_version(candidate)


def test_spec_compares_the_packaged_version_as_pep440_not_as_a_string(spec_text: str) -> None:
    """``%check`` must survive a pre-release.

    A tag spells a pre-release ``3.15.0-rc1``; the installed distribution
    metadata carries the normalised ``3.15.0rc1``. Comparing those as strings
    fails every pre-release build even though pip installed exactly the right
    release, so the check has to compare them as PEP 440 versions.
    """
    check_body = spec_text.split("%check", 1)[1].split("%files", 1)[0]

    assert "from packaging.version import Version" in check_body
    assert "Version(got) == Version(" in check_body
    assert 'got == "%{pypi_version}"' not in check_body
