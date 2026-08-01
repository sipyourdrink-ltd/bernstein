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


def test_render_spec_binds_the_spec_to_the_release_version(mod: ModuleType, spec_text: str) -> None:
    """The rendered spec must declare the release version, not the committed one."""
    rendered = mod.render_spec(spec_text, "v3.13.0", date(2026, 8, 1))

    version_lines = [line for line in rendered.splitlines() if line.startswith("Version:")]
    assert version_lines == ["Version:        3.13.0"]


def test_render_spec_records_the_release_in_the_changelog(mod: ModuleType, spec_text: str) -> None:
    rendered = mod.render_spec(spec_text, "v3.13.0", date(2026, 8, 1))
    body = rendered.split("%changelog\n", 1)[1]

    # 2026-08-01 is a Saturday; a wrong weekday makes rpmbuild warn.
    assert body.splitlines()[0].startswith("* Sat Aug 01 2026 ")
    assert body.splitlines()[0].endswith(" - 3.13.0-1")
    assert "- Release 3.13.0" in body


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
