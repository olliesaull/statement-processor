# Manual Token Adjustment

Use this script to add or remove tokens for a tenant without editing DynamoDB by hand.

## Why use the script
The script updates `TenantBillingTable` and writes a matching `ADJUSTMENT` row to `TenantTokenLedgerTable` in one atomic DynamoDB transaction.

Do not update the billing table manually unless you are also deliberately repairing the ledger.

## Setup
Run everything from inside this directory.

```bash
cd /home/ollie/statement-processor/scripts/manual_token_adjustment
python3.13 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## How to run
```bash
cd /home/ollie/statement-processor/scripts/manual_token_adjustment
source venv/bin/activate
python3.13 manual_token_adjustment.py <tenant_id> <token_delta>
```

Example grant:
```bash
python3.13 manual_token_adjustment.py tenant-123 50
```

Example removal:
```bash
python3.13 manual_token_adjustment.py tenant-123 -20
```

Skip the confirmation prompt:
```bash
python3.13 manual_token_adjustment.py tenant-123 50 --yes
```

## Notes
- The script loads `../../service/.env` by default.
- `token_delta` must be non-zero.
- Negative adjustments fail if the tenant does not have enough tokens.
- `requirements.txt` reuses `service/requirements.txt` and installs `common/sp_common` because the script imports and executes the same billing code as the web app.
- Importing `config.py` triggers SSM secret fetching and a Redis/Valkey connection, so the `.env` must have valid SSM parameter paths and `VALKEY_URL` pointing to a running instance.
