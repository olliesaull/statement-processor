"""Server-side pagination utilities.

Provides a single ``paginate()`` function used by both the statements list
and statement detail routes to slice a pre-sorted dataset into pages.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class PaginationResult:
    """Immutable container for computed pagination state.

    Attributes:
        page: Current page number (1-based, clamped to valid range).
        per_page: Items per page (snapped to nearest valid option when options provided).
        total_pages: Total number of pages (minimum 1, even for empty datasets).
        total_items: Total item count before slicing.
        start_index: Slice start index for the current page.
        end_index: Slice end index for the current page.
    """

    page: int
    per_page: int
    total_pages: int
    total_items: int
    start_index: int
    end_index: int


def _snap_to_nearest(value: int, options: list[int]) -> int:
    """Snap *value* to the nearest option in a sorted list.

    Ties (equal distance to two options) round down to the lower option.
    Values below the minimum snap to the minimum; above the maximum snap
    to the maximum.
    """
    return min(options, key=lambda opt: (abs(opt - value), opt))


def paginate(total_items: int, page: int, per_page: int, per_page_options: list[int] | None = None) -> PaginationResult:
    """Compute pagination slice parameters.

    Args:
        total_items: Total number of items in the full dataset.
        page: Requested page number (1-based). Clamped to ``[1, total_pages]``.
        per_page: Requested items per page. Snapped to nearest valid option
            when *per_page_options* is provided.
        per_page_options: Allowed per-page values (e.g. ``[25, 50, 100]``).
            When ``None``, *per_page* is used as-is.

    Returns:
        A :class:`PaginationResult` with all computed fields.
    """
    if per_page_options:
        per_page = _snap_to_nearest(per_page, sorted(per_page_options))

    per_page = max(1, per_page)
    total_pages = max(1, math.ceil(total_items / per_page))
    page = max(1, min(page, total_pages))
    start_index = (page - 1) * per_page
    end_index = start_index + per_page

    return PaginationResult(page=page, per_page=per_page, total_pages=total_pages, total_items=total_items, start_index=start_index, end_index=end_index)
