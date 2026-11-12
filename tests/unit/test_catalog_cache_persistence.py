"""Preserve the last good catalog when a newer response was rejected."""

from pathlib import Path

from bernstein.core.protocols.catalog_fetch import CacheEntry, RejectedResponse, read_cache, write_cache


def test_catalog_cache_round_trips_distinct_validator_and_last_good_body(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    url = "https://catalog.example/index.json"
    entry = CacheEntry(
        fetched_at="2026-01-01T00:00:00+00:00",
        etag='"accepted"',
        source_url=url,
        catalog={"entries": [{"id": "sample"}]},
        rejected=RejectedResponse(
            etag='"rejected"',
            fetched_at="2026-01-02T00:00:00+00:00",
            source_url=url,
            reason="schema mismatch",
        ),
    )

    write_cache(path, entry)
    reloaded = read_cache(path)

    assert reloaded == entry
    assert reloaded is not None
    assert reloaded.validator == '"rejected"'
    assert reloaded.last_checked_at == "2026-01-02T00:00:00+00:00"


def test_invalid_cache_is_a_miss(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text('{"catalog": ["not an object"]}', encoding="utf-8")
    assert read_cache(path) is None
