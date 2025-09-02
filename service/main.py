import json
from pathlib import Path

from core.normalisation import table_to_json

from configuration.config import S3_BUCKET_NAME
from core.extraction import get_tables
from core.validation.validate_item_count import validate_references_roundtrip
# from core.validation.anomaly_detection import apply_outlier_flags


def run(include_keys=None):
    tables_by_key = get_tables(bucket=S3_BUCKET_NAME, prefix="statements/", include_keys=include_keys)
    for key, tables_wp in tables_by_key.items():
        print(f"\n=== {key} ===")
        statement = table_to_json(key, tables_wp, config_dir="./statement_configs")

        statement_items = [item for item in statement["statement_items"]]
        validate_references_roundtrip(key, statement_items)

        # optional: ML outlier pass
        # statement, summary = apply_outlier_flags(statement, remove=False, one_based_index=True, threshold_method="iqr")
        # print(json.dumps(summary, indent=2))

        out_dir = Path("./structured_statements")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / (Path(key).stem + ".json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(statement, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    run(include_keys=["Bill Riley Z91.PDF", "ARSTMT11 (54).pdf"])
    # run(include_keys=["Bill Riley Z91.PDF"])
    # run(include_keys=["ARSTMT11 (54).pdf"])
