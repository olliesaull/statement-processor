# Billing Form Re-fill Regression Fix

## Context

During evaluation of the per-checkout Stripe customer refactor, a regression was found in `billing_details.html`. The template was simplified to remove all DynamoDB pre-fill logic, but the `checkout_create` route still passes `saved=request.form` on validation errors. The template now ignores `saved` entirely — all address fields have hardcoded `value=""` and name/email always show the Xero defaults. If a user submits the form with a required field missing, all their typed input is wiped and they must re-enter everything from scratch.

**Impact**: Minor UX regression. Only affects the validation-error path. The checkout flow itself is unaffected.

---

## Files to change

| File | Change |
|---|---|
| `service/templates/billing_details.html` | Restore `saved.get(field, default) if saved else default` for all fields |

---

## Template changes

`saved` is only passed on validation errors (from `request.form`); it is never passed on the initial GET, so this does not reintroduce DynamoDB pre-fill.

| Field | Current | Fixed |
|---|---|---|
| `billing_name` | `value="{{ default_name }}"` | `value="{{ saved.get('billing_name', default_name) if saved else default_name }}"` |
| `billing_email` | `value="{{ default_email }}"` | `value="{{ saved.get('billing_email', default_email) if saved else default_email }}"` |
| `billing_line1` | `value=""` | `value="{{ saved.get('billing_line1', '') if saved else '' }}"` |
| `billing_line2` | `value=""` | `value="{{ saved.get('billing_line2', '') if saved else '' }}"` |
| `billing_city` | `value=""` | `value="{{ saved.get('billing_city', '') if saved else '' }}"` |
| `billing_state` | `value=""` | `value="{{ saved.get('billing_state', '') if saved else '' }}"` |
| `billing_postal_code` | `value=""` | `value="{{ saved.get('billing_postal_code', '') if saved else '' }}"` |
| country select | `{% set selected_country = '' %}` | `{% set selected_country = saved.get('billing_country', '') if saved else '' %}` |

---

## Verification

1. `make dev` passes.
2. Manual: fill in the billing form, leave postal code blank, submit → form re-renders with error message and all typed values preserved (name, email, address fields retain what you typed; country select retains selection).
