"""Generate page-1 thumbnails from PDFs for visual triage.

Renders the first page of one PDF per supplier as a PNG thumbnail with the
supplier name and page count in the filename (e.g. 001_PeninsulaBevrages_14pages.png).

The script expects a nested folder structure from the Google Drive download:
  pdfs/{root folder}/{Supplier Name}/{Month Subfolder}/*.pdf

It groups PDFs by top-level supplier folder and picks one representative PDF
per supplier (the first found alphabetically).

A mapping file (thumbnails/mapping.txt) is written so you can look up the
original file path from the thumbnail name.

Requirements: pypdf, pdf2image (which needs poppler-utils installed).
  sudo apt install poppler-utils
  pip install pypdf pdf2image
"""

import sys
from collections import defaultdict
from pathlib import Path

from pdf2image import convert_from_path
from pypdf import PdfReader

SCRIPT_DIR = Path(__file__).parent
INPUT_DIR = SCRIPT_DIR / "pdfs"
OUTPUT_DIR = SCRIPT_DIR / "thumbnails"

# Thumbnail width in pixels — small enough to keep file sizes low,
# large enough for Claude to see layout/handwriting/quality.
THUMBNAIL_WIDTH = 800

# Max PDFs to sample per supplier (uses first N alphabetically).
MAX_PER_SUPPLIER = 3


def _find_supplier_root(input_dir: Path) -> Path:
    """Find the root folder containing supplier subdirectories.

    The Google Drive zip extracts with a wrapper folder like
    'Statements from Suppliers - to be processed'. This function
    detects that and returns the actual supplier root.
    """
    children = [d for d in input_dir.iterdir() if d.is_dir()]

    # If there's exactly one subfolder and it contains further subdirs,
    # it's probably the wrapper folder from the zip.
    if len(children) == 1:
        grandchildren = [d for d in children[0].iterdir() if d.is_dir()]
        if grandchildren:
            return children[0]

    return input_dir


def _sanitise_name(name: str) -> str:
    """Replace characters that are problematic in filenames."""
    return name.replace("/", "_").replace(" ", "_").replace("&", "and").strip("_")


def generate_thumbnails() -> None:
    """Render page 1 of one PDF per supplier as a named thumbnail."""
    if not INPUT_DIR.exists():
        print(f"ERROR: Input directory does not exist: {INPUT_DIR}")
        print("Create it and add your PDFs, then re-run.")
        sys.exit(1)

    supplier_root = _find_supplier_root(INPUT_DIR)
    print(f"Supplier root: {supplier_root}\n")

    # Group PDFs by top-level supplier folder
    supplier_pdfs: dict[str, list[Path]] = defaultdict(list)
    for pdf_path in supplier_root.rglob("*.pdf"):
        # The supplier name is the first directory component relative to supplier_root
        relative = pdf_path.relative_to(supplier_root)
        supplier_name = relative.parts[0]
        supplier_pdfs[supplier_name].append(pdf_path)

    if not supplier_pdfs:
        print(f"No PDFs found under {supplier_root}")
        sys.exit(1)

    # Sort suppliers alphabetically, pick up to MAX_PER_SUPPLIER PDFs from each
    suppliers = sorted(supplier_pdfs.keys())
    # (supplier_name, pdf_path, letter_suffix) — suffix distinguishes multiple from same supplier
    selected: list[tuple[str, Path, str]] = []
    for supplier in suppliers:
        pdfs = sorted(supplier_pdfs[supplier])
        for i, pdf in enumerate(pdfs[:MAX_PER_SUPPLIER]):
            suffix = chr(ord("a") + i)  # a, b, c
            selected.append((supplier, pdf, suffix))

    OUTPUT_DIR.mkdir(exist_ok=True)
    total_suppliers = len(suppliers)
    print(f"Found {total_suppliers} suppliers, {len(selected)} thumbnails (up to {MAX_PER_SUPPLIER} per supplier)\n")

    # Mapping file so the user can find original paths from thumbnail names
    mapping_lines: list[str] = []

    for idx, (supplier, pdf_path, suffix) in enumerate(selected, start=1):
        try:
            # Get page count from pypdf (fast, no rendering)
            reader = PdfReader(pdf_path)
            page_count = len(reader.pages)

            # Render first page only
            images = convert_from_path(
                pdf_path,
                first_page=1,
                last_page=1,
                size=(THUMBNAIL_WIDTH, None),
            )

            # Filename: 001a_SupplierName_14pages.png
            safe_name = _sanitise_name(supplier)
            out_name = f"{idx:03d}{suffix}_{safe_name}_{page_count}pages.png"
            out_path = OUTPUT_DIR / out_name

            images[0].save(out_path, "PNG")
            mapping_lines.append(f"{out_name} -> {pdf_path.relative_to(SCRIPT_DIR)}")
            print(f"  [{idx:3d}/{len(selected)}] {out_name}")

        except Exception as e:
            print(f"  [{idx:3d}/{len(selected)}] FAILED {supplier}: {e}")

    # Write mapping file
    mapping_path = OUTPUT_DIR / "mapping.txt"
    mapping_path.write_text("\n".join(mapping_lines) + "\n")

    print(f"\nThumbnails written to {OUTPUT_DIR}")
    print(f"Mapping file: {mapping_path}")


if __name__ == "__main__":
    generate_thumbnails()
