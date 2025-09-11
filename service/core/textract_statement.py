# from core.validation.validate_item_count import validate_references_roundtrip
# from core.validation.anomaly_detection import apply_outlier_flags
import io
import json
from pathlib import Path

from werkzeug.datastructures import FileStorage

from core.extraction import get_tables
from core.transform import table_to_json


def run_textraction(bucket, keys, tenant_id, contact_id) -> FileStorage:
    tables_by_key = get_tables(bucket, keys)
    for key, tables_wp in tables_by_key.items():
        print(f"\n=== {key} ===")
        statement = table_to_json(key, tables_wp, tenant_id, contact_id)

        # statement_items = [item for item in statement["statement_items"]]
        # validate_references_roundtrip(key, statement_items)

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
