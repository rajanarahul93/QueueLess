# QueueLess — Design (web app)

Stack: Next.js, Tailwind, shadcn/ui. Requirement ids (FR-n, NFR-n, D-n) refer to `docs/PRD.md`. Tokens below map to CSS variables in `:root` and `.dark`, exposed to Tailwind as semantic names (`bg-surface`, `text-muted`, …). Do not use raw hex in components.

## 1. Design principles

1. **Mobile-first.** Customers use phones in waiting rooms. Design at 360 px wide; larger layouts only add columns (NFR-7).
2. **Glanceable.** The one thing a customer needs (position, ETA range, status) is readable from arm's length: ≥ 48 px type, one screen, no scrolling to see it.
3. **Calm.** Neutral surfaces, one accent colour, status colours used only for status. No flashing, no sound, no toasts that auto-dismiss critical information.
4. **Accessible.**
   - WCAG AA contrast: ≥ 4.5:1 for text, ≥ 3:1 for UI boundaries and icons (ratios in §2).
   - Touch targets ≥ 44 × 44 px with ≥ 8 px between adjacent targets.
   - Focus ring on every interactive element: 2 px `--focus` outline, 2 px offset, never removed.
   - `prefers-reduced-motion` honoured (§7).
   - Status is never colour alone: always an icon and a text label.
   - Live regions: position/status changes announced with `aria-live="polite"`; errors with `role="alert"`.
5. **Light.** Performance budget (NFR-13): LCP ≤ 2.5 s on a mid-range phone over 4G; initial customer JavaScript < 200 KB gzipped. Consequences: server components for static shells, one font family loaded with `font-display: swap` (variable font, Latin subset), Recharts only on owner routes (dynamic import), no animation library.

## 2. Colour tokens

Ratios are WCAG 2 contrast, computed from the hex values. "Text pair" = foreground on its background.

### 2.1 Core

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--background` | `#F8FAFC` | `#0B1220` | Page |
| `--surface` | `#FFFFFF` | `#111A2E` | Cards, sheets, inputs |
| `--text` | `#0F172A` | `#E5EAF3` | Body, headings |
| `--muted` | `#475569` | `#A3AEC2` | Secondary text, labels |
| `--primary` | `#1D4ED8` | `#60A5FA` | Primary buttons, links, active nav |
| `--primary-fg` | `#FFFFFF` | `#0B1220` | Text on primary |
| `--border` | `#CBD5E1` | `#26324A` | Decorative dividers (not a required boundary) |
| `--border-strong` | `#76849A` | `#5B6B88` | Input, button outline, card boundary where it carries meaning |
| `--focus` | `#1D4ED8` | `#93C5FD` | Focus ring |
| `--danger` | `#B91C1C` | `#FCA5A5` | Error text, destructive actions |

| Pair | Light ratio | Dark ratio |
| --- | --- | --- |
| text on background | 17.06 | 15.51 |
| text on surface | 17.85 | 14.36 |
| muted on background | 7.24 | 8.37 |
| muted on surface | 7.58 | 7.75 |
| primary-fg on primary | 6.70 | 7.36 |
| primary on surface (links) | 6.70 | 6.82 |
| border-strong on surface (non-text, needs ≥ 3) | 3.79 | 3.22 |
| danger on surface | 6.47 | 9.14 |

### 2.2 Token and connection status

Each status has `fg` (text and icon) on `bg` (badge tint). Both are used together as a badge.

| Status | Light fg / bg | Light ratio | Dark fg / bg | Dark ratio | Icon (lucide) |
| --- | --- | --- | --- | --- | --- |
| waiting | `#92400E` / `#FEF3C7` | 6.37 | `#FCD34D` / `#3B2A06` | 9.58 | `clock` |
| called | `#1D4ED8` / `#DBEAFE` | 5.49 | `#93C5FD` / `#13284F` | 8.06 | `bell-ring` |
| in service | `#5B21B6` / `#EDE9FE` | 7.57 | `#C4B5FD` / `#2A1F52` | 8.03 | `loader-circle` (static) |
| completed | `#166534` / `#DCFCE7` | 6.49 | `#86EFAC` / `#0F3320` | 9.87 | `circle-check` |
| no-show | `#B91C1C` / `#FEE2E2` | 5.30 | `#FCA5A5` / `#431616` | 8.11 | `user-x` |
| cancelled | `#475569` / `#E2E8F0` | 6.15 | `#A3AEC2` / `#1E2A42` | 6.41 | `circle-slash` |
| live (connection) | `#166534` / `#DCFCE7` | 6.49 | `#86EFAC` / `#0F3320` | 9.87 | `wifi` |
| reconnecting | `#92400E` / `#FEF3C7` | 6.37 | `#FCD34D` / `#3B2A06` | 9.58 | `wifi-sync` |
| offline | `#B91C1C` / `#FEE2E2` | 5.30 | `#FCA5A5` / `#431616` | 8.11 | `wifi-off` |

The "running late" notice reuses the waiting (amber) pair. The default theme follows `prefers-color-scheme`; the user can override it in the account menu.

## 3. Typography

One family: **Inter** (variable, via `next/font`, Latin subset, `font-display: swap`). Numerals use `font-variant-numeric: tabular-nums` everywhere a number can change (token, position, ETA) so digits do not jitter.

| Style | Size / line height | Weight | Use |
| --- | --- | --- | --- |
| `token-number` | 72 / 76 px, letter-spacing -0.02em | 700 | Token number on the token card and hero |
| `display` | 36 / 40 px | 700 | Position number, ETA range |
| `h1` | 24 / 32 px | 600 | Screen title |
| `h2` | 20 / 28 px | 600 | Section title |
| `body-lg` | 18 / 28 px | 400 | Primary body text on customer screens |
| `body` | 16 / 24 px | 400 | Default; also minimum input size (prevents iOS zoom) |
| `label` | 14 / 20 px | 500 | Field labels, badges, buttons |
| `caption` | 12 / 16 px | 500 | Timestamps, helper text; never the only carrier of meaning |

Staff console tables use `body` / `label`; the token number in list rows is `h2` at weight 700. At ≥ 1024 px `token-number` stays 72 px (do not scale up).

## 4. Spacing, radius, elevation, breakpoints

- **Spacing** (4 px base): 0, 4, 8, 12, 16, 24, 32, 48, 64 (Tailwind 0, 1, 2, 3, 4, 6, 8, 12, 16). Page gutter 16 px below 640 px, 24 px from 640 px. Gap between list rows 8 px; card padding 16 px.
- **Radius:** `sm` 6 px (badges, inputs), `md` 12 px (cards, buttons), `lg` 16 px (sheets, hero), `full` (dots, avatars).
- **Elevation:** three levels, shadows only in light theme; in dark theme use a 1 px `--border` and a lighter surface instead.
  - 0: flat, `--border` 1 px.
  - 1 (cards on interaction): `0 1px 2px rgba(15,23,42,.08)`.
  - 2 (sheets, dialogs, sticky action bar): `0 8px 24px rgba(15,23,42,.16)`.
- **Breakpoints:** base 360 px (minimum supported), `sm` 640, `md` 768, `lg` 1024, `xl` 1280. Customer app is a single column capped at 480 px wide and centred. Staff console is single column below `md`, two panes (list + detail) from `lg`. Owner dashboard is one column below `md`, two columns from `lg`.

## 5. Components and states

All interactive components have: default, hover (desktop only, +4% foreground tint), focus-visible (ring), active/pressed, disabled (50% opacity plus `aria-disabled`; the reason is shown as text when action is blocked by a rule), and loading (spinner replaces the label, width fixed, button disabled).

**Token card.** Surface card, radius `md`, padding 16. Top row: business name (`label`, muted) and status badge. Centre: `token-number` prefixed `#`. Bottom: service name, appointment time if any. Priority 1/2 shows a text chip ("Priority") visible to staff only. States: waiting, called (card gets a 2 px `called` border and the card text reads "Please go to the counter"), in service, completed, no-show, cancelled (terminal states: muted text, no action buttons).

**Queue list row** (staff). Height 64 px. Left: token number (`h2`, 56 px min width); middle: customer name and service (`body`, truncated with ellipsis; full text in the detail pane); right: status badge and wait time since `joined_at` ("12 min"). Row tap opens the detail sheet. Variants: appointment (clock icon plus slot time) and walk-in. Selected row has a 2 px `--primary` left bar.

**Status badge.** Pill, radius `full`, height 28 px, padding 0 12 px, `label` type, icon 16 px plus text, colours from §2.2. Text is always present ("Waiting", "Called", "In service", "Completed", "No-show", "Cancelled").

**Position and ETA hero.** Surface card, radius `lg`, padding 24, centred.
- Line 1 (`label`, muted): "Your position".
- Line 2: `display` number, e.g. "3". When position is 1: "You're next" (`h1`).
- Line 3: `body-lg` "Estimated wait" then `display` range **"12–18 min"** (en dash, p25–p75, FR-15). Never a single number. If the range collapses (p25 = p75 after rounding) show "about 15 min". If ETA is unavailable show "Calculating…" (not 0).
- Caption: "Updated just now" / "Updated 2 min ago" (relative, updates every 30 s).
- States: loading (skeleton, §5 below), live, stale (connection lost: the numbers stay, caption turns amber "May be out of date"), called (hero replaced by "It's your turn" with `called` colours), terminal.

**Staff action bar.** Sticky bottom bar on mobile (height 72 px, elevation 2); inline toolbar on `lg`. Buttons in order: a state-driven **primary** button (filled, min width 40% of the bar, 56 px high), **Skip** (outline), **No-show** (outline, `danger` text), overflow menu (Priority, Cancel). The *target token* is the token the staff member has selected, or, if none is selected, the token they most recently called (the client remembers it from the call-next response; after a reload they select the row). The primary label follows the target token: **Call next** when they have no active token (no target, or the target is `waiting` or terminal; it calls the next token by queue order, not the selected one), **Start** when the target is `called`, **Complete** when the target is `in_service`. Skip and No-show act on the same target token and are disabled when there is none or it is not `called`; "Skip (2/2)" shows the remaining skips; a third skip is a no-show and the label changes to "No-show" with a confirmation. No-show and Cancel open a confirm dialog (focus on Cancel button, destructive action on the right). After a successful Call next, the bar shows the assigned token number in a `role="status"` line below the bar after success. Pressing a button disables the bar until the response arrives (max 5 s, then re-enabled with an error).

**Slot picker.** Date strip (7 days, each 56 × 64 px) above a grid of slot buttons, 3 columns at 360 px, each 48 px high showing the start time in the business timezone ("2:30 PM"). Selected slot: filled `--primary`. Unavailable slots are not shown (availability returns free slots only, FR-3). Footer shows duration and the timezone name. A confirmation step lists service, date, time and a "Confirm booking" button.

**OTP input.** Six separate 48 × 56 px boxes, 8 px gap (fits 360 px: 6 × 48 + 5 × 8 = 328 px), `inputmode="numeric"`, `autocomplete="one-time-code"`, paste fills all boxes, auto-submit on the 6th digit. Phone step first: country code select plus number, `inputmode="tel"`. Resend link disabled for 30 s with a visible countdown ("Resend in 24 s"). Errors (wrong code, expired code, locked out, rate limited) show under the boxes in `danger` text with an icon; boxes get a 2 px `--danger` border.

**Connection-status indicator.** Pill in the top bar, 28 px high, icon plus word, colours from §2.2.
- Live: "Live" — shown for 3 s after (re)connect, then collapses to a 10 px dot with an `aria-label` of "Live".
- Reconnecting: "Reconnecting…" — stays visible; no spinner animation under reduced motion.
- Offline: "Offline — retrying" with a "Retry now" button (44 px).
- Polling fallback: "Live (slow)"; amber pair, to show updates are 10 s polled (FR-23).

**Empty states.** Icon (48 px, muted), `h2` title, one `body` sentence, one primary action when there is one. Examples: customer with no appointments ("No upcoming appointments" / "Book a slot"); staff queue empty ("Queue is empty" / no action); owner reports with no data ("No visits in this period" / date range control).

**Loading states.** Skeleton blocks the size of the final content (no layout shift, CLS < 0.1), neutral `--border` colour, no shimmer under reduced motion (static). Content that takes > 400 ms shows the skeleton; faster loads show nothing. Buttons use inline spinner. Never a full-page spinner.

**Error states.**
- Inline field errors: `danger` text under the field, linked with `aria-describedby`.
- Page-level error: card with `circle-alert` icon, the problem `title` in plain language, a "Try again" button, and the request id in `caption` (for support).
- Network error: banner "You're offline" with retry; cached content stays on screen.
- 403 / 404: dedicated screen with a single link home.
- Problem `code` → copy is mapped in one place; no raw server `detail` is shown to customers.

**SLOT_TAKEN presentation.** Triggered by booking or reschedule (`409 SLOT_TAKEN` with `next_slots`).
- Stay on the slot picker; the chosen slot is deselected and removed from the grid.
- Inline alert (`role="alert"`, amber pair) above the grid: "That time was just taken. Pick another."
- Show up to 3 `next_slots` as buttons ("2:45 PM", "3:00 PM", "3:15 PM") directly in the alert; tapping one selects it and returns focus to Confirm. If `next_slots` is empty: "No more times today" with a "See tomorrow" button.
- On reschedule the alert adds "Your current appointment hasn't changed" (FR-19 AC2) and the current appointment stays pinned at the top.

**VERSION_CONFLICT presentation.** Triggered when a staff action races a customer cancellation or another staff action (`version` mismatch, FR-29).
- The action bar re-enables; the list row and detail refetch immediately.
- Inline notice in the detail sheet (`role="alert"`, amber pair): "This customer was just updated by someone else." followed by the new status badge.
- No automatic retry of the action. If the token is now terminal, the action buttons are removed. Staff press the action again deliberately.
- The customer side never sees this error; a failed cancel because staff acted first shows the new state of their ticket ("You were called — please go to the counter").

## 6. Screen inventory

Each screen lists the PRD requirements it serves.

### Customer (single column, max 480 px)

| Screen | Layout | Serves |
| --- | --- | --- |
| Phone login | Phone field, "Send code" button; then OTP boxes and resend countdown | FR-1, FR-34 |
| Business detail | Header (name, address, hours), service list with duration and price (display only, D12), "Book" and "Join queue" buttons | FR-3, FR-6 |
| Book appointment | Service, date strip, slot picker, confirm step; SLOT_TAKEN alert | FR-3, FR-4, FR-5, FR-2 |
| My appointments | List of upcoming and past cards, actions: Check in, Reschedule, Cancel | FR-9, FR-16, FR-19 |
| Reschedule | Slot picker with the current appointment pinned; cutoff message when within `reschedule_cutoff_min` | FR-19 |
| Live ticket | Connection pill, token card, position and ETA hero, "Leave queue" (outline, confirm dialog); running-late notice | FR-6, FR-7, FR-8, FR-14, FR-15, FR-20–FR-25 |
| Queue closed / paused | Card explaining the state (`QUEUE_CLOSED`), no join button | FR-8 |

The customer never picks a resource or queue (D15); "Join queue" targets the business's default resource; the queue id comes from `GET /businesses/{id}/queue` (D17). The live ticket resumes after a reload from `GET /tokens?active=true` (D18).

### Staff (single column below `lg`; two panes from `lg`)

| Screen | Layout | Serves |
| --- | --- | --- |
| Staff login | Email, password, submit (accounts are created by the owner, D19) | FR-1 |
| Queue console | Top bar: queue status (Open / Paused / Closed) menu, connection pill, now-serving token. Body: ordered queue list rows (priority, then queued time). Sticky action bar. Detail sheet (mobile) or right pane (`lg`) for a token | FR-10–FR-13, FR-17, FR-18, FR-20, FR-27, FR-28 |
| Appointments today | Time-ordered list with check-in button per row (`GET /appointments?business_id=`) | FR-9, FR-38 |
| Token detail | Customer name, service, skip count, priority control (0/1/2), and the timestamps from the token row only: joined, called, started, completed (shown when set). No per-event history | FR-12, FR-17, FR-29 |

### Owner

| Screen | Layout | Serves |
| --- | --- | --- |
| Owner login / register | Email, password (register creates owner accounts only); "Create business" step (name, timezone, hours) | FR-1 |
| Services | List with add/edit form (name, duration, buffer, price, active) | FR-3 |
| Staff | List of staff (name, email) with "Remove" (confirm dialog); "Add staff" form (name, email, initial password); duplicate-email and validation errors inline | D19, FR-39 |
| Settings | Business name, address, hours, timezone; grace period, reschedule cutoff, promotion wait limit, walk-in ratio, running-late threshold, with defaults and allowed range shown (D6); out-of-range values rejected with inline errors | D6, D19, FR-13, FR-17, FR-18, FR-25, FR-39 |
| Dashboard | Date-range control; cards: average wait, p90 wait, no-show rate; charts: peak-hours heatmap (weekday × hour), wait trend, predicted vs actual wait | FR-30, FR-31 |

Chart colours reuse §2.2 status colours for categories and a single-hue `--primary` ramp for the heatmap; every chart has a table fallback (`<details>` "View data"). There are no admin screens (D13).

## 7. Motion

- Durations: 150 ms for hover/press, 200 ms for number and status changes, 250 ms for sheets and dialogs. Easing `cubic-bezier(0.2, 0, 0, 1)`.
- **Position change:** the old number fades out and the new one fades in with a 4 px vertical slide (200 ms). The hero card background pulses once to a 12% `--primary` tint and back (600 ms total). Only when the position or status actually changed (FR-24).
- **List reorder** (staff): rows move with a 200 ms transform; added rows fade in; removed rows collapse over 200 ms.
- **Called:** the card border switches to the `called` colour with no animation beyond the 200 ms transition. No vibration or sound in v1.
- **Reduced motion** (`prefers-reduced-motion: reduce`): all transitions and slides are replaced by instant changes (≤ 1 ms); no pulse; skeletons are static; the reconnecting icon is static. Changes are conveyed by the text caption "Updated just now" and the live region.
- **No motion is required to understand state:** every state has a text label, an icon and a colour. Nothing flashes more than once per second (WCAG 2.3.1).

## 8. Live-update behaviour in the UI

Applies to the customer ticket and the staff console. Contract: FR-20–FR-24, FR-32, NFR-5, NFR-6.

1. **Open.** On mount, load the ticket with `GET /tokens/{id}` (gives `stream_token`), render the skeleton, then open `EventSource` on the stream. The first event is a **snapshot**; it replaces all local state and clears the skeleton. Later events are **deltas** applied in order. The staff console works the same way: `POST /queues/{id}/stream-token`, then `GET /queues/{id}/stream?stream_token=` (D20), with `GET /queues/{id}` as the initial load. Stream tokens are short-lived; on reconnect the client fetches a fresh one first.
2. **Steady state.** Pill shows "Live" (3 s, then dot). Heartbeats every 15 s are invisible. If no event or heartbeat arrives for 30 s, treat the stream as failed.
3. **Connection drop.** Keep the last numbers on screen, set the pill to "Reconnecting…", and mark the hero caption amber "May be out of date". Retry with backoff 1 s, 2 s, 4 s (capped at 10 s). The browser sends `Last-Event-ID`; the server replays missed events.
4. **Reconnect success.** Replayed events are applied in order; if the server instead sends a fresh snapshot (the id is older than the 7-day retention, D14, or the server cannot replay), it replaces local state. The pill returns to "Live" for 3 s, the caption returns to "Updated just now". If the position changed while away, the normal position-change motion plays once for the final value only (not for each missed step).
5. **Polling fallback.** After two consecutive stream failures (FR-23), stop retrying the stream and poll `GET /tokens/{id}` (staff: `GET /queues/{id}`) every 10 s. The pill reads "Live (slow)" in amber. Every 60 s, try the stream again; on success, polling stops and the pill returns to "Live".
6. **Offline.** If the browser reports `offline` or polling also fails, the pill shows "Offline — retrying" with "Retry now". Cached content remains visible with the amber caption; action buttons that need the network are disabled with the reason "You're offline".
7. **Unchanged data.** If a delta repeats the current position and ETA, the UI does not animate or move the "Updated" caption forward (FR-24).
8. **Running late.** When a `running_late` event arrives (FR-25), show an inline notice under the hero, amber pair, `clock` icon: "Running about 15 min behind. Your new estimate is 25–35 min." The notice shows the delay from the event and the updated range from the hero. It stays until the delay falls under `running_late_threshold_min` (default 10) or the token is called; it can be dismissed for the session but reappears if the delay grows by ≥ 5 min. It is announced once through the polite live region. No push, SMS or email is sent in v1 (D7).
9. **Called.** A `token.called` event replaces the hero with "It's your turn — please go to the counter" (`called` colours), moves focus to that heading, and announces through an assertive live region (the only assertive announcement in the app).
10. **Terminal state.** On completed, no-show or cancelled, close the stream, show the final badge and message, and offer "Back to business".
11. **Public board** (token numbers only, FR-35) is not a v1 screen (PRD §6); the endpoint exists but has no UI.
