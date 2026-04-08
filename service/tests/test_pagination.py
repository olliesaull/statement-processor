"""Tests for the pagination utility."""

from __future__ import annotations

import pytest

from utils.pagination import PaginationResult, paginate


class TestSnapPerPage:
    """Verify per_page snapping to nearest valid option."""

    def test_exact_match_returns_unchanged(self) -> None:
        result = paginate(total_items=100, page=1, per_page=25, per_page_options=[25, 50, 100])
        assert result.per_page == 25

    def test_snap_down_when_closer_to_lower(self) -> None:
        result = paginate(total_items=100, page=1, per_page=35, per_page_options=[25, 50, 100])
        assert result.per_page == 25

    def test_snap_up_when_closer_to_higher(self) -> None:
        result = paginate(total_items=100, page=1, per_page=45, per_page_options=[25, 50, 100])
        assert result.per_page == 50

    def test_tie_rounds_down(self) -> None:
        result = paginate(total_items=200, page=1, per_page=75, per_page_options=[25, 50, 100])
        assert result.per_page == 50

    def test_below_minimum_snaps_to_minimum(self) -> None:
        result = paginate(total_items=100, page=1, per_page=5, per_page_options=[25, 50, 100])
        assert result.per_page == 25

    def test_above_maximum_snaps_to_maximum(self) -> None:
        result = paginate(total_items=100, page=1, per_page=500, per_page_options=[25, 50, 100])
        assert result.per_page == 100

    def test_no_options_uses_per_page_as_is(self) -> None:
        result = paginate(total_items=100, page=1, per_page=50)
        assert result.per_page == 50


class TestPageClamping:
    """Verify page clamping to valid range."""

    def test_page_below_one_clamps_to_one(self) -> None:
        result = paginate(total_items=100, page=0, per_page=25)
        assert result.page == 1

    def test_negative_page_clamps_to_one(self) -> None:
        result = paginate(total_items=100, page=-5, per_page=25)
        assert result.page == 1

    def test_page_beyond_total_clamps_to_last(self) -> None:
        result = paginate(total_items=100, page=99, per_page=25)
        assert result.page == 4

    def test_valid_page_unchanged(self) -> None:
        result = paginate(total_items=100, page=2, per_page=25)
        assert result.page == 2


class TestPaginationResult:
    """Verify computed pagination fields."""

    def test_basic_pagination(self) -> None:
        result = paginate(total_items=100, page=2, per_page=25)
        assert result == PaginationResult(page=2, per_page=25, total_pages=4, total_items=100, start_index=25, end_index=50)

    def test_last_page_partial(self) -> None:
        result = paginate(total_items=30, page=2, per_page=25)
        assert result.total_pages == 2
        assert result.start_index == 25
        assert result.end_index == 50

    def test_zero_items(self) -> None:
        result = paginate(total_items=0, page=1, per_page=25)
        assert result.total_pages == 1
        assert result.page == 1
        assert result.start_index == 0
        assert result.end_index == 25

    def test_exactly_one_page(self) -> None:
        result = paginate(total_items=25, page=1, per_page=25)
        assert result.total_pages == 1

    def test_one_over_triggers_second_page(self) -> None:
        result = paginate(total_items=26, page=1, per_page=25)
        assert result.total_pages == 2

    def test_with_options_and_clamping(self) -> None:
        result = paginate(total_items=60, page=99, per_page=35, per_page_options=[25, 50, 100])
        assert result.per_page == 25
        assert result.total_pages == 3
        assert result.page == 3
        assert result.start_index == 50
        assert result.end_index == 75
