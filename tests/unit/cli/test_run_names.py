"""Tests for memorable deterministic run names (#1626)."""

from __future__ import annotations

import random
import re
import uuid

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.run_names_cmd import run_lookup_cmd
from bernstein.cli.run_names import (
    ADJECTIVES,
    MAX_WORD_LEN,
    NAME_RE,
    NOUNS,
    build_lookup,
    find_collisions,
    is_run_name,
    name_space_size,
    render_name,
)

_FIXED_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")


# ---------------------------------------------------------------------------
# Word-list hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("words", [ADJECTIVES, NOUNS], ids=["adjectives", "nouns"])
def test_word_lists_are_sorted_unique_and_short(words: tuple[str, ...]) -> None:
    assert list(words) == sorted(words), "word list must stay sorted for stable review diffs"
    assert len(set(words)) == len(words), "word list must not contain duplicates"
    for word in words:
        assert word.isascii() and word.islower() and word.isalpha(), word
        assert len(word) <= MAX_WORD_LEN, f"{word!r} exceeds MAX_WORD_LEN"


def test_word_lists_are_non_empty() -> None:
    assert ADJECTIVES
    assert NOUNS


# ---------------------------------------------------------------------------
# Determinism / stability
# ---------------------------------------------------------------------------


def test_render_name_is_deterministic_for_same_id() -> None:
    first = render_name(_FIXED_UUID)
    second = render_name(uuid.UUID(str(_FIXED_UUID)))
    assert first == second


def test_render_name_is_stable_across_versions() -> None:
    # Golden value: changing the recipe or word lists would break stored
    # references in logs and dashboards, so this is a deliberate guard.
    assert render_name(_FIXED_UUID) == "fancy-thicket-75"


def test_render_name_independent_of_uuid_object_identity() -> None:
    raw = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    assert render_name(uuid.UUID(raw)) == render_name(uuid.UUID(raw))


def test_render_name_distinguishes_different_ids() -> None:
    other = uuid.UUID("87654321-4321-8765-4321-876543210987")
    assert render_name(_FIXED_UUID) != render_name(other)


def test_render_name_rejects_non_uuid() -> None:
    with pytest.raises(TypeError):
        render_name("not-a-uuid")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Format
# ---------------------------------------------------------------------------


def test_render_name_matches_documented_format() -> None:
    name = render_name(_FIXED_UUID)
    assert NAME_RE.match(name)
    assert is_run_name(name)
    adjective, noun, suffix = name.split("-")
    assert adjective in ADJECTIVES
    assert noun in NOUNS
    assert re.fullmatch(r"\d{2}", suffix)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("swift-otter-07", True),
        ("calm-river-99", True),
        ("Swift-otter-07", False),  # uppercase
        ("swift-otter-7", False),  # one-digit suffix
        ("swift-otter", False),  # missing suffix
        ("swift_otter_07", False),  # wrong separator
        ("swift-otter-100", False),  # three-digit suffix
        ("", False),
    ],
)
def test_is_run_name_shape(value: str, expected: bool) -> None:
    assert is_run_name(value) is expected


def _uuid_sample(count: int) -> list[uuid.UUID]:
    rng = random.Random(42)
    return [uuid.UUID(int=rng.getrandbits(128), version=4) for _ in range(count)]


def test_all_names_match_format_over_sample() -> None:
    for run_id in _uuid_sample(1000):
        name = render_name(run_id)
        assert is_run_name(name), name


# ---------------------------------------------------------------------------
# Collision resistance
# ---------------------------------------------------------------------------


def test_name_space_size_matches_word_lists() -> None:
    assert name_space_size() == len(ADJECTIVES) * len(NOUNS) * 100


def test_collision_rate_is_low_over_large_sample() -> None:
    # 5000 random ids over a ~473k name space: by the birthday bound the
    # expected collision count is well under 30. Assert a generous ceiling
    # so the test is not flaky while still catching a degenerate recipe
    # (e.g. one that ignores most of the digest).
    sample = _uuid_sample(5000)
    collisions = find_collisions(sample)
    colliding_ids = sum(len(ids) for ids in collisions.values())
    assert colliding_ids < 100, f"unexpectedly high collision count: {colliding_ids}"


def test_find_collisions_detects_known_clash() -> None:
    sample = _uuid_sample(20_000)
    collisions = find_collisions(sample)
    assert collisions, "the fixed sample must contain at least one collision"
    for name, ids in collisions.items():
        assert is_run_name(name)
        assert len(ids) >= 2
        assert all(render_name(i) == name for i in ids)


def test_find_collisions_empty_for_distinct_names() -> None:
    # Hand-pick ids that we have verified render to distinct names.
    ids = [_FIXED_UUID, uuid.UUID("87654321-4321-8765-4321-876543210987")]
    assert render_name(ids[0]) != render_name(ids[1])
    assert find_collisions(ids) == {}


def test_build_lookup_round_trips_known_ids() -> None:
    ids = _uuid_sample(50)
    lookup = build_lookup(ids)
    for run_id in ids:
        name = render_name(run_id)
        assert name in lookup
        # First-writer-wins, so the resolved id must itself render to name.
        assert render_name(lookup[name]) == name


# ---------------------------------------------------------------------------
# run-lookup CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_run_lookup_resolves_candidate(runner: CliRunner) -> None:
    name = render_name(_FIXED_UUID)
    result = runner.invoke(run_lookup_cmd, [name, "--candidate", str(_FIXED_UUID)])
    assert result.exit_code == 0, result.output
    assert str(_FIXED_UUID) in result.output


def test_run_lookup_rejects_malformed_name(runner: CliRunner) -> None:
    result = runner.invoke(run_lookup_cmd, ["NOT_A_NAME", "--candidate", str(_FIXED_UUID)])
    assert result.exit_code == 2
    assert "not a valid run name" in result.output


def test_run_lookup_reports_unknown_name(runner: CliRunner) -> None:
    # Valid shape but no candidate renders to it.
    result = runner.invoke(run_lookup_cmd, ["swift-otter-07"])
    assert result.exit_code == 1
    assert "No known run id" in result.output


def test_run_lookup_reads_active_run_id(runner: CliRunner, tmp_path) -> None:
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "run_id").write_text(str(_FIXED_UUID), encoding="utf-8")
    name = render_name(_FIXED_UUID)
    result = runner.invoke(run_lookup_cmd, [name, "--workspace-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert str(_FIXED_UUID) in result.output


def test_run_lookup_rejects_bad_candidate_uuid(runner: CliRunner) -> None:
    result = runner.invoke(run_lookup_cmd, ["swift-otter-07", "--candidate", "not-a-uuid"])
    assert result.exit_code != 0
    assert "not a valid UUID" in result.output
