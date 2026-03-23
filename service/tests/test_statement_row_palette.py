"""Unit tests for shared statement row palette helpers."""

import pytest

from core.statement_row_palette import STATEMENT_ROW_BASE_COLORS, STATEMENT_ROW_PALETTE, blend_hex_towards_target, statement_row_palette_css_variables


def test_completed_backgrounds_are_derived_from_base_colors() -> None:
    """Verify completed backgrounds are computed from the base palette.

    Args:
        None.

    Returns:
        None.
    """
    expected_completed_backgrounds = {"match": "#D3FAE0", "mismatch": "#FEDDDD", "anomaly": "#FEEFB3"}

    for state, expected_background in expected_completed_backgrounds.items():
        assert STATEMENT_ROW_PALETTE[state]["normal"]["background"] == STATEMENT_ROW_BASE_COLORS[state]["background"]
        assert STATEMENT_ROW_PALETTE[state]["completed"]["background"] == expected_background
        assert STATEMENT_ROW_PALETTE[state]["completed"]["text"] == STATEMENT_ROW_PALETTE[state]["normal"]["text"]


def test_css_variable_map_contains_all_row_state_keys() -> None:
    """Verify CSS variable generation covers all states and variants.

    Args:
        None.

    Returns:
        None.
    """
    css_variables = statement_row_palette_css_variables(STATEMENT_ROW_PALETTE)
    assert css_variables["--statement-row-match-bg"] == "#BBF7D0"
    assert css_variables["--statement-row-match-completed-bg"] == "#D3FAE0"
    assert css_variables["--statement-row-mismatch-bg"] == "#FECACA"
    assert css_variables["--statement-row-mismatch-completed-bg"] == "#FEDDDD"
    assert css_variables["--statement-row-anomaly-bg"] == "#FDE68A"
    assert css_variables["--statement-row-anomaly-completed-bg"] == "#FEEFB3"


def test_blend_hex_towards_target_rejects_invalid_alpha() -> None:
    """Verify alpha validation protects the blend helper.

    Args:
        None.

    Returns:
        None.
    """
    with pytest.raises(ValueError):
        blend_hex_towards_target("#FFFFFF", target_hex="#000000", alpha=1.1)
