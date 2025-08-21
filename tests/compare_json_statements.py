"""Module for comparing the json created by extracting text from pdfs between pdfplumber and Amazon Textract"""

from pathlib import Path

from configuration.config import S3_BUCKET_NAME
from core.create_json_statements import create_structured_json
from core.extract_text_from_pdf import (
    count_pdf_pages,
    extract_text_from_pdf_bytes,
    extract_text_from_textract_s3,
)
from utils.aws import get_s3_object_bytes, get_statements_from_s3
from utils.json_statement_helpers import build_statement_prompt, write_json


def remove_unprintable(s: str) -> str:
    return "".join(ch for ch in s if ch.isprintable() or ch in ("\n", "\t"))

def store_text(text: str, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)  # ensure directory exists
    with path.open("w", encoding="utf-8") as f:
        f.write(text)

def run_all(bucket: str, prefix: str = "statements/") -> None:
    keys = get_statements_from_s3(bucket, prefix)
    for key in sorted(keys):
        print(f"\n=== s3://{bucket}/{key} ===")
        if "C131-M250731A3100.pdf" not in key:
            continue

        pdf_bytes = get_s3_object_bytes(bucket, key)
        page_count = count_pdf_pages(pdf_bytes)

        # Textract
        try:
            text_tex = extract_text_from_textract_s3(bucket, key, page_count)
            text_tex = remove_unprintable(text_tex)
            text_out_path = Path("./extracted_statements/textract/txt") / f"{key}.txt"
            store_text(text_tex, text_out_path)

            tex_prompt = build_statement_prompt(text_tex)
            print("*"*88)
            print(tex_prompt)
            print("*"*88)
            tex_json = create_structured_json(tex_prompt)
            print(tex_json)
            print("*"*88)
            out_path = Path("./extracted_statements/textract/json") / f"{key}.json"
            write_json(out_path, tex_json)
        except Exception as e:
            print(f"textract error: {e}")

        # pdfplumber
        try:
            text_pdf = extract_text_from_pdf_bytes(pdf_bytes)
            text_out_path = Path("./extracted_statements/pdfplumber/txt") / f"{key}.txt"
            store_text(text_pdf, text_out_path)

            pdf_prompt = build_statement_prompt(text_pdf)
            print("*"*88)
            print(pdf_prompt)
            print("*"*88)
            pdf_json = create_structured_json(pdf_prompt)
            print(pdf_json)
            print("*"*88)
            out_path = Path("./extracted_statements/pdfplumber/json") / f"{key}.json"
            write_json(out_path, pdf_json)
        except Exception as e:
            print(f"pdfplumber error: {e}")

if __name__ == "__main__":
    run_all(S3_BUCKET_NAME, "statements/")