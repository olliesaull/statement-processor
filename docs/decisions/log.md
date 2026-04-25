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

### [2026-04-18] convention | Hardening of sync-lock primitives

**Context:** Sharp-edges audit flagged footgun signatures on the new sync-lock
APIs — `try_acquire_sync` accepted `stale_threshold_ms <= 0` (which made the
"older than" comparison trivially true, clobbering an active sync) and
`update_resource_progress` accepted raw strings for `status`, letting a typo
silently stall the UI progress bar.

**Decision:**
- `try_acquire_sync` now raises ``ValueError`` on non-positive
  ``stale_threshold_ms``.
- `update_resource_progress.status` type narrowed to ``ProgressStatus``.

**Rationale:** Sync locks are a correctness-critical primitive — a zero
threshold can silently drop a live sync's data. Type-tightening and positional
validation shift a future-caller footgun into an import-time / call-time
error. Neither change affects current callsites.

**References:** `service/tenant_data_repository.py::try_acquire_sync`,
`service/tenant_data_repository.py::update_resource_progress`.

---

### [2026-04-17] convention | HTMX CSRF wired via `htmx:configRequest` global listener

**Context:** Global `CSRFProtect(app)` requires an `X-CSRFToken` on every state-changing request. Old per-endpoint `buildCsrfUrlEncodedBody()` approach is deleted with `tenant-sync.js`.

**Decision:** One global `htmx:configRequest` listener in `main.js` injects `X-CSRFToken` into every HTMX request from a `<meta name="csrf-token">` tag in `base.html`.

**Rationale:** Alternative (per-button `hx-headers`) repeats the CSRF token on every HTMX element. Global listener is one registration and covers all present and future HTMX POSTs. CSRF token in meta tag is already the Flask-WTF recommended pattern.

**References:** `plans/2026-04-17-contacts-first-unlock-plan.md` (Step 9).

---

### [2026-04-18] convention | Ops scripts declare their own minimal deps

**Context:** `scripts/backfill_reconcile_ready/requirements.txt` chained
`-r ../../service/requirements.txt`, which in turn has `-e ../common`. Pip 26
resolves `-e` paths relative to the invoking CWD, not the requirements file,
so running `pip install -r requirements.txt` from the script directory (as
the README instructs) failed: `../common` resolved to `scripts/common`.

**Decision:** Ops scripts under `scripts/` declare the minimal deps they
actually need rather than reusing `service/requirements.txt`. For the
reconcile-ready backfill this is `boto3`, `redis`, `dotenv` — exactly what
`service/config.py` imports. `sp_common` and the full service web stack
(Flask, Authlib, Stripe, openpyxl, etc.) are not pulled in because neither
the script nor `config.py` imports them.

**Rationale:** Decouples ops scripts from the service's full dependency set
(lighter venvs, fewer pip-version footguns), and makes each script's
dependency surface auditable on its own. Consistent with Python-style
"one module, one concept": a backfill script shouldn't need Flask.

**References:** `scripts/backfill_reconcile_ready/requirements.txt`,
`service/config.py`.

---

### [2026-04-20] convention | `_RETRYABLE_STATUSES` includes `IN_PROGRESS`

**Context:** Stage 3 smoke (Case 3, 2026-04-20) killed a gunicorn worker mid-payments fetch. `PaymentsProgress` stayed at `status=in_progress` (the worker never reached the `complete`/`failed` write), `payments.json` was missing in S3, and retry-sync selected only `["per_contact_index"]` which then failed on the missing object in a loop — the tenant was orphaned, unable to recover without manual intervention.

**Decision:** Add `ProgressStatus.IN_PROGRESS` to `_RETRYABLE_STATUSES` in `service/routes/api.py` so retry-sync picks up crashed-mid-fetch resources.

**Rationale:** Protection against racing a live sync does not come from excluding `IN_PROGRESS` from the retry set — it comes from `try_acquire_sync`'s stale-heartbeat gate, which runs first. A live sync's fresh heartbeat returns 409 before any retry-resource selection happens; only crashed (stale-heartbeat) tenants reach the expanded retryable set. Without this change, worker-crash recovery is not a supported path.

**References:** `service/routes/api.py::_RETRYABLE_STATUSES`, `service/tenant_data_repository.py::try_acquire_sync`, `plans/2026-04-20-contacts-first-unlock-stage3-fixes.md` (Step 3).

---

### [2026-04-20] convention | AFK / visibility gating via `hx-disable`, not `hx-trigger` bracket filter

**Context:** The polling partials used `hx-trigger="every 3s[(window.__userActive ?? true) && !document.hidden]"` to pause polling when the tab was hidden or the user was idle. htmx compiles the bracket filter via `new Function(expr)`, which our finance-app CSP refuses (`'unsafe-eval'` is not allowed). The filter silently fail-opened — polling ran 100% of the time — and also raised a console `EvalError` on every 3s tick.

**Decision:** Use plain `hx-trigger="every 3s"` and have `afk.js` toggle the `hx-disable` attribute on `#sync-progress-panel` and `#statement-reconcile-not-ready` via `visibilitychange` + throttled activity events.

**Rationale:** `hx-disable` is evaluated dynamically per trigger fire, so a disabled panel cleanly skips its 3s tick. Kept the existing `window.__userActive` signal so future consumers (if any) have a stable contract. Weakening CSP to allow `'unsafe-eval'` was not considered — this is a finance app, and the cost-benefit of eval permissions for a cosmetic polling filter is negative.

**References:** `service/static/assets/js/afk.js`, `service/templates/partials/sync_progress_panel.html`, `service/templates/partials/statement_wait_panel.html`, `plans/2026-04-20-contacts-first-unlock-stage3-fixes.md` (Step 5).

---

### [2026-04-20] design | `/sync` stays fire-and-forget; `/retry-sync` keeps synchronous acquire

**Context:** The rollout doc originally described `POST /api/tenants/<id>/sync` as returning 409 on concurrent clicks with an `htmx:responseError` toast. Actual behaviour: both POSTs return 200 with the panel fragment, and the worker-side `try_acquire_sync` inside `sync_data` silently drops the overlap with a WARNING log. Only `POST /api/tenants/<id>/retry-sync` synchronously acquires the lock before executor submission, and therefore returns 409 on concurrent calls.

**Decision:** Correct the runbook to match actual behaviour. Do not add symmetric 409 behaviour to `/sync`.

**Rationale:** Worker-side dedup on `/sync` is the right UX: benign double-clicks and background retries must not surface as errors. `/retry-sync` keeps the synchronous 409 because it is an explicit recovery action — the caller expects to observe the outcome of its invocation. Making `/sync` symmetric would add error-toast noise for no user benefit. Revisit if product signals confusion.

**References:** `service/routes/api.py::trigger_tenant_sync`, `service/routes/api.py::retry_tenant_sync`, `service/sync.py::sync_data`, `docs/rollout/2026-04-17-contacts-first-unlock.md`, `plans/2026-04-20-contacts-first-unlock-stage3-fixes.md` (Step 2).

---

### [2026-04-20] design | Stuck-SYNCING tenants surface Retry sync based on heartbeat staleness

**Context:** `TenantStatus=SYNCING` alone does not distinguish "live work in progress" from "crashed thread, heartbeat frozen". Case 3 Stage 3 smoke showed operators the plain `Sync` button against a worker whose heartbeat was already past the 5-minute stale threshold; clicking Sync silently no-ops because `sync_data` bails in the worker thread before touching any resources. Retry sync succeeds in the same state.

**Decision:** The tenant-management action button uses the pure helper `is_retry_recommended(tenant_item, now_ms=..., stale_threshold_ms=SYNC_STALE_THRESHOLD_MS)` to pick Sync vs Retry sync. Retry is recommended on `LOAD_INCOMPLETE`, any failed resource, or any `in_progress` resource whose `LastHeartbeatAt` is older than the stale threshold.

**Rationale:** The same heartbeat threshold is already the lock-acquire gate, so reusing it for the UI means the button the operator sees maps directly to whether retry-sync will succeed. `in_progress` with a fresh heartbeat (or missing heartbeat — defensive) keeps the Sync button to avoid flipping speculatively while a live sync is still making progress. `now_ms` is injected so the helper is pure and tests don't monkeypatch time.

**References:** `service/utils/sync_progress.py::is_retry_recommended`, `service/routes/tenants.py::tenant_management`, `service/tenant_data_repository.py::SYNC_STALE_THRESHOLD_MS`, `plans/2026-04-20-contacts-first-unlock-stage3-fixes.md` (Step 4).

---

### [2026-04-21] architecture | Dedicated AWS Organization for Statement Processor

**Context:** Statement Processor was deployed in a single standalone AWS account (`dotelastic-production`, `747310139457`) with no dev environment. Needed to split dev/prod and move off the standalone account for better isolation, SSO, and consolidated billing.

**Options considered:**
- A. Add `sp-dev` and `sp-prod` to the existing gwd-hub Organization (shared management account).
- B. Create a brand-new Organization for Statement Processor (new empty `sp-management` account, two member accounts).
- C. Convert `dotelastic-production` into an Organization and use it as management.

**Decision:** B — new dedicated Organization.

**Rationale:** A would couple Statement Processor's blast radius to gwd-hub's management account (billing lock, SCP changes, IAM admin errors). C is explicitly counter to AWS best practice, which advises keeping the management account empty. B has ~30 min extra setup cost but gives clean legal/operational separation (e.g., if Statement Processor is ever sold or split into its own entity) and leaves the door open for other dexero apps to join this Org later.

**References:** `plans/2026-04-21-aws-org-migration-plan.md`.

---

### [2026-04-21] infrastructure | App Runner stub deploy in both new accounts before 2026-04-30

**Context:** AWS blocks creation of new App Runner services after 2026-04-30. Existing services continue to work and can be updated. The new prod environment is blocked on domain naming (out of scope), which pushes the real cutover past the deadline. Without mitigation, `sp-prod` would never become App-Runner-eligible.

**Decision:** Deploy a stub App Runner service to both `sp-dev` (full) and `sp-prod` (empty data, no domain, paused immediately) before 2026-04-30. Real prod cutover happens later, on the user's timeline, by updating the stub service rather than creating a new one.

**Rationale:** App Runner service updates continue working post-deadline — only creation is blocked. The stub in `sp-prod` can sit paused (zero compute cost) until the real domain is chosen and LIVE Stripe is ready. Requires the CDK to tolerate `PROD_DOMAIN_NAME=""` (gate CloudFront on `apex_domain`) and `service/oauth_client.py` to tolerate blank `DOMAIN_NAME` in non-local stages (fall back to `request.host`). Both changes land in the pre-deadline refactor.

**References:** `plans/2026-04-21-aws-org-migration-plan.md` (Task A.26 stub; B.7 full re-deploy).

---

### [2026-04-21] security-tradeoff | `X_STATEMENT_CF` rotated and moved to SSM at new-prod launch

**Context:** The CloudFront→AppRunner origin-protection shared secret (`X_STATEMENT_CF`) was hardcoded as a plaintext constant in `cdk/stacks/statement_processor.py` and has been in git history since the CDK was first committed. During the fresh new-prod deploy, there's no downside to rotating.

**Decision:** Rotate the secret for `sp-prod` only (a fresh random value), move the value from a CDK constant to SSM Parameter Store at `/StatementProcessor/X_STATEMENT_CF`, and have CDK resolve it at CloudFormation deploy time via `ssm.StringParameter.value_for_string_parameter`. Dev keeps the current value (doesn't go through CloudFront, so the secret is unused). The SSM parameter is `String` type, not `SecureString`, because `value_for_string_parameter` compiles to the plain `{{resolve:ssm:...}}` dynamic reference which does not support SecureString.

**Rationale:** Free security posture improvement — the migration already involves a fresh CDK deploy, so rotation cost is zero. SecureString would not materially improve protection because the value lands in the CloudFormation template plaintext regardless; rotation on each fresh environment is the meaningful mitigation. Dev's CloudFront absence makes rotation there pointless.

**References:** `plans/2026-04-21-aws-org-migration-plan.md` (A.15 Step 7, B.5 Step 2).

---

### [2026-04-21] convention | Local Flask dev defaults `AWS_PROFILE=sp-dev`; ops scripts default `sp-prod`

**Context:** Pre-migration, ops scripts and local Flask `.env` both defaulted to `AWS_PROFILE=dotelastic-production`. This meant running `make run-app` on a laptop read from and wrote to production DDB/S3 — a subtle data-integrity antipattern.

**Decision:** After migration, local Flask `service/.env` defaults `AWS_PROFILE=sp-dev` (so local dev hits the dedicated dev environment). Ops scripts (`scripts/replace_textract_test`, `scripts/backfill_processing_stage`, etc.) continue to default to the prod profile — `sp-prod` in the new world — because their semantic is "operate against prod". README documents the pattern: local dev is dev-by-default, prod inspection requires explicit override (`AWS_PROFILE=sp-prod make run-app`).

**Rationale:** The new dev environment exists exactly so local testing can run against dev without risk. Keeping ops-script defaults as prod preserves current semantics (those tools are explicitly for prod maintenance). Split defaults plus documentation is cheaper than forcing `AWS_PROFILE` to be set explicitly on every command.

**References:** `plans/2026-04-21-aws-org-migration-plan.md` (A.17, A.18).

---

### [2026-04-21] scope | No DNS cutover from old prod — fresh launch on new domain

**Context:** `cloudcathode.com` was a placeholder domain. User wants to pick a new production domain as part of the migration. Original plan considered a DNS cutover (lower TTL, flip records) but with a placeholder domain being replaced anyway, that machinery is unnecessary.

**Decision:** New prod launches on a brand-new domain (user picks later, registered in Route 53 `sp-prod`). Old `cloudcathode.com` is not redirected or migrated — it's decommissioned after the 30-day retention. The beta tester is notified of the new URL directly.

**Rationale:** With 1 beta tester, the "smooth DNS cutover" value is near-zero. A single clean transition to the new domain is simpler than drop-TTL-then-flip mechanics. The domain registration for `cloudcathode.com` transfers from old prod to `sp-prod` before old-prod teardown, purely to keep it as a backup asset — no active services on it.

**References:** `plans/2026-04-21-aws-org-migration-plan.md` (B.14 for cloudcathode.com transfer; no DNS cutover task).

---

### [2026-04-21] infrastructure | `sp-prod` S3 buckets include account-ID suffix

**Context:** S3 bucket names are globally unique. Old-prod `747310139457` owns `dexero-statement-processor-prod`, `-prod-assets`, and `dexero-bedrock-invocation-logs-prod`. A plain `cdk deploy` into `sp-prod` would fail because CloudFormation can't create buckets with those global names while old prod still holds them.

**Decision:** For `stage == "prod"`, append `-{env.account}` to all three bucket names in the CDK stack. `sp-dev` keeps the un-suffixed names (old prod holds no `-dev` buckets). The suffix is kept after old-prod teardown — removing it would require destroy-and-recreate of the new-prod buckets (S3 has no rename), which isn't worth the churn for cosmetic cleanliness.

**Rationale:** Unblocks stub-prod deploy before old-prod is destroyed, which the 2026-04-30 App Runner deadline requires. The cosmetic cost (a long bucket name with an account ID suffix) is tolerable.

**References:** `plans/2026-04-21-aws-org-migration-plan.md` (A.15 Step 8).

---

### [2026-04-22] convention | `render_sync_progress_fragment` takes pre-fetched `tenant_rows`

**Context:** Phase 4 critique of the tenant-management-cards plan surfaced that the original fragment renderer performed its own `TenantDataRepository.get_many(tenant_ids)` call, but every caller (the poll route and the two API triggers in `routes/api.py`) *also* needs the rows in-scope to compute `needs_retry_by_id` via `is_retry_recommended`. The original plan had both layers calling `get_many`, doubling DynamoDB reads on every 3s poll.

**Decision:** The renderer is pure given a row snapshot — callers own the BatchGetItem. `render_sync_progress_fragment` now takes `tenant_rows: dict[str, dict[str, Any]]` as a required keyword argument and never touches DynamoDB directly. The private `_render_sync_progress_fragment` wrapper in `routes/api.py` centralises the fetch for Sync/Retry return paths so the five call sites inside `trigger_tenant_sync` and `retry_tenant_sync` all share one round-trip.

**Rationale:** One BatchGetItem per render, not two. Also keeps `needs_retry_by_id` computed against the exact row snapshot the UI is about to paint — no race between the retry-decision read and the progress-display read.

**References:** `plans/2026-04-22-tenant-management-cards-plan.md` (Task 7 Steps 2-4).

---

### [2026-04-22] UX | Tenant management: replace table with per-tenant cards

**Context:** `/tenant_management` rendered a top `sync_progress_panel` (banners + per-tenant compact list) above a `tenant-table-refined` table with the same tenants, one row each. Users had to cross-reference the top panel with the table row below to find their tenant, and each resource's progress lived far from that tenant's Sync/Retry buttons.

**Decision:** Replace both sections with a single vertical `<ul class="tenant-card-list">` of per-tenant `<li class="tenant-card">` cards. Each card owns state + pills + aggregate progress + expandable resource detail + all actions. The three banner variants (`banner-loading`, `banner-heavy`, `banner-failed`) drop in favour of per-card state pills (`Ready` / `Syncing · N of 4` / `Finalising…` / `Sync failed`) plus a 4px `border-left` stripe colour-coded by state.

**Rationale:** One decision surface per tenant; no cross-referencing. Matches the card pattern validated on `numerint/dexero/web` refactor/ui-overhaul.

**Tradeoff accepted:** Each card is ~150px tall vs. the old ~40px table row, so the page is taller overall. At typical 900-viewport sizes with 1-2 tenants, expanding Show detail pushes the footer by ~112px (reduced from ~148px via `:has()`-triggered `.tenant-content-area` `padding-bottom` shrink). Users with 8+ tenants have the footer already below the fold where the push is invisible.

**References:** `plans/2026-04-22-tenant-management-cards-plan.md`; mockup at `.superpowers/brainstorm/mockups/cards-expandable.html`.

---

### [2026-04-22] UX | Per-contact-index surfaced as `Finalising…`, not folded into Contacts

**Context:** When all four Xero fetchers (Contacts / Credit notes / Invoices / Payments) are complete but `PerContactIndexProgress.status != complete`, the card needs copy that distinguishes "waiting on Xero data" from "waiting on the reconcile index build". Two options considered: (a) fold the index phase into the Contacts resource row (keep Contacts as "Indexing…" until the index build finishes), or (b) add a fifth pill variant `Finalising…` on the card and let the four fetcher rows complete independently.

**Decision:** Option (b). Card pill flips to `Finalising…` once the four fetchers are done but the index is still building. Stripe stays blue (syncing-family) — no new stripe colour. New `TenantProgressView.is_finalising` property gates this on `all(r.is_complete) and per_contact_index_status not in (complete, failed)`.

**Rationale:** Folding the index into Contacts would delay when "Contacts: Done" appears, which blocks other pages from unblocking via the heavy-phase signal. Users need Contacts to flip Done as soon as the raw Xero fetch succeeds, independent of reconcile index build timing.

**References:** `plans/2026-04-22-tenant-management-cards-plan.md` (Task 4).

---

### [2026-04-22] UX | Tenant card detail state preserved across HTMX swaps via `beforeSwap`

**Context:** Tenant cards expose a `Show detail` button that slides open a per-resource detail block via CSS `max-height` transition. The card list lives inside an HTMX polling panel that swaps every 3s. Naive state restoration on `htmx:afterSwap` would re-apply `data-expanded="true"` to the live DOM, which triggers the CSS transition on each poll — the detail would flicker closed then reopen every 3 seconds.

**Decision:** Persist open detail IDs in `sessionStorage` (cleared on tab close). A `htmx:beforeSwap` listener mutates the incoming HTML *string* before the swap to pre-apply `data-expanded="true"` + `aria-expanded="true"`. The new DOM arrives already-open, so no property-change event fires and no transition animates.

**Rationale:** Pre-applying on the response string avoids the property-change event entirely — the alternative (`afterSwap`) fires the transition on every 3s tick. `hx-preserve` on the detail block was rejected because it freezes contents (per-resource counts would go stale) — opposite of what the progress UI needs.

**References:** `plans/2026-04-22-tenant-management-cards-plan.md` (Task 10); `service/static/assets/js/tenant-card-detail.js`.

---

### [2026-04-22] convention | Statement-extraction test suite — sys.path isolation, XFAIL triage, and diff coverage (Phase 4 plan refinements)

**Context:** Planning the new `scripts/accuracy_test/` extraction-accuracy test suite (master + Tier 1 + Tier 2 plan files). Phase 4 critique surfaced four substantive issues with the Phase 1–3 plan that would have produced a non-running or misleading suite if shipped as-written. These decisions are recorded now (rather than only inside the plan files) because they establish conventions that future test-suite work and any other "import service + lambda code in one process" tooling must follow.

**Options considered:**
- For lambda + service co-import: (a) two `sys.path.insert(0, ...)` calls — the original Phase 3 plan; silently breaks one import tree because both ship colliding top-level `core/` and `logger.py`. (b) Subprocess isolation — clean but expensive per scenario. (c) Restructure lambda into a real package — invasive, touches CDK Docker assembly. (d) **importlib spec_from_file_location for lambda's `extract_statement` under a non-colliding module name; service-only on `sys.path`**. Chose (d).
- For LLM mis-extraction triage: (a) hand-maintained `known_misses.md` outside the schema — no machine-checkable signal. (b) per-scenario boolean — masks unrelated regressions. (c) **`meta.known_miss_extraction: list[str]` (fnmatch globs against diff-line tags) + per-item `xero.known_miss_match: bool`**, with `PASS / XFAIL / FAIL` aggregation. Chose (c).
- For `expected.raw`: (a) compare with whitespace tolerance — heavy triage burden. (b) **drop entirely** — `_diff_extraction` doesn't read it; `total{}` is the cell-level signal. (c) keep for inspection only — drift trap for no asserted value. Chose (b).
- For diff coverage: (a) keep narrow (`date`, `number`, `reference`, `total`) — Tier 2's combined-Details and mixed-payments scenarios silently pass regardless of LLM output. (b) **expand to include `due_date` and `item_type`**. Chose (b); first-run noise absorbed by the new XFAIL mechanism.

**Decision:**
1. `scripts/accuracy_test/run_accuracy_test.py` puts ONLY `service/` on `sys.path`. Lambda's `core/extraction.py::extract_statement` is loaded via `importlib.util.spec_from_file_location` under module name `_lambda_extraction`. A smoke-import test (Tier 1 Task 10 Step 2) runs before any Bedrock call to fail-fast on regressions. This convention applies to any future tooling that needs both trees in one process.
2. The scenario JSON schema includes `meta.known_miss_extraction: list[str]` and per-item `xero.known_miss_match: bool`. The runner classifies each diff line as FAIL or XFAIL; suite output is `PASS / XFAIL / FAIL`; non-zero exit only on FAIL. Each known-miss entry MUST be paired with a dated note in `meta.description`.
3. `helpers.expected_extraction.build_expected_extraction` does NOT populate `raw`. `render_amount` remains a Jinja filter for the templates but is not called from the expected builder.
4. `_diff_extraction` per-item compare loop covers `date`, `number`, `reference`, `due_date`, `item_type`, plus `total{}` (0.01 tolerance).

**Rationale:** Each of these decisions removes a foreseeable failure mode in the suite without expanding scope. Items 1 and 2 are load-bearing — without them the suite either won't run or won't produce meaningful signal after the first dry run. Items 3 and 4 trade a small amount of helper logic for fewer drift surfaces and broader regression coverage. None of these decisions touch the production code paths under test; they are entirely internal to the test harness.

**How to apply:** When extending the suite (Tier 2 or later), inherit the Tier 1 import pattern, schema fields, and diff coverage. When adding a `known_miss_*` entry, file a follow-up issue and link it from `meta.description` so the miss can be reassessed after each Bedrock model upgrade.

**References:** `plans/2026-04-22-statement-test-suite-master.md` (§2 Coupling policy, §3 schema, §5 expected_extraction, §7 CLI sequence); `plans/2026-04-22-statement-test-suite-tier-1-impl.md` (Tasks 3, 10, 11–13); `plans/2026-04-22-statement-test-suite-tier-2-impl.md` (Review gate, Self-Review).


---

### [2026-04-23] architecture | Tenant management UX fixes — five choices

**Context:** Implementing `plans/2026-04-23-tenant-management-ux-fixes-design.md` (four UX/correctness fixes on `/tenant_management`). Each fix surfaced a genuine design choice worth recording so future readers don't have to excavate git blame or the impl plan.

**Decisions:**

1. **Synchronous sync-lock acquire in `trigger_tenant_sync`.** `POST /api/tenants/<id>/sync` now calls `try_acquire_sync` in the request thread before submitting `sync_data` to the executor (with `already_acquired=True`). Previously the endpoint returned the fragment before `sync_data` had flipped `TenantStatus=SYNCING`, so the UI showed "Ready" for 2–4 seconds until the next 3s poll. Synchronous acquire makes the returned fragment reflect the syncing state immediately. Trade-off: adds one DynamoDB conditional `update_item` to the request path (small); mirrors the existing retry-sync endpoint's pattern.

2. **`is_live_sync` as a `TenantProgressView` field, not a `@property`.** `build_tenant_progress_view` pre-computes `is_live_sync` from `status` + `LastHeartbeatAt` + injected `now_ms`, storing the result on the frozen dataclass. Three guards (`has_failure`, `is_retry_recommended`, `is_incremental_syncing`) all need the signal; a `@property` would force threading `now_ms` into every read site. Pre-computed field keeps the dataclass pure and pushes the clock read to the construction edge. Trade-off: `now_ms` becomes a required kwarg on the three view builders; one-time edit across five production callsites.

3. **Reset progress sub-maps at `sync_data` start, not inside `try_acquire_sync`.** `sync_data` calls `reset_resource_progress(tenant_id, resources_to_reset)` immediately after lock acquisition, scoped to `only_run_resources` for retry paths or all five resources for full syncs. Retry paths must preserve COMPLETE markers on resources they're deliberately skipping; lumping the reset into `try_acquire_sync` would force either a full-wipe (breaks retry) or a resource-aware acquire (couples concerns). Scoped reset in `sync_data` keeps the lock-acquire predicate focused on status transitions.

4. **Client-side local-time formatting via `Intl.DateTimeFormat`.** Last-sync timestamps render as UTC from the server (`<time datetime="...">`), and a `tenant-card-local-time.js` module rewrites them to the user's locale on DOMContentLoaded + on `htmx:beforeSwap`. No authoritative client timezone available server-side (cookie-based TZ sniffing is brittle and adds session surface). `beforeSwap` mutation of the detached HTML prevents UTC flicker between polls — mirrors the pattern already used by `tenant-card-detail.js`. Trade-off: JS-off users see UTC; acceptable — this app is authenticated and JS-dependent for HTMX anyway.

5. **Live-sync override on stale FAILED markers — a convention.** `has_failure`, `is_retry_recommended`, and `is_incremental_syncing` all short-circuit when `is_live_sync` is True (status LOADING/SYNCING AND heartbeat fresh). A transient Xero failure mid-incremental-sync used to flip the whole card to "Sync failed + Retry" while the sync was still making progress on other resources; clicking Retry then returned 409 because the live sync's heartbeat was fresh. The guard lets the running sync overwrite stale markers before the UI surfaces them. Establishes a convention: gate any "tenant is broken" signal on `is_live_sync` when a running sync would otherwise overwrite the underlying state.

**How to apply:** Future sync-lifecycle UX work should follow (5) — if adding a new derived property that flags a tenant as broken/retry-worthy/failed, first ask whether a live sync is currently overwriting the markers, and guard with `view.is_live_sync`. Decision (2) also sets a pattern for time-dependent view fields: inject `now_ms` at the view-builder boundary rather than reading the clock inside properties.

**References:** `plans/2026-04-23-tenant-management-ux-fixes-design.md` (Issues 1–4), `plans/2026-04-23-tenant-management-ux-fixes-impl.md`, `service/utils/sync_progress.py`, `service/routes/api.py`, `service/sync.py`, `service/templates/macros/tenant_card.html`, `service/static/assets/js/tenant-card-local-time.js`.
