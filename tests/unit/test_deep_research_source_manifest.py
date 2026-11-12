"""Pin canonical source identities without making any requests."""

from bernstein.adapters.deep_research_artifact import canonical_sources


def test_source_canonicalization_preserves_distinct_url_paths() -> None:
    assert canonical_sources(
        [
            " https://source.example/report/ ",
            None,
            "https://source.example/report",
            "https://source.example/report",
            " ",
        ]
    ) == ["https://source.example/report", "https://source.example/report/"]
