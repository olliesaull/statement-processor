#!/usr/bin/env python3
"""Backup and restore contact configs + statement PDFs for one tenant.

This utility is intentionally environment-driven (no CLI args).
Set the globals via environment variables before running.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

# Required: tenant you want to export/import.
TENANT_ID = (os.getenv("TENANT_ID") or "").strip()

# backup | restore
TENANT_SNAPSHOT_MODE = (os.getenv("TENANT_SNAPSHOT_MODE") or "backup").strip().lower()

# Optional controls
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / "service" / ".env"
TENANT_SNAPSHOT_ENV_FILE = Path((os.getenv("TENANT_SNAPSHOT_ENV_FILE") or str(DEFAULT_ENV_FILE)).strip()).expanduser()
TENANT_SNAPSHOT_DIR = Path((os.getenv("TENANT_SNAPSHOT_DIR") or str(REPO_ROOT / "scripts" / "tenant_snapshot" / "snapshots")).strip()).expanduser()
TENANT_SNAPSHOT_YES = (os.getenv("TENANT_SNAPSHOT_YES") or "false").strip().lower() in {"1", "true", "yes", "y"}
TENANT_SNAPSHOT_START_WORKFLOWS = (os.getenv("TENANT_SNAPSHOT_START_WORKFLOWS") or "true").strip().lower() in {"1", "true", "yes", "y"}
try:
    TENANT_SNAPSHOT_WORKFLOW_DELAY_SECONDS = max(0.0, float((os.getenv("TENANT_SNAPSHOT_WORKFLOW_DELAY_SECONDS") or "1").strip() or "1"))
except ValueError:
    TENANT_SNAPSHOT_WORKFLOW_DELAY_SECONDS = 1.0

CONTACT_CONFIGS_FILENAME = "contact_configs.json"
STATEMENTS_MANIFEST_FILENAME = "statements_manifest.json"
RESTORE_RESULTS_FILENAME = "restore_results.json"
PDFS_DIRNAME = "pdfs"


@dataclass(frozen=True)
class RuntimeConfig:
    """AWS/runtime settings required by this script.

    This object centralizes the environment-driven resource names so backup
    and restore logic read from one typed source.

    Attributes:
        bucket_name: S3 bucket storing statements.
        contacts_table_name: DynamoDB table storing contact configs.
        statements_table_name: DynamoDB table storing statement headers/items.
        state_machine_arn: StepFunctions state machine ARN used to regenerate JSON.
        aws_profile: Optional AWS profile name.
        aws_region: Optional AWS region.
    """

    bucket_name: str
    contacts_table_name: str
    statements_table_name: str
    state_machine_arn: str | None
    aws_profile: str | None
    aws_region: str | None


def _load_environment() -> None:
    """Load optional `.env` values used by the script.

    Args:
        None.

    Returns:
        None.
    """
    if TENANT_SNAPSHOT_ENV_FILE.exists():
        load_dotenv(TENANT_SNAPSHOT_ENV_FILE, override=False)
        print(f"Loaded environment from {TENANT_SNAPSHOT_ENV_FILE}")
    else:
        print(f"Env file not found at {TENANT_SNAPSHOT_ENV_FILE}; using existing environment only.")


def _build_runtime_config() -> RuntimeConfig:
    """Build and validate runtime config from environment variables.

    Args:
        None.

    Returns:
        RuntimeConfig: Validated runtime settings.

    Raises:
        ValueError: When required environment variables are missing.
    """
    bucket_name = (os.getenv("S3_BUCKET_NAME") or "").strip()
    contacts_table_name = (os.getenv("TENANT_CONTACTS_CONFIG_TABLE_NAME") or "").strip()
    statements_table_name = (os.getenv("TENANT_STATEMENTS_TABLE_NAME") or "").strip()
    state_machine_arn = (os.getenv("TEXTRACTION_STATE_MACHINE_ARN") or "").strip() or None
    aws_profile = (os.getenv("AWS_PROFILE") or "").strip() or None
    aws_region = (os.getenv("AWS_REGION") or "").strip() or None

    missing: list[str] = []
    if not TENANT_ID:
        missing.append("TENANT_ID")
    if not bucket_name:
        missing.append("S3_BUCKET_NAME")
    if not contacts_table_name:
        missing.append("TENANT_CONTACTS_CONFIG_TABLE_NAME")
    if not statements_table_name:
        missing.append("TENANT_STATEMENTS_TABLE_NAME")
    if TENANT_SNAPSHOT_MODE not in {"backup", "restore"}:
        missing.append("TENANT_SNAPSHOT_MODE must be 'backup' or 'restore'")

    if missing:
        raise ValueError(f"Missing/invalid required configuration: {', '.join(missing)}")

    return RuntimeConfig(
        bucket_name=bucket_name,
        contacts_table_name=contacts_table_name,
        statements_table_name=statements_table_name,
        state_machine_arn=state_machine_arn,
        aws_profile=aws_profile,
        aws_region=aws_region,
    )


def _build_session(config: RuntimeConfig) -> boto3.session.Session:
    """Create a boto3 session using optional profile/region overrides.

    Args:
        config: Runtime settings including profile/region.

    Returns:
        boto3.session.Session: Session configured for the target account/region.
    """
    session_kwargs: dict[str, str] = {}
    if config.aws_profile:
        session_kwargs["profile_name"] = config.aws_profile
    if config.aws_region:
        session_kwargs["region_name"] = config.aws_region
    return boto3.session.Session(**session_kwargs)


def _confirm(prompt: str) -> None:
    """Prompt for confirmation unless non-interactive mode is enabled.

    Args:
        prompt: Confirmation text displayed to the operator.

    Returns:
        None.
    """
    if TENANT_SNAPSHOT_YES:
        return
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    if answer not in {"y", "yes"}:
        print("Aborted.")
        sys.exit(0)


def _tenant_snapshot_dir() -> Path:
    """Return the local snapshot directory for the configured tenant.

    Args:
        None.

    Returns:
        Path: Snapshot root for this tenant.
    """
    return TENANT_SNAPSHOT_DIR / TENANT_ID


def _json_default(value: Any) -> Any:
    """Serialize non-JSON-native values.

    Args:
        value: Arbitrary value encountered during JSON dumping.

    Returns:
        Any: JSON-serializable value.
    """
    return str(value)


def _scan_query(table: Any, query_kwargs: dict[str, Any]) -> list[dict[str, Any]]:
    """Run a paginated DynamoDB query and return all items.

    Args:
        table: DynamoDB table resource.
        query_kwargs: Initial query kwargs.

    Returns:
        list[dict[str, Any]]: Flattened result items.
    """
    items: list[dict[str, Any]] = []
    kwargs = dict(query_kwargs)
    while True:
        response = table.query(**kwargs)
        batch = response.get("Items", [])
        if isinstance(batch, list):
            items.extend(item for item in batch if isinstance(item, dict))
        last_evaluated_key = response.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
        kwargs["ExclusiveStartKey"] = last_evaluated_key
    return items


def _query_contact_configs(contacts_table: Any) -> list[dict[str, Any]]:
    """Fetch all contact config rows for the tenant.

    Args:
        contacts_table: TenantContactsConfigTable handle.

    Returns:
        list[dict[str, Any]]: Contact config rows.
    """
    return _scan_query(contacts_table, {"KeyConditionExpression": Key("TenantID").eq(TENANT_ID)})


def _query_statement_headers(statements_table: Any) -> list[dict[str, Any]]:
    """Fetch statement header rows for the tenant.

    Args:
        statements_table: TenantStatementsTable handle.

    Returns:
        list[dict[str, Any]]: Statement header rows.
    """
    query_kwargs = {"KeyConditionExpression": Key("TenantID").eq(TENANT_ID), "FilterExpression": Attr("RecordType").not_exists() | Attr("RecordType").eq("statement")}
    rows = _scan_query(statements_table, query_kwargs)
    # Ignore malformed rows and keep order stable by upload time when present.
    rows = [row for row in rows if isinstance(row.get("StatementID"), str) and str(row.get("StatementID")).strip()]
    rows.sort(key=lambda row: str(row.get("UploadedAt") or ""))
    return rows


def _statement_pdf_key(statement_id: str) -> str:
    """Build the S3 key for a tenant statement PDF.

    Args:
        statement_id: Statement identifier.

    Returns:
        str: S3 key path.
    """
    return f"{TENANT_ID}/statements/{statement_id}.pdf"


def _statement_json_key(statement_id: str) -> str:
    """Build the S3 key for a tenant statement JSON.

    Args:
        statement_id: Statement identifier.

    Returns:
        str: S3 key path.
    """
    return f"{TENANT_ID}/statements/{statement_id}.json"


def _backup_contact_configs(contacts_rows: list[dict[str, Any]], tenant_dir: Path) -> Path:
    """Write contact config rows to disk.

    Args:
        contacts_rows: DynamoDB rows from the contacts config table.
        tenant_dir: Snapshot directory for this tenant.

    Returns:
        Path: Output JSON file path.
    """
    output_path = tenant_dir / CONTACT_CONFIGS_FILENAME
    payload: list[dict[str, Any]] = []
    for row in contacts_rows:
        contact_id = str(row.get("ContactID") or "").strip()
        config = row.get("config")
        if not contact_id or not isinstance(config, dict):
            continue
        payload.append({"ContactID": contact_id, "config": config})

    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default), encoding="utf-8")
    return output_path


def _backup_statement_pdfs(s3_client: Any, bucket_name: str, statement_headers: list[dict[str, Any]], tenant_dir: Path) -> Path:
    """Download all statement PDFs for the tenant and write a manifest.

    Args:
        s3_client: Boto3 S3 client.
        bucket_name: Source S3 bucket.
        statement_headers: Statement header rows from DynamoDB.
        tenant_dir: Snapshot directory for this tenant.

    Returns:
        Path: Manifest JSON file path.
    """
    pdf_dir = tenant_dir / PDFS_DIRNAME
    pdf_dir.mkdir(parents=True, exist_ok=True)

    manifest: list[dict[str, Any]] = []
    for row in statement_headers:
        original_statement_id = str(row.get("StatementID") or "").strip()
        if not original_statement_id:
            continue

        source_key = _statement_pdf_key(original_statement_id)
        local_pdf_name = f"{original_statement_id}.pdf"
        local_pdf_path = pdf_dir / local_pdf_name
        downloaded = False
        error_message = None

        try:
            with local_pdf_path.open("wb") as file_handle:
                s3_client.download_fileobj(bucket_name, source_key, file_handle)
            downloaded = True
        except ClientError as error:
            error_message = error.response.get("Error", {}).get("Code", str(error))
        except Exception as error:  # pragma: no cover - defensive catch for operational script use
            error_message = str(error)

        manifest.append(
            {
                "original_statement_id": original_statement_id,
                "contact_id": str(row.get("ContactID") or "").strip(),
                "contact_name": str(row.get("ContactName") or "").strip(),
                "original_statement_filename": str(row.get("OriginalStatementFilename") or "").strip() or local_pdf_name,
                "uploaded_at": str(row.get("UploadedAt") or "").strip(),
                "local_pdf_name": local_pdf_name,
                "downloaded": downloaded,
                "download_error": error_message,
            }
        )

    manifest_path = tenant_dir / STATEMENTS_MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=_json_default), encoding="utf-8")
    return manifest_path


def backup_tenant_snapshot(session: boto3.session.Session, config: RuntimeConfig) -> None:
    """Export configs + statement PDFs for one tenant.

    Args:
        session: Boto3 session.
        config: Runtime config values.

    Returns:
        None.
    """
    tenant_dir = _tenant_snapshot_dir()
    tenant_dir.mkdir(parents=True, exist_ok=True)

    ddb = session.resource("dynamodb")
    contacts_table = ddb.Table(config.contacts_table_name)
    statements_table = ddb.Table(config.statements_table_name)
    s3_client = session.client("s3")

    contact_rows = _query_contact_configs(contacts_table)
    statement_headers = _query_statement_headers(statements_table)

    print(f"Found {len(contact_rows)} contact config rows for tenant {TENANT_ID}")
    print(f"Found {len(statement_headers)} statement headers for tenant {TENANT_ID}")

    _confirm(f"Proceed with backup into {tenant_dir}?")

    contact_configs_path = _backup_contact_configs(contact_rows, tenant_dir)
    manifest_path = _backup_statement_pdfs(s3_client, config.bucket_name, statement_headers, tenant_dir)

    print(f"Backup complete:\n- {contact_configs_path}\n- {manifest_path}\n- {(tenant_dir / PDFS_DIRNAME)}")


def _load_json(path: Path) -> Any:
    """Load a JSON file from disk.

    Args:
        path: File path.

    Returns:
        Any: Parsed JSON value.

    Raises:
        FileNotFoundError: When file does not exist.
        json.JSONDecodeError: When content is invalid JSON.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def _restore_contact_configs(contacts_table: Any, tenant_dir: Path) -> int:
    """Restore contact config rows into DynamoDB.

    Args:
        contacts_table: TenantContactsConfigTable handle.
        tenant_dir: Snapshot directory for this tenant.

    Returns:
        int: Number of restored contact configs.
    """
    configs_path = tenant_dir / CONTACT_CONFIGS_FILENAME
    rows = _load_json(configs_path)
    if not isinstance(rows, list):
        raise ValueError(f"Expected list in {configs_path}")

    restored = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        contact_id = str(row.get("ContactID") or "").strip()
        config = row.get("config")
        if not contact_id or not isinstance(config, dict):
            continue
        contacts_table.put_item(Item={"TenantID": TENANT_ID, "ContactID": contact_id, "config": config})
        restored += 1
    return restored


def _start_workflow(stepfunctions_client: Any, state_machine_arn: str, statement_id: str, contact_id: str) -> bool:
    """Start Textraction for a newly restored statement.

    Args:
        stepfunctions_client: Boto3 StepFunctions client.
        state_machine_arn: State machine ARN.
        statement_id: New statement ID.
        contact_id: Contact ID used for extraction config.

    Returns:
        bool: True if execution started successfully.
    """
    payload = {
        "tenant_id": TENANT_ID,
        "contact_id": contact_id,
        "statement_id": statement_id,
        "s3Bucket": os.getenv("S3_BUCKET_NAME"),
        "pdfKey": _statement_pdf_key(statement_id),
        "jsonKey": _statement_json_key(statement_id),
    }
    execution_name = f"{TENANT_ID}-{statement_id}-{uuid.uuid4().hex[:8]}"[:80]

    try:
        stepfunctions_client.start_execution(stateMachineArn=state_machine_arn, name=execution_name, input=json.dumps(payload))
        return True
    except ClientError as error:
        error_code = error.response.get("Error", {}).get("Code")
        if error_code == "ExecutionAlreadyExists":
            return True
        print(f"Failed to start workflow for statement {statement_id}: {error}")
        return False


def _restore_statements(session: boto3.session.Session, config: RuntimeConfig, tenant_dir: Path) -> dict[str, Any]:
    """Restore statement PDFs and header rows, then optionally restart workflows.

    Args:
        session: Boto3 session.
        config: Runtime configuration.
        tenant_dir: Snapshot directory for this tenant.

    Returns:
        dict[str, Any]: Restore summary including id mappings.
    """
    manifest_path = tenant_dir / STATEMENTS_MANIFEST_FILENAME
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, list):
        raise ValueError(f"Expected list in {manifest_path}")

    ddb = session.resource("dynamodb")
    statements_table = ddb.Table(config.statements_table_name)
    s3_client = session.client("s3")
    stepfunctions_client = session.client("stepfunctions") if TENANT_SNAPSHOT_START_WORKFLOWS and config.state_machine_arn else None

    restore_rows: list[dict[str, Any]] = []
    restored_statements = 0
    started_workflows = 0
    skipped = 0

    for row in manifest:
        if not isinstance(row, dict):
            skipped += 1
            continue

        local_pdf_name = str(row.get("local_pdf_name") or "").strip()
        contact_id = str(row.get("contact_id") or "").strip()
        contact_name = str(row.get("contact_name") or "").strip()
        original_filename = str(row.get("original_statement_filename") or "").strip() or local_pdf_name
        old_statement_id = str(row.get("original_statement_id") or "").strip()
        pdf_path = tenant_dir / PDFS_DIRNAME / local_pdf_name

        if not local_pdf_name or not contact_id or not pdf_path.exists():
            skipped += 1
            restore_rows.append({"old_statement_id": old_statement_id, "new_statement_id": None, "restored": False, "workflow_started": False, "error": "missing_local_pdf_or_contact"})
            continue

        new_statement_id = str(uuid.uuid4())
        pdf_key = _statement_pdf_key(new_statement_id)

        try:
            with pdf_path.open("rb") as file_handle:
                s3_client.upload_fileobj(file_handle, config.bucket_name, pdf_key)

            statements_table.put_item(
                Item={
                    "TenantID": TENANT_ID,
                    "StatementID": new_statement_id,
                    "OriginalStatementFilename": original_filename,
                    "ContactID": contact_id,
                    "ContactName": contact_name,
                    "UploadedAt": datetime.now(UTC).replace(microsecond=0).isoformat(),
                    "Completed": "false",
                    "RecordType": "statement",
                }
            )

            workflow_started = False
            if TENANT_SNAPSHOT_START_WORKFLOWS and stepfunctions_client and config.state_machine_arn:
                workflow_started = _start_workflow(stepfunctions_client, config.state_machine_arn, new_statement_id, contact_id)
                if workflow_started:
                    started_workflows += 1
                if TENANT_SNAPSHOT_WORKFLOW_DELAY_SECONDS > 0:
                    time.sleep(TENANT_SNAPSHOT_WORKFLOW_DELAY_SECONDS)

            restored_statements += 1
            restore_rows.append({"old_statement_id": old_statement_id, "new_statement_id": new_statement_id, "restored": True, "workflow_started": workflow_started, "error": None})
        except Exception as error:  # pragma: no cover - operational script safety
            restore_rows.append({"old_statement_id": old_statement_id, "new_statement_id": new_statement_id, "restored": False, "workflow_started": False, "error": str(error)})

    summary = {"tenant_id": TENANT_ID, "restored_statements": restored_statements, "started_workflows": started_workflows, "skipped": skipped, "rows": restore_rows}
    results_path = tenant_dir / RESTORE_RESULTS_FILENAME
    results_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def restore_tenant_snapshot(session: boto3.session.Session, config: RuntimeConfig) -> None:
    """Restore configs and statements from a local tenant snapshot.

    Args:
        session: Boto3 session.
        config: Runtime configuration.

    Returns:
        None.
    """
    tenant_dir = _tenant_snapshot_dir()
    if not tenant_dir.exists():
        raise FileNotFoundError(f"Snapshot directory not found: {tenant_dir}")

    _confirm("Proceed with restore? This writes contact configs, uploads PDFs, creates statement header rows, and can trigger textraction workflows.")

    ddb = session.resource("dynamodb")
    contacts_table = ddb.Table(config.contacts_table_name)

    restored_configs = _restore_contact_configs(contacts_table, tenant_dir)
    print(f"Restored {restored_configs} contact configs")

    summary = _restore_statements(session, config, tenant_dir)
    print(
        "Restore complete:\n"
        f"- restored_statements: {summary['restored_statements']}\n"
        f"- started_workflows: {summary['started_workflows']}\n"
        f"- skipped: {summary['skipped']}\n"
        f"- details: {tenant_dir / RESTORE_RESULTS_FILENAME}"
    )


def main() -> None:
    """Execute backup or restore for one tenant.

    Args:
        None.

    Returns:
        None.
    """
    _load_environment()
    config = _build_runtime_config()
    session = _build_session(config)

    print(
        f"Mode={TENANT_SNAPSHOT_MODE} TenantID={TENANT_ID}\n"
        f"SnapshotDir={_tenant_snapshot_dir()}\n"
        f"Bucket={config.bucket_name}\n"
        f"ContactsTable={config.contacts_table_name}\n"
        f"StatementsTable={config.statements_table_name}\n"
        f"WorkflowDelaySeconds={TENANT_SNAPSHOT_WORKFLOW_DELAY_SECONDS}"
    )

    if TENANT_SNAPSHOT_MODE == "backup":
        backup_tenant_snapshot(session, config)
    elif TENANT_SNAPSHOT_MODE == "restore":
        restore_tenant_snapshot(session, config)
    else:
        raise ValueError("TENANT_SNAPSHOT_MODE must be 'backup' or 'restore'")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # pragma: no cover - CLI boundary
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
