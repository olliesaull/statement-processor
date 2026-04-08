#!/usr/bin/env python3.13
"""One-off migration: backfill ProcessingStage on existing statement headers.

Sets ProcessingStage based on TokenReservationStatus:
- consumed → "complete"
- released → "failed"
- reserved → skipped (in-progress, Lambda will set it)

Idempotent: uses ConditionExpression to skip rows that already have
ProcessingStage. Safe to re-run.

Usage:
    AWS_PROFILE=<profile> python3.13 scripts/backfill_processing_stage.py

Environment:
    AWS_PROFILE: AWS credentials profile (required)
    AWS_REGION: Region (default: eu-west-1)
    TENANT_STATEMENTS_TABLE_NAME: DynamoDB table name (default: TenantStatementsTable-prod)
    DRY_RUN: Set to "false" to apply changes (default: "true")
"""

import os

import boto3
from botocore.exceptions import ClientError

AWS_REGION = os.getenv("AWS_REGION", "eu-west-1")
AWS_PROFILE = os.getenv("AWS_PROFILE", "dotelastic-production")
TABLE_NAME = os.getenv("TENANT_STATEMENTS_TABLE_NAME", "TenantStatementsTable")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"

STATUS_MAP = {
    "consumed": "complete",
    "released": "failed",
}


def main() -> None:
    """Scan statement headers and backfill ProcessingStage."""
    session = boto3.session.Session(region_name=AWS_REGION, profile_name=AWS_PROFILE)
    table = session.resource("dynamodb").Table(TABLE_NAME)

    print(f"Table: {TABLE_NAME}")
    print(f"Region: {AWS_REGION}")
    print(f"Profile: {AWS_PROFILE}")
    print(f"Dry run: {DRY_RUN}")
    print()

    # Scan for statement header rows (RecordType = "statement").
    scan_kwargs = {
        "FilterExpression": "RecordType = :rt",
        "ExpressionAttributeValues": {":rt": "statement"},
        "ProjectionExpression": "TenantID, StatementID, TokenReservationStatus, ProcessingStage",
    }

    items = []
    while True:
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        scan_kwargs["ExclusiveStartKey"] = lek

    print(f"Found {len(items)} statement header(s)")

    updated = 0
    skipped_has_stage = 0
    skipped_reserved = 0

    for item in items:
        tenant_id = item["TenantID"]
        statement_id = item["StatementID"]
        existing_stage = item.get("ProcessingStage")
        reservation_status = str(item.get("TokenReservationStatus", "")).strip().lower()

        if existing_stage:
            print(f"  SKIP {statement_id} — already has ProcessingStage={existing_stage}")
            skipped_has_stage += 1
            continue

        new_stage = STATUS_MAP.get(reservation_status)
        if not new_stage:
            print(f"  SKIP {statement_id} — TokenReservationStatus={reservation_status} (in-progress)")
            skipped_reserved += 1
            continue

        print(f"  {'WOULD SET' if DRY_RUN else 'SET'} {statement_id} → ProcessingStage={new_stage}")

        if not DRY_RUN:
            try:
                table.update_item(
                    Key={"TenantID": tenant_id, "StatementID": statement_id},
                    UpdateExpression="SET ProcessingStage = :stage",
                    ConditionExpression="attribute_not_exists(ProcessingStage)",
                    ExpressionAttributeValues={":stage": new_stage},
                )
                updated += 1
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    print(f"    Condition failed (concurrent update?) — skipped")
                    skipped_has_stage += 1
                else:
                    raise

    print()
    print(f"Updated: {updated}")
    print(f"Skipped (already has stage): {skipped_has_stage}")
    print(f"Skipped (reserved/in-progress): {skipped_reserved}")

    if DRY_RUN and updated == 0 and (len(items) - skipped_has_stage - skipped_reserved) > 0:
        print()
        print("This was a dry run. Set DRY_RUN=false to apply changes.")


if __name__ == "__main__":
    main()
