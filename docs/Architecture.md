# QueueLess — Architecture (single source of truth for structure)

Derived from the original project brief and `docs/PRD.md`. FR/NFR ids refer to the PRD.

**Marking convention.** Anything tagged **[added]** is not in the source document; it is a design addition needed to make the stated requirements implementable. All **[added]** items are approved (PRD D5). Resolved gaps are marked in [Contradictions and gaps](#10-contradictions-and-gaps-found). Versions the source does not state are marked "pin at scaffold" rather than guessed.

---

## 1. System overview and component responsibilities

A modular monolith: one FastAPI codebase deployed as two processes (API, worker), plus Postgres, Redis, and a separate Next.js app.

| Component | Responsibility |
| --- | --- |
| **FastAPI API** (`backend/app`) | REST under `/v1`; validates input, authenticates, calls `services/`; owns all transactions and rules; serves SSE streams; subscribes to Redis Pub/Sub and pushes events to connected clients. Stateless, so replicas can be added. |
| **Background worker** (same codebase, separate process) | Outbox relay (unpublished rows → Redis), no-show sweeper, reminders / running-late notifications, nightly `daily_stats` rollups, cleanup (published outbox rows older than 7 days, `audit_log` rows older than 12 months). |
| **PostgreSQL 16** | Source of truth (principle 1). Enforces correctness via exclusion/unique constraints and row locks. Holds the outbox. |
| **Redis 7** | Pub/Sub fan-out across API instances, idempotency keys (24 h), rate limiting, ETA cache (seconds), OTP state **[added]**. Never the only copy of durable state. |
| **Next.js web app** (`web/`) | One codebase for customer app, staff console, owner dashboard. Talks to API over REST + `EventSource`. |

Design principles (from source): DB is truth; correctness via constraints; one writer per queue (row lock); events after commit; low-cost hosting.

## 2. Request flow and real-time flow

### 2.1 Request flow (write)

```text
Browser (Next.js)
  │  HTTPS  POST /v1/...   [Authorization: Bearer <JWT>] [Idempotency-Key]
  ▼
FastAPI router (thin)
  │  1. rate limit (Redis)                       FR-34
  │  2. authenticate JWT; for staff/owner check business_members   FR-27
  │  3. idempotency check (Redis, 24 h)          FR-5
  │  4. validate body (Pydantic)
  ▼
services/<domain>.py   ── one DB transaction ──────────────────────┐
  │  a. lock / constraint-guarded write (see §7)                    │
  │  b. bump token.version where applicable        FR-29            │
  │  c. INSERT outbox_events row (same tx)         FR-32            │
  │  d. INSERT audit_log row for staff actions     FR-28            │
  └─────────────────────────── COMMIT ──────────────────────────────┘
  ▼
store idempotent response → return JSON (or RFC 7807 problem + error code)
```

### 2.2 Real-time flow

```text
  COMMIT (state row + outbox row)
        │
        ▼
  Worker: SELECT unpublished outbox rows ORDER BY id   (partial index published_at IS NULL)
        │  PUBLISH queue:{queue_id}  <event envelope>
        │  UPDATE outbox_events SET published_at = now()
        ▼
  Redis Pub/Sub  ──►  every API instance (subscribed to channels its clients need)
        │
        ▼
  API instance: recompute per-customer position + ETA (cached few seconds),
                skip if unchanged (FR-24), write to SSE streams
        ▼
  Browser EventSource   id: <outbox id>   event: token.called   data: {...}
```

Stream behaviour (FR-20 to FR-23, FR-33):
- First event on connect is a **snapshot** (position, ETA range, status), then deltas. For staff the snapshot has the content of `GET /queues/{id}`.
- SSE `id` = `outbox_events.id`; on reconnect, `Last-Event-ID` makes the server replay later events for that queue from the outbox. Events older than the 7-day retention (PRD D14) are gone; the server then tells the client to fetch a fresh snapshot.
- Heartbeat comment every 15 s; client falls back to 10 s polling after 2 stream failures.
- Per-user, per-IP and per-instance connection caps.

Event envelope:

```json
{ "id": 18422, "type": "token.called", "queue_id": "q_91",
  "occurred_at": "2026-10-20T12:31:07Z", "data": { "token_no": 42, "resource": "Room 2" } }
```

Ordering note: replay by `id > Last-Event-ID` is only safe if ids commit in order. Within a queue this holds because every queue mutation holds the queue row lock until commit (§7). Events not produced under a queue lock (e.g. appointment booking) must not rely on id ordering for replay **[added constraint]**.

## 3. Technology stack

Versions are those the source states; "pin at scaffold" means the source names the tool but no version.

| Layer | Choice (version) | Reason |
| --- | --- | --- |
| Language / API | Python 3.12, FastAPI (pin at scaffold), Pydantic v2 | Async, native SSE via `StreamingResponse`, auto OpenAPI |
| ORM / migrations | SQLAlchemy 2.0 (async), Alembic (pin) | Explicit transactions, `SELECT … FOR UPDATE`, versioned schema |
| Database | PostgreSQL 16 | Exclusion constraints, row locks, partial indexes, `btree_gist` |
| Cache / pub-sub | Redis 7 | Pub/Sub fan-out, rate limiting, idempotency, ETA cache |
| Real-time | Server-Sent Events | One-way push, auto-reconnect, `Last-Event-ID` resume, proxy-friendly (commands go over REST) |
| Background jobs | Separate worker process; **arq** (source leaves arq vs APScheduler open) | Outbox relay, sweeper, reminders, rollups. arq proposed because Redis is already present |
| Frontend | Next.js + TypeScript, TanStack Query, Tailwind (pin at scaffold) | Three UIs from one codebase |
| Charts | Recharts (pin) | Peak-hour heatmap, wait trends, predicted-vs-actual |
| Auth | JWT access (15 min) + rotating refresh tokens, argon2 | Stateless API, role claims; phone OTP for customers (§8) |
| Notifications | In-app SSE only in v1; provider adapter with mock provider (OTP SMS is the only outbound message) | Swappable providers (FR-36) |
| OTP SMS | Mock in dev; MSG91 or Firebase phone auth in prod (open) | India SMS needs DLT registration |
| Testing | pytest, httpx, testcontainers, Locust, Playwright | Real Postgres in tests, load tests back the concurrency claims |
| Infra | Docker Compose locally; Vercel (web) + Railway (API, worker, Postgres, Redis) | Low cost; long-lived containers needed for SSE |
| CI/CD | GitHub Actions | Lint, test, migrate, build image, deploy on merge to main |
| Observability | structlog, Prometheus metrics, Sentry | Queue depth, SSE connections, outbox lag, ETA error, p95 latency |
| DB tools | pgAdmin or DBeaver | (phpMyAdmin is MySQL-only) |

SSE over WebSockets: updates are server → client only; WebSockets only if two-way chat is added later (out of scope).

## 4. Repository structure

Source layout, plus a few files marked **[added]** that any runnable scaffold needs.

```text
queueless/
  docs/                    # PRD.md, Architecture.md, Design.md
  backend/
    pyproject.toml         # [added] Python deps and tool config
    Dockerfile             # [added] one image, run as `api` or `worker`
    alembic.ini            # [added]
    app/
      main.py              # [added] FastAPI app factory, router mounting
      api/v1/              # routers: auth, businesses, appointments, queues, tokens, reports, streams
      core/                # config, security, dependencies, rate limiting
      db/                  # session, base, alembic migrations
      models/              # SQLAlchemy models
      schemas/             # Pydantic request and response models
      services/            # booking, queue, eta, notifications — ALL business logic and transactions (no repository layer)
      realtime/            # outbox relay, Redis pub/sub, SSE manager
      workers/             # no-show sweeper, reminders, nightly rollups
    tests/                 # unit, integration (testcontainers), concurrency
  web/
    app/                   # Next.js routes: customer, staff, owner
    components/            # queue card, token ticket, heatmap
    lib/                   # api client, useEventSource hook
  load-tests/              # Locust scenarios
  docker-compose.yml       # api, worker, postgres, redis, web
  .env.example             # [added] variable names only (§9)
  .github/workflows/       # lint, test, migrate, deploy
```

Rule: routers validate and delegate; every transaction and rule lives in `services/` (makes concurrency tests easy).

## 5. Database schema

PostgreSQL 16, extension `btree_gist` (and `citext` **[added]** for email). All timestamps `timestamptz` UTC; "today's queue" resolved in `businesses.timezone` (FR-2). Primary keys `uuid` unless noted **[types are proposed; the source lists columns but not types]**. Every tenant-owned table carries `business_id` (FR-2). Enumerated columns are `text` with a `CHECK`, so values can change by migration.

**Conventions [added, approved in Phase 1].**
- uuid PKs use `server_default gen_random_uuid()`.
- Every FK is `ON DELETE RESTRICT`; nothing cascades.
- `outbox_events.business_id`, `outbox_events.queue_id` and `audit_log.business_id` are plain uuid columns with no FK (append-only logs cleaned by age).
- Names are deterministic via a SQLAlchemy naming convention: `pk_<table>`, `fk_<table>_<col>_<referred>`, `uq_<table>_<col>`, `ix_<table>_<cols>`, `ck_<table>_<name>`. CHECKs below are listed by their short name. Objects named in this section (`no_overlap`, `uq_token`, `one_active_token`, `active_tokens`) keep those exact names.
- Status columns and `businesses.settings` / `open_hours` have no server default; services set them.
- **Appointment ranges are always half-open `[start, end)`**, so back-to-back slots (10:00-10:30, 10:30-11:00) do not conflict. The `range_valid` CHECK enforces this.

### users
| Column | Type | Constraints |
| --- | --- | --- |
| id | uuid | PK |
| name | text | NOT NULL |
| email | citext | UNIQUE, NULL for OTP-only customers **[added: nullable]** |
| phone | text (E.164) | UNIQUE, NULL for email users |
| password_hash | text | NULL for OTP-only customers **[added: nullable]**; argon2 |
| phone_verified_at | timestamptz | NULL **[added]** |
| role | text | CHECK in (customer, staff, owner, admin); `admin` has no features in v1 (PRD D13) |
| no_show_count | int | NOT NULL DEFAULT 0 **[added; per-customer counter, FR-13]** |
| created_at | timestamptz | NOT NULL DEFAULT now() |

CHECK: `email IS NOT NULL OR phone IS NOT NULL` **[added]**.

### businesses
| Column | Type | Constraints |
| --- | --- | --- |
| id | uuid | PK |
| owner_id | uuid | FK users(id), NOT NULL |
| name, category, address | text | NOT NULL (address nullable) |
| timezone | text | NOT NULL (IANA name) |
| open_hours | jsonb | NOT NULL |
| settings | jsonb | NOT NULL; keys and defaults: grace_period_min 5, reschedule_cutoff_min 120, promotion_wait_limit_min 20, walk_in_ratio 1 walk-in per 2 appointments, running_late_threshold_min 10, max_walk_ins. **[added]** Stored as `{grace_period_min: 5, reschedule_cutoff_min: 120, promotion_wait_limit_min: 20, walk_in_ratio: {walk_ins: 1, appointments: 2}, running_late_threshold_min: 10, max_walk_ins: 50}`; `max_walk_ins` is walk-in tokens allowed per queue per day. `open_hours` is `{mon..sun: [{open: "HH:MM", close: "HH:MM"}]}` in local time, half-open, open < close, at most 4 intervals per day. `timezone` must be in the IANA list. The default resource is named `Main`. |

**Settings bounds [added, Phase 1 task 10]** (all integers, `StrictInt`: no bool, no float; unknown keys, null and an empty object are rejected):

| Key | Min | Max | Reason |
| --- | --- | --- | --- |
| grace_period_min | 0 | 60 | 0 = no grace; over an hour a called token no longer means "now" |
| reschedule_cutoff_min | 0 | 10080 | 0 = up to the start; one week is the longest sensible notice |
| promotion_wait_limit_min | 1 | 240 | 0 would promote every appointment at once |
| running_late_threshold_min | 1 | 120 | 0 would flag everyone; over 2 h is a no-show |
| walk_in_ratio.walk_ins | 1 | 10 | walk-ins must get turns |
| walk_in_ratio.appointments | 1 | 10 | min 1: no walk-ins-only mode through the ratio |
| max_walk_ins | 0 | 1000 | 0 turns walk-ins off |

PATCH merges only the keys sent into the stored document with one atomic `UPDATE ... settings = settings || :patch`, so parallel PATCHes of different keys both survive and the document always has every key. `walk_in_ratio` is replaced as a whole (both keys required). Staff accounts have no forced password change and no reset in v1 (FR-36, no email delivery): the owner sets the initial password and tells the staff member. Emails are globally unique, so an existing account is never attached to a business.

### business_members
| Column | Type | Constraints |
| --- | --- | --- |
| business_id | uuid | FK businesses |
| user_id | uuid | FK users |
| role | text | CHECK in (owner, staff) |
| | | PK (business_id, user_id) |

### services
id uuid PK · business_id FK · name text · duration_min int · buffer_min int DEFAULT 0 · price numeric(10,2) (display-only; no payments in v1, PRD D12) · is_active bool DEFAULT true. `price` is NOT NULL (0 = free). CHECK `duration_min > 0` **[added]**, CHECK `buffer_min >= 0` **[added]**. Index `(business_id, is_active)`.

### resources
id uuid PK · business_id FK · name text (doctor/chair/counter) · is_active bool DEFAULT true. v1 businesses have one default resource and customers never choose a queue; schema is multi-resource (PRD D15).

### queues
id uuid PK · business_id FK · resource_id FK · queue_date date · last_token_no int NOT NULL DEFAULT 0 · status text CHECK in (open, paused, closed). **UNIQUE (resource_id, queue_date).** Created lazily on first access (join, check-in, console open, or `GET /businesses/{id}/queue`) via `INSERT ... ON CONFLICT (resource_id, queue_date) DO NOTHING` with status `open`, then locked `FOR UPDATE` for the token increment; no midnight job (PRD D10).

### appointments
| Column | Type | Constraints |
| --- | --- | --- |
| id | uuid | PK |
| business_id, service_id, resource_id | uuid | FKs |
| customer_id | uuid | FK users |
| during | tstzrange | NOT NULL |
| status | text | CHECK in (booked, checked_in, in_service, completed, cancelled, no_show, rescheduled) (D2) |
| priority | smallint | NOT NULL DEFAULT 0, CHECK `priority BETWEEN 0 AND 2` **[added]** |
| rescheduled_from_id | uuid | FK appointments(id), NULL |
| created_at | timestamptz | NOT NULL DEFAULT now() **[added]** |

**Stored range [added, Phase 2]:** `during` = `[start, start + duration_min + buffer_min)`, so `no_overlap` enforces the service buffer; the slot shown to customers ends at `start + duration_min`. The service time must fit inside an open interval, the buffer may run past closing. Slots sit on a 15-minute wall-clock grid from each opening time; `open_hours` close `23:59` means 24:00 local. Zones come from the packaged `tzdata` (`app/core/timezones.py`), never from the host's tz files.

CHECK `range_valid` **[added]**: `lower_inc(during) AND NOT upper_inc(during) AND NOT isempty(during) AND NOT lower_inf(during) AND NOT upper_inf(during)`. An empty or unbounded range never overlaps and would silently bypass `no_overlap`; ranges are half-open `[start, end)`.

```sql
ALTER TABLE appointments ADD CONSTRAINT no_overlap
  EXCLUDE USING gist (resource_id WITH =, during WITH &&)
  WHERE (status IN ('booked', 'checked_in', 'in_service'));
```
Index `(customer_id, status)`.

### queue_tokens
| Column | Type | Constraints |
| --- | --- | --- |
| id | uuid | PK |
| business_id | uuid | FK, NOT NULL **[added]** |
| queue_id | uuid | FK queues, NOT NULL |
| token_no | int | NOT NULL |
| source | text | CHECK in (walk_in, appointment) |
| appointment_id | uuid | FK appointments, NULL |
| customer_id | uuid | FK users, NOT NULL (D1: no anonymous tokens) |
| status | text | CHECK in (waiting, called, in_service, completed, no_show, cancelled) (D2; "joined" and "skipped" are events, not statuses) |
| priority | smallint | NOT NULL DEFAULT 0, CHECK 0..2 |
| skip_count | smallint | NOT NULL DEFAULT 0 **[added; FR-12]** |
| joined_at, called_at, started_at, completed_at | timestamptz | joined_at NOT NULL DEFAULT now() (never changes; wait metrics); others NULL |
| queued_at | timestamptz | NOT NULL DEFAULT now(); ordering key; a skip sets it to now() **[added, D4]** |
| predicted_wait_sec | int | NULL; ETA stored at join time for predicted-vs-actual **[added, D5]** |
| version | int | NOT NULL DEFAULT 1 (optimistic lock, FR-29) |

```sql
ALTER TABLE queue_tokens ADD CONSTRAINT uq_token UNIQUE (queue_id, token_no);

-- [added] one appointment produces at most one token (NULLs allowed for walk-ins)
ALTER TABLE queue_tokens ADD CONSTRAINT uq_queue_tokens_appointment_id UNIQUE (appointment_id);

CREATE UNIQUE INDEX one_active_token ON queue_tokens (queue_id, customer_id)
  WHERE status IN ('waiting', 'called', 'in_service');

CREATE INDEX active_tokens ON queue_tokens (queue_id, priority DESC, queued_at)
  WHERE status IN ('waiting', 'called', 'in_service');   -- partial index on active tokens
```

### service_sessions
id uuid PK · business_id FK **[added]** · token_id FK · service_id FK · resource_id FK · started_at, ended_at timestamptz · duration_sec int. Index `(service_id, ended_at)` (feeds ETA, FR-15).

### outbox_events
id **bigserial PK** (used as SSE id) · business_id uuid **[added]** · queue_id uuid NULL (booking events have no queue) · event_type text · payload jsonb · created_at timestamptz DEFAULT now() · published_at timestamptz NULL. Indexes: partial `(id) WHERE published_at IS NULL`; `(queue_id, id)` for replay **[added]**. Published rows are deleted after 7 days by a worker job (PRD D14); older events cannot be replayed and the client falls back to a fresh snapshot.

### daily_stats
business_id, service_id, stat_date date, hour smallint, tokens_served int, avg_wait_sec int, p90_wait_sec int, no_shows int. **PK (business_id, service_id, stat_date, hour).** CHECK `hour BETWEEN 0 AND 23` **[added]**. Counters are NOT NULL DEFAULT 0; `avg_wait_sec` and `p90_wait_sec` are NULL when nobody was served. Needs `tokens_joined` and `cancelled` columns to satisfy FR-30 **[added; gap G-6]**.

### refresh_tokens **[added]**
id uuid PK · user_id FK · family_id uuid · token_hash text UNIQUE · expires_at · revoked_at NULL · created_at. Index `(family_id)` **[added]** (reuse detection revokes a whole family). Supports rotation and reuse detection (FR-26).

### audit_log **[added]**
id bigserial PK · business_id uuid · actor_id uuid FK users · action text · entity_type text · entity_id uuid · data jsonb · created_at. Index `(business_id, created_at)`. Written in the same transaction as the action (FR-28). Retained 12 months (PRD D14); rows older than that are deleted by the worker cleanup job, together with the outbox cleanup.

## 6. API

Base path `/v1`. JSON bodies, cursor pagination, errors as RFC 7807 problem JSON with a stable `code`:

```json
{ "type": "about:blank", "title": "Slot taken", "status": 409, "code": "SLOT_TAKEN", "detail": "...", "next_slots": [] }
```

Source codes: `SLOT_TAKEN` (409), `INVALID_TRANSITION` (409), `QUEUE_CLOSED` (409 **[status proposed]**). Proposed additions: `VALIDATION_ERROR` 422, `UNAUTHENTICATED` 401, `FORBIDDEN` 403, `NOT_FOUND` 404, `VERSION_CONFLICT` 409, `ALREADY_IN_QUEUE` 409, `RATE_LIMITED` 429, `OTP_INVALID` 400, `OTP_EXPIRED` 400, `RESCHEDULE_CUTOFF` 409, `BOOKING_RESTRICTED` 403, `IDEMPOTENCY_CONFLICT` 409, `INTERNAL_ERROR` 500 **[added]** (unhandled errors, generic body), `SERVICE_UNAVAILABLE` 503 **[added]** (e.g. the rate limiter cannot reach Redis; it fails closed), `METHOD_NOT_ALLOWED` 405 **[added]** (raised by the framework). `BOOKING_LIMIT_REACHED` 409 **[added, Phase 2]** (a customer already has the maximum of 5 active appointments that end in the future). `IDEMPOTENCY_CONFLICT` 409 **[added detail]**. Every mutating endpoint **except the auth endpoints and DELETE [added]** accepts `Idempotency-Key` (FR-5): 1 to 255 printable ASCII characters else 422; scoped per user, method, concrete path and key; same key and same JSON body replays the stored 2xx response with `Idempotent-Replayed: true`; same key and a different body is `IDEMPOTENCY_CONFLICT`; a concurrent duplicate gets `IDEMPOTENCY_CONFLICT` with `Retry-After: 1` and `in_progress: true`. Only 2xx responses are stored (24 h, `IDEMPOTENCY_TTL_SEC`); every other outcome releases the claim. Keys and body fingerprints are keyed hashes (HMAC derived from `JWT_SECRET`), never raw. It runs after authentication, authorization and the rate limit; Redis down gives 503. Applied to POST /businesses, POST and PATCH on services, POST staff and PATCH /businesses/{id}. Token objects: `{id, queue_id, token_no, status, priority, position, eta:{min_min,max_min}, version}`; `GET /tokens/{id}` also returns `stream_token`.

| Method and path | Role | Request | Response | Errors | FR |
| --- | --- | --- | --- | --- | --- |
| GET /healthz **[added, infrastructure]** | public | — | `{"status":"ok"}` (liveness only; no DB or Redis check) | — | — |
| POST /auth/otp/request **[added]** | public | `{phone}` (strict E.164 `^\+[1-9]\d{7,14}$`) | 202 `{}` (identical whether or not an account exists) | RATE_LIMITED (per IP, per phone, 30 s resend cooldown), VALIDATION_ERROR, SERVICE_UNAVAILABLE | 1 |
| POST /auth/otp/verify **[added]** | public | `{phone, code, name?}` | `{access_token, user}` + refresh cookie; `user` is `{id, name, email, phone, role}` **[added]** | OTP_INVALID (also for a staff, owner or admin phone), OTP_EXPIRED (also for a used code), RATE_LIMITED (per IP, or locked after `OTP_MAX_ATTEMPTS` wrong codes, with `Retry-After`) **[added]**, SERVICE_UNAVAILABLE | 1, 26 |
| POST /businesses **[added]** | owner | `{name, category, address?, timezone, open_hours}` | 201 business (also creates default resource and owner membership in one transaction) **[added]**; the owner role is read from the database; an owner may have several businesses; 10 per hour per user, checked after 401/403 and before validation **[added]** | VALIDATION_ERROR, UNAUTHENTICATED, FORBIDDEN, RATE_LIMITED **[added]** | 2 |
| POST /auth/register | public | `{name, email, password}`; password 10 to 128 characters **[added]** (creates **owner** accounts only, D19) | 201 `{user}` **[added]** | VALIDATION_ERROR (incl. email already used), RATE_LIMITED **[added]** | 1 |
| POST /auth/login | public | `{email, password}` (roles owner, staff, admin only) | `{access_token, user}` + refresh cookie | UNAUTHENTICATED (same body for unknown email, wrong password and customer), RATE_LIMITED (per IP, per email) | 1 |
| POST /auth/refresh | public (cookie) | — | `{access_token, user}` **[added]**, rotated refresh cookie; the role comes from the database | UNAUTHENTICATED (unknown, expired or reused token; the cookie is cleared), FORBIDDEN **[added]** (Origin header present and not in `CORS_ORIGINS`) | 26 |
| POST /auth/logout **[added]** | public (cookie) | — | 204; revokes the whole token family and clears the cookie; idempotent | FORBIDDEN (Origin not allowed) | 26 |
| GET /businesses, /businesses/{id} | public | `?q=&category=&cursor=&limit=` (limit 1..100, default 20 **[added]**) | business `{id, name, category, address, timezone, open_hours}` (no settings); list is `{items, next_cursor}` ordered by (name, id) keyset, `q` is a case-insensitive substring of name with `%`, `_` and `\` escaped **[added]**. The cursor is opaque base64url JSON `[name, id]`, only a position, decoded strictly | NOT_FOUND, VALIDATION_ERROR (bad cursor, loc `["query","cursor"]`) **[added]** | 2 |
| GET /businesses/mine **[added]** | owner or staff (customers and admins get FORBIDDEN) | — | `[{id,name,category,address,timezone,open_hours,role}]` the caller is a member of, ordered by (name, id), no settings; declared before `/businesses/{id}` | UNAUTHENTICATED, FORBIDDEN | 39 |
| GET /businesses/{id}/settings **[added]** | owner | — | owner business: public fields plus `settings` (no `owner_id`) | UNAUTHENTICATED, NOT_FOUND, FORBIDDEN | 39 |
| GET /businesses/{id}/staff **[added]** | owner | — | `[{id,name,email}]` of role `staff` members (never password hash or phone) | UNAUTHENTICATED, NOT_FOUND, FORBIDDEN | 39 |
| GET /businesses/{id}/services | public | `?include_inactive=` **[added]** (owner only: 401, then 404, then 403) | `[{id,name,duration_min,buffer_min,price,is_active}]` **[added: buffer_min, is_active]**; active only unless `include_inactive` | NOT_FOUND, UNAUTHENTICATED, FORBIDDEN | 3 |
| PATCH /businesses/{id} **[added, D19]** | owner | `{name?, address?, open_hours?, timezone?, settings?}`; each settings key validated against min/max bounds | owner business (public fields plus `settings`, no `owner_id`); at least one field; `address` may be null to clear it, other nulls and `category` are rejected; settings merged per the bounds in §5; changing `timezone` is refused with VALIDATION_ERROR (loc `["body","timezone"]`) while the business has a booked, checked_in or in_service appointment whose range ends after now **[added, Phase 2]** | UNAUTHENTICATED, NOT_FOUND, FORBIDDEN, VALIDATION_ERROR | 39 |
| POST /businesses/{id}/staff **[added, D19]** | owner | `{name, email, initial_password}` | 201 `{id,name,email,role}`; always creates a NEW user (role `staff`) and the `business_members` row in one transaction; an email that exists in any role or business gives the same VALIDATION_ERROR as register, nothing is attached; 20 per hour per owner **[added]** | UNAUTHENTICATED, NOT_FOUND, FORBIDDEN, VALIDATION_ERROR, RATE_LIMITED | 39 |
| DELETE /businesses/{id}/staff/{user_id} **[added, D19]** | owner | — | 204; removes only a `staff` membership of THIS business (the account stays; refresh tokens are not revoked because membership is read on every call); anything else, including the owner's id, staff of another business and a repeated delete, is NOT_FOUND; 60 per hour per owner **[added]** | UNAUTHENTICATED, FORBIDDEN, NOT_FOUND, RATE_LIMITED | 39 |
| GET /businesses/{id}/queue **[added, D17]** | any authenticated | — | today's queue for the default resource (created lazily per D10) | NOT_FOUND | 37 |
| POST, PATCH /businesses/{id}/services | owner | `{name,duration_min,buffer_min,price,is_active}`; PATCH path is `/businesses/{id}/services/{service_id}`, fields optional, null rejected **[added]** | service (POST 201) | UNAUTHENTICATED, NOT_FOUND (unknown business, or a service not in this business), FORBIDDEN (no owner membership; admin is not a superuser), RATE_LIMITED (60 per hour per user, after 401/404/403, before validation), VALIDATION_ERROR **[added]** | 27 |
| GET /businesses/{id}/availability?service_id=&date= | any authenticated user **[added]** | `date` is YYYY-MM-DD in the business time zone: today to 60 days ahead, else 422; `service_id` must be an active service of this business (a malformed id is 404). Check order 401, 404 (business, then service), 429, 422. 120 per minute per user **[added]** | `{timezone, slots:[{start,end,resource_id}]}` **[added: timezone]**; times are ISO 8601 with the business-local offset; `end` = start + service duration; the first active resource by (name, id) is used (none: 404). Spring-forward: nonexistent local times give no slots; fall-back: each wall time is offered once (first occurrence), so the repeated hour has no second pass | UNAUTHENTICATED, NOT_FOUND, RATE_LIMITED, VALIDATION_ERROR | 3 |
| GET /appointments **[added, D18]** | customer, owner, staff (admin: FORBIDDEN) | `?status=` (one of the seven values), `?from=` and `?to=` (ISO 8601 with offset, on start; from inclusive, to exclusive, from before to), `?limit=` (1..50, default 20), `?cursor=` (opaque keyset on (start, id), strict; bad value 422 on `["query","cursor"]`). Customer: own appointments only, passing `business_id` is 422. Owner/staff: `?business_id=` required (missing or invalid 422 on `["query","business_id"]`, unknown 404, non-member 403, membership checked every call). 120 per minute per user. Order: 401, 403, business_id 422, 404, 403, 429, other 422 | `{items, next_cursor}`; item `{id, business_id, service_id, service_name, resource_id, start, end, status, created_at}` with business-local offsets, `end` = start + the service's CURRENT `duration_min` (editing a service later changes the displayed end); the owner/staff view adds `customer: {id, name}` only (no phone, email or hash). One query with joins | FORBIDDEN, NOT_FOUND, VALIDATION_ERROR, RATE_LIMITED | 38 |
| POST /appointments **[added detail]** | customer only (owner, staff, admin: FORBIDDEN) | `{service_id, resource_id, start}`; `start` is ISO 8601 WITH offset (naive is 422); honours `Idempotency-Key`. Order: 401, 403, body shape 422, 404 (service, then resource: unknown, foreign or inactive), 429 (`BOOKING_RATE_LIMIT_PER_HOUR`, default 20 per user), idempotency claim, 422 on `["body","start"]` (past, over 60 days, off the 15-minute grid, outside open hours, closed day, does not fit), then in one transaction: lock the customer row (`FOR NO KEY UPDATE`), BOOKING_RESTRICTED, BOOKING_LIMIT_REACHED, insert. The insert is the authority (no busy pre-check): SQLSTATE 23P01 on constraint `no_overlap` is SLOT_TAKEN; SQLSTATE 40P01 (deadlock) rolls back and retries the whole transaction up to twice, then 503 with `Retry-After: 1`; any other database error is a 500 | 201 appointment (same item shape as GET /appointments, without `customer`); stored range `[start, start + duration + buffer)` | SLOT_TAKEN (409, body carries `next_slots`: up to 3 later slots on the same local date, same service and resource, `{start,end,resource_id}`, empty when none), BOOKING_RESTRICTED (403, `users.no_show_count >= 3`, PRD D6), BOOKING_LIMIT_REACHED (409, more than 5 active appointments that end in the future), RATE_LIMITED, NOT_FOUND, VALIDATION_ERROR, SERVICE_UNAVAILABLE | 4, 5, 34 |
| PATCH /appointments/{id}/reschedule **[added detail]** | customer only (owner, staff, admin: FORBIDDEN, decided on the caller's role before the id is read) | `{start}` only (ISO 8601 WITH offset; naive is 422). The service and resource cannot change on reschedule in v1: both come from the old appointment. Another customer, or any id the caller does not own, gets the same NOT_FOUND as a missing id. Honours `Idempotency-Key`; shares the booking rate-limit bucket (`BOOKING_RATE_LIMIT_PER_HOUR`), and failed attempts count. Order: 401, 403, 404, 429, idempotency claim, 422 body, 422 `["body","start"]` (same rules as booking), then one transaction: lock the customer row (`FOR NO KEY UPDATE`), lock the old appointment (`FOR UPDATE`), INVALID_TRANSITION (status not `booked`), INVALID_TRANSITION (already started), RESCHEDULE_CUTOFF (old start less than `reschedule_cutoff_min` from now; exactly at the cutoff is allowed), 422 `same time` (new start equals old start), BOOKING_RESTRICTED, BOOKING_LIMIT_REACHED (the appointment being replaced does not count), mark the old row `rescheduled`, insert the new row, commit; then SLOT_TAKEN. Deadlock retry and 503 as for booking | 200: the new appointment in the GET /appointments item shape plus `rescheduled_from_id` (the old id). The new row has status `booked`, `rescheduled_from_id` = old id, stored range `[start, start + current duration + buffer)`; the old row becomes `rescheduled` (terminal, keeps its own `rescheduled_from_id`, frees its range, cannot be cancelled or rescheduled again) | SLOT_TAKEN (old appointment exactly unchanged; `next_slots` leaves the customer's own old range out of the busy list but never contains the rejected start), RESCHEDULE_CUTOFF (409), INVALID_TRANSITION, BOOKING_RESTRICTED, BOOKING_LIMIT_REACHED, NOT_FOUND, FORBIDDEN, RATE_LIMITED, VALIDATION_ERROR, SERVICE_UNAVAILABLE | 19 |
| DELETE /appointments/{id} **[added detail]** | customer, owner, staff (admin: FORBIDDEN) | no body, no `Idempotency-Key`. Allowed for the customer who owns it, or an owner/staff member of the appointment's business (membership read in the database). Everyone else, including a stranger and a member of another business, gets the same NOT_FOUND as a missing id, because appointment ids are not public (not FORBIDDEN). One conditional `UPDATE … WHERE id AND status='booked' AND start > now AND visible RETURNING`; a second read explains a no-op | 204; already `cancelled` is also 204 (nothing audited); status `checked_in`, `in_service`, `completed`, `no_show`, `rescheduled`, or a started appointment (customers, staff and owners alike) is INVALID_TRANSITION; staff and owner cancellations write an `audit_log` row (`appointment.cancel`) in the same transaction, customer cancellations do not | INVALID_TRANSITION, NOT_FOUND, FORBIDDEN, VALIDATION_ERROR (malformed id) | 16, 28 |
| POST /appointments/{id}/check-in | customer or staff | — | token | QUEUE_CLOSED, INVALID_TRANSITION, ALREADY_IN_QUEUE | 9 |
| POST /queues/{id}/tokens | customer | `{}` | token | QUEUE_CLOSED, ALREADY_IN_QUEUE, RATE_LIMITED | 6, 7, 8 |
| GET /tokens?active=true **[added, D18]** | customer | own active tokens | `[token]` | VALIDATION_ERROR | 38 |
| GET /tokens/{id} | customer | — | token + `stream_token` | NOT_FOUND, FORBIDDEN | 20 |
| DELETE /tokens/{id} | customer | — | 204 | INVALID_TRANSITION, VERSION_CONFLICT | 16, 29 |
| GET /tokens/{id}/stream | customer | stream token (query) from `GET /tokens/{id}` | SSE | UNAUTHENTICATED, RATE_LIMITED | 20–24, 33, 35 |
| POST /queues/{id}/call-next | staff | `{}` | called token (or 204 if none waiting) | FORBIDDEN, QUEUE_CLOSED | 10 |
| POST /tokens/{id}/skip, /start, /complete, /no-show | staff | `{version}` | token | INVALID_TRANSITION, VERSION_CONFLICT, FORBIDDEN | 11–13, 29 |
| PATCH /tokens/{id}/priority | staff | `{priority:0..2, version}` | token | VALIDATION_ERROR, VERSION_CONFLICT | 17 |
| PATCH /queues/{id} | staff | `{status}` | queue | FORBIDDEN, VALIDATION_ERROR | 8 |
| GET /queues/{id} **[added, D17]** | staff | — | queue snapshot: status and ordered tokens; first SSE snapshot content and polling-fallback endpoint | FORBIDDEN, NOT_FOUND | 37 |
| POST /queues/{id}/stream-token **[added, D20]** | staff | — | `{stream_token}` (signed, short-lived; membership checked at issue) | FORBIDDEN, NOT_FOUND | 40 |
| GET /queues/{id}/stream | staff | `?stream_token=` from `POST /queues/{id}/stream-token` | SSE | UNAUTHENTICATED, FORBIDDEN | 20, 35, 40 |
| GET /businesses/{id}/board | public | — | SSE: now serving + next five token numbers only | NOT_FOUND | 20, 35 |
| GET /businesses/{id}/reports/peak-hours | owner | `?from=&to=` | `[{weekday,hour,tokens}]` | FORBIDDEN | 30 |
| GET /businesses/{id}/reports/wait-times | owner | `?from=&to=&service_id=` | `[{date,avg_wait_sec,p90_wait_sec}]` + predicted vs actual | FORBIDDEN | 30, 31 |
| GET /businesses/{id}/reports/no-shows | owner | `?from=&to=` | `[{service_id,no_show_rate,cancel_rate}]` | FORBIDDEN | 30 |

`/healthz` is the only unversioned path; every other route is under `/v1`.

Staff and owner routes check `business_members` on every call (FR-27). Stream URLs carry `stream_token` in the query string, so request logs must redact that parameter (tokens are never logged).

## 7. Concurrency strategy

| Race | Mechanism | Backstop / test |
| --- | --- | --- |
| Duplicate token numbers (FR-6) | In one transaction: `SELECT … FROM queues WHERE id=:q FOR UPDATE`; `last_token_no += 1`; insert token + outbox row. Serializes joins per queue (one writer per queue). | `UNIQUE (queue_id, token_no)`. Test: 500 concurrent joins → exactly 1..500. |
| Double-booked slot (FR-4) | **Insert, don't check-then-insert.** The exclusion constraint rejects overlaps; catch the integrity error → `409 SLOT_TAKEN` + next free slots. No slot hold in v1 (D6). | `no_overlap` EXCLUDE. Test: 50 bookings on one slot → exactly one wins. |
| Two staff call same customer (FR-10) | `SELECT id FROM queue_tokens WHERE queue_id=:q AND status='waiting' ORDER BY priority DESC, queued_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED`, then update to `called` in the same transaction. | Test: N simultaneous calls → N distinct tokens, none blocked. |
| Retried tap / duplicate request (FR-5) | `Idempotency-Key` header; response stored in Redis 24 h keyed by (user, key, route); same key + same body replays the stored response; same key + different body → `IDEMPOTENCY_CONFLICT`. | `one_active_token` still prevents duplicate active tokens if Redis state is lost. |
| Staff action vs customer cancel (FR-29) | `queue_tokens.version`; `UPDATE … WHERE id=:id AND version=:v`; zero rows → `VERSION_CONFLICT`. | Test: racing skip and cancel → exactly one succeeds. |
| Illegal state moves (FR-11) | Single service method per transition validates current state, bumps `version`, writes outbox, commits. | Unit test of the transition table. |
| Reschedule (FR-19) | One transaction, lock order customer row then old appointment (booking takes only the customer row; cancel takes only the appointment row in one UPDATE, so no lock cycle is possible). **[added]** Mark old `rescheduled` FIRST, then insert the new appointment with `rescheduled_from_id` (constraint checks the slot): `no_overlap` ignores rescheduled rows, so a new range that overlaps the one it replaces (a 15-minute shift) is accepted. A failed insert rolls everything back, including the mark. | Integration test: rollback leaves old appointment intact; concurrency: 20 reschedules into one slot, exactly one wins, losers untouched. |
| Lost events (FR-32) | Outbox row written in the same transaction as state; published only after commit by the worker. | Test: outbox ordering and `Last-Event-ID` replay. |

## 8. Authentication design

**Customers — phone OTP (FR-1)** (the only customer login; no anonymous tokens, D1):
1. `POST /auth/otp/request {phone}` → API validates strict E.164 (no server-side normalization; the client formats the number) **[added]**, applies Redis sliding-window rate limits (per IP 20/hour, per phone 5/hour, 30 s resend cooldown per phone) **[added]**, generates a 6-digit code, stores only a keyed HMAC-SHA256 of it in Redis (key derived from `JWT_SECRET` with domain separation; Redis keys use an HMAC of the phone, never the number) with a TTL and attempt counter **[added: TTL, digits, attempt limit are proposals]**, sends via the SMS adapter (mock provider in dev logs the code; the mock is refused when `APP_ENV=production`; MSG91 or Firebase phone auth in production).
2. `POST /auth/otp/verify {phone, code}` → on match, delete the OTP key (the code is single use; `DEL` decides the winner of parallel verifies), then upsert the `users` row with `INSERT ... ON CONFLICT` (role `customer`, `phone_verified_at = now()`), issue tokens. A phone that belongs to a staff, owner or admin account is rejected with `OTP_INVALID` **[added]**.
3. Every verify attempt increments the counter atomically; after `OTP_MAX_ATTEMPTS` the phone gets `RATE_LIMITED` with `Retry-After` until the code expires **[added]**. Per-IP verify limit 30 per 10 minutes. All Redis failures in this path return `SERVICE_UNAVAILABLE`.

**Staff and owners — email + password:** argon2 hashing; membership in `business_members` grants business scope. Login verifies against a dummy hash when the email is unknown, so failures look and cost the same **[added]**.

**Tokens (FR-26):** 15-minute JWT access token with `sub` and role claim; rotating refresh token stored hashed in `refresh_tokens`, delivered in an HttpOnly, Secure, SameSite=Lax cookie with `Domain=.<domain>` (`COOKIE_DOMAIN`, empty in local development, where `Secure` is off; PRD D20); reuse of a rotated token revokes its family (checked before expiry). The cookie has `Path=/v1/auth` and `Max-Age = REFRESH_TTL_DAYS` **[added]**; `/auth/refresh` and `/auth/logout` reject a present `Origin` that is not in `CORS_ORIGINS` (CSRF hardening) **[added]**.

**Authorization (FR-27):** role claim for coarse checks, plus a `business_members` lookup on every staff/owner call; every query filters by `business_id`. Optional Postgres row-level security as a second layer.

**Stream access (FR-35):** `EventSource` cannot send an Authorization header, so every stream uses a short-lived signed stream token passed as `?stream_token=`, so no stream needs an Authorization header or cookie. Customers get theirs as `stream_token` from `GET /tokens/{id}`; staff get theirs from `POST /queues/{id}/stream-token`, which checks `business_members` at issue time. The public board carries token numbers only.

**Browser auth and domains (PRD D20):** the access token lives in memory; the refresh cookie is sent to the API with `credentials: include`. Production uses one custom domain, web at `app.<domain>` and api at `api.<domain>`, so the cookie is same-site. CORS allows only the web origin (`CORS_ORIGINS`, exact origins, never `*`) with credentials; allowed methods GET, POST, PATCH, DELETE and headers Authorization, Content-Type, Idempotency-Key, Last-Event-ID **[added]**. Locally, web (`localhost:3000`) and API (`localhost:8000`) are same-site on localhost. The default `vercel.app` and `railway.app` domains are on the Public Suffix List and cannot share a cookie, so the custom domain is required; confirm it with a real Safari test in Phase 7.

**Other:** strict CORS, Pydantic validation, parameterised queries, secrets from environment.

## 9. Environment, local setup, deployment

### 9.1 Environment variables (names only; proposed — the source defines none)

| Group | Names |
| --- | --- |
| App | `APP_ENV`, `LOG_LEVEL`, `CORS_ORIGINS` (web origin only), `COOKIE_DOMAIN` (e.g. `.example.com`; empty locally), `SENTRY_DSN` |
| Postgres | `DB_POOL_SIZE` **[added]** (connections per process, default 10; the hosted Postgres has its own limit, re-check in Phase 7), `DATABASE_URL`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` (Compose) |
| Redis | `REDIS_URL` |
| Auth | `JWT_SECRET`, `JWT_ACCESS_TTL_MIN`, `REFRESH_TTL_DAYS`, `STREAM_TOKEN_SECRET`, `STREAM_TOKEN_TTL_SEC` |
| OTP | `OTP_PROVIDER` (mock/msg91/firebase), `OTP_TTL_SEC`, `OTP_MAX_ATTEMPTS`, `OTP_RESEND_COOLDOWN_SEC` **[added]**, `MSG91_AUTH_KEY`, `MSG91_TEMPLATE_ID`, `FIREBASE_PROJECT_ID` |
| Realtime / limits | `SSE_HEARTBEAT_SEC`, `SSE_MAX_CONN_PER_USER`, `SSE_MAX_CONN_PER_IP`, `SSE_MAX_CONN_PER_INSTANCE`, `ETA_CACHE_TTL_SEC`, `IDEMPOTENCY_TTL_SEC`, `BOOKING_RATE_LIMIT_PER_HOUR` **[added]** (per user, default 20) |
| Web | `NEXT_PUBLIC_API_URL` |

Secrets are never committed; `.env.example` lists names only.

### 9.2 Local setup (Docker Compose)

`docker-compose.yml` runs five services: `api`, `worker`, `postgres` (16, `btree_gist` enabled by migration), `redis` (7), `web`.

```text
cp .env.example .env            # fill in values
docker compose up -d postgres redis
docker compose run --rm api alembic upgrade head
docker compose up api worker web
# API: http://localhost:8000 (OpenAPI at /docs)   Web: http://localhost:3000
```
Ports are conventional defaults, not source requirements. Dev uses the mock OTP provider.

### 9.3 Deployment

- **Domains:** one custom domain; Vercel serves web at `app.<domain>`, Railway serves the API at `api.<domain>`; `COOKIE_DOMAIN=.<domain>`. The default `vercel.app` and `railway.app` domains cannot share the refresh cookie (see §8).
- **Web:** Vercel (Next.js); `NEXT_PUBLIC_API_URL` points to `https://api.<domain>`.
- **Railway project, four services on its private network:** `api` (long-lived container, not serverless, because SSE streams stay open), `worker` (same image, different start command), `postgres`, `redis`.
- **CI/CD:** GitHub Actions runs lint, tests and migrations, builds the image, and deploys on merge to `main`.
- **Scale path:** more API replicas behind Railway's load balancer (Redis Pub/Sub already fans out), then a Postgres read replica for reports, then PgBouncer if connections run short.
- **Cost:** budget from about $5/month plus a custom domain (registration fee, billed yearly). Railway has no permanent free tier; Neon (Postgres) and Upstash (Redis) are free-tier alternatives, limits to be verified.

---

## 10. Contradictions and gaps found

Status: **resolved** items are decided (see PRD §8 Decisions); the rest stay open or informational.

- **G-1. OTP optional vs primary. Resolved (D1).** Every customer verifies by OTP; no "optional OTP". OTP endpoints and storage added.
- **G-2. Anonymous walk-ins. Resolved (D1).** `queue_tokens.customer_id` is NOT NULL.
- **G-3. Tenancy rule vs schema. Resolved.** `business_id` added to `queue_tokens`, `service_sessions` and `outbox_events`.
- **G-4. Check-in role. Resolved (D3).** Customer or staff may check in.
- **G-5. Token states. Resolved (D2).** Six stored token statuses; "joined"/"skipped" are events. Appointment statuses fixed at seven.
- **G-6. Reports vs rollup. Resolved (D5).** `tokens_joined` and `cancelled` added to `daily_stats`.
- **G-7. Lobby screen. Resolved.** Endpoint in v1, screen UI out.
- **G-8. Stream auth. Resolved (D5).** `GET /tokens/{id}` returns `stream_token`.
- **G-9. Redis vs "DB is source of truth".** Open (informational). Idempotency keys live only in Redis; losing them permits duplicate non-token writes (appointments are protected by the exclusion constraint; a retried reschedule is not). **[added, Phase 2]** A retried reschedule whose idempotency key was lost now gets 409 INVALID_TRANSITION (the old row is already `rescheduled`), so it cannot create a second appointment.
- **G-10. Missing tables. Resolved (D5).** `refresh_tokens`, `audit_log`, `skip_count`, `users.no_show_count` added.
- **G-11. Path prefix. Resolved.** `/v1` everywhere.
- **G-12. Skip priority. Resolved (D4).** `queued_at` ordering key; skip moves the token to the back of its priority tier.
- **G-13. Unpinned choices.** Open. arq vs APScheduler (arq checked 2026-10-20: not archived, release 0.28.0 on 2026-04-16 with Python 3.14 support, about 111 open issues; choice unchanged), MSG91 vs Firebase (decided at the start of Phase 7, PRD D16), and all front-end/FastAPI versions (email provider choice dropped: no email in v1, D7).
- **G-14. Counter scope.** Per-customer no-show counter placed on `users` (global); three no-shows block advance booking (D6).
