# Invoice / Billing Details Page

## Context
Stripe already creates an invoice on every token purchase (`invoice_creation={"enabled": True}`), but the Stripe Customer is created with the Xero-derived org name and user email — no billing address, and no way for the user to specify where the invoice goes. This adds a `/billing-details` form between the token count step and Stripe checkout, mirroring the numerint `/invoice` page. User-provided details always override Xero-derived values on the Stripe Customer. Stripe auto-emails the finalised invoice PDF to the billing email (enable in Stripe Dashboard: Settings → Billing → Invoices → "Email finalized invoices to customers"). No SES needed.

---

## Flow

```
GET  /buy-tokens          → token count form (unchanged UI, form action changes)
POST /buy-tokens          → NEW: validate token_count, store in session["pending_token_count"], redirect to /billing-details
GET  /billing-details     → NEW: render billing form pre-filled from TenantBillingTable
POST /api/checkout/create → MODIFIED: read token_count from session, accept billing fields,
                            update Stripe Customer, cache billing details, create session, redirect
```

---

## Files to Change

| File | Change |
|---|---|
| `service/app.py` | Add `POST /buy-tokens` route; add `GET /billing-details` route; modify `POST /api/checkout/create` |
| `service/stripe_service.py` | Add `update_customer()` method |
| `service/stripe_repository.py` | Add `cache_billing_details()` and `get_billing_details()` methods |
| `service/templates/buy_tokens.html` | Change form action to `POST /buy-tokens`; change button label to "Next: Billing Details" |
| `service/templates/billing_details.html` | **New** — billing details form |

---

## 1. `POST /buy-tokens` (new route in app.py)

```python
@app.route("/buy-tokens", methods=["POST"])
@xero_token_required
@route_handler_logging
def buy_tokens_post():
    token_count_raw = request.form.get("token_count", "").strip()
    try:
        token_count = int(token_count_raw)
    except (ValueError, TypeError):
        return render_template("buy_tokens.html", error="Please enter a valid number of tokens.",
                               token_balance=..., min_tokens=STRIPE_MIN_TOKENS,
                               max_tokens=STRIPE_MAX_TOKENS, price_pence=STRIPE_PRICE_PER_TOKEN_PENCE), 400
    if not STRIPE_MIN_TOKENS <= token_count <= STRIPE_MAX_TOKENS:
        return render_template("buy_tokens.html", error=f"Token count must be between ...",
                               ...), 400
    session["pending_token_count"] = token_count
    return redirect(url_for("billing_details"))
```

Move the validation logic currently in `POST /api/checkout/create` into this route. The session key `pending_token_count` is consumed (and deleted) in `POST /api/checkout/create`.

---

## 2. `GET /billing-details` (new route in app.py)

```python
@app.route("/billing-details", methods=["GET"])
@xero_token_required
@route_handler_logging
def billing_details():
    tenant_id = session.get("xero_tenant_id")
    if not session.get("pending_token_count"):
        return redirect(url_for("buy_tokens"))
    saved = StripeRepository.get_billing_details(tenant_id)  # None if first visit
    return render_template("billing_details.html",
        token_count=session["pending_token_count"],
        saved=saved,                        # pre-fill dict or None
        default_email=session.get("xero_user_email", ""),
        default_name=session.get("xero_tenant_name", ""),
    )
```

---

## 3. `POST /api/checkout/create` (modified)

Remove token_count validation (moved to `POST /buy-tokens`). Add billing fields. New responsibilities:
1. Read `token_count = session.get("pending_token_count")` — guard if missing (redirect to `/buy-tokens`). Do **not** pop yet; pop only after the Stripe session is successfully created so that billing re-renders on validation failure still have the key.
2. Read billing fields from `request.form`: `billing_name`, `billing_email`, `billing_line1`, `billing_line2`, `billing_city`, `billing_state`, `billing_postal_code`, `billing_country`.
3. Server-validate required fields (name, email, line1, postal_code, country) — re-render `billing_details.html` with error on failure (session key survives, retry works). Pass `saved=request.form` so the form re-fills with what the user typed; also pass `token_count` from the local variable.
4. Get or create Stripe Customer (existing logic).
5. **NEW**: inside a `try/except stripe.StripeError` block (same pattern as existing checkout session creation):
   - call `stripe_service.update_customer(customer_id, name, email, address)`
   - call `stripe_service.create_checkout_session(...)`
   - on `StripeError`: log, generate ref, redirect to `checkout_failed`
6. **NEW**: call `StripeRepository.cache_billing_details(tenant_id, billing_dict)` to persist for pre-fill (before the try block — DynamoDB cache is best-effort and independent of Stripe).
7. `session.pop("pending_token_count", None)` — consume only after successful Stripe session creation.

---

## 4. `StripeService.update_customer()` (stripe_service.py)

```python
def update_customer(self, customer_id: str, *, name: str, email: str, address: dict[str, str]) -> None:
    """Overwrite billing details on an existing Stripe Customer.

    Called on every purchase so user-provided billing details always override
    Xero-derived name/email. The address dict must use Stripe's field names:
    line1, line2, city, state, postal_code, country.
    """
    stripe.Customer.modify(customer_id, name=name, email=email, address=address)
    logger.info("Updated Stripe customer billing details", customer_id=customer_id)
```

---

## 5. `StripeRepository` additions (stripe_repository.py)

```python
_BILLING_FIELDS = ("BillingName", "BillingEmail", "BillingAddressLine1",
                   "BillingAddressLine2", "BillingCity", "BillingState",
                   "BillingPostalCode", "BillingCountryCode")

@classmethod
def cache_billing_details(cls, tenant_id: str, details: dict[str, str]) -> None:
    """Persist user-provided billing details to TenantBillingTable for pre-fill."""
    # UpdateItem writing ALL 8 fields unconditionally (empty string for blanks)
    # so previously-set optional fields are cleared when user submits them empty.

@classmethod
def get_billing_details(cls, tenant_id: str) -> dict[str, str] | None:
    """Return cached billing details dict, or None if never set."""
    # GetItem with ProjectionExpression covering the 8 billing fields
```

DynamoDB fields added to `TenantBillingTable` (schemaless, optional):

| Attribute | Type | Description |
|---|---|---|
| `BillingName` | String | Company or person name for invoice |
| `BillingEmail` | String | Email Stripe sends invoice to |
| `BillingAddressLine1` | String | Required |
| `BillingAddressLine2` | String | Optional |
| `BillingCity` | String | Optional |
| `BillingState` | String | Optional |
| `BillingPostalCode` | String | Required |
| `BillingCountryCode` | String | ISO 2-letter, required |

No CDK changes — schemaless DynamoDB, no new table.

---

## 6. `billing_details.html` (new template)

- Extends `base.html`, uses `page-main`/`page-header`/`page-panel` structure.
- Form POSTs to `url_for('checkout_create')`.
- CSRF hidden field.
- Fields (required unless stated): `billing_name`, `billing_email`, `billing_line1`, `billing_line2` (optional), `billing_city` (optional), `billing_state` (optional), `billing_postal_code`, `billing_country` (dropdown, comprehensive country list matching numerint).
- Pre-filled from `saved` dict; falls back to `default_name` / `default_email` from Xero session on first visit.
- Shows token count summary at top ("You are purchasing X tokens — £Y.YY").
- "Back" link → `url_for('buy_tokens')`.

---

## 7. `buy_tokens.html` change

- Form `action` → `url_for('buy_tokens_post')`, `method="POST"`.
- CSRF hidden field (required now that it's a POST).
- Button label: "Next: Billing Details →".

---

## Session Key

`session["pending_token_count"]` — set by `POST /buy-tokens`, consumed (popped) by `POST /api/checkout/create`. If missing at `GET /billing-details` or `POST /api/checkout/create`, redirect back to `/buy-tokens` with a message.

---

## Stripe Dashboard (manual, one-time)

Enable: **Settings → Billing → Invoices → "Email finalized invoices to customers"**. This is what actually triggers Stripe to email the invoice PDF to the billing email after payment. No code change.

---

## Tests to write

- `test_stripe_service.py`: `update_customer()` calls `stripe.Customer.modify` with correct args.
- `test_stripe_repository.py`: `cache_billing_details()` writes correct fields; `get_billing_details()` returns them; returns `None` when not set.
- `test_app_billing_details.py` (new): `POST /buy-tokens` stores token_count in session and redirects; missing/invalid token_count returns 400; `GET /billing-details` redirects to `/buy-tokens` when no pending_token_count in session; `POST /api/checkout/create` with missing required billing fields re-renders `billing_details.html` with error (and session key survives).

---

## Verification

1. `make dev` — linting, mypy, tests all pass.
2. Manual: buy-tokens → billing-details → Stripe test checkout → success page shows correct token credit.
3. Stripe Dashboard: Customer record shows updated name/email/address after purchase.
4. Second purchase: billing details form is pre-filled with values from first purchase.
5. Stripe Dashboard (after enabling invoice email): invoice PDF emailed to billing email.
