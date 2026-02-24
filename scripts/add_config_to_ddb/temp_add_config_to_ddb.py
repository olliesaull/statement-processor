#!/usr/bin/env python3
# save as put_config.py

import argparse
import sys

import boto3
from botocore.exceptions import ClientError

CONFIG = {
    "statement_meta": {"supplier_name": "", "statement_date": {"value": "", "format": "DD/MM/YY"}, "currency": "", "source_filename": ""},
    "statement_items": [
        {
            "transaction_date": {"value": "date", "format": "DD/MM/YY"},
            "customer_account_number": "",
            "branch_store_shop": "",
            "document_type": "description",
            "description_details": "",
            "debit": "",
            "credit": "",
            "invoice_balance": "invoice amount",
            "balance": "balance zar",
            "customer_reference": "reference",
            "supplier_reference": "activity",
            "allocated_to": "",
            "raw": {"date": "date", "activity": "activity", "reference": "reference", "due date": "due date", "invoice amount": "invoice amount", "payments": "payments", "balance zar": "balance zar"},
        }
    ],
}


def main():
    parser = argparse.ArgumentParser(description="Put config map into DynamoDB item.")
    parser.add_argument("--profile", required=True, help="AWS profile (e.g., default, dev).")
    parser.add_argument("--region", required=True, help="AWS region (e.g., eu-west-1).")
    parser.add_argument("--table", required=True, help="DynamoDB table name.")
    parser.add_argument("--tenant-id", required=True, help="TenantID (partition key).")
    parser.add_argument("--contact-id", required=True, help="ContactID (sort key).")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite if item exists (default: write only if absent).")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    table = session.resource("dynamodb").Table(args.table)

    item = {
        "TenantID": args.tenant_id,
        "ContactID": args.contact_id,
        "config": CONFIG,
    }

    kwargs = {"Item": item}
    if not args.overwrite:
        kwargs["ConditionExpression"] = "attribute_not_exists(TenantID) AND attribute_not_exists(ContactID)"

    try:
        table.put_item(**kwargs)
        print(f"✅ Put config into {args.table} (TenantID={args.tenant_id}, ContactID={args.contact_id}).")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            print("❌ Item already exists. Use --overwrite to replace it.", file=sys.stderr)
            sys.exit(1)
        print(f"❌ DynamoDB error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

# python3 temp_add_config_to_ddb.py \
#  --profile dotelastic-production \
#  --region eu-west-1 \
#  --table TenantContactsConfigTable \
#  --tenant-id 234a8cb8-33d4-45d9-a1cc-d6075fb65533 \
#  --contact-id cc1e8ee2-30e2-400d-ac10-404b3c01784c \
#  --overwrite
