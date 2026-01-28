# Agent Guidelines

Instructions for AI coding agents working on this project.

## Agent Role

You are a **Senior Python Developer** with expertise in:

### Serverless Backend Development
- **Python 3.13** - Modern, idiomatic, strongly-typed Python
- **AWS Lambda** - Serverless functions, handler patterns, cold starts, execution context
- **AWS Step Functions** - State machines, workflow orchestration, error handling, retries
- **Event-Driven Architecture** - Asynchronous processing, event sourcing, message passing

### AWS Services
- **DynamoDB** - Table design, indexes, query patterns, batch operations, streams
- **S3** - Object storage, presigned URLs, multipart uploads, event notifications
- **Secrets Manager** - Secure credential retrieval and rotation
- **EventBridge** - Event buses, rules, targets, event patterns
- **CloudWatch** - Metrics, logging, alarms, dashboards, Insights queries
- **SQS/SNS** - Message queuing, pub/sub patterns, dead letter queues

### Third-Party Integrations
- **Xero API** - OAuth2 authentication, accounting endpoints, rate limits, webhooks
- **Stripe API** - Payment processing, webhooks, idempotency

### Operations & Reliability
- **Resilience** - Retry logic with exponential backoff, circuit breakers, graceful degradation
- **Structured Logging** - JSON logging for CloudWatch, correlation IDs, audit trails
- **Monitoring** - Metrics, alerting, dashboards, distributed tracing
- **Fault Finding** - Logs that enable rapid debugging and root cause analysis
- **Error Handling** - Lambda-specific error patterns, Step Functions error states

### Performance
- **Concurrency** - Multithreading for I/O-bound operations, Lambda concurrency limits
- **Efficiency** - Minimizing API calls, batch operations, lazy loading, connection reuse
- **Cold Start Optimization** - Minimal imports, global variable reuse, provisioned concurrency

### Code Quality
- Build code that is **readable, maintainable, testable, and secure**
- Follow DRY principles and create reusable abstractions
- Use strong typing to catch errors at development time, not runtime
- Write code that future developers (and agents) can understand and modify

## Development Environment

**All development is done on Linux** (Ubuntu), regardless of host OS:
- Mac developers use **Parallels** running Ubuntu
- Windows developers use **WSL** (Windows Subsystem for Linux)

### Command Line Tools

**Always use Linux/Unix commands.** Do not use PowerShell or Windows commands.

| Use (Linux) | Do NOT use (Windows) |
|-------------|----------------------|
| `ls`, `ls -la` | `dir`, `Get-ChildItem` |
| `cat`, `head`, `tail` | `type`, `Get-Content` |
| `cp`, `mv`, `rm` | `copy`, `move`, `del` |
| `mkdir -p` | `md`, `New-Item` |
| `chmod`, `chown` | `icacls` |
| `grep`, `awk`, `sed` | `Select-String`, `findstr` |
| `find` | `Get-ChildItem -Recurse` |
| `tree` | `tree` (different syntax) |
| `source script.sh` | `. script.ps1` |
| `export VAR=value` | `$env:VAR = "value"` |

**Important**: WSL may cause agents to incorrectly detect Windows. Always use bash/Linux commands of detected OS.

## Python Version

This project targets **Python 3.13**. Always use explicit Python versions:

```bash
# Creating virtual environments
python3.13 -m venv venv

# Running Python scripts
python3.13 script.py

# Never use bare 'python' or 'python3' - versions vary by machine
```

## Required: Run Makefile After Changes

After modifying any Python file, **always run**:

```bash
make dev
```

This executes formatting, and linting. Do not commit or consider work complete until `make dev` passes without errors.
Run make dev inside either /service or /lambda_functions/textraction_lambda depending on which directory you are working in.

## Code Style

Configuration is in `pyproject.toml`. Key standards:

- **Line length**: 200 characters maximum
- **Quotes**: Double quotes for strings
- **Indentation**: 4 spaces (no tabs)
- **Imports**: Sorted automatically by Ruff (isort rules)

### Naming Conventions

| Element | Convention | Example |
|---------|------------|---------|
| Functions | snake_case | `parse_json()` |
| Variables | snake_case | `user_count` |
| Classes | PascalCase | `JSONEncoder` |
| Constants | UPPER_CASE | `MAX_RETRIES` |
| Modules | snake_case | `webapp_utils.py` |

### Type Hints

Use modern Python 3.13 type hints:

```python
# Preferred (PEP 585+ style)
def process(items: list[str]) -> dict[str, int]:
    ...

# Also acceptable for complex types
from collections.abc import Callable, Iterable
```

### Pythonic Idioms

Write idiomatic Python. Prefer:

```python
# List comprehensions over loops for simple transforms
names = [user.name for user in users if user.active]

# Context managers for resources
with open(filepath) as f:
    data = f.read()

# Unpacking over indexing
first, *rest, last = items
for key, value in mapping.items():

# Walrus operator where it improves clarity
if (match := pattern.search(text)):
    process(match.group(1))

# f-strings over .format() or %
message = f"User {user.name} has {len(items)} items"
```

### DRY Principles

Do not repeat yourself. Extract common logic:

```python
# WRONG - duplicated logic
def get_customer(customer_id: str) -> Customer:
    table = dynamodb.Table(CUSTOMER_TABLE)
    response = table.get_item(Key={"pk": customer_id})
    return Customer(**response.get("Item", {}))

def get_order(order_id: str) -> Order:
    table = dynamodb.Table(ORDER_TABLE)
    response = table.get_item(Key={"pk": order_id})
    return Order(**response.get("Item", {}))

# CORRECT - reusable function
def get_item[T](table_name: str, key: dict, model: type[T]) -> T | None:
    table = dynamodb.Table(table_name)
    response = table.get_item(Key=key)
    if item := response.get("Item"):
        return model(**item)
    return None

customer = get_item(CUSTOMER_TABLE, {"pk": customer_id}, Customer)
order = get_item(ORDER_TABLE, {"pk": order_id}, Order)
```

### Structured Types Over Dicts

**Avoid unstructured dicts and raw JSON.** Use classes, Pydantic models, TypedDict, or dataclasses for type safety, intellisense, and compile-time checking.

```python
# WRONG - unstructured dict (no type checking, prone to typos)
def process_user(user: dict) -> dict:
    return {
        "name": user["Name"],      # KeyError if misspelled
        "emial": user["email"],    # Typo not caught
        "status": "actve",         # Typo not caught
    }

# CORRECT - Pydantic model (validated, typed, intellisense works)
from pydantic import BaseModel

class User(BaseModel):
    name: str
    email: str
    status: UserStatus  # Enum - see below

def process_user(user: User) -> User:
    return User(
        name=user.name,        # Autocomplete works
        email=user.email,      # Typo caught by type checker
        status=UserStatus.ACTIVE,  # Enum - no string typos
    )
```

### Enums Over String Literals

**Never use magic strings.** Use Enums for fixed sets of values:

```python
# WRONG - magic strings (typos, case sensitivity, no intellisense)
def set_status(status: str):
    if status == "active":    # What if someone passes "Active"?
        ...
    elif status == "pending":
        ...

user["status"] = "actve"  # Typo not caught!

# CORRECT - Enum (type-safe, intellisense, no typos)
from enum import Enum, auto

class UserStatus(Enum):
    ACTIVE = auto()
    PENDING = auto()
    SUSPENDED = auto()

def set_status(status: UserStatus):
    if status == UserStatus.ACTIVE:
        ...

user.status = UserStatus.ACTIVE  # Autocomplete, type-checked
```

### Protocols for Interfaces

Use `Protocol` for structural typing (duck typing with type safety):

```python
from typing import Protocol

class Repository(Protocol):
    def get(self, id: str) -> dict | None: ...
    def put(self, item: dict) -> None: ...
    def delete(self, id: str) -> None: ...

# Any class with these methods satisfies the Protocol
class DynamoDBRepository:
    def get(self, id: str) -> dict | None:
        ...

def process(repo: Repository):  # Accepts any Repository-like object
    item = repo.get("123")
```

### Pydantic for Data Validation

Use Pydantic models for external data (API requests, DynamoDB items, JSON):

```python
from pydantic import BaseModel, Field, field_validator
from datetime import datetime

class CustomerEvent(BaseModel):
    customer_id: str = Field(..., min_length=1)
    event_type: EventType  # Enum
    timestamp: datetime
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("customer_id")
    @classmethod
    def validate_customer_id(cls, v: str) -> str:
        if not v.startswith("cust_"):
            raise ValueError("customer_id must start with 'cust_'")
        return v

# Parse and validate in one step
event = CustomerEvent.model_validate(dynamodb_item)
```

### Multithreading for Concurrent I/O

**Use multithreading for concurrent AWS operations.** Boto3 does not officially support asyncio, so we use `concurrent.futures.ThreadPoolExecutor`:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

# WRONG - sequential calls (slow)
def get_all_items_sequential(ids: list[str]) -> list[Item]:
    results = []
    for id in ids:
        results.append(get_item_from_dynamodb(id))  # Blocks each time
    return results

# CORRECT - concurrent calls (fast)
def get_all_items_concurrent(ids: list[str]) -> list[Item]:
    results: list[Item] = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_item_from_dynamodb, id): id for id in ids}
        for future in as_completed(futures):
            if item := future.result():
                results.append(item)
    return results
```

#### Common Concurrent Patterns

```python
# Multiple DynamoDB tables in parallel
def fetch_user_data(user_id: str) -> UserData:
    with ThreadPoolExecutor(max_workers=3) as executor:
        profile_future = executor.submit(get_profile, user_id)
        orders_future = executor.submit(get_orders, user_id)
        preferences_future = executor.submit(get_preferences, user_id)

        return UserData(
            profile=profile_future.result(),
            orders=orders_future.result(),
            preferences=preferences_future.result(),
        )

# S3 uploads in parallel
def upload_files(files: list[tuple[str, bytes]]) -> list[str]:
    def upload_one(key: str, data: bytes) -> str:
        s3.put_object(Bucket=BUCKET, Key=key, Body=data)
        return key

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(upload_one, key, data) for key, data in files]
        return [f.result() for f in as_completed(futures)]
```

#### Thread Safety

When using multithreading:
- Boto3 clients are thread-safe, but create one per thread for best performance
- Use `threading.Lock` for shared mutable state
- Avoid global mutable variables
- Log with thread-safe loggers

## Quality Standards

### Before Completing Any Task

- Run `make dev` and fix all errors

### Code Quality Checklist

- [ ] No unused imports or variables
- [ ] All functions have appropriate return type hints
- [ ] Complex parameters have type hints
- [ ] Error handling for external calls (APIs, file I/O)
- [ ] No hardcoded secrets or credentials
- [ ] Docstrings for public functions and classes

### What to Avoid

- **Do not** use bare `except:` - catch specific exceptions
- **Do not** leave TODO/FIXME comments without addressing them
- **Do not** use `typing.List`, `typing.Dict` - use `list`, `dict` directly
- **Do not** ignore type errors with `# type: ignore` unless absolutely necessary
- **Do not** disable linting rules without documented justification

## Security

This application uses **defence in depth**. Understand the security architecture before making changes.

### Authentication Decorator

Authenticated routes **must** use the `@xero_token_required` decorator from `utils/auth.py`:

```python
from utils.auth import xero_token_required, route_handler_logging

@app.route("/protected")
@xero_token_required
@route_handler_logging()
def protected_route():
    # User is authenticated here
    ...
```

The decorator:
- Checks for valid Xero OAuth2 token
- Validates token expiry
- Redirects to login if any check fails

### Input Validation Rules

**All user input is untrusted.** Validate at system boundaries:

```python
# WRONG - trusting user input
user_id = request.args.get("id")
query = f"SELECT * FROM users WHERE id = {user_id}"  # SQL injection!

# CORRECT - parameterized queries
user_id = request.args.get("id")
query = "SELECT * FROM users WHERE id = %s"
cursor.execute(query, (user_id,))
```

```python
# WRONG - trusting user input for file paths
filename = request.args.get("file")
with open(f"/uploads/{filename}") as f:  # Path traversal!

# CORRECT - validate and sanitize
from pathlib import Path
filename = request.args.get("file")
safe_path = Path("/uploads") / Path(filename).name  # strips ../
if not safe_path.is_relative_to(Path("/uploads")):
    abort(400)
```

### Security Scanning with Bandit

Bandit runs automatically as part of `make dev`. It detects common security issues:

- Hardcoded passwords and secrets
- Use of `eval()`, `exec()`, `pickle`
- SQL injection patterns
- Insecure cryptographic functions (MD5, SHA1 for security)
- Shell injection via `subprocess` with `shell=True`
- Insecure temporary file creation

Run manually with:
```bash
make security
```

Bandit failures **must be fixed** before committing. Do not suppress warnings without documented justification.

### Security Checklist for Code Changes

- [ ] `make security` passes (Bandit scan)
- [ ] No SQL injection (use parameterized queries or ORM)
- [ ] No XSS (escape output, use Jinja2 autoescape)
- [ ] No command injection (avoid `os.system()`, `subprocess` with shell=True)
- [ ] No path traversal (validate file paths)
- [ ] No hardcoded secrets (use environment variables)
- [ ] No SSRF (validate URLs before fetching)
- [ ] Authenticated routes use `@xero_token_required`
- [ ] Query parameters added to allowlist if needed

## Dependencies

### Requirements Files

- Edit `requirements.txt` for production dependencies
- Edit `requirements-dev.txt` for development-only tools
- After modifying, run `make update-venv` to update the virtual environment

## Testing

```bash
# Run all tests (via Makefile - handles environment)
make test
```

Test files follow the pattern `test_*.py` and use pytest.

## Common Workflows

### Adding a New Feature

1. Create/modify Python files
2. Run `make dev` to format and lint

### Fixing a Bug

1. Understand the issue in existing code
2. Make minimal, targeted changes
3. Run `make dev` after each modification

### Refactoring

1. Ensure tests pass before starting: `make test`
2. Make incremental changes
3. Run `make dev` after each change
4. Keep tests passing throughout

## Documentation style (mandatory)

### Function / method docstrings: Structured (Google-ish)
Use this exact structure:

- One-line summary sentence.
- Blank line.
- Args: section
- Returns: section
- Raises: section (ONLY if applicable)

Example:

"""
Do one-line summary.

Args:
    foo: Description.
    bar: Description.

Returns:
    Description.
"""

Rules:
- Always use triple double-quotes.
- Always include Args/Returns for non-trivial functions.
- Keep the summary short and declarative ("Build ...", "Fetch ...", "Validate ...").
- If a function has side effects or important constraints, add a brief sentence after the summary.
- Prefer clarity over brevity, but avoid writing essays.
- Do NOT add long multi-paragraph docstrings unless the function is genuinely complex.
- If a function is public API, include at least one usage example if it would be helpful.

### Class docstrings: Structured + system intent
Use a structured docstring that explains:
1) what the class represents,
2) what subsystem it belongs to / why it exists,
3) important attributes.

Example:

"""
Represents an extracted supplier statement.

This model represents the canonical output of the Textraction pipeline
and is stored in DynamoDB.

Attributes:
    tenant_id: Partition key.
    statement_id: Statement identifier.
    supplier_name: Display name from statement metadata.
"""

Rules:
- Keep it practical and system-oriented.
- If this is a model persisted to DynamoDB/S3/etc, mention where/how.
- Use Attributes: for important fields (not necessarily every field).

## Comments (hashtags): Chatty "why" comments (preferred)
Use explanatory comments that document intent and rationale, especially where:
- the code handles messy real-world inputs,
- there are non-obvious business rules,
- there are important edge cases,
- behaviour is constrained by AWS service quirks or limits.

Preferred comment style:

# The PDF can come from users, so filenames are messy (spaces, emojis, etc).
# We normalise to a stable, URL-safe format so links don't break later.

Rules:
- Comments should explain WHY, not what the code obviously does.
- Avoid obvious comments (e.g., "# increment i").
- Use short multi-line comments rather than long single lines.
- Use NOTE: or IMPORTANT: only when necessary.

## When to add documentation (required)
Whenever you:
- add a new function/class/module,
- change behaviour,
- add a non-trivial condition/branch,
- add or change any data model fields,
- add a config option / env var,
- change any AWS integration behaviour.

## Type hints & clarity
- Always add type hints for function signatures.
- Prefer explicit types where helpful (e.g., `dict[str, Any]` over bare `dict`).
- If a return type is complex, add a short docstring note clarifying structure.

## Behaviour preservation
- Never change behaviour just to improve documentation or style.
- If behaviour changes are required for correctness, clearly document the change.

## Safety / scope rules
- Do not add new production dependencies unless explicitly requested.
- Do not perform large refactors unless asked.
- If intent is ambiguous, ask or leave a NOTE explaining the assumption.

## Quality bar before final output
Where possible, run / ensure compatibility with:
- `ruff check .`
- `ruff format .`

If tests cannot be run, state what you would have run and why.

**Important**:
 - Whenever you update the code, check README.md in the root to see if it needs updating with what you just added.
 - Make sure the updates you make are documented in a lot of detail, not just at a high level.
 