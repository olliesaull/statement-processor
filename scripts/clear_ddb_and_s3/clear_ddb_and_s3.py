#!/usr/bin/env python3
"""Utility to purge DynamoDB tables and an S3 bucket defined in a .env file."""

from __future__ import annotations

import argparse
import os
import sys
from itertools import islice
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv


def chunked(iterable: Iterable[Dict[str, str]], size: int) -> Iterable[List[Dict[str, str]]]:
    """Yield fixed-size chunks from *iterable*."""
    iterator = iter(iterable)
    while True:
        block = list(islice(iterator, size))
        if not block:
            return
        yield block


def parse_args() -> argparse.Namespace:
    root_dir = Path(__file__).resolve().parents[2]
    default_env = root_dir / "service" / ".env"

    parser = argparse.ArgumentParser(description="Delete all objects from an S3 bucket and all items from DynamoDB tables.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=default_env,
        help="Path to the .env file containing AWS_PROFILE, AWS_REGION, S3_BUCKET_NAME, "
        "TENANT_CONTACTS_CONFIG_TABLE_NAME, TENANT_STATEMENTS_TABLE_NAME, and "
        "TENANT_DATA_TABLE_NAME. "
        f"Defaults to {default_env}",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )
    return parser.parse_args()


def confirm(prompt: str, skip: bool) -> None:
    if skip:
        return
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    if answer not in {"y", "yes"}:
        print("Aborted.")
        sys.exit(0)


def build_session() -> boto3.session.Session:
    session_kwargs = {}
    profile = os.getenv("AWS_PROFILE")
    region = os.getenv("AWS_REGION")
    if profile:
        session_kwargs["profile_name"] = profile
    if region:
        session_kwargs["region_name"] = region
    return boto3.session.Session(**session_kwargs)


def clear_bucket(s3_client, bucket_name: str) -> None:
    print(f"Clearing S3 bucket: {bucket_name}")
    total_deleted = 0
    error_count = 0

    try:
        paginator = s3_client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket_name):
            objects: List[Dict[str, str]] = []
            for version in page.get("Versions", []):
                objects.append({"Key": version["Key"], "VersionId": version["VersionId"]})
            for marker in page.get("DeleteMarkers", []):
                objects.append({"Key": marker["Key"], "VersionId": marker["VersionId"]})
            for batch in chunked(objects, 1000):
                response = s3_client.delete_objects(
                    Bucket=bucket_name,
                    Delete={"Objects": batch, "Quiet": True},
                )
                error_count += len(response.get("Errors", []))
                total_deleted += len(response.get("Deleted", []))
    except ClientError as error:
        if error.response["Error"]["Code"] not in {"NoSuchBucket", "404"}:
            raise
        print(f"Bucket {bucket_name} does not exist.")
        return

    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket_name):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        for batch in chunked(keys, 1000):
            response = s3_client.delete_objects(
                Bucket=bucket_name,
                Delete={"Objects": batch, "Quiet": True},
            )
            error_count += len(response.get("Errors", []))
            total_deleted += len(response.get("Deleted", []))

    if total_deleted == 0:
        print("Bucket is already empty.")
    else:
        message = f"Deleted {total_deleted} objects (including versions) from bucket."
        if error_count:
            message += f" Encountered {error_count} delete errors; see AWS logs for details."
        print(message)


def projection_expression_from_key_schema(key_names: Sequence[str]) -> Dict[str, object]:
    expression_attribute_names = {f"#k{idx}": name for idx, name in enumerate(key_names)}
    projection_expression = ", ".join(expression_attribute_names.keys())
    return {
        "ProjectionExpression": projection_expression,
        "ExpressionAttributeNames": expression_attribute_names,
    }


def clear_table(ddb_resource, table_name: str) -> None:
    table = ddb_resource.Table(table_name)
    print(f"Clearing DynamoDB table: {table_name}")
    deleted = 0

    try:
        table.load()
    except ClientError as error:
        if error.response["Error"]["Code"] == "ResourceNotFoundException":
            print(f"Table {table_name} does not exist.")
            return
        raise

    key_names = [key["AttributeName"] for key in table.key_schema]
    scan_kwargs: Dict[str, object] = {}
    if key_names:
        scan_kwargs.update(projection_expression_from_key_schema(key_names))

    with table.batch_writer() as batch:
        while True:
            response = table.scan(**scan_kwargs)
            items = response.get("Items", [])
            if not items:
                break
            for item in items:
                key = {name: item[name] for name in key_names}
                batch.delete_item(Key=key)
                deleted += 1
            if "LastEvaluatedKey" not in response:
                break
            scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    if deleted == 0:
        print("Table is already empty.")
    else:
        print(f"Deleted {deleted} items from table.")


def main() -> None:
    args = parse_args()
    if not args.env_file.exists():
        print(f"Env file not found: {args.env_file}", file=sys.stderr)
        sys.exit(1)

    load_dotenv(args.env_file)

    bucket_name = os.getenv("S3_BUCKET_NAME")
    table_env_values = [
        ("TENANT_CONTACTS_CONFIG_TABLE_NAME", os.getenv("TENANT_CONTACTS_CONFIG_TABLE_NAME")),
        ("TENANT_STATEMENTS_TABLE_NAME", os.getenv("TENANT_STATEMENTS_TABLE_NAME")),
        ("TENANT_DATA_TABLE_NAME", os.getenv("TENANT_DATA_TABLE_NAME")),
    ]
    missing = [name for name, value in [("S3_BUCKET_NAME", bucket_name), *table_env_values] if not value]
    if missing:
        print(f"Missing required environment variables in {args.env_file}: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    table_names = [value for _, value in table_env_values if value is not None]

    session = build_session()
    resources_summary = f"S3 bucket: {bucket_name}\nDynamoDB tables: {', '.join(table_names)}"
    print(resources_summary)
    confirm("Proceed with deleting all data from these resources?", args.yes)

    s3_client = session.client("s3")
    ddb_resource = session.resource("dynamodb")

    clear_bucket(s3_client, bucket_name)
    for table_name in table_names:
        clear_table(ddb_resource, table_name)


if __name__ == "__main__":
    try:
        main()
    except ClientError as error:
        print(f"AWS client error: {error}", file=sys.stderr)
        sys.exit(1)
    except Exception as error:  # pragma: no cover - catch-all for CLI
        print(f"Unexpected error: {error}", file=sys.stderr)
        sys.exit(1)
