# Stripe One-Time Payments Implementation Plan

## Statement Processor — Token Purchase via Stripe Checkout

> **Scope:** One-time payments only. Subscriptions will be added later.
> Designed to avoid decisions that would make subscriptions hard to add.

---

## Table of Contents

1. [UX/UI Design](#1-uxui-design)
2. [Data Model Changes](#2-data-model-changes)
3. [Stripe Configuration](#3-stripe-configuration)
4. [Backend Implementation](#4-backend-implementation)
5. [Frontend Implementation](#5-frontend-implementation)
6. [Infrastructure / Deployment](#6-infrastructure--deployment)
7. [Testing Strategy](#7-testing-strategy)
8. [Implementation Order](#8-implementation-order)
9. [Future Subscription Considerations](#9-future-subscription-considerations)

---

## Design Decisions

### No webhook for MVP
The success redirect retrieves the Stripe session, verifies `payment_status == "paid"`, and credits tokens. Idempotency is handled via `StripeEventStoreTable` so a page refresh won't double-credit. If a user's browser closes before the redirect fires, tokens can be manually adjusted via the existing admin tool — acceptable for a small B2B user base. Webhooks can be added later when subscriptions require reliable async credit.

**Known edge case — auth expiry during checkout:** `@xero_token_required` redirects to `/login` with no `next` param. If the user's Xero session expires while they are on Stripe's hosted checkout page, the success redirect will hit `/login` and the `session_id` query param will be lost. Tokens must be credited manually via the admin tool. Webhooks (added for subscriptions) will eliminate this failure mode for one-time payments too, as crediting will happen server-side regardless of browser state.

### Dynamic `price_data` (not fixed Price objects)
Because pricing is a continuous quantity (N × price_per_token), we use `price_data` in the checkout session. One Stripe **Product** is created manually in the dashboard ("Statement Processor Tokens") and referenced by ID so purchase history is correctly attributed in Stripe's reporting. The Product ID is a plain env var (not a secret).

### Stripe handles billing details
No billing address form on our side. Stripe collects card details and any required address fields on its hosted page.

**Stripe Customer name/email:** The Xero user's email is not stored in the Flask session by default. The OAuth callback (`app.py`) already calls `oauth.xero.parse_id_token(tokens, nonce=nonce)` to validate the id_token, but discards its return value. The callback will be extended to capture that return value (`claims`) and store `claims.get("email", "")` as `session["xero_user_email"]`. No new dependencies or manual JWT decoding needed — Authlib has already validated the token at that point. The Stripe Customer is then created with `name=session["xero_tenant_name"]` and `email=session["xero_user_email"]`.

Note: `customer_email` cannot be set on a Checkout Session when `customer=id` is also provided — Stripe rejects both. So there is no separate email pre-fill on the session; the Customer's email covers that.

### Stripe Customer created pre-checkout
We search/create a Stripe Customer keyed on `metadata["tenant_id"]` and cache the ID in `TenantBillingTable.StripeCustomerID`. The ID is cached **at checkout creation time** (end of `POST /api/checkout/create`), not only on success, so the cache is populated even if the user abandons. The success route also attempts to cache it as a fallback. Subsequent purchases skip the Stripe search. This is essential for subscriptions (which require a persistent Customer) and means purchase history is attributed correctly in Stripe.

### No VAT
Not VAT-registered (correct for UK businesses below the £90k threshold). No `tax_rates` on line items. No country-based tax logic required.

### SSM for secrets only
Only the Stripe secret key is stored in SSM (SecureString). All non-secret config (`STRIPE_PRODUCT_ID`, `STRIPE_PRICE_PER_TOKEN_PENCE`, etc.) is stored as plain AppRunner environment variables and in `.env` for local dev.

---

## 1. UX/UI Design

### 1.1 Where the "Buy Tokens" CTA Appears

| Surface | Location | Trigger | Rationale |
|---------|----------|---------|-----------|
| **Primary** | `/tenant_management` page | Always visible next to the token balance chip | Users managing their workspace naturally think about "do I have enough?" here. Lowest-friction discovery point. |
| **Secondary** | Upload preflight shortfall message | When `is_sufficient` is `false` | Highest-intent moment — user is actively blocked. A "Buy Tokens" link in the red summary converts the dead-end into an action. |
| **Tertiary (deferred)** | Navbar | Always visible when logged in | Add later if analytics justify it. Not needed for MVP. |

### 1.2 End-to-End User Flow

```
1. User sees token balance on /tenant_management
   OR gets "short by N tokens" on upload preflight

2. User clicks "Buy Tokens"

3. GET /buy-tokens
   -> Shows current balance, token amount input, live price display

4. User enters token count (10–10,000) and clicks "Proceed to Payment"

5. POST /api/checkout/create
   -> Validate token_count (min 10, max 10,000)
   -> Get or create Stripe Customer
   -> Cache StripeCustomerID in TenantBillingTable immediately (opportunistic — avoids Stripe search on retry even if payment is abandoned)
   -> Create Stripe Checkout Session (mode="payment", price_data)
   -> Redirect to Stripe-hosted checkout page

6. Stripe: User enters card details (email pre-filled from Xero session)

7a. SUCCESS: Stripe redirects to /checkout/success?session_id={ID}
    -> Retrieve session from Stripe, verify payment_status == "paid"
    -> Check idempotency (StripeEventStoreTable)
    -> Credit tokens via BillingService.adjust_token_balance()
    -> Cache StripeCustomerID in TenantBillingTable
    -> Render success page with tokens credited and new balance

7b. CANCEL: Stripe redirects to /checkout/cancel
    -> Render cancellation page with "Try Again" link

7c. FAILURE: Server error during session creation
    -> Redirect to /checkout/failed with reference ID
```

### 1.3 Token Amount Input

- Free-form positive integer: **10 to 10,000 tokens** (minimum 10 = £1.00)
- Linear pricing: price = `token_count × STRIPE_PRICE_PER_TOKEN_PENCE`
- Live price display updated via JS as user types
- No fixed packages; pricing is transparent and predictable

### 1.4 Pages/Templates

| Template | Action | Purpose |
|----------|--------|---------|
| `templates/pricing.html` | **New** | Public-facing page explaining how token pricing works |
| `templates/buy_tokens.html` | **New** | Token amount input + live price display |
| `templates/checkout_success.html` | **New** | Payment confirmation with tokens credited and new balance |
| `templates/checkout_cancel.html` | **New** | Payment cancelled, retry CTA |
| `templates/checkout_failed.html` | **New** | Payment error with reference ID |
| `templates/tenant_management.html` | **Modify** | Add "Buy Tokens" button next to balance chip |
| `templates/upload_statements.html` | **Modify** | Add `data-buy-tokens-url` attribute on the upload form |
| `static/assets/js/upload-statements.js` | **Modify** | Render "Buy Tokens" link in shortfall message |

### 1.5 `pricing.html` — Public Pricing Page

A simple, public-facing page (no login required) that explains how token-based pricing works. Extends `base.html`. No JS needed — all content is static.

**Route:** `GET /pricing` — no `@xero_token_required` decorator so prospective customers can see it before signing up.

**Content sections:**

1. **Header** — "Simple, pay-as-you-go pricing."

2. **How it works** — three short bullet points:
   - Each PDF page costs **1 token**.
   - Tokens are purchased in advance and deducted as you upload.
   - Unused tokens roll over — they never expire.

3. **Pricing** — a single clear card (not a tier grid) showing:
   - **£0.10 per token** (10p per page)
   - Minimum purchase: **10 tokens = £1.00**
   - No subscription, no commitment — buy what you need

4. **Example table** — three illustrative scenarios to make the cost tangible:

   | Example | Pages | Tokens used | Cost |
   |---------|-------|-------------|------|
   | Small supplier statement | 2 | 2 | £0.20 |
   | Typical monthly statement | 8 | 8 | £0.80 |
   | Large annual statement | 30 | 30 | £3.00 |

5. **Disclaimers** (small text below the card):
   - Prices are exclusive of VAT. Not currently VAT-registered.
   - Tokens are non-refundable once purchased.

**Navbar:** Add "Pricing" link to `base.html` navbar between "About" and "Instructions" — active when `request.endpoint == 'pricing'`.

---

## 2. Data Model Changes

### 2.1 `StripeEventStoreTable` — existing table, used for idempotency

The CDK stack already defines this table and grants the AppRunner instance role read-write access. Used to prevent double-crediting if the user refreshes `/checkout/success`.

**Record written after tokens are credited:**

| Attribute | Type | Value |
|-----------|------|-------|
| `StripeEventID` (PK) | String | Checkout session ID (`cs_xxx`) |
| `EventType` | String | `"checkout.session.completed"` |
| `TenantID` | String | Tenant that purchased |
| `TokensCredited` | Number | Tokens granted |
| `LedgerEntryID` | String | `purchase#<session_id>` |
| `ProcessedAt` | String | ISO-8601 timestamp |

### 2.2 `TenantBillingTable` — one new optional attribute

| Attribute | Type | Description |
|-----------|------|-------------|
| `StripeCustomerID` | String (optional) | Cached Stripe customer ID — avoids repeated Stripe API search on subsequent purchases |

No other schema changes needed. The existing `TokenBalance`, `UpdatedAt`, `LastLedgerEntryID`, `LastMutationType`, and `LastMutationSource` fields support purchases without modification.

### 2.3 Billing Ledger — new source constant and ledger_entry_id param

Add to `billing_service.py`:

```python
LAST_MUTATION_SOURCE_STRIPE_CHECKOUT = "stripe-checkout"
```

`adjust_token_balance()` gains an optional `ledger_entry_id: str | None = None` keyword argument. When supplied (e.g. `purchase#<session_id>`), that value is used as the ledger entry ID instead of generating `f"adjustment#{uuid4()}"`. This:
- Fixes the audit cross-reference between `StripeEventStoreTable` and `TenantTokenLedgerTable` — a support engineer can look up `purchase#cs_xxx` in either table and find the same operation.
- Makes the ledger write conditionally idempotent via `attribute_not_exists` on the entry ID.

Call site: `adjust_token_balance(tenant_id, token_count, source=LAST_MUTATION_SOURCE_STRIPE_CHECKOUT, ledger_entry_id=f"purchase#{session_id}")`.

---

## 3. Stripe Configuration

### 3.1 Stripe Dashboard — Manual Steps

1. Create **Product**: name `Statement Processor Tokens`, type one-time
2. Note the Product ID (`prod_xxx`) → store as `STRIPE_PRODUCT_ID` env var
3. Create test-mode secret key → store in SSM `/StatementProcessor/StripeApiKey`
4. Enable Invoicing on the Stripe account (Dashboard → Settings → Billing → Invoices). Required because `create_checkout_session` uses `invoice_creation={"enabled": True}`.
5. (Live) Repeat with live-mode keys for production

No Price objects to create — pricing is fully dynamic via `price_data`.

### 3.2 Secrets (SSM SecureString)

| SSM Path | Purpose |
|----------|---------|
| `/StatementProcessor/StripeApiKey` | Stripe secret key (`sk_test_xxx` / `sk_live_xxx`) |

Only one new SSM secret. No webhook signing secret needed (no webhook endpoint).

### 3.3 Non-Secret Environment Variables

| Variable | Example Value | Purpose |
|----------|--------------|---------|
| `STRIPE_API_KEY_SSM_PATH` | `/StatementProcessor/StripeApiKey` | SSM path read by `config.py` at startup |
| `STRIPE_PRODUCT_ID` | `prod_xxx` | Stripe Product ID created in dashboard |
| `STRIPE_PRICE_PER_TOKEN_PENCE` | `10` | Price per token in pence (e.g. `10` = £0.10/token) |
| `STRIPE_CURRENCY` | `gbp` | Defaults to `gbp` |
| `STRIPE_MIN_TOKENS` | `10` | Minimum purchase quantity (£1.00 at 10p/token — Stripe's minimum charge for GBP is £0.30; 10 tokens gives a round minimum and room to lower the per-token price in future) |
| `STRIPE_MAX_TOKENS` | `10000` | Maximum purchase quantity |

These go in `.env` for local dev and in the CDK AppRunner `environment_variables` for deployment.

---

## 4. Backend Implementation

### 4.1 New Routes

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/buy-tokens` | `@xero_token_required` | Render token amount form |
| `POST` | `/api/checkout/create` | `@xero_token_required` | Validate amount, create Stripe session, redirect |
| `GET` | `/checkout/success` | `@xero_token_required` | Verify payment, credit tokens (idempotent), show confirmation |
| `GET` | `/checkout/cancel` | `@xero_token_required` | Show cancellation page |
| `GET` | `/checkout/failed` | `@xero_token_required` | Show error page with reference ID |

All use `@route_handler_logging`. No CSRF exemptions needed (no webhook endpoint).

### 4.2 New Files

| File | Purpose |
|------|---------|
| `service/stripe_service.py` | Stripe API wrapper: customer search/create, checkout session creation, session retrieval |
| `service/stripe_repository.py` | DynamoDB ops on `StripeEventStoreTable`: check/record processed sessions; read/write cached customer ID on `TenantBillingTable` |

No `stripe_config.py` or `stripe_models.py` needed — config is a small set of module-level constants in `stripe_service.py`.

### 4.3 `stripe_service.py`

```python
"""Stripe API interactions for token purchases.

All stripe SDK calls are encapsulated here so they can be mocked
in tests without patching the module globally.
"""

import stripe
from config import STRIPE_API_KEY, get_envar
from logger import logger

stripe.api_key = STRIPE_API_KEY

STRIPE_PRODUCT_ID: str = get_envar("STRIPE_PRODUCT_ID")
STRIPE_PRICE_PER_TOKEN_PENCE: int = int(get_envar("STRIPE_PRICE_PER_TOKEN_PENCE"))
STRIPE_CURRENCY: str = get_envar("STRIPE_CURRENCY", "gbp")
STRIPE_MIN_TOKENS: int = int(get_envar("STRIPE_MIN_TOKENS", "10"))
STRIPE_MAX_TOKENS: int = int(get_envar("STRIPE_MAX_TOKENS", "10000"))


class StripeService:
    """Encapsulate Stripe API calls for customer management and checkout."""

    def get_or_create_customer(
        self, *, tenant_id: str, name: str, email: str = ""
    ) -> str:
        """Search Stripe for a customer by tenant_id metadata; create if not found.

        Keying on tenant_id (not email) means multiple Xero users in the
        same organisation share one Stripe customer, which is correct for
        subscriptions added later.

        name: session["xero_tenant_name"] (org name).
        email: session["xero_user_email"] — extracted from the validated id_token
               JWT payload in the OAuth callback. Empty string if not set.
        """
        results = stripe.Customer.search(
            query=f'metadata["tenant_id"]:"{tenant_id}"'
        )
        if results.data:
            return results.data[0].id

        customer = stripe.Customer.create(
            name=name,
            email=email,
            metadata={"tenant_id": tenant_id},
        )
        logger.info(
            "Created Stripe customer",
            tenant_id=tenant_id,
            stripe_customer_id=customer.id,
        )
        return customer.id

    def create_checkout_session(
        self,
        *,
        customer_id: str,
        token_count: int,
        tenant_id: str,
        success_url: str,
        cancel_url: str,
    ) -> stripe.checkout.Session:
        """Create a Stripe Checkout Session for a one-time token purchase.

        Uses price_data (dynamic pricing) rather than fixed Price objects
        because token count is a free-form integer. The total unit_amount
        is the full price for the purchase (token_count × price_per_token).
        Metadata carries tenant_id and token_count for the success route.
        """
        unit_amount = token_count * STRIPE_PRICE_PER_TOKEN_PENCE
        return stripe.checkout.Session.create(
            customer=customer_id,
            mode="payment",
            invoice_creation={"enabled": True},
            billing_address_collection="auto",
            line_items=[
                {
                    "price_data": {
                        "currency": STRIPE_CURRENCY,
                        "product": STRIPE_PRODUCT_ID,
                        "unit_amount": unit_amount,
                    },
                    "quantity": 1,
                }
            ],
            metadata={
                "tenant_id": tenant_id,
                "token_count": str(token_count),
            },
            success_url=success_url,
            cancel_url=cancel_url,
        )

    def retrieve_session(self, session_id: str) -> stripe.checkout.Session:
        """Retrieve a checkout session by ID (called on success return)."""
        return stripe.checkout.Session.retrieve(session_id)
```

### 4.4 `stripe_repository.py`

```python
"""DynamoDB operations for Stripe checkout state.

Provides idempotent processing records on StripeEventStoreTable
and caches the Stripe customer ID on TenantBillingTable.
"""

from config import ddb, get_envar
from datetime import datetime, timezone

_event_store = ddb.Table(get_envar("STRIPE_EVENT_STORE_TABLE_NAME"))
_billing_table = ddb.Table(get_envar("TENANT_BILLING_TABLE_NAME"))


class StripeRepository:
    """Manages Stripe-related state in DynamoDB."""

    @classmethod
    def is_session_processed(cls, session_id: str) -> bool:
        """Return True if this checkout session has already been processed."""
        ...

    @classmethod
    def record_processed_session(
        cls,
        *,
        session_id: str,
        tenant_id: str,
        tokens_credited: int,
        ledger_entry_id: str,
    ) -> None:
        """Record a completed checkout session to prevent double-crediting on page refresh."""
        ...

    @classmethod
    def get_processed_session(cls, session_id: str) -> dict | None:
        """Retrieve a processed session record (to show success page on refresh)."""
        ...

    @classmethod
    def get_cached_customer_id(cls, tenant_id: str) -> str | None:
        """Read StripeCustomerID from TenantBillingTable. Returns None if not set."""
        ...

    @classmethod
    def cache_customer_id(cls, tenant_id: str, stripe_customer_id: str) -> None:
        """Write StripeCustomerID to TenantBillingTable for future checkout sessions."""
        ...
```

### 4.5 `POST /api/checkout/create` Route Logic

```python
@app.route("/api/checkout/create", methods=["POST"])
@xero_token_required
@route_handler_logging
def checkout_create():
    tenant_id = session.get("xero_tenant_id")
    token_count_raw = request.form.get("token_count", "").strip()

    # Validate input — re-render form on error (this is a form POST, not AJAX;
    # JSON responses would render as raw text in the browser).
    try:
        token_count = int(token_count_raw)
    except (ValueError, TypeError):
        return render_template(
            "buy_tokens.html",
            error="Please enter a valid number of tokens.",
            min_tokens=STRIPE_MIN_TOKENS,
            max_tokens=STRIPE_MAX_TOKENS,
            price_pence=STRIPE_PRICE_PER_TOKEN_PENCE,
        ), 400

    if not (STRIPE_MIN_TOKENS <= token_count <= STRIPE_MAX_TOKENS):
        return render_template(
            "buy_tokens.html",
            error=f"Token count must be between {STRIPE_MIN_TOKENS} and {STRIPE_MAX_TOKENS}.",
            min_tokens=STRIPE_MIN_TOKENS,
            max_tokens=STRIPE_MAX_TOKENS,
            price_pence=STRIPE_PRICE_PER_TOKEN_PENCE,
        ), 400

    # Get or create Stripe Customer; cache ID immediately (before checkout)
    # so future sessions skip the Stripe search even if this one is abandoned.
    cached_customer_id = StripeRepository.get_cached_customer_id(tenant_id)
    if cached_customer_id:
        customer_id = cached_customer_id
    else:
        customer_id = stripe_service.get_or_create_customer(
            tenant_id=tenant_id,
            name=session.get("xero_tenant_name", tenant_id),
            email=session.get("xero_user_email", ""),
        )
        StripeRepository.cache_customer_id(tenant_id, customer_id)

    # Build URLs — {CHECKOUT_SESSION_ID} is a Stripe template literal substituted
    # by Stripe before redirecting the browser to the success page.
    success_url = url_for("checkout_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}"
    cancel_url = url_for("checkout_cancel", _external=True)

    try:
        stripe_session = stripe_service.create_checkout_session(
            customer_id=customer_id,
            token_count=token_count,
            tenant_id=tenant_id,
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except stripe.StripeError:
        logger.exception("Failed to create Stripe checkout session", tenant_id=tenant_id)
        ref = secrets.token_hex(8)
        return redirect(url_for("checkout_failed", ref=ref))

    return redirect(stripe_session.url, code=303)
```

---

### 4.6 `/checkout/success` Route Logic

```python
@app.route("/checkout/success")
@xero_token_required
@route_handler_logging
def checkout_success():
    tenant_id = session.get("xero_tenant_id")
    session_id = request.args.get("session_id", "").strip()
    if not session_id:
        return redirect(url_for("checkout_failed"))

    # Idempotency: already processed? Show success without re-crediting.
    if StripeRepository.is_session_processed(session_id):
        record = StripeRepository.get_processed_session(session_id)
        if record:
            new_balance = TenantBillingRepository.get_tenant_token_balance(tenant_id)
            return render_template("checkout_success.html",
                tokens_credited=int(record["TokensCredited"]),
                new_balance=new_balance)
        # record is None: tiny race window between is_session_processed and
        # get_processed_session — fall through to normal processing path.

    # Retrieve session from Stripe and verify payment
    try:
        stripe_session = stripe_service.retrieve_session(session_id)
    except stripe.StripeError:
        logger.exception("Failed to retrieve Stripe session", session_id=session_id)
        return redirect(url_for("checkout_failed"))

    if stripe_session.payment_status != "paid":
        return redirect(url_for("checkout_failed"))

    # Security: verify the session belongs to the authenticated tenant.
    # Prevents a user who obtains another tenant's session_id from crediting
    # the wrong account.
    session_tenant_id = stripe_session.metadata.get("tenant_id")
    if session_tenant_id != tenant_id:
        logger.warning("Session tenant_id mismatch", session_id=session_id,
                       session_tenant_id=session_tenant_id, auth_tenant_id=tenant_id)
        return redirect(url_for("checkout_failed"))

    token_count = int(stripe_session.metadata["token_count"])

    # Credit tokens. ledger_entry_id ties this ledger row to the Stripe session
    # in StripeEventStoreTable, enabling audit cross-reference and making the
    # ledger write conditionally idempotent via attribute_not_exists.
    ledger_entry_id = f"purchase#{session_id}"
    BillingService.adjust_token_balance(
        tenant_id, token_count,
        source=LAST_MUTATION_SOURCE_STRIPE_CHECKOUT,
        ledger_entry_id=ledger_entry_id,
    )

    # Cache Stripe customer ID for future checkouts (idempotent — UpdateItem
    # with same value is harmless; checkout create already cached it but this
    # acts as a fallback if create was skipped or failed before caching).
    if stripe_session.customer:
        StripeRepository.cache_customer_id(tenant_id, stripe_session.customer)

    # Mark as processed
    StripeRepository.record_processed_session(
        session_id=session_id, tenant_id=tenant_id,
        tokens_credited=token_count, ledger_entry_id=ledger_entry_id
    )

    new_balance = TenantBillingRepository.get_tenant_token_balance(tenant_id)
    return render_template("checkout_success.html",
        tokens_credited=token_count, new_balance=new_balance)
```

### 4.6 Preflight Modification

`statement_upload_validation.py` needs no changes for this feature. Instead, inject `buy_tokens_url` in the `upload_statements_preflight` route handler in `app.py` after calling `to_response_payload()`:

```python
payload = preflight_result.to_response_payload()
if preflight_result.shortfall > 0:
    payload["buy_tokens_url"] = url_for("buy_tokens")
return jsonify(payload), 200
```

This avoids giving `to_response_payload()` knowledge of Flask's URL routing (which would require app context) and keeps the validation model clean.

---

## 5. Frontend Implementation

### 5.1 `tenant_management.html`

Add "Buy Tokens" button next to the existing balance chip (line 23):

```html
<span class="tenant-current-chip tenant-token-chip">
  Available tokens: <strong>{{ ct_token_balance }}</strong>
</span>
<a class="btn btn-outline-primary btn-sm" href="{{ url_for('buy_tokens') }}">
  Buy Tokens
</a>
```

### 5.2 `buy_tokens.html` (New)

Extends `base.html`. Contains:

- Current token balance display
- Number input: `min`, `max`, and `data-price-pence` / `data-min-tokens` / `data-max-tokens` are passed as template variables from the route handler (read from `stripe_service` module constants), not hardcoded in the template:

```html
<input type="number"
  min="{{ min_tokens }}"
  max="{{ max_tokens }}"
  data-price-pence="{{ price_pence }}"
  data-min-tokens="{{ min_tokens }}"
  data-max-tokens="{{ max_tokens }}"
>
```

- Live price display line, e.g. `"50 tokens = £5.00"` — updated by JS as user types using `data-price-pence`
- "Proceed to Payment" submit button → POST to `/api/checkout/create`
- CSRF hidden field (`{{ csrf_token() }}`)

No billing address fields — Stripe Checkout handles all payment details.

### 5.3 Checkout Result Templates (New)

All extend `base.html` using the existing `page-main` / `page-header` / `page-panel` structure.

**`checkout_success.html`**: Confirmation message, tokens purchased, updated balance, "Upload Statements" and "Back to Tenant Management" links.

**`checkout_cancel.html`**: "Payment was cancelled." "Try Again" → `/buy-tokens`, "Back to Tenant Management" link.

**`checkout_failed.html`**: Error message, reference ID (checkout session ID or UUID), "Try Again" and "Back to Tenant Management" links.

### 5.4 `upload-statements.js` — Shortfall Link

Modify the shortfall display to include a "Buy Tokens" link. The URL comes from a `data-buy-tokens-url` attribute set by the template (so `url_for` is used server-side, not hardcoded in JS):

```javascript
// In upload_statements.html:
// <form id="upload-form" data-buy-tokens-url="{{ url_for('buy_tokens') }}" ...>

if (!payload.is_sufficient) {
    const buyUrl = uploadForm.dataset.buyTokensUrl || "/buy-tokens";
    // Integer values from server — no XSS risk with innerHTML here
    uploadPreflightSummary.innerHTML =
        `Server confirmed ${totalPages} ${pageLabel}. ` +
        `${availableTokens} ${tokenLabel} available, short by ${shortfall}. ` +
        `<a href="${buyUrl}" class="btn btn-sm btn-outline-primary ms-2">Buy Tokens</a>`;
    uploadPreflightSummary.dataset.preflightState = "error";
    return;
}
```

---

## 6. Infrastructure / Deployment

### 6.1 CDK Stack Changes (`cdk/stacks/statement_processor.py`)

Add to the AppRunner `environment_variables` dict:

```python
# Stripe — secret key fetched from SSM at startup
"STRIPE_API_KEY_SSM_PATH": "/StatementProcessor/StripeApiKey",

# Stripe — non-secret config (plain env vars)
"STRIPE_PRODUCT_ID": "prod_xxx",
"STRIPE_PRICE_PER_TOKEN_PENCE": "10",
"STRIPE_CURRENCY": "gbp",
"STRIPE_MIN_TOKENS": "10",
"STRIPE_MAX_TOKENS": "10000",
```

**IAM:** No changes needed. The `StripeEventStoreTable` already has `grant_read_write_data` on the instance role. The SSM `GetParameters` policy already uses wildcard `/StatementProcessor/*`.

### 6.2 `config.py` Changes

Extend `_fetch_ssm_secrets()` to include `STRIPE_API_KEY_SSM_PATH` in the existing batched `GetParameters` call. Export:

```python
STRIPE_API_KEY: str = _secrets[get_envar("STRIPE_API_KEY_SSM_PATH")]
```

The non-secret values (`STRIPE_PRODUCT_ID`, `STRIPE_PRICE_PER_TOKEN_PENCE`, etc.) are read directly via `get_envar()` in `stripe_service.py` at module load time.

### 6.3 SSM Parameter to Create

```bash
aws ssm put-parameter \
  --name "/StatementProcessor/StripeApiKey" \
  --type SecureString \
  --value "sk_test_xxx"
```

### 6.4 Requirements

Add to `service/requirements.txt`:

```
stripe
```

Then run `make update-venv`.

---

## 7. Testing Strategy

### 7.1 Unit Tests

| Test File | What It Tests |
|-----------|--------------|
| `tests/test_stripe_service.py` | Customer search/create logic, checkout session creation (correct `price_data` calculation), session retrieval. Mock `stripe` SDK. |
| `tests/test_stripe_repository.py` | Idempotency check, session recording, customer ID cache read/write. Mock DynamoDB. |
| `tests/test_billing_service.py` (extend) | Token adjustment with `source="stripe-checkout"`. Verify ledger entry gets correct source. |

### 7.2 Integration Testing

- Use Stripe test key + test card `4242 4242 4242 4242` for happy path
- Test card `4000 0000 0000 0002` for declined card
- Verify token balance increases after successful checkout
- Use Stripe Dashboard "Checkout Sessions" view to confirm sessions are created correctly

### 7.3 Key Scenarios

| Scenario | Expected Outcome |
|----------|-----------------|
| Happy path: enter 50 tokens, complete checkout | 50 tokens credited, balance updated, ledger entry with `source="stripe-checkout"` |
| Refresh `/checkout/success` after purchase | Second visit shows success page without re-crediting (idempotency) |
| Cancel at Stripe checkout | No tokens credited, cancel page shown |
| Server error during session creation | Redirected to `/checkout/failed` with reference ID |
| Token count < 10 or > 10,000 in POST | 400 error, no Stripe API call |
| First purchase (no cached customer) | New Stripe customer created, ID cached in `TenantBillingTable` |
| Second purchase (cached customer) | Existing customer reused, no Stripe Customer search |
| Preflight with shortfall | "Buy Tokens" link appears in red summary |
| Token count = 10 (minimum) | Works: £1.00 purchase |
| Token count = 10,000 (maximum) | Works: £1,000.00 purchase |

---

## 8. Implementation Order

### Phase 1: Foundation
1. Add `stripe` to `service/requirements.txt`, run `make update-venv`
1a. Create SSM param `/StatementProcessor/StripeApiKey` (test-mode key) — must exist before config.py tries to fetch it
1b. Add `STRIPE_API_KEY_SSM_PATH=/StatementProcessor/StripeApiKey` to `.env` — must be set before `config.py` is loaded locally
1c. Extend `config.py` to load `STRIPE_API_KEY` from SSM (safe now that both the param and the env var path exist)
1d. Extend OAuth callback in `app.py`: capture return value of `oauth.xero.parse_id_token()` as `claims` and store `claims.get("email", "")` as `session["xero_user_email"]`
2. Create Stripe Product in dashboard, note Product ID
3. Add `LAST_MUTATION_SOURCE_STRIPE_CHECKOUT` to `billing_service.py`
4. Add optional `ledger_entry_id` param to `adjust_token_balance()`

### Phase 2: Stripe Layer
6. Create `service/stripe_service.py`
7. Create `service/stripe_repository.py`

### Phase 3: Routes and Templates
8. Add `GET /pricing` route (no auth) + `pricing.html`; add "Pricing" link to `base.html` navbar
9. Add `GET /buy-tokens` route + `buy_tokens.html`
10. Add `POST /api/checkout/create` route
11. Add `GET /checkout/success` route + `checkout_success.html`
12. Add `GET /checkout/cancel` + `GET /checkout/failed` routes + templates

### Phase 4: UI Integration
13. Modify `tenant_management.html` — "Buy Tokens" button
14. Inject `buy_tokens_url` in `upload_statements_preflight` route handler in `app.py` (no changes to `statement_upload_validation.py`)
15. Modify `upload_statements.html` — add `data-buy-tokens-url`
16. Modify `upload-statements.js` — add "Buy Tokens" link in shortfall message

### Phase 5: Infrastructure and Tests
17. Update CDK stack with Stripe env vars
18. Update `.env` with local dev values
19. Write unit tests (`test_stripe_service.py`, `test_stripe_repository.py`)
20. Run `make dev` — verify formatting, linting, tests pass

### Phase 6: Documentation
21. Update `README.md` with:
    - Stripe setup section (create Product in dashboard, SSM parameter)
    - New environment variables table (with descriptions)
    - Local dev setup instructions for Stripe

---

## 9. Future Subscription Considerations

| Decision | Why It Helps Subscriptions |
|----------|---------------------------|
| **Single Stripe Product** referenced by ID | Subscription recurring Prices attach to the same Product — no duplication |
| **`StripeCustomerID` cached per tenant** | Subscriptions require a persistent Customer; already in place |
| **`StripeEventStoreTable` is generic** (keyed by event ID) | Subscription webhooks (`invoice.paid`) use the same idempotency store |
| **Webhook endpoint (no auth required)** will credit tokens server-side | Eliminates the auth-expiry edge case for one-time payments too — crediting no longer depends on the browser completing the redirect |
| **`source` field on ledger mutations** | `"stripe-checkout"` vs future `"stripe-subscription"` distinguishable in audit log |
| **Token count in session metadata** | Pattern naturally extends to subscription credits stored in invoice metadata |
| **No billing address form on our side** | Stripe Checkout already has customer details; billing portal can be added for subscription management |

**What to avoid:**
- Do not tie the Stripe Customer to a Xero *user* — it is tied to the **tenant**. Subscriptions are per-tenant.
- Do not store token balances in Stripe metadata — `TenantBillingTable` is the single source of truth.
- Keep checkout session creation in a service method that accepts `mode` as a parameter, so `mode="subscription"` is a future call-site change, not a rewrite.

---

## Critical Files

| File | Change |
|------|--------|
| `service/requirements.txt` | Add `stripe` |
| `service/config.py` | Load `STRIPE_API_KEY` from SSM in `_fetch_ssm_secrets()` |
| `service/stripe_service.py` | **New** — Stripe API wrapper |
| `service/stripe_repository.py` | **New** — DynamoDB ops for checkout state |
| `service/app.py` | OAuth callback: extract `xero_user_email` from id_token claims; add 6 new routes; inject `buy_tokens_url` in preflight route handler |
| `service/billing_service.py` | Add `LAST_MUTATION_SOURCE_STRIPE_CHECKOUT` constant; add optional `ledger_entry_id` param to `adjust_token_balance()` |
| `service/utils/statement_upload_validation.py` | No change needed (URL injected in route handler) |
| `templates/pricing.html` | **New** — public pricing explanation page |
| `templates/buy_tokens.html` | **New** |
| `templates/checkout_success.html` | **New** |
| `templates/checkout_cancel.html` | **New** |
| `templates/checkout_failed.html` | **New** |
| `templates/base.html` | Add "Pricing" nav link |
| `templates/tenant_management.html` | Add "Buy Tokens" button |
| `templates/upload_statements.html` | Add `data-buy-tokens-url` attribute |
| `static/assets/js/upload-statements.js` | Shortfall message with "Buy Tokens" link |
| `cdk/stacks/statement_processor.py` | Add Stripe env vars to AppRunner config |
| `.env` | Add local dev Stripe values |
| `README.md` | Document Stripe setup and env vars |
