#!/usr/bin/env python3
"""Clear configured DynamoDB tables and S3 data for all tenants or one tenant."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable, Sequence
from itertools import islice
from pathlib import Path
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from dotenv import load_dotenv


def chunked(iterable: Iterable[dict[str, str]], size: int) -> Iterable[list[dict[str, str]]]:
    """Yield fixed-size chunks from *iterable*.

    Args:
        iterable: Source iterable that should be consumed in batches.
        size: Maximum number of items to include in each chunk.

    Returns:
        Iterable[list[dict[str, str]]]: Successive fixed-size lists.
    """
    iterator = iter(iterable)
    while True:
        block = list(islice(iterator, size))
        if not block:
            return
        yield block


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the clear operation.

    Returns:
        argparse.Namespace: Parsed CLI arguments.
    """
    root_dir = Path(__file__).resolve().parents[2]
    default_env = root_dir / "service" / ".env"

    parser = argparse.ArgumentParser(description="Delete all configured tenant data, or narrow the delete to one TenantID.")
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
    parser.add_argument(
        "--tenant-id",
        help="Only delete data for this TenantID. When omitted, all tenant data is deleted.",
    )
    return parser.parse_args()


def confirm(prompt: str, skip: bool) -> None:
    """Ask the operator to confirm a destructive action.

    Args:
        prompt: Confirmation text shown before reading input.
        skip: When true, bypass the prompt.

    Returns:
        None.
    """
    if skip:
        return
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    if answer not in {"y", "yes"}:
        print("Aborted.")
        sys.exit(0)


def build_session() -> boto3.session.Session:
    """Create a boto3 session from environment overrides.

    Returns:
        boto3.session.Session: Session configured with optional profile/region.
    """
    profile = os.getenv("AWS_PROFILE")
    region = os.getenv("AWS_REGION")
    if profile and region:
        return boto3.session.Session(profile_name=profile, region_name=region)
    if profile:
        return boto3.session.Session(profile_name=profile)
    if region:
        return boto3.session.Session(region_name=region)
    return boto3.session.Session()


def normalize_tenant_id(raw_tenant_id: str | None) -> str | None:
    """Validate and normalize the optional tenant identifier.

    Args:
        raw_tenant_id: Tenant ID provided on the CLI, if any.

    Returns:
        str | None: Normalized tenant ID or ``None`` for all-tenant mode.

    Raises:
        ValueError: If the tenant ID is blank or contains path separators.
    """
    if raw_tenant_id is None:
        return None

    tenant_id = raw_tenant_id.strip()
    if not tenant_id:
        raise ValueError("--tenant-id cannot be empty.")
    if "/" in tenant_id or "\\" in tenant_id:
        raise ValueError("--tenant-id must not contain path separators.")
    return tenant_id


def tenant_prefix(tenant_id: str) -> str:
    """Return the tenant-specific S3 prefix used by statement data.

    Args:
        tenant_id: Tenant identifier used as the first S3 key segment.

    Returns:
        str: Prefix rooted at the tenant namespace.
    """
    return f"{tenant_id}/"


def clear_bucket(s3_client: Any, bucket_name: str, tenant_id: str | None = None) -> None:
    """Delete S3 objects for the full bucket or one tenant prefix.

    Args:
        s3_client: boto3 S3 client.
        bucket_name: Bucket to delete from.
        tenant_id: Optional tenant filter. When provided, only keys under
            ``<tenant_id>/`` are deleted.

    Returns:
        None.
    """
    prefix = tenant_prefix(tenant_id) if tenant_id else None
    if prefix:
        print(f"Clearing S3 bucket: {bucket_name} (prefix: {prefix})")
    else:
        print(f"Clearing S3 bucket: {bucket_name}")
    total_deleted = 0
    error_count = 0

    try:
        paginator = s3_client.get_paginator("list_object_versions")
        version_kwargs: dict[str, str] = {"Bucket": bucket_name}
        if prefix:
            version_kwargs["Prefix"] = prefix
        for page in paginator.paginate(**version_kwargs):
            objects: list[dict[str, str]] = []
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
    object_kwargs: dict[str, str] = {"Bucket": bucket_name}
    if prefix:
        object_kwargs["Prefix"] = prefix
    for page in paginator.paginate(**object_kwargs):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        for batch in chunked(keys, 1000):
            response = s3_client.delete_objects(
                Bucket=bucket_name,
                Delete={"Objects": batch, "Quiet": True},
            )
            error_count += len(response.get("Errors", []))
            total_deleted += len(response.get("Deleted", []))

    if total_deleted == 0:
        print("Tenant S3 prefix is already empty." if prefix else "Bucket is already empty.")
    else:
        target = f"bucket prefix {prefix}" if prefix else "bucket"
        message = f"Deleted {total_deleted} objects (including versions) from {target}."
        if error_count:
            message += f" Encountered {error_count} delete errors; see AWS logs for details."
        print(message)


def projection_expression_from_key_schema(key_names: Sequence[str]) -> dict[str, object]:
    """Build a projection expression that only reads primary-key fields.

    Args:
        key_names: Table key attribute names in schema order.

    Returns:
        dict[str, object]: Projection expression kwargs for DynamoDB reads.
    """
    expression_attribute_names = {f"#k{idx}": name for idx, name in enumerate(key_names)}
    projection_expression = ", ".join(expression_attribute_names.keys())
    return {
        "ProjectionExpression": projection_expression,
        "ExpressionAttributeNames": expression_attribute_names,
    }


def build_table_read_kwargs(table: Any, key_names: Sequence[str], tenant_id: str | None) -> tuple[str, dict[str, object]]:
    """Choose the DynamoDB read operation for the requested delete scope.

    Args:
        table: boto3 table resource.
        key_names: Table primary-key attribute names.
        tenant_id: Optional tenant filter.

    Returns:
        tuple[str, dict[str, object]]: Method name (``scan`` or ``query``) and
            the kwargs to pass to it.
    """
    read_kwargs: dict[str, object] = {}
    if key_names:
        read_kwargs.update(projection_expression_from_key_schema(key_names))

    if not tenant_id:
        return "scan", read_kwargs

    partition_key_name = table.key_schema[0]["AttributeName"] if table.key_schema else None
    if partition_key_name == "TenantID":
        # Query is cheaper and safer than a full scan when TenantID is the partition key.
        read_kwargs["KeyConditionExpression"] = Key("TenantID").eq(tenant_id)
        return "query", read_kwargs

    read_kwargs["FilterExpression"] = Attr("TenantID").eq(tenant_id)
    return "scan", read_kwargs


def clear_table(ddb_resource: Any, table_name: str, tenant_id: str | None = None) -> None:
    """Delete DynamoDB items from the full table or one tenant partition.

    Args:
        ddb_resource: boto3 DynamoDB resource.
        table_name: Table to delete from.
        tenant_id: Optional tenant filter. When provided, only rows for that
            tenant are deleted.

    Returns:
        None.
    """
    table = ddb_resource.Table(table_name)
    scope_suffix = f" for TenantID {tenant_id}" if tenant_id else ""
    print(f"Clearing DynamoDB table: {table_name}{scope_suffix}")
    deleted = 0

    try:
        table.load()
    except ClientError as error:
        if error.response["Error"]["Code"] == "ResourceNotFoundException":
            print(f"Table {table_name} does not exist.")
            return
        raise

    key_names = [key["AttributeName"] for key in table.key_schema]
    read_method_name, read_kwargs = build_table_read_kwargs(table, key_names, tenant_id)
    read_method = getattr(table, read_method_name)

    with table.batch_writer() as batch:
        while True:
            response = read_method(**read_kwargs)
            items = response.get("Items", [])
            if not items:
                break
            for item in items:
                key = {name: item[name] for name in key_names}
                batch.delete_item(Key=key)
                deleted += 1
            if "LastEvaluatedKey" not in response:
                break
            read_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    if deleted == 0:
        print("No items matched the tenant filter." if tenant_id else "Table is already empty.")
    else:
        print(f"Deleted {deleted} items from table.")


def main() -> None:
    """Run the clear workflow using the configured AWS resources.

    Returns:
        None.
    """
    args = parse_args()
    if not args.env_file.exists():
        print(f"Env file not found: {args.env_file}", file=sys.stderr)
        sys.exit(1)

    try:
        tenant_id = normalize_tenant_id(args.tenant_id)
    except ValueError as error:
        print(str(error), file=sys.stderr)
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
        print(
            f"Missing required environment variables in {args.env_file}: {', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(1)
    assert bucket_name is not None

    table_names = [value for _, value in table_env_values if value is not None]

    session = build_session()
    scope_summary = f"Tenant scope: {tenant_id}" if tenant_id else "Tenant scope: all tenants"
    s3_summary = f"{bucket_name} (prefix: {tenant_prefix(tenant_id)})" if tenant_id else bucket_name
    resources_summary = f"{scope_summary}\nS3 bucket: {s3_summary}\nDynamoDB tables: {', '.join(table_names)}"
    print(resources_summary)
    prompt = f"Proceed with deleting data for TenantID {tenant_id} from these resources?" if tenant_id else "Proceed with deleting all data from these resources?"
    confirm(prompt, args.yes)

    s3_client = session.client("s3")
    ddb_resource = session.resource("dynamodb")

    clear_bucket(s3_client, bucket_name, tenant_id)
    for table_name in table_names:
        clear_table(ddb_resource, table_name, tenant_id)


if __name__ == "__main__":
    try:
        main()
    except ClientError as error:
        print(f"AWS client error: {error}", file=sys.stderr)
        sys.exit(1)
    except Exception as error:  # pragma: no cover - catch-all for CLI
        print(f"Unexpected error: {error}", file=sys.stderr)
        sys.exit(1)
