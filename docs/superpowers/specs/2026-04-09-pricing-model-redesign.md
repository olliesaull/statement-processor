# Pricing Model Redesign

**Date:** 2026-04-09
**Status:** Approved
**Phases:** Two — Phase 1 (pay-as-you-go enhancements), Phase 2 (subscriptions)
**Plan documents:** `plans/pricing-phase-1.md`, `plans/pricing-phase-2.md`

---

## Context

The app currently uses a simple pay-as-you-go token model: 1 token = 1 PDF page, priced at a flat £0.10/token. Users pick any quantity (10-10,000) and pay via one-time Stripe Checkout. There is no subscription offering.

Adding subscriptions serves two goals:
1. **Revenue predictability** — recurring monthly payments and provable MRR, important if ever selling the solution to Xero or raising.
2. **Retention** — subscribers keep paying by default; the friction is in cancelling, not in re-purchasing.

The redesign keeps pay-as-you-go as a fallback/top-up option while making subscriptions the more attractive choice through tiered per-token discounts.

### Cost basis

Processing uses Claude Haiku 4.5 via Bedrock. Per-page AWS costs (Bedrock + S3 + DynamoDB + Lambda + Step Functions):

| Document size | Approx cost/page |
|---|---|
| 1-page (worst case) | ~£0.027 |
| 5-page | ~£0.005 |
| 10-page | ~£0.003 |

Even at the deepest subscription discount (£0.07/page), worst-case margins are ~61%. Typical margins are 90%+.

> **Note:** Bedrock S3 invocation logs from April 2026 show `claude-sonnet-4-6` — these are from a prior period before the switch to Haiku 4.5. The codebase hardcodes `eu.anthropic.claude-haiku-4-5-20251001-v1:0`.

---

## Phase 1: Pay-as-you-go Enhancements

### 1.1 Graduated volume pricing

Replace the flat £0.10/token rate with graduated pricing on one-off purchases:

| Token range | Price per token |
|---|---|
| 1-499 | £0.10 |
| 500-999 | £0.09 |
| 1,000+ | £0.08 |

**Graduated, not flat-rate:** The first 499 tokens are always at £0.10 regardless of total quantity. Only tokens above each breakpoint get the discounted rate. This avoids price cliffs where buying 501 tokens would be cheaper than buying 499.

**Example:** 1,200 tokens = (499 x £0.10) + (500 x £0.09) + (201 x £0.08) = £49.90 + £45.00 + £16.08 = **£110.98** (effective rate: £0.0925/token).

### 1.2 Pricing config as single source of truth

The graduated tier breakpoints and rates must be defined in **one place** and shared between Python (server-side validation, Stripe session creation) and JavaScript (live price calculator on buy-tokens page).

Approach: define pricing config in Python (Pydantic model or similar), serialise to JSON, inject into templates via a context processor. The JavaScript reads the same data from a data attribute or inline JSON block — the same pattern used for the colour scheme.

This avoids maintaining duplicate pricing logic in Python and JavaScript.

### 1.3 Persistent Stripe Customer

**Current:** A new Stripe Customer is created per purchase. No persistent record.

**New:** One persistent Stripe Customer per tenant.

- On first purchase, create a Stripe Customer and store `StripeCustomerID` on `TenantBillingTable`.
- On subsequent purchases, reuse that customer. Billing details update the existing customer (last-write-wins).
- Pre-fill billing details from Stripe on repeat purchases.

**Last-write-wins rationale:** Different people within a multi-user tenant may purchase tokens. The Stripe Customer represents the tenant, not the individual. Invoices are immutable and snapshot billing details at creation time, so the historical audit trail is always correct regardless of what the customer record currently shows.

**Data change:** Add `StripeCustomerID` attribute to `TenantBillingTable`.

### 1.4 Token ledger enhancement

Add `PricePerTokenPence` (Number, optional) to `TenantTokenLedgerTable` entries.

- Recorded on every token **grant** (ADJUSTMENT entries from purchases and welcome grants).
- Not recorded on RESERVE/CONSUME/RELEASE entries (usage, not purchases).
- For graduated pricing, stores the **effective rate**: total price in pence / token count, rounded to 2 decimal places.
- For welcome grants, this is 0 (free).
- Enables revenue reporting: `sum(TokenDelta x PricePerTokenPence)` across purchase entries = total revenue.

**Design decision:** We chose to store a single effective rate rather than individual ledger entries per graduated tier. This simplifies the ledger — the Stripe invoice is authoritative for exact per-tier breakdowns if ever needed. The rounding to 2dp means reconstructing total price from `tokens x effective_rate` may differ by a few pence from the actual Stripe charge; the Stripe invoice is always the source of truth for exact amounts.

**Backwards compatibility:** The code must handle ledger entries without `PricePerTokenPence` gracefully (treat as optional/nullable). A fresh AWS organisation is planned before production launch (all tables empty), but this deployment may land before that migration.

### 1.5 Buy tokens UX improvements

**Current:** "Buy Tokens" is a small `btn-outline-primary btn-sm` in the tenant management header bar, easily missed among other controls.

**Changes:**

1. **Remove** the "Buy Tokens" button from the tenant management header bar.
2. **Add** a "Buy Tokens" button in the Actions column of each tenant row.
3. Clicking it navigates to `/buy-tokens?tenant_id=<id>`. The query param **pre-selects the dropdown only** — it does NOT switch the session tenant. No state change on GET (avoids CSRF concerns and side effects from browser prefetch, link sharing, etc.).
4. On the buy-tokens page, add a **tenant selector dropdown** showing all connected tenants, pre-filled with the tenant from the query param (or current tenant if navigated directly).
5. The actual tenant context is determined at the point of purchase (POST to create checkout session). The server validates the selected tenant exists in `session["xero_tenants"]` before proceeding — same access check pattern as `select_tenant`.
6. Make it **visually prominent** which tenant is being purchased for.
7. **Keep** the token balance chip in the header bar (read-only, useful at a glance).
8. **Keep** the "insufficient tokens" prompt on upload (already works well).

**Nginx:** Add `tenant_id` to `nginx_route_querystring_allow_list.json` for the `/buy-tokens` route.

### 1.6 Pricing page update

Update the pricing page to show graduated volume pricing. Use "from" wording to keep it simple:

> | Tokens | Price per token |
> |---|---|
> | Up to 499 | £0.10 |
> | 500+ | from £0.09 |
> | 1,000+ | from £0.08 |

The live price calculator on the buy-tokens page shows the exact total — the pricing page doesn't need to explain graduated mechanics.

---

## Phase 2: Subscriptions

### 2.1 Subscription tiers

| Tier | Tokens/month | Price/token | Monthly price |
|---|---|---|---|
| TBD name | 50 | £0.09 | £4.50 |
| TBD name | 200 | £0.08 | £16.00 |
| TBD name | 500 | £0.07 | £35.00 |

**No perverse incentives:** At these prices, upgrading to a higher tier is never cheaper unless the customer actually needs the tokens. E.g., someone needing 51 tokens pays £16.00 on the 200 tier vs £4.50 + one-off top-up — the lower tier + top-up is cheaper.

**Rolling balance:** Unused subscription tokens roll over indefinitely. They are added to the same token balance as pay-as-you-go purchases. The ledger doesn't distinguish — it already tracks `Source` per entry.

**Future consideration:** Capped rollover (option C) can be introduced later for new subscribers. Existing subscribers would be grandfathered on rolling balance. The immutable ledger supports this — a check at renewal time against accumulated balance is all that's needed.

### 2.2 Subscription + pay-as-you-go interaction

Subscribers can buy one-off top-ups at **pay-as-you-go rates** (graduated pricing from Phase 1), not at their tier rate.

**Rationale:** Offering top-ups at the tier rate is gameable — someone could subscribe to the 500 tier for one month, buy thousands of extra tokens at £0.07, and cancel. Pay-as-you-go rates for top-ups preserve the subscription value proposition.

### 2.3 Stripe implementation

**Stripe Subscriptions (native)**, not self-managed recurring charges.

- One Stripe Product (e.g., "Statement Processor Subscription")
- Three Stripe Prices (one per tier): fixed monthly recurring (£4.50, £16.00, £35.00)
- Subscription attached to the persistent Stripe Customer (from Phase 1)

**Token crediting flow:**

1. Customer subscribes via Stripe Checkout in `mode="subscription"`
2. Stripe charges immediately and emits `invoice.paid` webhook
3. Webhook endpoint verifies the event, extracts tenant ID and tier from metadata
4. Calls `BillingService.adjust_token_balance()` with tier token count, source `"stripe-subscription"`, `PricePerTokenPence` set to the tier rate
5. Ledger entry ID: `subscription#{invoice_id}` for idempotency
6. Same flow repeats each billing cycle automatically

### 2.4 Webhook endpoint

**New route:** `POST /api/stripe/webhook` on App Runner.

- Validates Stripe webhook signature (signing secret from SSM Parameter Store)
- No auth decorator — Stripe calls it directly; signature verification replaces authentication
- Added to nginx config

**Implementation note:** Signature verification must use the **raw request body** (`request.get_data()`), not parsed JSON (`request.json`). Parsing may reorder keys, invalidating the signature. Use `stripe.Webhook.construct_event(payload, sig_header, webhook_secret)`.

**Handled events:**

| Event | Action |
|---|---|
| `invoice.paid` | Credit tokens to tenant balance |
| `customer.subscription.updated` | Update cached subscription state |
| `customer.subscription.deleted` | Mark subscription as cancelled |

**Why App Runner, not API Gateway + Lambda:**

1. App Runner is already always-on and handles HTTP requests. A webhook is just another request.
2. API Gateway + Lambda would introduce a new API Gateway resource, separate Lambda, separate logging/monitoring, and a second deployment path for billing logic that already lives in the service.
3. **Stripe retries failed webhook deliveries for up to 3 days with exponential backoff.** Brief App Runner outages or deployments are handled automatically.
4. The billing logic (`BillingService.adjust_token_balance()`) lives in the Flask app. Using Lambda would mean duplicating or cross-importing it.
5. Idempotency via `StripeEventStoreTable` (existing pattern) makes duplicate deliveries safe.

### 2.5 Subscription state on DynamoDB

New attributes on `TenantBillingTable`:

| Attribute | Type | Purpose |
|---|---|---|
| `SubscriptionTierID` | String | Which tier (e.g., "tier_50", "tier_200", "tier_500") |
| `SubscriptionStatus` | String | "active", "cancelled", "past_due" |
| `StripeSubscriptionID` | String | Stripe subscription ID for API calls |
| `SubscriptionCurrentPeriodEnd` | String | ISO-8601, for display ("renews on...") |

| `TokensCreditedThisPeriod` | Number | Tokens already granted in current billing cycle |

These are **informational cache** — Stripe is the source of truth. Cached so the app can show subscription status without calling Stripe on every page load. Updated via webhooks.

**Tier change token crediting:**

- **Normal renewal:** `TokensCreditedThisPeriod` resets to 0, credit full tier amount.
- **Upgrade (proration):** credit `new_tier_tokens - TokensCreditedThisPeriod` (e.g., upgrading from 50 to 200 mid-cycle credits 150 additional tokens). Customer always ends up with their new tier's full token count for the period.
- **Downgrade:** credit 0 — tokens already granted exceed the new tier. No clawback (those tokens were paid for and possibly used). Next renewal credits the lower tier amount.
- `TokensCreditedThisPeriod` is updated after every token grant and reset on each new billing period.

### 2.6 Tenant disconnection with active subscription

Subscriptions are **not** cancelled on tenant disconnection — only when the tenant's data is actually erased. This preserves the subscription (and token accumulation) during the grace period, so reconnecting tenants find everything intact.

- The **tenant erasure lambda** cancels the Stripe Subscription (if active) as part of its cleanup, before deleting S3/statement data. It reads `StripeSubscriptionID` from `TenantBillingTable` and calls `stripe.Subscription.cancel()`.
- The erasure lambda needs access to the Stripe API key (SSM parameter) — requires a CDK change to grant the permission.
- The **disconnect modal** should warn: "Your subscription will remain active during the grace period. It will be cancelled when your data is deleted."
- The **FAQ** should document this behaviour.

### 2.7 Subscription guards

- Before creating a subscription checkout session, check `SubscriptionStatus` on `TenantBillingTable`. If `active` or `past_due`, redirect to subscription management (or Stripe Customer Portal) instead of allowing a new subscription.
- The UI should show "Manage Subscription" instead of "Subscribe" when an active subscription exists.
- Below the tier options, include a contact prompt for customers who need more than the largest tier: e.g., "Need more than 500 tokens/month? Contact us at [email]."

### 2.7 Customer management

- **In-app:** Show current tier, renewal date, and token balance on tenant management page.
- **Stripe Customer Portal:** Link out for upgrade/downgrade, cancellation, payment method updates, invoice history. Configured in Stripe Dashboard.

---

## Data changes summary

### Phase 1

| Table | Change |
|---|---|
| `TenantBillingTable` | Add `StripeCustomerID` (String) |
| `TenantTokenLedgerTable` | Add `PricePerTokenPence` (Number, optional) |

No schema migrations needed (DynamoDB is schemaless). Old entries without `PricePerTokenPence` handled gracefully.

### Phase 2

| Table | Change |
|---|---|
| `TenantBillingTable` | Add `SubscriptionTierID`, `SubscriptionStatus`, `StripeSubscriptionID`, `SubscriptionCurrentPeriodEnd` |

**Stripe setup (one-time):**
- Create Product + 3 Prices in Stripe Dashboard (or via script)
- Add webhook signing secret to SSM Parameter Store
- Configure Stripe Customer Portal in Stripe Dashboard

---

## UI naming: "pages" not "tokens"

The backend uses "tokens" as an abstract billing unit (1 token = 1 PDF page today, but the ratio could change). The **user-facing UI uses "pages"** exclusively — customers think in terms of PDF pages, and adding a "token" abstraction creates unnecessary cognitive load.

**Rules:**
- All customer-visible text says "pages" (templates, JS messages, error messages, `llms.md`, pricing page, etc.)
- All backend code, DynamoDB attributes, ledger entries, logs, and internal variable names continue to say "tokens"
- README and developer docs note that "tokens are called pages in the UI"
- Flask route URLs (`/buy-tokens`, `url_for('buy_tokens')`) stay as-is — they're internal identifiers, not customer-visible text

**Scope:** ~37 user-facing string occurrences across templates, JS, Python error messages, `llms.md`, and the welcome banner.

---

## Decisions log

| Decision | Choice | Rationale |
|---|---|---|
| Token rollover | Rolling balance (no expiry) | Customer acquisition priority; generous policy; migration path to capped rollover exists |
| Discount model | Tiered per-tier (not flat) | Incentivises larger commitments; real savings at higher tiers |
| One-off pricing | Graduated (not flat-rate) | Avoids price cliffs; fair; naturally less attractive than subscriptions |
| Ledger price tracking | Single effective rate, not per-tier entries | Simpler; Stripe invoice is authoritative for exact breakdown; rounding to 2dp documented |
| Stripe Customer | Persistent, last-write-wins | Required for subscriptions; invoices snapshot billing details at creation; audit trail intact |
| Webhook hosting | App Runner, not API Gateway | Simpler; billing code locality; Stripe retries for 3 days; idempotency already built |
| Top-up pricing for subscribers | Pay-as-you-go rates, not tier rates | Prevents gaming (subscribe, bulk buy at discount, cancel) |
| Subscription management | Hybrid (in-app status + Stripe Portal) | Shows key info without building full management UI |

---

## Future considerations (documented, not built)

- **Capped rollover:** Introduce for new subscribers; grandfather existing ones on rolling balance.
- **Unify pay-as-you-go crediting to webhooks:** Currently pay-as-you-go tokens are credited on the success page redirect. In Phase 2, subscriptions use webhook-based crediting. Consider moving pay-as-you-go to `checkout.session.completed` webhook as the primary crediting path (success page becomes display-only). More resilient — handles user closing tab before redirect. Phase 1 code should be structured so this migration is straightforward.
- **Starter bundles:** Discounted initial token grant tied to subscription sign-up (e.g., "Subscribe and get your first 500 tokens at your tier rate"). Adds backend complexity — deferred.
- **Overage billing (Option C):** Subscription tokens at tier rate + mid-cycle overages at pay-as-you-go rate. Requires mid-cycle detection + separate billing. Deferred until usage data shows customers regularly exceed allowance.

---

## Environment notes

- **Not in production yet.** Backwards compatibility is not a hard requirement.
- A fresh AWS organisation is planned before production launch. All tables will be empty.
- However, this deployment may land before the org switch — code should handle missing attributes gracefully.
