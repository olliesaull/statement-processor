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

### [2026-04-14] design | Inline 401 handling in buy pages balance pill JS

**Context:** The balance pill on `/buy-pages` fetches token balance via `GET /api/tenants/<id>/token-balance` when the tenant dropdown changes. Needed auth error handling for the fetch call.

**Options considered:**
- Option A: Use the shared `redirectForUnauthorizedResponse` helper from `main.js`
- Option B: Inline a simplified 401/redirect check in the page script

**Decision:** Option B — inline simplified check.

**Rationale:** `main.js` is loaded as `type="module"`, making its top-level functions module-scoped and inaccessible from plain inline `<script>` tags. The shared helper also clears the `session_is_set` cookie and handles `cookie_consent_required` — neither is critical here since the login page handles cleanup on arrival. Exporting the helper to `window` would work but couples module internals to global scope for a single use case.

---

### [2026-04-14] design | Scoped CSS classes for pricing and tenant management redesign

**Context:** Visual uplift of `/pricing` and `/tenant_management` pages. Needed new CSS for headers, cards, summary strips, and tables.

**Options considered:**
- Option A: Modify existing shared classes (`.page-header-hero`, `.cta-panel`, `.page-table-shell`)
- Option B: Create new page-scoped classes (`.pricing-*`, `.tenant-*`)

**Decision:** Option B — scoped classes.

**Rationale:** Modifying shared classes risks regressions on other pages. Scoped classes can be iterated on independently and are easier to remove or replace in a future site-wide redesign. The slight duplication (e.g., `.pricing-header` padding similar to `.page-header-hero`) is acceptable for isolation.

**References:** `service/static/assets/css/main.css` (pricing and tenant management redesign sections)

---

### [2026-04-14] convention | `_dig()` helper for Stripe nested dict traversal

**Context:** Production alarm — `invoice.get("parent", {}).get("subscription_details")` crashed because `parent` was explicitly `None`. Fourth recurring incident of the same `.get()` chaining bug pattern with Stripe data.

**Options considered:**
- Option A: Fix each `.get()` chain individually with `(x.get("key") or {})` pattern
- Option B: Introduce a `_dig()` helper that safely traverses nested dicts

**Decision:** Option B — `_dig()` helper in `stripe_webhook_handler.py`.

**Rationale:** Stripe sends `null` for many fields that logically "should" be dicts. The `dict.get("key", {})` default only applies when the key is missing, not when it's present-but-None. A helper eliminates the entire class of bug: `_dig(obj, "a", "b", "c", default={})` handles None at any level. Scoped to the webhook handler module — not a shared utility — since this is the only file that traverses raw Stripe event JSON.

**References:** `service/stripe_webhook_handler.py` `_dig()`

---

### [2026-04-17] architecture | Per-resource progress fields over richer TenantStatus enum

**Context:** Planning contacts-first unlock: need to represent "contacts done, heavy phase still running" state. Three candidates for the schema.

**Options considered:**
- Option A: Richer `TenantStatus` enum — split `LOADING` into `LOADING_CONTACTS` / `LOADING_REST`.
- Option B: Keep coarse `TenantStatus`, add single `ReconcileReadyAt` boolean/timestamp.
- Option C: Per-resource fields (`ContactsProgress`, `InvoicesProgress`, `CreditNotesProgress`, `PaymentsProgress`, `PerContactIndexProgress`) + `ReconcileReadyAt` timestamp.

**Decision:** Option C.

**Rationale:** Only C gives us real per-resource % progress in the UI (`Invoices 47%`), which is the UX payoff that justifies doing the refactor at all. A/B leave the user staring at a spinner for 30 minutes. C is additive to the DDB row — existing readers keep working. `ReconcileReadyAt` is retained from B as a single authoritative gate for `/statement/<id>`, keeping guard-decorator logic simple (one field read, not five). The Xero SDK exposes `result.pagination.item_count` on list-response models, so real % is wire-supported (confirmed against `xero_python` source).

**References:** `plans/2026-04-17-contacts-first-unlock-plan.md`

---

### [2026-04-17] convention | Overload `TenantStatus.SYNCING` rather than add `LOADING_HEAVY`

**Context:** After splitting initial load into contacts-phase + heavy-phase, the post-contacts state could be a new enum value (`LOADING_HEAVY`) or reuse existing `SYNCING`.

**Options considered:**
- Option A: New `LOADING_HEAVY` enum value. Clearer naming. Affects ~6 touch-points (enum, `block_when_loading`, `schedule_erasure` transitions, `check_load_required`, API serialization, UI state handling, test suite).
- Option B: Reuse `SYNCING`. `SYNCING` becomes overloaded: either "post-contacts heavy phase of initial load" or "manual incremental sync". Documented in enum docstring + README + decision log.

**Decision:** Option B — overload + document.

**Rationale:** `ReconcileReadyAt` is the load-bearing distinction (null = initial load never completed; set = reconcile is available). `TenantStatus` is only a navigation gate indicator. Overload is a naming imperfection, not a logic imperfection. ~6 touch-points of enum churn isn't justified when documentation can fully convey the nuance.

**References:** `service/tenant_data_repository.py::TenantStatus`, README "Tenant sync lifecycle", `plans/2026-04-17-contacts-first-unlock-plan.md`

---

### [2026-04-17] architecture | HTMX polling over SSE for sync progress

**Context:** How to deliver live sync-progress updates to the tenant_management page.

**Options considered:**
- Option A: Faster HTMX polling (3s) with AFK gate and auto-stop on completion.
- Option B: Server-Sent Events via Redis pub/sub.
- Option C: WebSockets.

**Decision:** Option A.

**Rationale:** Runtime is gunicorn `gthread` (2 workers × 8 threads) behind nginx behind CloudFront on App Runner. SSE and WebSockets both require switching worker class to gevent/async and tuning nginx `proxy_read_timeout` + sending heartbeats. HTMX polling at 3s cadence with `[window.__userActive && !document.hidden]` gating is declarative (no custom JS beyond the AFK shim), auto-stops when `ReconcileReadyAt` flips (fragment returned without `hx-trigger`), and reuses patterns already present in the codebase. Latency improvement from SSE (~2s) does not justify the infra change.

**References:** `plans/2026-04-17-contacts-first-unlock-plan.md`

---

### [2026-04-17] architecture | Deploy-time migration script for ReconcileReadyAt backfill

**Context:** New `ReconcileReadyAt` attribute doesn't exist on any current DDB row. Existing fully-synced tenants need it set on deploy, otherwise every `/statement/<id>` request hits the not-ready gate.

**Options considered:**
- Option A: One-shot migration script scanning `TenantData`, setting `ReconcileReadyAt = LastSyncTime` for FREE + synced tenants. Runs during deploy.
- Option B: Runtime lazy backfill inside `reconcile_ready_required` decorator — first request per tenant triggers the write.
- Option C: Both.

**Decision:** Option A.

**Rationale:** Migration is a one-time operation; permanent runtime code to handle it is pollution. Script is idempotent and re-runnable (dry-run supported). Lazy backfill (B) forces the gate decorator to contain conditional legacy-compat logic, muddying `ReconcileReadyAt` as the single source of truth. Script pattern follows existing `scripts/manual_token_adjustment/`.

**References:** `scripts/backfill_reconcile_ready/` (planned), `plans/2026-04-17-contacts-first-unlock-plan.md`

---

### [2026-04-17] scope | Defer parallelization, Valkey caching, SSE, token-refresh lock

**Context:** Several adjacent optimizations were considered alongside the contacts-first unlock work.

**Deferred items + rationale:**
- **Parallel heavy-phase resource fetches**: Xero's 60 rpm per-tenant limit is the real ceiling; concurrency doesn't improve wall-time for large tenants (5 concurrent calls finish in 1/5 the time but consume 5 slots from the 60/min bucket, so total throughput is unchanged).
- **Token-refresh distributed lock**: required IF parallelization is enabled (concurrent refreshes race and Xero may revoke the token family). Not needed today (single-threaded sync per tenant).
- **SSE for sync progress**: requires gunicorn worker-class change + Redis pub/sub + nginx proxy-buffering tuning; HTMX polling at 3s is sufficient UX.
- **Valkey caching of `/tenants/sync-progress`**: single-tab users see ~30% cache hit rate at 2s TTL with 3s polling; per-request DDB cost is already negligible after the `BatchGetItem` fix.
- **Token refresh during long sync thread**: pre-existing bug (refreshed token lost because background thread cannot write Flask session). Not made worse by this change. Observability (log line on `token_saver` fire) added as part of this plan; actual fix deferred to its own PR.

**References:** `plans/2026-04-17-contacts-first-unlock-plan.md` ("Deferred" section)

---

### [2026-04-17] design | Drop nav sync-indicator dot

**Context:** Phase 4 critique of `plans/2026-04-17-contacts-first-unlock-plan.md`. Original Step 10 planned a passive "sync in progress" dot on the nav tenant-management link, driven by a Flask context processor reading DynamoDB on each full-page render.

**Options considered:**
- A: Keep the dot with layered caching (Flask.g memoization + session-flag flip + auto-heal on read). ~40 extra lines; ~20–50 DDB reads/user/year.
- B: Drop the dot entirely. Rely on the progress panel on `/tenant_management` and the not-ready view on `/statement/<id>` for sync visibility.

**Decision:** B — drop the dot.

**Rationale:** The dot is a discoverability enhancement, not a required signal. The progress panel already lives on the dedicated management page (which is also the only page that can trigger sync), and the not-ready view surfaces the state wherever a user would otherwise be blocked. Zero per-request DDB cost in steady state. Users who ignore the progress panel on `/tenant_management` are not the target case — manual-sync triggering is a deliberate action on that page.

**References:** `plans/2026-04-17-contacts-first-unlock-plan.md` (Step 10 — marked DROPPED).

---

### [2026-04-17] convention | Retry vs Sync button rendered server-side, not via JS switching

**Context:** Step 9 tenant-management row now has a single action button that must present as either "Sync" or "Retry sync" depending on the tenant's current state (`LOAD_INCOMPLETE` or any resource `failed` → Retry).

**Decision:** Render the button conditionally in the Jinja template based on server-side state. Do not toggle label/URL in JavaScript after load.

**Rationale:** Eliminates a hydration/flicker class where the row first renders "Sync" then swaps to "Retry" on next poll. The HTMX panel already re-fetches on polling, so a post-state-change label swap arrives naturally via the fragment swap. Keeps button semantics in one place (the template), not split across template + JS.

**References:** `plans/2026-04-17-contacts-first-unlock-plan.md` (Step 9).

---

### [2026-04-18] convention | `ProgressStatus` StrEnum + shared resource→attribute mapping

**Context:** Review of the contacts-first-unlock branch surfaced magic-string
usage of `"pending"`, `"in_progress"`, `"complete"`, `"failed"` across
`sync.py`, `routes/api.py`, `utils/sync_progress.py`, and a resource→attribute
mapping dict duplicated across four files with no canonical source.

**Decision:** Introduce ``ProgressStatus(StrEnum)`` in
``tenant_data_repository.py`` adjacent to ``TenantStatus``; route all
progress-status comparisons and writes through it. Delete the duplicate
`_RESOURCE_PROGRESS_ATTRS` / `_PROGRESS_ATTR_NAMES` / `_RESOURCE_ATTRIBUTES`
dicts and call ``_progress_attribute_name(resource)`` instead.

**Rationale:** `python-style.md` mandates enums over string literals for small
fixed vocabularies. A typo in one of the three magic-string sites silently
misclassifies a sync outcome; an enum makes that class of bug a type error
at import time. Similarly, DRY on the attribute map means adding a new
resource touches one file, not four.

**References:** `service/tenant_data_repository.py::ProgressStatus`,
`service/utils/sync_progress.py`, `service/sync.py`, `service/routes/api.py`.

---

### [2026-04-18] convention | `SYNC_STALE_THRESHOLD_MS` centralised in `tenant_data_repository.py`

**Context:** `_SYNC_STALE_THRESHOLD_MS = 5 * 60 * 1000` was defined
independently in both `sync.py` and `routes/api.py`. `try_acquire_sync` is the
only function that acts on it, so a drift between callers would split-brain
the stale-heartbeat recovery window.

**Decision:** Promote the constant to a module-level `Final[int]` in
`tenant_data_repository.py` and import it from both `sync.py` and
`routes/api.py`.

**Rationale:** Single source of truth for a value that has load-bearing
correctness implications. A tuning change (e.g. after observing real crash
recovery in production) now affects both call sites atomically.

**References:** `service/tenant_data_repository.py::SYNC_STALE_THRESHOLD_MS`.

---

### [2026-04-18] scope | Drop `sync_contacts_phase` / `sync_heavy_phase` wrapper functions

**Context:** The plan introduced `sync_contacts_phase` and `sync_heavy_phase`
as "phase helpers" in `sync.py`. In practice `sync_heavy_phase` was never
called — `sync_data` inlines the heavy loop with retry-skip semantics that the
helper can't express. `sync_contacts_phase` was a one-line wrapper with one
caller (`sync_data`) and no extra logic.

**Decision:** Delete both functions. Call `sync_contacts()` directly in
`sync_data` with a comment explaining why contacts is extracted from the
heavy loop.

**Rationale:** Dead public functions rot — future edits to the inlined heavy
loop would silently diverge from the unused `sync_heavy_phase` body. The
phase-split design intent is preserved by the in-line comment and the README
"Tenant sync lifecycle" section, which is where readers actually look.

**References:** `service/sync.py::sync_data`.

---

### [2026-04-17] convention | HTMX CSRF wired via `htmx:configRequest` global listener

**Context:** Global `CSRFProtect(app)` requires an `X-CSRFToken` on every state-changing request. Old per-endpoint `buildCsrfUrlEncodedBody()` approach is deleted with `tenant-sync.js`.

**Decision:** One global `htmx:configRequest` listener in `main.js` injects `X-CSRFToken` into every HTMX request from a `<meta name="csrf-token">` tag in `base.html`.

**Rationale:** Alternative (per-button `hx-headers`) repeats the CSRF token on every HTMX element. Global listener is one registration and covers all present and future HTMX POSTs. CSRF token in meta tag is already the Flask-WTF recommended pattern.

**References:** `plans/2026-04-17-contacts-first-unlock-plan.md` (Step 9).
