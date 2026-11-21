"""Test the retention CSV projection with synthetic audit facts."""

import csv
import io

from bernstein.core.compliance.regulator_renderers import render_retention_csv


def test_retention_csv_preserves_boundaries_and_counts_gaps() -> None:
    evidence = {
        "period": {"since": "2026-01-01", "until": "2026-01-31"},
        "boundary": {"first_entry_hash": "sha256:first", "last_entry_hash": "sha256:last"},
        "entry_count": 3,
        "coverage_gaps": [{"day": "2026-01-12"}, {"day": "2026-01-13"}],
        "retention_params": {"period_days": 30, "minimum_required_days": 60, "meets_minimum": False},
    }

    assert list(csv.DictReader(io.StringIO(render_retention_csv(evidence)))) == [
        {
            "period_since": "2026-01-01",
            "period_until": "2026-01-31",
            "entry_count": "3",
            "first_entry_hash": "sha256:first",
            "last_entry_hash": "sha256:last",
            "coverage_gaps": "2",
            "period_days": "30",
            "minimum_required_days": "60",
            "meets_minimum": "False",
        }
    ]
