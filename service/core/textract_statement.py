# from core.validation.anomaly_detection import apply_outlier_flags
import io
import json
from pathlib import Path

from werkzeug.datastructures import FileStorage

from core.extraction import get_tables
from core.transform import table_to_json
from core.validation.validate_item_count import validate_references_roundtrip
from configuration.resources import s3_client


def run_textraction(bucket, pdf_key, tenant_id, contact_id) -> FileStorage:
    tables_by_key = get_tables(bucket, pdf_key)
    for key, tables_wp in tables_by_key.items():
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

        # optional: ML outlier pass
        # statement, summary = apply_outlier_flags(statement, remove=False, one_based_index=True, threshold_method="iqr")
        # print(json.dumps(summary, indent=2))

        out_dir = Path("./statements/structured_statements")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / (Path(key).stem + ".json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(statement, f, ensure_ascii=False, indent=2)

        # Serialize to bytes in memory
        buf = io.BytesIO(json.dumps(statement, ensure_ascii=False, indent=2).encode("utf-8"))
        buf.seek(0)

        filename = f"{Path(key).stem}.json"
        return FileStorage(stream=buf, filename=filename, content_type="application/json")
