# AGENTS.md

# DJANGO BACKEND — READ THIS BEFORE EDITING

This file defines the working rules for the Django backend.

Before modifying backend code, establish the actual environment from the repository.

Do **not** guess:

* Python version
* Django version
* Django REST Framework version
* database engine
* dependency manager
* test runner
* settings module
* task queue
* cache implementation
* authentication mechanism
* deployment environment

Inspect the repository first.

---

# Mandatory preflight

Before debugging or changing backend code, inspect the relevant project configuration.

Check whichever of these exist:

```text
pyproject.toml
requirements.txt
requirements/*.txt
Pipfile
Pipfile.lock
poetry.lock
uv.lock
setup.cfg
tox.ini
pytest.ini
manage.py
docker-compose.yml
docker-compose.yaml
compose.yml
Dockerfile
.env.example
```

Determine:

```text
Python version
Django version
dependency manager
database engine
test command
lint command
format command
type-check command
settings module
```

Do not change source in response to an environment error until dependencies and configuration are known to be correct.

---

# Source-of-truth priority

When information conflicts, use this priority:

1. Current repository code
2. Locked dependency files
3. Current project configuration
4. Existing tests
5. Existing project patterns
6. Documentation for the exact installed dependency version
7. Generic/current documentation only as secondary guidance

Do not rewrite working code merely because newer Django documentation recommends another API.

---

# Establish the environment before debugging

If the repository uses a virtual environment, activate or use it before trusting Python diagnostics.

Examples may include:

```bash
source .venv/bin/activate
```

or commands provided by the project's package manager.

Confirm the interpreter when useful:

```bash
python --version
```

Confirm Django when useful:

```bash
python -m django --version
```

Do not install or upgrade packages merely because an import fails.

First determine whether:

* the correct virtual environment is active
* dependencies are installed
* the dependency lock is current
* the command is running from the correct directory
* the correct settings module is being used

---

# Do not upgrade dependencies unless requested

Do not silently upgrade:

```text
Python
Django
Django REST Framework
Celery
Redis clients
database drivers
authentication libraries
storage libraries
testing libraries
linting tools
```

while fixing an unrelated bug.

Dependency upgrades are separate changes unless explicitly requested.

When changing a dependency:

1. determine the currently locked version
2. confirm compatibility with the project's Python and Django versions
3. make the smallest required version change
4. update the proper lock/dependency file
5. run relevant tests
6. inspect the resulting dependency diff

Do not replace pinned dependencies with `latest`.

---

# Django project discovery

Before changing Django-specific behavior, locate:

```text
manage.py
settings package
root urls.py
installed apps
middleware
authentication configuration
database configuration
REST framework configuration
cache configuration
Celery configuration, if present
storage configuration
```

Do not assume conventional paths if the repository uses a different layout.

---

# Understand the local architecture before editing

For a non-trivial task, inspect the surrounding code first.

Identify whether this project uses layers such as:

```text
models
managers/querysets
repositories
selectors
services
serializers
schemas
forms
views/viewsets
permissions
tasks
signals
domain modules
API clients
```

Follow the existing architecture.

Do not create a new abstraction merely because it is common in another Django project.

If business logic is consistently placed in services, do not move it into serializers.

If query logic is consistently placed in selectors or custom QuerySets, follow that convention.

If the project intentionally keeps logic on models, do not introduce a service layer without a concrete reason.

---

# Debugging protocol

When a backend error appears:

1. reproduce the problem
2. verify the environment
3. inspect the traceback completely
4. identify the first relevant application frame
5. inspect the surrounding implementation
6. inspect relevant tests
7. inspect model/schema/database assumptions
8. form a root-cause hypothesis
9. make the smallest correct change
10. run focused validation
11. run broader validation when warranted
12. review the diff

Avoid:

```text
error message
→ web search
→ copy first generic Django fix
```

Prefer:

```text
error
→ reproduce
→ inspect environment
→ inspect traceback
→ inspect project architecture
→ determine root cause
→ patch
→ verify
```

---

# Database safety

Treat database operations as potentially destructive.

Do not run destructive commands without explicit need.

Never casually run commands equivalent to:

```text
DROP DATABASE
DROP TABLE
TRUNCATE
flush
reset_db
delete all rows
```

Do not delete production or shared-development data.

Do not assume a database is disposable.

Do not change database credentials or connection configuration without a concrete task requiring it.

---

# Migrations are part of the codebase

Django migrations must be treated as source code.

When model changes require migrations, create them using the project's normal workflow.

Typically:

```bash
python manage.py makemigrations
```

Then inspect the generated migration before accepting it.

Do not blindly trust generated migration output.

Check for:

* unexpected field changes
* unexpected index changes
* destructive operations
* nullable/non-nullable transitions
* default values
* table renames
* field renames detected as remove/add
* large data rewrites
* database-specific behavior

---

# Do not rewrite migration history casually

Do not:

* edit old applied migrations merely to make them prettier
* delete migrations to solve an unrelated problem
* squash migrations without being asked
* renumber migration files
* regenerate the entire migration chain
* fake migrations without understanding why
* change an already-deployed migration casually

Prefer a new migration for changes to an existing deployed schema.

Historical migrations may need to remain compatible with fresh database creation.

---

# Data migrations require special care

For data migrations, prefer migration-safe model access:

```python
apps.get_model(...)
```

rather than importing current application models directly.

Remember that migrations may be run in the future when current model code has changed.

When practical, make data migrations:

* deterministic
* restart-safe
* bounded in memory usage
* compatible with existing rows
* safe for the expected database size

For large tables, consider batching rather than loading every row at once.

Do not introduce expensive data migrations casually.

---

# Model changes

Before changing a model, inspect:

* existing migrations
* related serializers/forms
* admin configuration
* constraints
* indexes
* managers/querysets
* API behavior
* tests
* signals/tasks that consume the model

Changing a field can affect far more than `models.py`.

Do not assume a model change is complete merely because Django starts.

---

# Null versus blank

Remember Django's distinction:

```text
null
→ database storage

blank
→ validation/form behavior
```

Do not mechanically add both.

Use the project's existing conventions and the semantics of the field.

Be especially careful with string fields and uniqueness constraints.

---

# Foreign keys and deletion behavior

Do not change `on_delete` behavior without understanding the domain consequences.

Review whether the relationship should use behavior such as:

```text
CASCADE
PROTECT
RESTRICT
SET_NULL
SET_DEFAULT
DO_NOTHING
```

Do not default to `CASCADE` just because it is convenient.

Changes can cause data loss.

---

# Query performance

Avoid accidental N+1 queries.

When adding API endpoints, serializers, loops, or model access patterns, inspect related database access.

Use tools such as:

```python
select_related(...)
prefetch_related(...)
```

where appropriate.

Do not add eager loading blindly.

Use it when it matches the relationships actually accessed.

For performance-sensitive code, reason about the number and shape of queries.

---

# Do not hide query problems with premature caching

Do not introduce Redis/cache behavior merely to hide inefficient ORM queries.

First determine whether the database access itself is correct.

Use caching only when it is consistent with the project's architecture and invalidation strategy.

---

# Transactions

Use transactions when a business operation must be atomic.

Typical pattern:

```python
from django.db import transaction

with transaction.atomic():
    ...
```

Do not wrap large or slow operations in transactions unnecessarily.

Do not perform slow external network calls inside a database transaction unless there is a specific architectural reason.

Keep transaction boundaries intentional.

---

# Concurrency

When modifying code that can execute concurrently, consider:

* duplicate requests
* background workers
* retries
* race conditions
* lost updates
* double creation
* inventory/counter consistency
* idempotency

Do not assume:

```python
if not exists:
    create()
```

is concurrency-safe.

Use database constraints, transactions, locking, or idempotent patterns when required by the domain.

---

# Database constraints are valuable

When an invariant must always hold, prefer enforcing it at the database level when appropriate.

Examples:

```text
unique constraints
check constraints
foreign keys
non-null constraints
```

Application validation alone may be insufficient under concurrency.

Do not add constraints without checking existing data and migration implications.

---

# Django REST Framework

If this project uses Django REST Framework, inspect existing API conventions before modifying endpoints.

Determine whether the codebase uses:

```text
APIView
GenericAPIView
ViewSets
ModelViewSet
routers
serializers
service functions
selectors
custom exception handlers
custom pagination
custom permissions
```

Follow the established pattern.

Do not convert an endpoint to a different DRF abstraction merely for stylistic reasons.

---

# Serializer responsibilities

Before adding business logic to a serializer, inspect the architecture.

Serializers should not automatically become the application's service layer.

Avoid hiding complex side effects inside:

```python
validate()
create()
update()
```

unless that is already the established convention.

Business logic should live where this project consistently places business logic.

---

# API compatibility

Treat existing API behavior as a contract unless the task explicitly changes it.

Be cautious about changing:

* HTTP status codes
* response field names
* response nesting
* nullability
* ordering
* pagination
* error formats
* validation messages
* authentication requirements
* permission rules

A cleaner implementation is not worth silently breaking API clients.

---

# Authentication

Do not assume the authentication mechanism.

Inspect configuration and existing code first.

The project may use:

```text
Django sessions
DRF TokenAuthentication
JWT
OAuth
social authentication
custom authentication
multiple authentication methods
```

Do not replace authentication mechanisms while fixing unrelated behavior.

Never log:

```text
passwords
tokens
authorization headers
session secrets
refresh tokens
private keys
```

---

# Authorization and permissions

Authentication and authorization are different.

A user being authenticated does not imply they may access an object.

When changing an endpoint, inspect:

* global permission classes
* view permissions
* object-level permissions
* queryset filtering
* ownership checks
* organization/tenant boundaries

Do not rely solely on UI restrictions for backend authorization.

---

# Multi-tenant boundaries

If this application is multi-tenant, organization-scoped, team-scoped, or workspace-scoped, treat tenant filtering as a security boundary.

Never remove or weaken scope filters casually.

When querying user-owned or tenant-owned objects, verify that the request cannot access another tenant's data.

Prefer established scoped managers/selectors/querysets.

---

# Input validation

Validate untrusted input at the appropriate boundary.

Do not trust:

```text
request.data
query parameters
headers
uploaded files
webhook payloads
external API responses
```

merely because TypeScript or frontend validation exists.

The backend is the authoritative security boundary.

---

# SQL safety

Prefer Django ORM parameterization.

Do not construct SQL by interpolating untrusted values.

If raw SQL is genuinely required, parameterize values properly.

Never introduce SQL string concatenation from request-controlled data.

---

# File upload safety

When handling uploads, consider:

* file size
* content type
* extension
* storage backend
* filename handling
* path traversal
* duplicate names
* public/private access
* asynchronous processing

Do not trust the original filename as a safe filesystem path.

Follow existing storage conventions.

---

# External API calls

When calling external services:

* use the existing API client abstraction
* set appropriate timeouts
* handle expected failures
* avoid logging secrets
* consider retries carefully
* avoid retrying non-idempotent actions blindly

Do not scatter new `requests`/HTTP-client calls throughout views if the project already has dedicated clients/services.

---

# Background tasks

If this project uses Celery, Django-Q, RQ, Dramatiq, or another task system, inspect existing task patterns before adding new jobs.

Tasks should be designed with retries in mind.

Where possible, make tasks idempotent.

Do not assume a task executes exactly once.

Be careful with:

* duplicate delivery
* partial completion
* retries
* transaction timing
* stale database objects
* deleted objects

If a task depends on committed database state, consider transaction boundaries before enqueueing it.

---

# Signals

Do not introduce Django signals merely to avoid an explicit function call.

Signals make control flow less visible.

Use them when they are already an intentional architectural pattern or when they genuinely fit the problem.

Before modifying a model, inspect whether:

```text
pre_save
post_save
pre_delete
post_delete
m2m_changed
```

or custom signals depend on it.

---

# Settings and environment variables

Do not hardcode secrets or environment-specific values.

Use the project's existing settings/configuration mechanism.

Never commit:

```text
SECRET_KEY
database passwords
API secrets
private keys
production credentials
access tokens
```

Do not invent new environment-variable names if an existing configuration mechanism already covers the need.

If adding a required environment variable, update the appropriate example/config documentation when that is part of the project's convention.

---

# Environment files

Treat real `.env` files as sensitive.

Do not expose their contents in logs, tests, documentation, or responses.

When examples are needed, use:

```text
.env.example
```

or placeholder values.

Never copy production secrets into example configuration.

---

# Logging

Use the project's logging configuration.

Prefer structured, useful logs over ad-hoc `print()` statements.

Do not log sensitive information.

Logs should provide enough context to diagnose failures without leaking user data or credentials.

Do not leave temporary debugging output behind unless intentionally required.

---

# Exceptions

Do not catch broad exceptions merely to silence errors.

Avoid patterns such as:

```python
try:
    ...
except Exception:
    pass
```

unless there is a very specific justified reason.

Catch the narrowest expected exception.

Preserve useful traceback/context.

For APIs, use the project's established exception/error-response mechanism.

---

# Tests

Before changing behavior, inspect relevant tests.

When fixing a bug:

1. reproduce the bug when practical
2. add or update a regression test when appropriate
3. implement the smallest fix
4. run the focused test
5. run the related test module/app
6. run the broader suite when warranted

Do not delete or weaken a legitimate assertion just to make a failing test pass.

---

# Discover the test runner before using it

Do not assume the project uses pytest.

Look for:

```text
pytest.ini
pyproject.toml
tox.ini
manage.py
tests/
```

The project may use:

```bash
pytest
```

or:

```bash
python manage.py test
```

or another wrapper command.

Use the repository's existing test command.

---

# Test database safety

Tests should use the configured test database.

Do not point tests at:

```text
production
staging
shared development databases
```

Do not change database settings merely to make tests easier to run.

Be careful with commands that preserve or reuse test databases.

---

# Mocking

Mock external boundaries, not arbitrary internal implementation details.

Prefer testing observable behavior.

Avoid excessive mocking that causes tests to pass while real integrations fail.

Use the existing project testing style.

---

# Time-dependent code

Use timezone-aware Django utilities where appropriate.

Prefer:

```python
from django.utils import timezone

timezone.now()
```

over naive datetime usage in Django domain code unless the project has a specific reason otherwise.

Do not mix naive and aware datetimes casually.

---

# Time zones

Do not assume server-local time equals user-local time.

Inspect:

```text
USE_TZ
TIME_ZONE
```

and existing project conventions.

Store and compare timestamps consistently.

Only convert to user-local time at the appropriate boundary.

---

# Security-sensitive changes

Be especially cautious when touching:

* authentication
* authorization
* password handling
* token handling
* webhook verification
* file uploads
* redirects
* CORS
* CSRF
* sessions
* cookies
* encryption
* secrets
* tenant filtering
* administrative actions

For these areas, inspect existing security assumptions before modifying behavior.

Do not disable protections merely to make development easier.

---

# CSRF and CORS are not interchangeable

Do not solve a CSRF problem by arbitrarily changing CORS configuration.

Do not solve a CORS problem by disabling CSRF globally.

Understand whether the client uses:

```text
session authentication
token authentication
JWT
cross-origin cookies
```

before changing security configuration.

---

# Admin changes

Django admin is privileged functionality.

When adding admin actions or editing admin behavior:

* preserve authorization
* avoid destructive bulk behavior without confirmation
* consider query performance
* avoid exposing secrets
* use existing project conventions

Do not treat admin code as security-insensitive merely because staff users access it.

---

# Management commands

Management commands may operate on real data.

Before changing or running one, inspect what database/environment it affects.

For destructive commands, prefer:

* dry-run support
* explicit arguments
* clear logging
* transactions where suitable
* idempotency where practical

Do not run destructive management commands casually.

---

# Performance

Do not optimize without evidence, but avoid obvious regressions.

Watch for:

* N+1 queries
* repeated expensive queries
* unbounded QuerySets
* loading entire tables into memory
* serialization of unnecessary fields
* expensive work during request handling
* unnecessary external API calls
* repeated cache misses
* large transactions

Use pagination for potentially large API collections unless the API intentionally behaves otherwise.

---

# QuerySet evaluation

Remember that Django QuerySets are lazy.

Be intentional when operations cause evaluation.

Avoid unnecessary conversion such as:

```python
list(queryset)
```

when streaming/lazy behavior is preferable.

Do not call:

```python
len(queryset)
```

when:

```python
queryset.count()
```

is the intended database operation.

Do not call:

```python
queryset.count()
```

when the rows are already loaded and `len(...)` is more appropriate.

Choose based on actual context.

---

# Existence checks

When only existence matters, prefer established efficient patterns such as:

```python
queryset.exists()
```

rather than loading objects unnecessarily.

When the object itself is required immediately afterward, avoid introducing redundant queries solely for stylistic purity.

Optimize the complete access pattern, not a single line in isolation.

---

# Bulk operations

Be cautious with:

```python
bulk_create()
bulk_update()
QuerySet.update()
QuerySet.delete()
```

These may bypass model methods, validation, or signals depending on the operation.

Before introducing them, verify whether the application relies on those hooks.

---

# Formatting and linting

Use the repository's configured tools.

Possible tools include:

```text
ruff
black
isort
flake8
pylint
mypy
pyright
django-stubs
```

Do not introduce another formatter/linter merely because you prefer it.

Do not reformat unrelated files.

Keep diffs focused.

---

# Type checking

If the project uses Python type checking, preserve its conventions.

Do not silence type errors with broad `Any`, casts, or ignores unless justified.

If Django typing helpers such as `django-stubs` are present, follow their established configuration.

Do not add type-checking infrastructure as part of an unrelated task.

---

# API schema

If the project generates OpenAPI or another API schema, consider whether endpoint changes require schema updates.

Follow the existing schema framework.

Examples may include:

```text
drf-spectacular
drf-yasg
Django Ninja
custom schema generation
```

Do not invent annotations for a framework the project does not use.

---

# Generated files

Do not manually edit generated artifacts unless they are intentionally source-controlled and meant to be edited.

Examples can include:

```text
OpenAPI output
generated clients
compiled translations
coverage reports
static build output
```

Find the source and generation command instead.

---

# Static and media files

Do not confuse:

```text
STATIC_ROOT / STATIC_URL
MEDIA_ROOT / MEDIA_URL
```

Static assets and user-uploaded media have different semantics.

Do not expose private media merely to fix a URL problem.

Follow the configured storage backend.

---

# Docker

If the backend runs in Docker, determine whether commands should run:

```text
on the host
inside the application container
through docker compose
```

before debugging environment-specific failures.

Do not assume a host Python environment is equivalent to the container environment.

Inspect:

```text
Dockerfile
compose files
entrypoint scripts
```

first.

---

# CI is part of the contract

Inspect CI configuration when local behavior disagrees with automated checks.

Possible locations include:

```text
.github/workflows/
.gitlab-ci.yml
tox.ini
noxfile.py
Makefile
scripts/
```

Use the same commands CI uses where practical.

Do not "fix" CI by weakening checks without understanding why they fail.

---

# Git discipline

Do not revert unrelated user changes.

Do not reset files merely because they differ from the main branch.

Do not discard uncommitted work.

Before modifying a file, inspect relevant existing changes when necessary.

Keep the patch scoped to the requested task.

Avoid unrelated formatting churn.

---

# Do not hide failures

Do not solve errors by broadly:

* swallowing exceptions
* skipping tests
* weakening validation
* removing constraints
* disabling middleware
* disabling authentication
* disabling permissions
* disabling CSRF
* disabling CORS restrictions
* turning off migrations
* adding blanket `# noqa`
* adding blanket type ignores

Find and address the actual root cause.

---

# Non-trivial implementation protocol

For substantial backend changes:

1. inspect architecture
2. inspect relevant models
3. inspect callers
4. inspect API surface
5. inspect tests
6. inspect migrations if database behavior is involved
7. form a plan
8. implement the smallest coherent change
9. run focused tests
10. run broader tests/checks as appropriate
11. inspect the final diff
12. check for migration/API/security consequences

---

# Self-review requirements

Before considering a backend change complete, review the diff as a skeptical senior engineer.

Check for:

* incorrect assumptions
* missing authorization checks
* missing tenant scoping
* migration problems
* accidental schema changes
* N+1 queries
* race conditions
* non-atomic multi-step writes
* duplicate task execution
* poor exception handling
* leaked secrets
* API compatibility breaks
* untested edge cases
* unnecessary dependency changes
* unrelated edits
* formatting churn

Fix issues discovered during review before considering the task complete.

---

# Completion checklist

Before finishing a backend task:

```text
[ ] Actual Python/Django environment was established
[ ] Existing architecture was inspected
[ ] Root cause was identified before editing
[ ] Change is limited to task scope
[ ] Database implications were considered
[ ] Migration implications were considered
[ ] Authorization/security implications were considered
[ ] API compatibility was considered
[ ] Query performance was considered
[ ] Relevant focused tests were run
[ ] Broader validation was run when appropriate
[ ] Final diff was reviewed
[ ] No unrelated dependency upgrades were introduced
[ ] No unrelated source changes were introduced
```

---

# Core rules to preserve

If context is compressed, preserve these rules:

1. **Never guess the Python or Django version. Inspect the repository.**
2. **Never upgrade Django or dependencies during an unrelated fix.**
3. **Reproduce and identify the root cause before changing code.**
4. **Follow this repository's architecture rather than generic Django preferences.**
5. **Treat migrations as source code and inspect generated migrations.**
6. **Do not rewrite applied migration history casually.**
7. **Treat database operations as potentially destructive.**
8. **Preserve existing API contracts unless the task explicitly changes them.**
9. **Authentication is not authorization; enforce object/tenant permissions.**
10. **Avoid N+1 queries and accidental unbounded database work.**
11. **Use transactions and database constraints where domain correctness requires them.**
12. **Background tasks may execute more than once; design accordingly.**
13. **Never expose or log secrets.**
14. **Do not disable security controls merely to make an error disappear.**
15. **Run the repository's actual tests/checks, not guessed commands.**
16. **Review the final diff for security, migrations, performance, compatibility, and unrelated edits.**
