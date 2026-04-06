"""Accuracy test suite for Bedrock Haiku extraction.

Generates synthetic PDFs with known expected JSON, sends each through
extract_statement() against real Bedrock Haiku, and diffs results.

Build BEFORE the migration. Run AFTER the extraction module exists.
The import of extract_statement() is deferred to runtime — this script
will fail with an ImportError until the migration creates that function.

Usage:
    python3.13 scripts/accuracy_test/run_accuracy_test.py

Cost: ~$0.10-0.20 per run for 8 PDFs through Haiku.
"""

import io
import json
import logging
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).parent

# Load AWS_REGION and AWS_PROFILE from .env before importing Lambda code.
load_dotenv(SCRIPT_DIR / ".env")
OUTPUT_DIR = SCRIPT_DIR / "output"
LOG_FILE = SCRIPT_DIR / "accuracy_test.log"
FLOAT_TOLERANCE = 0.01


def _configure_logging() -> None:
    """Route all library logs (Lambda powertools, boto3, etc.) to a log file.

    Keeps stdout clean for test progress output only.
    """
    file_handler = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))

    # Capture everything from the root logger.
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

# Add Lambda source to path so we can import extract_statement post-migration.
LAMBDA_DIR = Path(__file__).parent.parent.parent / "lambda_functions" / "textraction_lambda"
sys.path.insert(0, str(LAMBDA_DIR))

from generate_pdfs import generate_all_scenarios  # noqa: E402


def compare_results(
    expected: dict[str, Any],
    actual: dict[str, Any],
    scenario_name: str,
) -> list[str]:
    """Compare extracted output against expected JSON.

    Exact match on: header_mapping, date_format, date_confidence.

    For statement_items: match on count, then per-item comparison
    of date, number, reference, total keys and numeric values
    (within float tolerance). Order-sensitive.
    """
    errors: list[str] = []

    # Exact-match metadata fields.
    for field in ["header_mapping", "date_format", "date_confidence"]:
        if expected.get(field) != actual.get(field):
            errors.append(f"[{scenario_name}] {field}: expected={expected.get(field)}, actual={actual.get(field)}")

    # Item count.
    expected_items = expected.get("statement_items", [])
    actual_items = actual.get("statement_items", [])
    if len(expected_items) != len(actual_items):
        errors.append(f"[{scenario_name}] item_count: expected={len(expected_items)}, actual={len(actual_items)}")
        return errors  # Can't compare per-item if counts differ

    # Per-item comparison.
    for i, (exp_item, act_item) in enumerate(zip(expected_items, actual_items)):
        for key in ["date", "number", "reference"]:
            exp_val = exp_item.get(key, "")
            act_val = act_item.get(key, "")
            if str(exp_val).strip() != str(act_val).strip():
                errors.append(f"[{scenario_name}] item[{i}].{key}: expected='{exp_val}', actual='{act_val}'")

        # Total: compare numeric values within tolerance.
        exp_total = exp_item.get("total", {})
        act_total = act_item.get("total", {})
        all_labels = set(list(exp_total.keys()) + list(act_total.keys()))
        for label in all_labels:
            exp_v = exp_total.get(label)
            act_v = act_total.get(label)
            if isinstance(exp_v, (int, float)) and isinstance(act_v, (int, float)):
                if abs(exp_v - act_v) > FLOAT_TOLERANCE:
                    errors.append(f"[{scenario_name}] item[{i}].total[{label}]: expected={exp_v}, actual={act_v}")
            elif str(exp_v) != str(act_v):
                errors.append(f"[{scenario_name}] item[{i}].total[{label}]: expected={exp_v}, actual={act_v}")

    return errors


def main() -> None:
    """Run all accuracy test scenarios.

    Imports extract_statement at runtime — will fail with ImportError
    until the migration creates the extraction module.
    """
    # Deferred import: this module doesn't exist until the migration.
    from core.extraction import extract_statement  # noqa: E402
    from pypdf import PdfReader  # noqa: E402

    _configure_logging()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Logs → {LOG_FILE}")

    scenarios = generate_all_scenarios()
    all_errors: list[str] = []

    for name, pdf_bytes, expected in scenarios:
        print(f"Testing: {name}...")
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            page_count = len(reader.pages)

            result = extract_statement(pdf_bytes, page_count)
            actual = result.model_dump()
            # Flatten items to dicts for comparison.
            actual["statement_items"] = [item.model_dump() for item in result.items]

            # Write output for inspection.
            output_path = OUTPUT_DIR / f"{name}.json"
            output_path.write_text(json.dumps(actual, indent=2, ensure_ascii=False))

            errors = compare_results(expected, actual, name)
            all_errors.extend(errors)

            if errors:
                print(f"  FAIL: {len(errors)} discrepancies")
                for err in errors:
                    print(f"    {err}")
            else:
                print("  PASS")
        except Exception as exc:
            all_errors.append(f"[{name}] EXCEPTION: {exc}")
            print(f"  ERROR: {exc}")

    print()
    if all_errors:
        print(f"FAILED: {len(all_errors)} total discrepancies across all scenarios")
        sys.exit(1)
    else:
        print(f"ALL {len(scenarios)} SCENARIOS PASSED")


if __name__ == "__main__":
    main()
