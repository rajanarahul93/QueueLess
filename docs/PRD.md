# QueueLess — Product Requirements Document (v1)

Source: the original project brief (Oct 20, 2026) plus project-owner decisions. Requirements are numbered for cross-reference (FR-n, NFR-n). Resolved choices are in [Decisions](#8-decisions); the one undecided item is in [Open questions](#9-open-questions).

## 1. Problem statement and goals

**Problem.** Businesses that mix appointments and walk-ins run two disconnected lists, so customers wait without knowing how long, slots get double-booked, and no-shows stall the line. Owners have no data on peak hours or waits.

**Goals**

- G1. One queue for appointments and walk-in tokens, with deterministic ordering.
- G2. Customers see live position and an ETA range that updates in real time.
- G3. Staff run the queue from one console: call next, skip, no-show, cancel, reschedule.
- G4. Correctness by construction: no double bookings, no duplicate token numbers, no two staff calling the same customer.
- G5. Owners get reports: peak hours, average/p90 wait, no-show rate.

## 2. Target users and personas

| Persona | Who | Jobs-to-be-done |
| --- | --- | --- |
| **Customer** | Books a slot or walks in; verifies a phone by OTP (no anonymous use) | Book/reschedule/cancel without calling; join a queue remotely; know how long the wait is and when to leave; get told when it is their turn |
| **Front-desk staff** | Runs the queue for a business (`business_members` role `staff`; email + password) | See the whole queue at a glance; call the next customer without clashing with colleagues; handle skips, no-shows, cancellations, priority cases; check customers in (customers may also check themselves in) |
| **Business owner** | Owns the business (`owner`; email + password) | Configure services, durations and settings (grace period, max walk-ins); understand peak hours, waits and no-shows to plan staffing |

## 3. Core features

Each feature lists user stories and acceptance criteria (AC) as testable statements. Requirement IDs are in section 4.

### 3.1 Appointment booking
- *As a customer, I book a free slot for a service so I don't have to wait in line.*
- AC1. `GET /businesses/{id}/availability?service_id=&date=` returns only slots with no overlapping active appointment on that resource. (FR-3)
- AC2. 50 concurrent booking requests for one slot yield exactly one success; the other 49 receive `409 SLOT_TAKEN` with the next free slots. (FR-4)
- AC3. Repeating a booking request with the same `Idempotency-Key` within 24 h creates no second appointment. (FR-5)
- AC4. A booking in a business's timezone is stored in UTC and displayed in the business timezone. (FR-2)

### 3.2 Walk-in tokens
- *As a customer, I take a token remotely so I can wait elsewhere.*
- AC1. 500 concurrent joins to one queue produce token numbers exactly 1..500, with no gaps or duplicates. (FR-6)
- AC2. A customer with an active token (`waiting`, `called`, `in_service`) in a queue cannot take a second one in it. (FR-7)
- AC3. Joining a `paused` or `closed` queue returns `QUEUE_CLOSED`. (FR-8)
- AC4. Retrying a join with the same `Idempotency-Key` returns the original token. (FR-5)
- AC5. An appointment holder who checks in, or staff checking them in, creates a token in the same queue. (FR-9)
- AC6. Every token belongs to a phone-verified customer; there are no anonymous tokens. (FR-1, FR-7)

### 3.3 Live position and ETA
- *As a customer, I see my position and a wait range that updates itself.*
- AC1. On opening the stream, the first event is a snapshot of current position and ETA range, followed by deltas. (FR-20, FR-21)
- AC2. After a staff action that changes a customer's position, that customer's client shows the new position within 1 s. (NFR-1)
- AC3. After a reconnect with `Last-Event-ID`, missed events are replayed in order. (FR-22)
- AC4. If the stream fails twice, the client polls every 10 s. (FR-23)
- AC5. ETA is shown as a range (p25–p75) and falls back: service+hour band (≥10 samples) → service → configured `duration_min`. (FR-14, FR-15)
- AC6. No message is pushed to a customer whose position and ETA did not change. (FR-24)

### 3.4 Staff queue console
- *As staff, I see the live queue and call the next customer.*
- AC1. Console receives joined, called, skipped, completed and reordered events via `GET /queues/{id}/stream`. (FR-20)
- AC2. Two staff pressing "call next" simultaneously are assigned different tokens; neither blocks. (FR-10)
- AC3. Next token is chosen by `priority DESC, queued_at ASC`. (FR-10)
- AC4. Staff can start and complete a called token; completing a never-called token returns `409 INVALID_TRANSITION`. (FR-11)
- AC5. Staff can pause, resume and close a queue. (FR-8)
- AC6. Staff/owner endpoints verify `business_members` membership on every call; a member of business A gets 403 on business B. (FR-27)
- AC7. Each staff action (call, skip, cancel, etc.) is written to an audit log with actor. (FR-28)

### 3.5 Skip, no-show, cancel
- *As staff, I handle customers who don't respond or leave.*
- AC1. Skip returns a called token to `waiting`, at the back of its priority tier (`queued_at = now()`, priority unchanged). (FR-12)
- AC2. A third skip on the same token sets it to `no_show`. (FR-12)
- AC3. A called token not started within the grace period (`grace_period_min`, default 5, per-business setting) is set to `no_show` by the worker and its slot freed; staff can also mark no-show manually. (FR-13)
- AC4. A customer can leave the queue (`DELETE /tokens/{id}`); a customer or staff can cancel an appointment. (FR-16)
- AC5. A staff action racing a customer cancellation on the same token resolves via `version`: exactly one succeeds and the other receives a conflict error. (FR-29)
- AC6. `completed`, `no_show` and `cancelled` are terminal; no transition leaves them. (FR-11)

### 3.6 Priority queues
- *As staff, I move urgent customers ahead; as a normal customer, I'm not starved.*
- AC1. Only staff can set priority (0 normal, 1 senior/disabled, 2 emergency). (FR-17)
- AC2. A normal token waiting longer than `promotion_wait_limit_min` (default 20) is promoted one level. (FR-17)
- AC3. A checked-in appointment is called within its slot, subject to the `walk_in_ratio` (default 1 walk-in per 2 appointments). (FR-18)
- AC4. An appointment arriving after the grace period falls back to normal priority. (FR-18)

### 3.7 Rescheduling
- *As a customer, I move my appointment without losing it if the new slot is taken.*
- AC1. Reschedule creates the new appointment, marks the old `rescheduled` and links via `rescheduled_from_id` in one transaction. (FR-19)
- AC2. If the new slot is taken, the old appointment is unchanged and `SLOT_TAKEN` is returned. (FR-19)
- AC3. Changes within `reschedule_cutoff_min` (default 120) before the slot are rejected. (FR-19)

### 3.8 Owner reports
- *As an owner, I see peak hours, waits and no-shows.*
- AC1. Peak-hours report returns visits by weekday and hour. (FR-30)
- AC2. Wait-times report returns average and p90 (`started_at − joined_at`) over a date range, by service. (FR-30)
- AC3. No-show report returns no-show (and cancel) rate by service. (FR-30)
- AC4. Today's figures come from live queries; earlier days come from the nightly `daily_stats` rollup. (FR-31)
- AC5. Dashboard includes a predicted-vs-actual wait chart, using `predicted_wait_sec` stored at join time. (FR-31)
- AC6. Only the business owner can access reports. (FR-27)

### 3.9 Phone OTP login
- *As a customer, I log in with my phone number and a one-time code.*
- AC1. Every customer registers/logs in by verifying a phone number via OTP; no password required and no anonymous access, including walk-in tokens. (FR-1)
- AC2. Staff and owners log in with email and password (argon2). (FR-1)
- AC3. In development, a mock OTP provider is used; production uses a real SMS provider. (FR-1, NFR-9)
- AC4. Access tokens expire after 15 min; refresh tokens rotate and are stored hashed. (FR-26)

## 4. Functional requirements

**Accounts and tenancy**
- FR-1. Every customer authenticates with phone OTP (no anonymous tokens); staff and owners with email + password. `POST /auth/register` creates owner accounts only. An owner creates a business via `POST /businesses`, which also creates a default resource and the owner membership. Staff accounts are created by the owner (FR-39).
- FR-2. All data is multi-tenant: every tenant-owned record carries `business_id` and every query filters by it. Timestamps are stored UTC; "today's queue" resolves in the business timezone.
- FR-26. JWT access (15 min) and rotating hashed refresh tokens; the refresh token is an HttpOnly, Secure, SameSite=Lax cookie on the parent domain (D20).
- FR-27. Staff/owner endpoints verify `business_members` membership on every call, not only role claims.
- FR-28. Staff actions (call, skip, cancel, etc.) are audit-logged with actor.

**Booking**
- FR-3. Provide availability per service, resource and date.
- FR-4. Overlapping active appointments on one resource are rejected by a database exclusion constraint, returning `409 SLOT_TAKEN` with the next free slots.
- FR-5. All mutating endpoints except the auth endpoints and DELETE honour `Idempotency-Key` (retained 24 h).
- FR-19. Reschedule is atomic (new + old + link in one transaction) and respects `reschedule_cutoff_min` (default 120).
- FR-16. Customers and staff can cancel appointments; customers can leave a queue.

**Queue**
- FR-6. Token numbers are unique and gap-free per queue-day, generated under a queue row lock. The day's `queues` row is created lazily on first access (D10).
- FR-7. A customer holds at most one active token per queue.
- FR-8. Queue status is `open`, `paused` or `closed`; joins to non-open queues return `QUEUE_CLOSED`.
- FR-9. Check-in (by the customer or by staff) converts an appointment into a token.
- FR-10. Call-next selects by `priority DESC, queued_at ASC` using `FOR UPDATE SKIP LOCKED`.
- FR-11. Every transition is one service method that validates state, bumps `version`, writes an outbox event and commits together; illegal moves return `409 INVALID_TRANSITION`; completed, no-show, cancelled are terminal.
- FR-12. Skip returns the token to `waiting` with `queued_at = now()` and priority unchanged (back of its tier); `joined_at` never changes. Max two skips, then `no_show`.
- FR-13. Worker sweeper marks called tokens `no_show` after the grace period (`grace_period_min`, default 5); staff may mark manually; each no-show increments a per-customer counter; three no-shows block advance booking for that customer (walk-in tokens remain allowed).
- FR-17. Priority levels 0/1/2 set by staff only; waiting-time promotion by one level past `promotion_wait_limit_min` (default 20).
- FR-18. Checked-in appointments get priority to be called within their slot, with `walk_in_ratio` (default 1 walk-in per 2 appointments); late arrivals fall back to normal. Ordering follows D23 and D24.
- FR-29. Tokens use optimistic locking (`version`) against racing staff/customer actions.

**ETA**
- FR-14. ETA = time left on current session + sum of expected durations of tokens ahead ÷ active resources.
- FR-15. Expected duration: trimmed mean (drop top/bottom 10%) of last 50 sessions per service/hour band; fallback to per-service when <10 samples; then configured `duration_min`. Blend `0.7 × historical + 0.3 × today so far`. Return p25–p75 range; recompute on every queue event; cache per queue for a few seconds.
- FR-25. When a session overruns, later ETAs update automatically, and appointments projected to slip past `running_late_threshold_min` (default 10) trigger a "running late" event.

**Real-time**
- FR-20. Streams: staff queue stream, per-customer token stream, public board (token numbers only).
- FR-21. Each stream sends a snapshot first, then deltas.
- FR-22. SSE event `id` = outbox row id; `Last-Event-ID` resumes by replay from the outbox. Events older than the 7-day outbox retention (D14) cannot be replayed; the client falls back to a fresh snapshot.
- FR-23. Server sends a heartbeat every 15 s; client falls back to 10 s polling after two stream failures.
- FR-24. Per-customer messages are sent only if position or ETA changed.
- FR-32. Events publish only after DB commit (outbox → worker → Redis Pub/Sub → API instances → SSE).
- FR-33. Connections are limited per user and per IP, and capped per instance.

**Reports**
- FR-30. Endpoints: peak hours, wait times (avg + p90), no-show/cancel rate.
- FR-31. Live queries for today; nightly `daily_stats` rollup for history; predicted-vs-actual wait chart from `queue_tokens.predicted_wait_sec` (stored at join time).

**Abuse control and privacy**
- FR-34. Redis sliding-window rate limits per IP and per user on token creation and booking; cap on active bookings per customer (at most 5 active appointments that end in the future; beyond that `409 BOOKING_LIMIT_REACHED`).
- FR-35. Public board/lobby screens expose token numbers only; stream URLs (customer and staff) use short-lived signed stream tokens passed as `?stream_token=`: customers get theirs from `GET /tokens/{id}`, staff from `POST /queues/{id}/stream-token` (D20). No stream needs an Authorization header or cookie.

**Lookups, lists and settings (D17-D20)**
- FR-37. `GET /businesses/{id}/queue` (any authenticated user) returns today's queue for the business's default resource, creating it lazily per D10, so customers and staff can obtain the queue id. `GET /queues/{id}` (staff) returns the full queue snapshot (ordered tokens, status); it is the first SSE snapshot content and the staff polling-fallback endpoint.
- FR-38. `GET /appointments` lists a customer's own appointments (`?status=&from=&to=`) or, for staff, a business's appointments (`?business_id=` required, membership checked; "Appointments today"). `GET /tokens?active=true` lists the customer's own active tokens so the live ticket resumes after a reload.
- FR-39. `PATCH /businesses/{id}` (owner) updates name, address, open hours, timezone and settings, with validated min/max bounds for each settings key. The owner creates staff with `POST /businesses/{id}/staff` (also creates the `business_members` row with role `staff`) and removes them with `DELETE /businesses/{id}/staff/{user_id}`.
- FR-40. Staff stream access: `POST /queues/{id}/stream-token` (staff, membership checked) returns a signed short-lived token used as `?stream_token=` on `GET /queues/{id}/stream`.

**Notifications**
- FR-36. v1 notifications are in-app live updates over SSE only (turn called, "running late", etc. as events). No email or SMS except OTP. Notifications go through a provider adapter with the mock provider.

## 5. Non-functional requirements

| ID | Area | Target |
| --- | --- | --- |
| NFR-1 | Live update latency | Customer position updates within 1 s of the staff action (p95, measured at the Phase 4 test load) |
| NFR-2 | Join concurrency | 500 concurrent joins → tokens exactly 1..500, 0 duplicates/gaps (Locust/asyncio test) |
| NFR-3 | Booking concurrency | 50 concurrent bookings on one slot → exactly 1 success |
| NFR-4 | Call-next concurrency | N simultaneous call-next requests → N distinct tokens, none blocked |
| NFR-5 | Durability | DB is source of truth; Redis/SSE loss never loses state; clients recover via snapshot + replay |
| NFR-6 | Resilience | Reconnecting client catches up with no missed events; polling fallback after 2 stream failures |
| NFR-7 | Mobile-first | Customer app is mobile-first: minimum width 360 px; latest two versions of Chrome and Safari; not an installable PWA in v1 |
| NFR-8 | Availability | No availability SLA in v1; API runs as a long-lived container, not serverless |
| NFR-9 | Security | argon2, parameterised queries, strict CORS, input validation, per-tenant filtering, secrets via environment |
| NFR-10 | Observability | Structured logs with request id; metrics for queue depth, SSE connections, outbox lag, ETA error, p95 latency; Sentry |
| NFR-11 | Cost | Hosting starts around $5/month plus a custom domain (D20); components movable to free tiers |
| NFR-12 | Testability | Unit, integration (real Postgres/Redis), concurrency, real-time, and end-to-end (book → check-in → called → complete) tests; CI runs lint, tests, migrations |
| NFR-13 | Performance | Customer pages reach Largest Contentful Paint within 2.5 s on a mid-range phone over 4G; initial JavaScript for the customer app stays under 200 KB gzipped (D11) |

## 6. Out of scope for v1

From the source's post-v1 list:
- Email, SMS, WhatsApp and push notifications (SMS is used only for OTP login)
- Slot hold at checkout (no Redis hold in v1)
- Installable PWA and an availability SLA
- QR-code check-in
- Multi-branch businesses
- Lobby display screen (the public board *endpoint* is in scope per FR-20; a dedicated screen is not)
- ML duration-prediction model
- AWS variant of the backend

Also out of scope by decision:
- Payments: `services.price` is display-only (D12)
- Features for the `admin` role: the value stays in the schema, with no admin endpoints or screens (D13)
- Multi-resource selection UI: each business has one default resource and customers never choose a queue (D15)
- WebSockets and two-way staff–customer chat

## 7. Success metrics and demo scenario

**Metrics**
- 0 duplicate/gap token numbers across the 500-join test; exactly 1 winner across the 50-booking test (results in README).
- Position update ≤ 1 s after staff action.
- Predicted-vs-actual wait chart renders from real session data.
- Public demo URL live, with load-test results in README.

**Demo scenario**
1. Owner registers, creates a business with a service, sets grace period, and creates a staff account.
2. Customer A verifies a phone by OTP and books a slot; customer B tries the same slot and gets `SLOT_TAKEN`.
3. Customer C takes a walk-in token; the customer's phone shows position and ETA range.
4. Staff opens the console; two staff click "call next" at once and get different tokens.
5. Customer's screen updates within 1 s; airplane-mode toggle on the phone reconnects and catches up.
6. Staff skips one customer, marks another no-show, and sets an emergency priority token that jumps the line.
7. Customer A reschedules (success) and attempts a reschedule into a taken slot (no change).
8. Owner opens the dashboard: peak hours, avg/p90 wait, no-show rate, predicted vs actual.
9. Load-test output shows 500 joins → tokens 1..500.

## 8. Decisions

Resolved from the source's open questions and gaps (Architecture §10).

- **D1 Identity.** Every customer verifies a phone by OTP; there are no anonymous tokens. `queue_tokens.customer_id` is NOT NULL. (G-1, G-2)
- **D2 Token states.** Stored token statuses: `waiting`, `called`, `in_service`, `completed`, `no_show`, `cancelled`. "Joined" and "skipped" are events, not statuses; a skip returns the token to `waiting`. Appointment statuses: `booked`, `checked_in`, `in_service`, `completed`, `cancelled`, `no_show`, `rescheduled`. (G-5)
- **D3 Check-in.** Customer or staff may check in an appointment. (G-4)
- **D4 Skip ordering.** `queue_tokens.queued_at timestamptz NOT NULL DEFAULT now()` is the ordering key; order is `priority DESC, queued_at ASC`. A skip sets `queued_at = now()` and keeps priority. `joined_at` never changes and is used for wait metrics. (G-12)
- **D5 Approved additions.** All **[added]** items in Architecture §5 and §6 (columns, `refresh_tokens`, `audit_log`, extra error codes, OTP endpoints); `POST /businesses`; `stream_token` returned by `GET /tokens/{id}` (G-8); `queue_tokens.predicted_wait_sec int NULL` stored at join time.
- **D6 Defaults** (in `businesses.settings`): `grace_period_min` 5, `reschedule_cutoff_min` 120, `promotion_wait_limit_min` 20, `walk_in_ratio` 1 walk-in per 2 appointments, `running_late_threshold_min` 10. Three no-shows block advance booking (walk-in tokens still allowed). Slot hold is not built in v1.
- **D7 Notifications v1.** In-app live updates over SSE only, including "running late" as an event. No email or SMS except OTP. The notification adapter and mock provider stay.
- **D8 Engineering rules.** No repository modules; raw SQL only in migrations and commented locking queries.
- **D9 Non-functional targets.** Customer app: 360 px minimum width, latest two versions of Chrome and Safari, no installable PWA. No availability SLA in v1. The 1 s update latency is a p95 measured at the Phase 4 test load.

- **D10 Queue creation.** A day's `queues` row is created lazily on first access (customer join, check-in, staff opening the console, or `GET /businesses/{id}/queue`) with `INSERT ... ON CONFLICT (resource_id, queue_date) DO NOTHING`, status `open`, then locked `FOR UPDATE` for the token increment. No midnight job. (Source gap, formerly open question 7)
- **D11 Performance budget.** Customer pages reach Largest Contentful Paint within 2.5 s on a mid-range phone over 4G, and the initial JavaScript for the customer app stays under 200 KB gzipped (NFR-13).
- **D12 Payments.** Out of scope for v1; `services.price` is display-only.
- **D13 Admin role.** The `admin` value stays in the `users.role` check constraint but has no features in v1: no admin endpoints or screens.
- **D14 Retention.** Published `outbox_events` rows are deleted after 7 days by a worker job; `audit_log` is kept 12 months; phone numbers are kept while the account exists. Events older than 7 days cannot be replayed with `Last-Event-ID`; the client falls back to a fresh snapshot.
- **D15 Resource choice.** v1 businesses have one default resource, so customers never choose a queue. Multi-resource selection UI is out of scope.
- **D16 OTP provider.** Mock provider for all development. MSG91 vs Firebase phone auth is decided at the start of Phase 7, after checking current pricing and DLT requirements.
- **D17 Queue lookup.** `GET /businesses/{id}/queue` (any authenticated user) returns today's queue for the default resource, created lazily per D10 (`INSERT ... ON CONFLICT DO NOTHING`, status `open`). `GET /queues/{id}` (staff) returns the full queue snapshot (ordered tokens, status), used as the first SSE snapshot content and as the staff polling-fallback endpoint. (FR-37)
- **D18 Lists.** `GET /appointments` (customer: own, with `?status=&from=&to=`; staff: `?business_id=` required, membership checked, used for "Appointments today") and `GET /tokens?active=true` (customer: own active tokens, so the live ticket resumes after a reload). (FR-38)
- **D19 Business settings and staff accounts.** `PATCH /businesses/{id}` (owner: name, address, open_hours, timezone, settings with validated min/max bounds for each D6 key). `POST /auth/register` creates owner accounts only. The owner creates staff with `POST /businesses/{id}/staff {name, email, initial_password}`, which also creates the `business_members` row with role `staff`, and removes them with `DELETE /businesses/{id}/staff/{user_id}`. (FR-1, FR-39)
- **D20 Browser auth and stream auth.** Production uses one custom domain: web at `app.<domain>`, api at `api.<domain>`, so the refresh-token cookie is same-site (HttpOnly, Secure, SameSite=Lax, `Domain=.<domain>`); CORS allows only the web origin, with credentials. Local development runs both on localhost. Staff streams also use a signed short-lived stream token: `POST /queues/{id}/stream-token` (staff, membership checked) and `?stream_token=` on `GET /queues/{id}/stream`, so no stream needs an Authorization header or cookie. (FR-26, FR-35, FR-40)
- **D21 Staff action bar.** The primary button is state-driven and acts on the token the staff member has selected, or on the token they most recently called if none is selected: Call next (when they have no active token), then Start (token is `called`), then Complete (token is `in_service`). Skip and No-show are secondary buttons acting on that same token. Token detail history shows only timestamps from the token row (joined, called, started, completed) plus skip count; there is no per-event history.
- **D22 Walk-in ratio.** Derived from the call history, no schema change: when the last `walk_in_ratio.appointments` tokens called in this queue today (ordered by `called_at`) all have source `appointment`, the next call-next goes to the oldest waiting walk-in token (by `queued_at`) if one exists and no priority-2 token is waiting; otherwise the normal order applies.
- **D23 Appointment ordering.** An appointment token checked in on time gets `queued_at` equal to the slot start (not the check-in time); `joined_at` stays the check-in time. The `priority` column stays staff-only (0, 1, 2). Order is still `priority DESC, queued_at ASC`, so on-time appointments are called near their slot without using priority.
- **D24 Check-in window and late arrivals.** Check-in opens 30 minutes before the slot start and closes at the slot end (start plus service duration). Check-in up to slot start plus `grace_period_min` is on time. After that, until the slot end, it is late: `queued_at` is the check-in time and priority stays 0, so the token is treated like a walk-in. After the slot end check-in returns `INVALID_TRANSITION` and the customer can take a walk-in token.
- **D25 Promotion.** A worker job runs every minute and promotes waiting tokens with priority 0 whose wait (now minus `joined_at`) exceeds `promotion_wait_limit_min` to priority 1, once and never higher. A promoted token is indistinguishable from a staff-set 1. A delay of up to about one minute is accepted.
- **D26 Queue states.** `paused`: no new joins or check-ins, staff keep calling and serving, and the queue can be resumed. `closed`: no new joins or check-ins, staff finish the tokens already waiting, and the queue cannot be reopened the same day.
- **D27 End of day.** At business-local midnight a worker job cancels tokens that are `waiting` or `called` (not `in_service`) and sets the queue to `closed`. What happens to the linked appointment status is decided in the Phase 3D plan.
- **D28 Concurrent service.** The server enforces no limit on `called` or `in_service` tokens per queue in v1; the staff UI prevents double calls. Known gap.
- **D29 No-shows.** A token that becomes `no_show` (sweeper, staff action or third skip) sets its appointment to `no_show` when the source is `appointment`, and adds 1 to `users.no_show_count` exactly once per token.

## 9. Open questions

1. **OTP provider.** MSG91 vs Firebase phone auth, to be decided at the start of Phase 7 after checking current pricing and DLT requirements (D16). The mock provider covers development.
