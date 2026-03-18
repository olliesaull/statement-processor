# Clear DDB And S3

Operator notes for running `scripts/clear_ddb_and_s3/clear_ddb_and_s3.py`.

## What it touches

The script reads resource names from a `.env` file and clears:

- `S3_BUCKET_NAME`
- `TENANT_CONTACTS_CONFIG_TABLE_NAME`
- `TENANT_STATEMENTS_TABLE_NAME`
- `TENANT_DATA_TABLE_NAME`

It does not touch billing or token-ledger tables.

## Setup

From the script directory:

```bash
cd /home/ollie/statement-processor/scripts/clear_ddb_and_s3
python3.13 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run modes

### 1. All tenants, interactive

Default mode. Prints the target resources and asks for confirmation before deleting.

```bash
cd /home/ollie/statement-processor/scripts/clear_ddb_and_s3
python3.13 clear_ddb_and_s3.py
```

### 2. All tenants, non-interactive

Skips the confirmation prompt and immediately deletes all tenant data from the configured resources.

```bash
cd /home/ollie/statement-processor/scripts/clear_ddb_and_s3
python3.13 clear_ddb_and_s3.py --yes
```

### 3. One tenant, interactive

Deletes data for a single tenant only:

- DynamoDB: only rows with `TenantID=<tenant_id>`
- S3: only keys under `<tenant_id>/`

```bash
cd /home/ollie/statement-processor/scripts/clear_ddb_and_s3
python3.13 clear_ddb_and_s3.py --tenant-id <tenant_id>
```

### 4. One tenant, non-interactive

Same tenant-scoped delete, but without the confirmation prompt.

```bash
cd /home/ollie/statement-processor/scripts/clear_ddb_and_s3
python3.13 clear_ddb_and_s3.py --tenant-id <tenant_id> --yes
```

## CLI arguments

### `--env-file`

Path to the `.env` file the script should load.

- Default: `../../service/.env`
- Use this when you want to target a different environment or account config file.

Example:

```bash
cd /home/ollie/statement-processor/scripts/clear_ddb_and_s3
python3.13 clear_ddb_and_s3.py --env-file /path/to/.env
```

### `--tenant-id`

Scopes the delete to one tenant instead of all tenants.

- If omitted, the script deletes all tenant data in the configured resources.
- If provided, the script only deletes that tenant's DynamoDB rows and S3 prefix.
- Path separators are rejected so a malformed value cannot widen the S3 delete scope.

Example:

```bash
cd /home/ollie/statement-processor/scripts/clear_ddb_and_s3
python3.13 clear_ddb_and_s3.py --tenant-id tenant-123
```

### `--yes`

Skips the interactive confirmation prompt.

Use this for automation or when you have already verified the target account and resources.

## Environment file expectations

The `.env` file should contain:

- `S3_BUCKET_NAME`
- `TENANT_CONTACTS_CONFIG_TABLE_NAME`
- `TENANT_STATEMENTS_TABLE_NAME`
- `TENANT_DATA_TABLE_NAME`

It can also contain:

- `AWS_PROFILE`
- `AWS_REGION`

If required resource variables are missing, the script exits before making changes.

## Notes

- There is no dry-run mode.
- Tenant-scoped mode is safer when you only want to reset one tenant without affecting other tenants.
- The confirmation prompt shows the resolved tenant scope, S3 target, and DynamoDB tables before deletion starts.
