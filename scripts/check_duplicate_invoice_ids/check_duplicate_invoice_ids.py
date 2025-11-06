#!/usr/bin/env python3
"""
Tiny helper to detect duplicate invoice_id values in a JSON file.

Usage:
  python scripts/check_duplicate_invoice_ids/check_duplicate_invoice_ids.py [path_to_invoices.json]

If no path is provided, it defaults to the repo's sample path under service/:
  service/tmp/data/4757d19e-372a-4c56-86f3-7c7aa86baba1/invoices.json
"""

import argparse
import json
import os
import sys
from collections import Counter


# Resolve default path relative to repository root (parent of the top-level scripts directory)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_PATH = os.path.join(
    REPO_ROOT,
    "service",
    "tmp",
    "data",
    "4757d19e-372a-4c56-86f3-7c7aa86baba1",
    "invoices.json",
)


def load_items(json_path):
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return list(data.values())
    if isinstance(data, list):
        return data
    raise ValueError(f"Unsupported JSON structure: {type(data).__name__}")


def main():
    parser = argparse.ArgumentParser(description="Check for duplicate invoice_id values.")
    parser.add_argument("path", nargs="?", default=DEFAULT_PATH, help="Path to invoices.json (default: %(default)s)")
    args = parser.parse_args()

    json_path = args.path
    if not os.path.isfile(json_path):
        print(f"Error: file not found: {json_path}", file=sys.stderr)
        sys.exit(2)

    try:
        items = load_items(json_path)
    except Exception as exc:
        print(f"Error: failed to read/parse JSON: {exc}", file=sys.stderr)
        sys.exit(2)

    counter = Counter()
    missing_invoice_id = 0
    total_items = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        total_items += 1
        invoice_id = item.get("invoice_id")
        if invoice_id is None:
            missing_invoice_id += 1
            continue
        counter[invoice_id] += 1

    duplicates = [(iid, c) for iid, c in counter.items() if c > 1]
    duplicates.sort(key=lambda x: (-x[1], x[0]))

    print(f"File: {json_path}")
    print(f"Total invoices processed: {total_items}")
    if missing_invoice_id:
        print(f"Missing invoice_id: {missing_invoice_id}")
    print(f"Unique invoice_id: {len(counter)}")

    if not duplicates:
        print("No duplicate invoice_id found.")
        return

    print("Duplicate invoice_id values:")
    for iid, count in duplicates:
        print(f"- {iid}: {count} occurrences")


if __name__ == "__main__":
    main()
