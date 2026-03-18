#!/usr/bin/env python3.13
"""Apply a manual token adjustment for one tenant.

This script is intentionally small and reuses ``service/billing_service.py`` so
manual top-ups/removals follow the same atomic DynamoDB transaction pattern as
runtime billing writes.

Usage:
    python3.13 scripts/manual_token_adjustment/manual_token_adjustment.py <tenant_id> <token_delta>
    python3.13 scripts/manual_token_adjustment/manual_token_adjustment.py <tenant_id> <token_delta> --yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_DIR = REPO_ROOT / "service"
DEFAULT_ENV_FILE = SERVICE_DIR / ".env"


def _build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser for the manual adjustment tool."""
    parser = argparse.ArgumentParser(description="Apply a manual token adjustment for one tenant.")
    parser.add_argument("tenant_id", help="TenantID to adjust.")
    parser.add_argument("token_delta", type=int, help="Signed token delta. Positive grants tokens; negative removes them.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="Path to the service env file (default: %(default)s).")
    return parser


def _load_environment(env_file: Path) -> None:
    """Load the service environment before importing service modules."""
    if env_file.exists():
        load_dotenv(env_file, override=False)
        print(f"Loaded environment from {env_file}")
    else:
        print(f"Env file not found at {env_file}; using existing shell environment only.")


def _confirm_or_abort(message: str, *, assume_yes: bool) -> None:
    """Prompt once before mutating a tenant balance."""
    if assume_yes:
        return

    answer = input(f"{message} [y/N]: ").strip().lower()
    if answer not in {"y", "yes"}:
        print("Aborted.")
        raise SystemExit(0)


def main() -> int:
    """Run the manual token adjustment flow."""
    parser = _build_parser()
    args = parser.parse_args()

    env_file = Path(args.env_file).expanduser().resolve()
    _load_environment(env_file)

    if str(SERVICE_DIR) not in sys.path:
        sys.path.insert(0, str(SERVICE_DIR))

    from billing_service import (  # pylint: disable=import-outside-toplevel
        LAST_MUTATION_SOURCE_MANUAL_ADJUSTMENT,
        BillingService,
        BillingServiceError,
        InsufficientTokensError,
    )
    from tenant_billing_repository import TenantBillingRepository  # pylint: disable=import-outside-toplevel

    tenant_id = args.tenant_id.strip()
    token_delta = int(args.token_delta)
    current_balance = TenantBillingRepository.get_tenant_token_balance(tenant_id)
    expected_balance = current_balance + token_delta
    sign = "+" if token_delta > 0 else ""

    print(f"TenantID: {tenant_id}")
    print(f"Current balance: {current_balance}")
    print(f"Adjustment: {sign}{token_delta}")
    print(f"Expected balance after adjustment: {expected_balance}")

    # Negative adjustments should still require an explicit confirmation even if
    # the operator can already see the balance on screen.
    _confirm_or_abort(f"Apply token adjustment {sign}{token_delta} to tenant {tenant_id}?", assume_yes=args.yes)

    try:
        result = BillingService.adjust_token_balance(tenant_id, token_delta, source=LAST_MUTATION_SOURCE_MANUAL_ADJUSTMENT)
    except InsufficientTokensError:
        print("Error: the tenant does not have enough tokens for that negative adjustment.", file=sys.stderr)
        return 1
    except BillingServiceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    updated_balance = TenantBillingRepository.get_tenant_token_balance(tenant_id)
    print("Adjustment applied.")
    print(f"Updated balance: {updated_balance}")
    print(f"LedgerEntryID: {result.ledger_entry_id}")
    print(f"UpdatedAt: {result.updated_at}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
