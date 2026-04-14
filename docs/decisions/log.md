# Decision Log

This file records significant decisions made during development — architectural choices, design tradeoffs, security decisions, convention choices, and anything where we deliberately chose one option over another.

Entries are append-only. Newest entries go at the bottom.

## Decision types

Common types (use existing types when they fit, or add new ones as needed):

- `architecture` — structural or system design decisions
- `security-tradeoff` — security considerations we consciously accepted or deferred
- `design` — UI/UX or API design choices
- `dependency` — library, framework, or tool choices
- `convention` — coding style, naming, or workflow conventions
- `performance` — performance-related tradeoffs
- `scope` — what we chose to include or exclude
- `infrastructure` — deployment, CI/CD, environment decisions

## Entry format

Use this format for each entry:

### [YYYY-MM-DD] type | Brief title

**Context:** What we were working on and what prompted this decision.

**Options considered:**
- Option A: description
- Option B: description

**Decision:** What we chose.

**Rationale:** Why, including what we traded off or accepted.

**References:** Relevant files, plan docs, or PRs (if applicable).

---

<!-- Entries start below this line -->

### [2026-04-13] architecture | One Stripe Product per subscription tier

**Context:** Setting up the Stripe Customer Portal for subscription tier switching.

**Decision:** One Product per tier (three Products, one Price each) instead of one Product with three Prices.

**Rationale:** The Customer Portal rejects multiple Prices on the same Product when they share the same billing interval and currency — it can't distinguish them for tier switching.

---

### [2026-04-13] architecture | Immediate downgrade (no subscription schedules)

**Context:** Configuring Customer Portal downgrade behaviour.

**Decision:** Immediate update with proration, not "wait until end of billing period".

**Rationale:** Subscription schedules add significant complexity (schedule ID storage, extra webhook events, Schedule API). Immediate downgrade works with existing webhook logic: `invoice.paid` credits `max(0, new_tokens - already_credited)`, which is 0 for downgrades. Customer gets a fair prorated billing credit.

---

### [2026-04-13] architecture | Dedicated webhook Blueprint for CSRF exemption

**Context:** Stripe webhook requests authenticate via signature verification, not session cookies. Global CSRFProtect would reject all webhook POSTs.

**Decision:** Separate `webhook_bp` Blueprint with `csrf.exempt(webhook_bp)` in `app.py`.

**Rationale:** Blueprint-level exemption prevents accidental CSRF exposure on other API routes. The webhook route has fundamentally different auth semantics, so a separate Blueprint makes the boundary explicit.

**References:** `service/routes/webhook.py`, `service/app.py`

---

### [2026-04-13] architecture | Webhook on App Runner, not API Gateway + Lambda

**Context:** Where to host the Stripe webhook endpoint.

**Decision:** Flask route on App Runner.

**Rationale:** Billing code locality (BillingService, repositories are all in `service/`). Stripe retries for 3 days with backoff. Idempotency handled via StripeEventStoreTable. No new infrastructure.

---

### [2026-04-13] design | Subscribers top up at pay-as-you-go rates

**Context:** Whether subscribers should get discounted top-up prices.

**Decision:** Standard graduated rates for top-ups.

**Rationale:** Prevents gaming (subscribe, bulk buy at discount, cancel). Simple implementation. Customers wanting more pages should upgrade their tier.

---

### [2026-04-13] design | Subscription preserved during disconnect grace period

**Context:** When to cancel a Stripe subscription on tenant disconnect.

**Decision:** Cancel only when data is actually erased, not on disconnection.

**Rationale:** Reconnecting during the grace period should restore everything including the subscription. Cancelling immediately would break this expectation.

**References:** `lambda_functions/tenant_erasure_lambda/main.py`

---

### [2026-04-13] scope | Disconnect subscription warning only checks current tenant

**Context:** The disconnect modal shows a subscription warning per-tenant.

**Decision:** Only check the current tenant's subscription state.

**Rationale:** N DynamoDB lookups per page load for a cosmetic warning is disproportionate. The erasure lambda handles cancellation regardless. Most users have 1-3 tenants.

**References:** `service/templates/tenant_management.html`

---

### [2026-04-13] security-tradeoff | AppRunner `states:StartExecution` on `resources=["*"]`

**Context:** Noticed during subscription feature security audit that the AppRunner instance role grants `states:StartExecution` on all state machines in the account.

**Decision:** Accept for now — add to backlog.

**Rationale:** The `cloudwatch:PutMetricData` action in the same policy statement genuinely requires `resources=["*"]` (AWS limitation). The `states:StartExecution` action should be scoped to `state_machine.state_machine_arn` but splitting the statement is a low-priority refactor. There is only one state machine in the account today, so blast radius is minimal. Fix when next touching CDK IAM grants.

**References:** `cdk/stacks/statement_processor.py` lines 300-306

---

### [2026-04-14] architecture | Stripe Customer created before checkout, not on payment success

**Context:** `create_subscription_checkout_session` requires a `customer_id`. The customer is created and stored in DynamoDB before Stripe Checkout opens, meaning a cancelled checkout leaves an orphaned Stripe customer.

**Options considered:**
- Option A: Create customer upfront, pass to checkout session (current approach)
- Option B: Omit `customer_id` and let Stripe create the customer implicitly, then retrieve and store it in the webhook/success handler
- Option C: Current approach + periodic cleanup of orphaned customers

**Decision:** Option A — create upfront, accept orphans.

**Rationale:** Stripe subscription-mode checkout requires a customer ID. Letting Stripe create one implicitly (Option B) would require retrieving the customer ID after checkout completes and adds complexity to the webhook flow. Orphaned Stripe customers have no cost or billing impact — they're inert records. Periodic cleanup (Option C) is available if orphan volume becomes a concern but isn't justified now.

**References:** `service/routes/billing.py` lines 341-346, `service/stripe_service.py` `create_subscription_checkout_session`
