"""Tests for repository_flow_series module (#4940)."""

from pathlib import Path

import pytest

from bernstein.core.observability.repository_flow_series import (
    RepositoryFlowSample,
    RepositoryFlowSeriesError,
    append_sample,
    deserialize_sample,
    read_samples,
    serialize_sample,
)


def test_append_three_samples_produces_three_parseable_lines(
    tmp_path: Path,
) -> None:
    """Append three distinct samples, file holds exactly three lines that all parse."""
    series_path = tmp_path / "flow.jsonl"
    samples = [
        RepositoryFlowSample(
            observed_at=1700000000.0,
            commits_per_min=1.5,
            open_prs=10,
            churn_lines=500,
            open_issues=5,
        ),
        RepositoryFlowSample(
            observed_at=1700000060.0,
            commits_per_min=2.0,
            open_prs=12,
            churn_lines=600,
            open_issues=3,
        ),
        RepositoryFlowSample(
            observed_at=1700000120.0,
            commits_per_min=1.8,
            open_prs=11,
            churn_lines=550,
            open_issues=4,
        ),
    ]

    for s in samples:
        append_sample(series_path, s)

    content = series_path.read_text()
    lines = [ln for ln in content.split("\n") if ln]
    assert len(lines) == 3

    # Each line parses
    for line in lines:
        deserialize_sample(line.encode("utf-8"))


def test_read_returns_samples_in_write_order_with_all_fields(
    tmp_path: Path,
) -> None:
    """Read them back, every field equals the original."""
    series_path = tmp_path / "flow.jsonl"
    samples = [
        RepositoryFlowSample(
            observed_at=1700000000.0,
            commits_per_min=1.5,
            open_prs=10,
            churn_lines=500,
            open_issues=5,
        ),
        RepositoryFlowSample(
            observed_at=1700000060.0,
            commits_per_min=2.0,
            open_prs=12,
            churn_lines=600,
            open_issues=3,
        ),
    ]

    for s in samples:
        append_sample(series_path, s)

    read_back = read_samples(series_path)
    assert len(read_back) == 2

    for orig, parsed in zip(samples, read_back, strict=True):
        assert parsed.observed_at == orig.observed_at
        assert parsed.commits_per_min == orig.commits_per_min
        assert parsed.open_prs == orig.open_prs
        assert parsed.churn_lines == orig.churn_lines
        assert parsed.open_issues == orig.open_issues


def test_append_does_not_rewrite_existing_bytes(tmp_path: Path) -> None:
    """Capture file bytes after append #2, append #3, assert prefix is byte-identical."""
    series_path = tmp_path / "flow.jsonl"

    s1 = RepositoryFlowSample(
        observed_at=1700000000.0,
        commits_per_min=1.5,
        open_prs=10,
        churn_lines=500,
        open_issues=5,
    )
    s2 = RepositoryFlowSample(
        observed_at=1700000060.0,
        commits_per_min=2.0,
        open_prs=12,
        churn_lines=600,
        open_issues=3,
    )
    s3 = RepositoryFlowSample(
        observed_at=1700000120.0,
        commits_per_min=1.8,
        open_prs=11,
        churn_lines=550,
        open_issues=4,
    )

    append_sample(series_path, s1)
    append_sample(series_path, s2)

    prefix = series_path.read_bytes()

    append_sample(series_path, s3)

    new_content = series_path.read_bytes()
    assert new_content[: len(prefix)] == prefix


def test_serialize_is_byte_deterministic() -> None:
    """Serialize same sample twice, two byte strings are equal."""
    sample = RepositoryFlowSample(
        observed_at=1700000000.0,
        commits_per_min=1.5,
        open_prs=10,
        churn_lines=500,
        open_issues=5,
    )

    b1 = serialize_sample(sample)
    b2 = serialize_sample(sample)

    assert b1 == b2


def test_malformed_line_error_names_its_line_number(tmp_path: Path) -> None:
    """Write two valid samples then malformed JSON, expect error with line 3."""
    series_path = tmp_path / "flow.jsonl"

    s1 = RepositoryFlowSample(
        observed_at=1700000000.0,
        commits_per_min=1.5,
        open_prs=10,
        churn_lines=500,
        open_issues=5,
    )
    s2 = RepositoryFlowSample(
        observed_at=1700000060.0,
        commits_per_min=2.0,
        open_prs=12,
        churn_lines=600,
        open_issues=3,
    )

    append_sample(series_path, s1)
    append_sample(series_path, s2)

    # Append malformed line
    with open(series_path, "ab") as f:
        f.write(b"{not json\n")

    with pytest.raises(RepositoryFlowSeriesError) as exc_info:
        read_samples(series_path)

    assert "line 3" in str(exc_info.value)


@pytest.mark.parametrize("field", ["open_prs", "churn_lines", "open_issues"])
def test_read_samples_names_text_counter_and_line(tmp_path: Path, field: str) -> None:
    """A text value in an integer counter is an error that names the field and its line (#8025)."""
    import json

    valid = {"observed_at": 0.0, "commits_per_min": 0.0, "open_prs": 2, "churn_lines": 0, "open_issues": 3}
    series_path = tmp_path / "series.jsonl"
    series_path.write_text(json.dumps(valid) + "\n" + json.dumps({**valid, field: "three"}) + "\n")

    with pytest.raises(RepositoryFlowSeriesError) as exc_info:
        read_samples(series_path)

    assert "line 2" in str(exc_info.value)
    assert field in str(exc_info.value)


def test_integer_only_series_still_round_trips(tmp_path: Path) -> None:
    sample = RepositoryFlowSample(
        observed_at=1700000000.0, commits_per_min=1.5, open_prs=10, churn_lines=500, open_issues=5
    )
    series_path = tmp_path / "flow.jsonl"
    append_sample(series_path, sample)
    assert read_samples(series_path) == [sample]
