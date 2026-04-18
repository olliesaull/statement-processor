---
paths:
  - "**/*.py"
---

# Python Style Guide

## How to read this file

**Strong defaults, conscious exceptions.** Every rule below is how you write Python here **by default**. You may deviate, but you should be able to name the reason in one sentence — "this dict is built and consumed on the next line and typing it would duplicate two fields" is a reason; "it was faster to write" isn't. When in doubt, follow the rule — the common failure mode is too-loose, not too-strict.

The aim: exceptionally beautiful Python. Strongly typed, loosely coupled, clearly named, with meaningful logging.

## General principles

- Clarity over cleverness.
- Small, single-purpose functions.
- Small, incremental changes over wide refactors.
- Preserve behaviour unless a change is explicitly required.
- Make failure modes explicit — raise meaningful errors; log with context.

## Types & interfaces

### Every public function has explicit type hints
Arguments and return type, including `-> None`. Use modern built-ins (`list`, `dict`, `set`) — not `typing.List`, `typing.Dict`.

### Return a domain type, not `list[dict[str, Any]]`
Promote DynamoDB / Xero / Bedrock / S3 payloads to a typed model (`list[Statement]`, `ContactConfig`, `StatementItem`) at the repository boundary. The raw dict should not leak past the module that fetched it. Name the type after the thing, not the shape.

Bad:
```python
def get_incomplete_statements(tenant_id: str) -> list[dict[str, Any]]: ...
```

Good:
```python
def get_incomplete_statements(tenant_id: str) -> list[Statement]: ...
```

*When you can skip it:* a private helper used in exactly one place, within the same module, where the shape is built and consumed in a handful of lines. Even then, prefer a `TypedDict` over `dict[str, Any]`.

### Convert boundary payloads at the edge
A DynamoDB `query` response is a dict. The function that reads it may handle dicts; the functions that call it should not. Parse once, at the boundary (e.g. inside `service/utils/dynamo.py` or `service/xero_repository.py`), then work with the typed model.

### `Mapping` / `Sequence` for inputs; concrete `list` / `dict` for outputs
Broader on input, specific on output. Lets callers pass narrower types without erasing information on the way back.

### `NewType` for domain primitives that are easy to mix up
If `tenant_id: str` and `statement_id: str` both float around and could be passed to each other's functions, wrap them:
```python
TenantID = NewType("TenantID", str)
StatementID = NewType("StatementID", str)
```
*When you can skip it:* ad-hoc strings used only within a single function. `NewType` earns its keep on identifiers that cross modules often enough for a mixup to hurt.

### Avoid `Any`
`Any` erases contracts. If you reach for it, the type usually *exists* — you just haven't written it down yet.

*When you can skip it:* at serialisation boundaries (Bedrock response, raw DynamoDB item, inbound JSON) where the shape genuinely isn't known until parsed.

### Enums over string literals
For fixed vocabularies (`ProcessingStage`, `TokenReservationStatus`, etc.), use `Enum`. String literals compared by equality are where typos go to hide. `sp_common.enums` is the home for shared enums — add to it rather than coining new string constants.

### `Protocol` for structural typing
Use `Protocol` to describe what a collaborator must *look like* rather than what it must *inherit from*. Keeps modules decoupled.

## Module boundaries & coupling

### Depend on interfaces, not concrete classes
If a route handler imports `boto3` or calls `AccountingApi` directly, it can't be tested without hitting AWS or Xero. Depend on a repository / client interface (`Protocol` or abstract class). The concrete class lives in one place.

*When you can skip it:* stdlib and framework types you'll never swap (Flask's `request`, `session`, `json`, `uuid`).

### Pass collaborators as parameters, not via module-level imports
If a function needs the clock, the DB, or an HTTP client, take it as an argument with a sensible default. Tests can inject fakes without monkeypatching. `service/xero_repository.py` already does this for `api: AccountingApi | None = None` — follow that pattern.

*When you can skip it:* pure stateless helpers. Injection pays off for I/O and time; it's noise for pure computation.

### No circular imports
If module A imports from B and B imports from A, the boundary between them is wrong. Extract the shared surface into a third module — the `oauth_client.py` / `tenant_activation.py` split in `service/` exists for exactly this reason (see `project.md` → "Circular imports"). Do **not** paper over the problem with function-local imports; that's a smell, not a fix.

### Don't reach into other modules' internals
`_private` attributes, globals read from outside the module, runtime monkeypatching — all of these say "the public API is missing something." Add the missing public API instead.

### One module, one concept
When a file's name stops describing what's in it, split it. `service/utils/dynamo.py` currently owns statement queries, item-type updates, completion toggles, deletions, and S3 cleanup — split when a module starts listing four-plus concerns.

### Design for testability
If you can only test a function by monkeypatching a module global, the coupling is too tight — refactor to inject the collaborator. Unit tests are a pressure-test on design; listen to them.

## Structured data

Prefer, in order:
- **Pydantic models** — external inputs, JSON payloads, Bedrock responses (validation matters).
- **Dataclasses** — internal data containers (`@dataclass(frozen=True)` by default).
- **TypedDict** — when a dict shape is genuinely required but you still want typing.

Plain dicts remain common at boundaries (Flask request/session, Xero/Bedrock responses, DynamoDB items). Handle them explicitly, convert them early, and keep the untyped shape contained to the module that owns the boundary.

### Value objects are frozen
Dataclasses carrying data default to `@dataclass(frozen=True)`. Immutability makes equality, hashing, and concurrency safe. Opt out only when mutation is the point of the type.

## Control flow & data handling

### Guard clauses over nested `if`
Handle invalid or empty cases with early returns. The happy path reads straight down the left margin.

### Don't mutate function arguments
If a caller passes a list or dict and you `.append()` / `.update()` it, the caller's data changes too. Copy, don't mutate.

### No mutable default arguments
`def f(xs: list[str] = [])` is a bug — the list is shared across calls. Use `None` as the sentinel and build the default inside the function.

### Compare to `None` with `is`
`x is None`, `x is not None`. Never `x == None`.

### `pathlib.Path` for filesystem paths
Not `os.path`, not string concatenation. `Path` composes, normalises, and reads cleaner.

### Datetimes are always timezone-aware
`datetime.now(UTC)`, never `datetime.now()`. Store and compute in UTC; convert to local time only at the presentation layer.

### Context managers for every resource
Files, locks, connections, executors — every one uses `with`. `ThreadPoolExecutor` is a resource. `lock.acquire()` is a resource. Never leak one.

### f-strings for prose; kwargs for logs
`f"Reconciled {count} items"` for user-facing or exception strings. Structured log calls stay as `logger.info("reconciled items", count=count, tenant_id=tenant_id)` — lazy and structured. See `AGENTS.md` for full logging conventions.

## Errors

### Catch specific exceptions
Never bare `except:`. Avoid `except Exception:` except at genuine top-level boundaries (Flask error handlers, Step Function handlers) where anything must be converted into a response or a workflow failure.

### Re-raise with context
`raise BillingServiceError(...) from exc` — chaining preserves the original traceback and the causal chain.

### Domain-scoped exceptions
Each subsystem owns its own error type (`BillingServiceError`, `InsufficientTokensError`, `StatementUploadStartError`, `StatementJSONNotFoundError`, `PDFPageCountError`). Raise the narrowest applicable type so callers can catch intentionally; don't collapse distinct failure modes into one generic error.

### Preserve the API auth response shape
`/api/...` endpoints return `401` JSON on auth failure; UI routes redirect. Frontend JS depends on this split — don't change it without coordination.

## Concurrency

Boto3 does not officially support asyncio. Use `concurrent.futures.ThreadPoolExecutor` for concurrent I/O:
- Multiple DynamoDB / S3 calls in parallel.
- Concurrent Xero / Bedrock API requests.
- Boto3 clients are thread-safe; create one per thread for best performance.
- Prefer avoiding shared mutable state; use `threading.Lock` when you can't.
- The `aws_lambda_powertools` logger is thread-safe.

Every `ThreadPoolExecutor` is used with a `with` block.

## Repo-specific contracts

- **DynamoDB boolean-string contract**: fields like `Completed` are persisted as the strings `"true"` / `"false"`. Do not silently switch to native booleans without a coordinated migration.
- **Statement item IDs**: format `<statement_id>#item-XXXX`. The prefix relationship is relied on for parent/child queries — keep the format stable unless the persistence contract is being deliberately changed.
- **Step Functions payload size**: full extracted JSON is stored in S3, not passed between states. Keep this split — don't inline large payloads into the state machine input/output.
- **API auth split**: `/api/...` → `401` JSON; UI → redirect. Both are part of the contract with the frontend.
- **Directory responsibilities**: `service/` (Flask web app), `lambda_functions/` (extraction + tenant erasure Lambdas), `cdk/` (infrastructure), `common/sp_common` (shared enums / helpers). Don't cross these lines without a reason.

## Common foot-guns

- Don't disable lint rules without a documented reason.
- Don't change behaviour for style-only reasons.
- Don't ignore type errors with `# type: ignore` without a comment explaining why.
- Don't use `typing.List`, `typing.Dict` — use `list`, `dict`.
- Don't repeat yourself — extract common logic.
- Don't add production deps without explicit request (see `AGENTS.md`).
