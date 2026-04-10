"""Shared type aliases used across both service and extraction lambda."""

# Total values can arrive as numbers or numeric-looking strings.
# Both codebases normalize into this union.
Number = int | float | str
