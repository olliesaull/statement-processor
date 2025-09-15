import io
import json
from pathlib import Path
from typing import Dict, List

from werkzeug.datastructures import FileStorage

from core.extraction import TableOnPage, get_tables
from core.transform import table_to_json
from core.validation.validate_item_count import validate_references_roundtrip
from configuration.resources import s3_client


def run_textraction(bucket: str, pdf_key: str, tenant_id: str, contact_id: str) -> FileStorage:
    """Run Textract, transform to canonical JSON, validate, and return as FileStorage."""
    tables_by_key: Dict[str, List[TableOnPage]] = get_tables(bucket, pdf_key)

    # get_tables returns a mapping with the input key; handle robustly
    if tables_by_key:
        key = next(iter(tables_by_key.keys()))
        tables_wp = tables_by_key[key]
    else:
        key = pdf_key
        tables_wp = []

    print(f"\n=== {key} ===")
    statement = table_to_json(key, tables_wp, tenant_id, contact_id)

    # Fetch PDF bytes from S3 and validate against extracted JSON
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        pdf_bytes = obj["Body"].read()
        statement_items = statement.get("statement_items", []) or []
        validate_references_roundtrip(pdf_bytes, statement_items)
    except Exception as e:
        print(f"[WARNING] Reference validation skipped: {e}")

    # optional: ML outlier pass (kept commented; requires sklearn and data volume)
    # from core.validation.anomaly_detection import apply_outlier_flags
    # statement, summary = apply_outlier_flags(statement, remove=False, one_based_index=True, threshold_method="iqr")
    # print(json.dumps(summary, indent=2))

    # TODO: This is just here for testing, remove for production:

    out_dir = Path("./statements/structured_statements")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (Path(key).stem + ".json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(statement, f, ensure_ascii=False, indent=2)

    # Serialize to bytes in memory for upload/response
    buf = io.BytesIO(json.dumps(statement, ensure_ascii=False, indent=2).encode("utf-8"))
    buf.seek(0)

    filename = f"{Path(key).stem}.json"
    return FileStorage(stream=buf, filename=filename, content_type="application/json")
