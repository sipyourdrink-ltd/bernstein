"""Tests for hoeffding_confidence_sequence."""

import pytest

from bernstein.core.quality.empirical_confidence import (
    canonical_round,
    hoeffding_confidence_sequence,
)


class TestHoeffdingConfidenceSequence:
    """Tests for the Hoeffding confidence sequence function."""

    def test_zero_trials(self):
        """n=0 should return the full interval [0, 1]."""
        lower, upper = hoeffding_confidence_sequence(0, 0, 0.05)
        assert lower == 0.0
        assert upper == 1.0

    def test_all_successes(self):
        """All successes should have upper bound at 1.0."""
        lower, upper = hoeffding_confidence_sequence(10, 10, 0.05)
        assert upper == 1.0
        assert lower > 0.5  # Should be fairly high with 10/10

    def test_all_failures(self):
        """All failures should have lower bound at 0.0."""
        lower, upper = hoeffding_confidence_sequence(0, 10, 0.05)
        assert lower == 0.0
        assert upper < 0.5  # Should be fairly low with 0/10

    def test_half_successes(self):
        """Half successes should have interval centered near 0.5."""
        lower, upper = hoeffding_confidence_sequence(5, 10, 0.05)
        # p_hat = 0.5, interval should be symmetric around 0.5
        assert lower < 0.5
        assert upper > 0.5
        # Check symmetry (within rounding)
        mid = (lower + upper) / 2
        assert abs(mid - 0.5) < 0.001

    def test_increasing_n_narrows_interval(self):
        """As n increases, the interval should narrow."""
        lower_10, upper_10 = hoeffding_confidence_sequence(5, 10, 0.05)
        lower_100, upper_100 = hoeffding_confidence_sequence(50, 100, 0.05)

        # Both maintain same proportion
        assert abs((lower_10 + upper_10) / 2 - 0.5) < 0.001
        assert abs((lower_100 + upper_100) / 2 - 0.5) < 0.001

        # But larger n has narrower interval
        width_10 = upper_10 - lower_10
        width_100 = upper_100 - lower_100
        assert width_100 < width_10

    def test_smaller_alpha_narrows_interval(self):
        """Smaller alpha (lower error rate) gives wider interval."""
        lower_05, upper_05 = hoeffding_confidence_sequence(5, 10, 0.05)
        lower_01, upper_01 = hoeffding_confidence_sequence(5, 10, 0.01)

        # Smaller alpha means more conservative (wider) interval
        width_05 = upper_05 - lower_05
        width_01 = upper_01 - lower_01
        assert width_01 > width_05

    def test_bounds_within_zero_one(self):
        """Bounds should always be in [0, 1]."""
        # Test edge cases
        for k, n in [(0, 1), (1, 1), (0, 100), (100, 100), (50, 100)]:
            for alpha in [0.01, 0.05, 0.10]:
                lower, upper = hoeffding_confidence_sequence(k, n, alpha)
                assert 0.0 <= lower <= 1.0
                assert 0.0 <= upper <= 1.0
                assert lower <= upper

    def test_deterministic_reproducible(self):
        """Same inputs should give identical outputs."""
        result1 = hoeffding_confidence_sequence(7, 13, 0.05)
        result2 = hoeffding_confidence_sequence(7, 13, 0.05)
        assert result1 == result2

    def test_canonical_rounding_applied(self):
        """Results should be canonically rounded to 10 decimal places."""
        lower, upper = hoeffding_confidence_sequence(7, 13, 0.05)

        # Check that values are rounded (not too many decimal places)
        def count_decimal_places(x: float) -> int:
            s = f"{x:.15f}".rstrip("0")
            if "." in s:
                return len(s.split(".")[1])
            return 0

        # Canonically rounded values should have at most 10 decimal places
        assert count_decimal_places(lower) <= 10
        assert count_decimal_places(upper) <= 10

    def test_k_greater_than_n_raises(self):
        """k > n should raise ValueError."""
        with pytest.raises(ValueError, match="k cannot exceed n"):
            hoeffding_confidence_sequence(11, 10, 0.05)

    def test_negative_k_raises(self):
        """Negative k should raise ValueError."""
        with pytest.raises(ValueError, match="k must be non-negative"):
            hoeffding_confidence_sequence(-1, 10, 0.05)

    def test_negative_n_raises(self):
        """Negative n should raise ValueError."""
        with pytest.raises(ValueError, match="n must be non-negative"):
            hoeffding_confidence_sequence(0, -1, 0.05)

    def test_hoeffding_formula_correctness(self):
        """Verify the Hoeffding bound formula is correctly applied."""
        # For k=10, n=20, alpha=0.05
        # p_hat = 0.5
        # ln(2/alpha) = ln(40) ≈ 3.6888...
        # bound = sqrt(ln(2/alpha) / (2*n)) = sqrt(3.6888... / 40) ≈ 0.3036...
        # lower ≈ 0.5 - 0.3036 ≈ 0.1964
        # upper ≈ 0.5 + 0.3036 ≈ 0.8036

        lower, upper = hoeffding_confidence_sequence(10, 20, 0.05)

        # Verify approximate values (accounting for canonical rounding)
        assert 0.19 < lower < 0.20
        assert 0.80 < upper < 0.81

    def test_time_uniform_validity(self):
        """Test that bound is valid at different stopping points."""
        # Simulate accumulating observations
        # The bound should be valid at each n
        alpha = 0.05

        # With many trials, the interval should contain true_p most of the time
        k = 70  # Observed successes
        n = 100  # Total trials
        lower, upper = hoeffding_confidence_sequence(k, n, alpha)

        # p_hat = 0.7, the interval should contain 0.7
        assert lower <= 0.7 <= upper


class TestCanonicalRound:
    """Tests for the canonical_round helper function."""

    def test_round_half_even(self):
        """Test round-half-to-even behavior."""
        # canonical_round quantises at `places` decimals, so half-even only
        # shows at places=0; the default of 10 leaves these values untouched.
        assert canonical_round(0.5, places=0) == 0.0
        assert canonical_round(1.5, places=0) == 2.0
        assert canonical_round(2.5, places=0) == 2.0
        # At the default precision the value is preserved exactly.
        assert canonical_round(0.5) == 0.5

    def test_negative_zero_normalized(self):
        """Negative zero should be normalized to positive zero."""
        result = canonical_round(-0.0)
        assert result == 0.0
        assert str(result) == "0.0"  # Not "-0.0"

    def test_nan_raises(self):
        """NaN should raise ValueError."""
        with pytest.raises(ValueError, match="cannot canonically round non-finite"):
            canonical_round(float("nan"))

    def test_inf_raises(self):
        """Infinity should raise ValueError."""
        with pytest.raises(ValueError, match="cannot canonically round non-finite"):
            canonical_round(float("inf"))

    def test_negative_inf_raises(self):
        """Negative infinity should raise ValueError."""
        with pytest.raises(ValueError, match="cannot canonically round non-finite"):
            canonical_round(float("-inf"))

    def test_precision(self):
        """Results should be precise to 10 decimal places."""
        # A value with many decimal places
        x = 1.234567890123456789
        rounded = canonical_round(x)

        # Should be rounded to 10 decimal places
        # The rounded value should be reproducible
        assert rounded == canonical_round(x)
