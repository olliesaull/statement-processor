"""Shared statement row color palette for UI and Excel exports.

This module keeps statement row colors in one place so the web table and XLSX export stay in sync.
Completed row colors are generated from base colors rather than hard-coded so we can tweak one alpha value.
"""

from typing import Final

# Type shape (inside-out):
# RowColorSet: {"background": "#C6EFCE", "text": "#0F3B1F"}
# RowPaletteState: {"normal": RowColorSet, "completed": RowColorSet}
# StatementRowPalette: {"match": RowPaletteState, "mismatch": RowPaletteState, "anomaly": RowPaletteState}

# Example:
#    palette: StatementRowPalette = {
#       "match": {
#           "normal": {"background": "#C6EFCE", "text": "#0F3B1F"},
#           "completed": {"background": "#DAF5DF", "text": "#0F3B1F"},
#       }
#   }

type RowColorSet = dict[str, str]
type RowPaletteState = dict[str, RowColorSet]
type StatementRowPalette = dict[str, RowPaletteState]

STATEMENT_ROW_BASE_COLORS: Final[dict[str, RowColorSet]] = {
    "match": {"background": "#C6EFCE", "text": "#0F3B1F"},
    "mismatch": {"background": "#CD5C5C", "text": "#7F1D1D"},
    "anomaly": {"background": "#FFEB9C", "text": "#713F12"},
}
STATEMENT_ROW_COMPLETED_ALPHA: Final[float] = 0.65
STATEMENT_ROW_BLEND_TARGET: Final[str] = "#FFFFFF"


def _normalize_hex_color(hex_color: str) -> str:
    """Normalize a hex color to #RRGGBB.

    Args:
        hex_color: Hex color with or without a leading '#'.

    Returns:
        Upper-case hex color in #RRGGBB format.

    Raises:
        ValueError: Color is not a 6-digit hex value.
    """
    value = hex_color.strip().lstrip("#").upper()
    if len(value) != 6 or any(char not in "0123456789ABCDEF" for char in value):
        raise ValueError(f"Invalid hex color: {hex_color}")
    return f"#{value}"


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert a #RRGGBB color to RGB integers.

    Args:
        hex_color: Hex color value.

    Returns:
        RGB channels as integers.
    """
    normalized = _normalize_hex_color(hex_color).lstrip("#")
    return (int(normalized[0:2], 16), int(normalized[2:4], 16), int(normalized[4:6], 16))


def _rgb_to_hex(red: int, green: int, blue: int) -> str:
    """Convert RGB channels to a #RRGGBB color.

    Args:
        red: Red channel in 0-255.
        green: Green channel in 0-255.
        blue: Blue channel in 0-255.

    Returns:
        Color in #RRGGBB format.
    """
    return f"#{red:02X}{green:02X}{blue:02X}"


def blend_hex_towards_target(hex_color: str, *, target_hex: str, alpha: float) -> str:
    """Blend a base color towards a target color using alpha.

    Args:
        hex_color: Base hex color.
        target_hex: Target hex color to blend towards.
        alpha: Base color retention (1.0 keeps base unchanged, 0.0 becomes target).

    Returns:
        Blended color in #RRGGBB format.

    Raises:
        ValueError: Alpha is outside 0..1 or color inputs are invalid.
    """
    if not 0 <= alpha <= 1:
        raise ValueError(f"Alpha must be between 0 and 1, got {alpha}")
    base_rgb = _hex_to_rgb(hex_color)
    target_rgb = _hex_to_rgb(target_hex)
    # Use explicit half-up rounding so palette tweaks behave predictably.
    blended = tuple(int(((base_channel * alpha) + (target_channel * (1 - alpha))) + 0.5) for base_channel, target_channel in zip(base_rgb, target_rgb, strict=True))
    return _rgb_to_hex(*blended)


def build_statement_row_palette(
    *, base_colors: dict[str, RowColorSet] | None = None, completed_alpha: float = STATEMENT_ROW_COMPLETED_ALPHA, blend_target: str = STATEMENT_ROW_BLEND_TARGET
) -> StatementRowPalette:
    """Build the statement row palette with normal and completed variants.

    Args:
        base_colors: Base colors for match, mismatch, and anomaly states.
        completed_alpha: Blend ratio to retain from the base color for completed rows.
        blend_target: Target color used to lighten completed row backgrounds.

    Returns:
        Palette containing normal/completed colors for each row state.
    """
    source = base_colors or STATEMENT_ROW_BASE_COLORS
    palette: StatementRowPalette = {}
    for state, colors in source.items():
        base_background = _normalize_hex_color(colors["background"])
        base_text = _normalize_hex_color(colors["text"])
        completed_background = blend_hex_towards_target(base_background, target_hex=blend_target, alpha=completed_alpha)

        # Keep text colors unchanged for completed rows so status text remains readable.
        palette[state] = {"normal": {"background": base_background, "text": base_text}, "completed": {"background": completed_background, "text": base_text}}
    return palette


def statement_row_palette_css_variables(palette: StatementRowPalette) -> dict[str, str]:
    """Convert a row palette into CSS custom properties.

    Args:
        palette: Statement row palette with normal/completed variants.

    Returns:
        CSS variable map consumed by the statement table stylesheet.
    """
    css_variables: dict[str, str] = {}
    for state, variants in palette.items():
        css_variables[f"--statement-row-{state}-bg"] = variants["normal"]["background"]
        css_variables[f"--statement-row-{state}-text"] = variants["normal"]["text"]
        css_variables[f"--statement-row-{state}-completed-bg"] = variants["completed"]["background"]
        css_variables[f"--statement-row-{state}-completed-text"] = variants["completed"]["text"]
    return css_variables


STATEMENT_ROW_PALETTE: Final[StatementRowPalette] = build_statement_row_palette()
STATEMENT_ROW_CSS_VARIABLES: Final[dict[str, str]] = statement_row_palette_css_variables(STATEMENT_ROW_PALETTE)
