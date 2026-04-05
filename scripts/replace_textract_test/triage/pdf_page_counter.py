"""Count pages in all PDFs in a directory and report the one with the most pages."""

import sys
from pathlib import Path

from pypdf import PdfReader


def count_pages(pdf_dir: Path) -> list[tuple[str, int]]:
    """Return a list of (relative_path, page_count) for every PDF in pdf_dir."""
    results: list[tuple[str, int]] = []
    for pdf_path in sorted(pdf_dir.rglob("*.pdf")):
        try:
            reader = PdfReader(pdf_path)
            rel = str(pdf_path.relative_to(pdf_dir))
            results.append((rel, len(reader.pages)))
        except Exception as e:
            print(f"  ERROR reading {pdf_path.name}: {e}", file=sys.stderr)
    return results


def main() -> None:
    default_dir = (
        Path(__file__).parent
        / "pdfs"
        / "Statements from Suppliers - to be processed"
    )
    pdf_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else default_dir

    if not pdf_dir.is_dir():
        print(f"Directory not found: {pdf_dir}", file=sys.stderr)
        sys.exit(1)

    results = count_pages(pdf_dir)
    if not results:
        print("No PDFs found.")
        sys.exit(0)

    # Sort by page count descending
    results.sort(key=lambda x: x[1], reverse=True)

    # Show top 20 by page count
    print(f"{'File':<80} {'Pages':>5}")
    print("-" * 86)
    for name, pages in results[:20]:
        print(f"{name:<80} {pages:>5}")

    winner_name, winner_pages = results[0]
    print("-" * 86)
    print(f"\nTotal PDFs scanned: {len(results)}")
    print(f"Most pages: {winner_name} ({winner_pages} pages)")


if __name__ == "__main__":
    main()
