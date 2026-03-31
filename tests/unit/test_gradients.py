"""Unit tests for terminal gradient rendering."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import pytest

from bernstein.cli.gradients import _LOWER_HALF, _RESET, _make_stops, linear_gradient, radial_gradient


def test_make_stops_evenly_distributes_colors_and_validates_lengths() -> None:
    stops = _make_stops([(0, 0, 0), (255, 255, 255), (10, 20, 30)], None)

    assert [position for position, _ in stops] == [0.0, 0.5, 1.0]

    with pytest.raises(ValueError, match="stops length"):
        _make_stops([(0, 0, 0), (255, 255, 255)], [0.0])


def test_linear_gradient_renders_requested_number_of_rows() -> None:
    rendered = linear_gradient(4, 2, [(0, 0, 0), (255, 255, 255)])

    lines = rendered.splitlines()
    assert len(lines) == 2
    assert all(line.endswith(_RESET) for line in lines)
    assert all(line.count(_LOWER_HALF) == 4 for line in lines)


def test_radial_gradient_returns_empty_for_zero_dimensions() -> None:
    assert radial_gradient(0, 4, (255, 255, 255), (0, 0, 0)) == ""
    assert radial_gradient(4, 0, (255, 255, 255), (0, 0, 0)) == ""


def test_linear_gradient_returns_empty_for_zero_dimensions() -> None:
    assert linear_gradient(0, 3, [(0, 0, 0), (255, 255, 255)]) == ""
    assert linear_gradient(4, 0, [(0, 0, 0), (255, 255, 255)]) == ""


def test_make_stops_handles_single_color() -> None:
    stops = _make_stops([(1, 2, 3)], None)

    assert stops == [(0.0, (1, 2, 3)), (1.0, (1, 2, 3))]
