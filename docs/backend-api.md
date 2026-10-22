# REWIND backend API

Base URL when running locally: `http://127.0.0.1:8000` (`cd backend; uvicorn main:app --reload`).

Scope: Undo is supported for **customer status changes only**. This is not a universal rollback.

## Shapes

**Customer**
```json
{"account_id": "ACC-102", "name": "Meera Iyer", "status": "Active",
 "last_activity_at": "2025-03-10T12:00:00Z", "protected": false, "version": 1}
```
`last_activity_at` is ISO-8601 UTC with a trailing `Z`.

**Verification** (deterministic code re-reading SQLite; never the LLM)
```json
{"status": "verified" | "failed",
 "checks": {"<check>": true, "...": true},
 "current_customer": Customer | null}
```
Update checks: `requested_status_applied`, `version_incremented`, `unrelated_records_unchanged`, `database_integrity_ok`.
Undo checks: `original_status_restored`, `version_incremented`, `unrelated_records_unchanged`, `database_integrity_ok`.

**Error** (any 4xx/5xx the backend produces itself)
```json
{"status": "error", "reason": "Customer not found"}
```
Request-body validation errors are still FastAPI's default 422 `{"detail": [...]}`.

## Routes

### `GET /api/customers` → `Customer[]` ordered by `account_id`

### `GET /api/actions` → ledger, newest first
```json
[{"id", "run_id", "tool_name", "arguments", "before", "after", "risk",
  "status", "recovery_state", "verification", "created_at", "recovered_at",
  "actor", "decision", "reason", "account_id", "before_version", "after_version",
  "mode", "scenario", "undo_of_action_id"}]
```
- `actor`: `api` for direct calls (`agent` / `system` later)
- `decision`: `allow` | `block`
- `mode`: `null` for direct API calls, later `live` | `replay` (replay is never a live model run)
- `scenario`: `null`, or `injected_fault` for demo fault runs
- `undo_of_action_id`: reserved for later; `null` today
- `status`: `executed` | `blocked` | `failed`
- `recovery_state`: `available` | `unavailable` | `conflict_detected` | `verified` | `verification_failed`
- `risk`: `low` | `medium` | `high`

### `POST /api/tools/update-customer-status`
Request: `{"account_id": "ACC-102", "new_status": "Active" | "Archived", "run_id": "optional"}`

| Code | Body |
|---|---|
| 200 | `{action_id, status: "executed", risk, recovery_state: "available", before, after, verification}` |
| 403 | `{action_id, status: "blocked", reason, before, after}` (protected customer) |
| 409 | `{action_id, status: "blocked", reason, before, after}` (already has that status) |
| 404 | Error |
| 500 | `{action_id, status: "failed", risk, recovery_state: "unavailable", before, after, verification}`; the customer write was rolled back, the attempt is still logged |

### `POST /api/actions/{action_id}/undo`

| Code | Body |
|---|---|
| 200 | `{action_id, ...Verification}` with `status: "verified"`; customer restored |
| 409 | `{action_id, status: "conflict_detected", reason, expected_after, current_customer}`; a newer edit exists, nothing changed. Permanent for that action. |
| 409 | `{action_id, status: "refused", reason}`; Undo not available in the current `recovery_state` |
| 404 | Error |
| 500 | `{action_id, ...Verification}` with `status: "failed"`; the restore was rolled back, customer unchanged |

### `POST /api/demo/reset`
Only works when the backend runs with `REWIND_DEMO_MODE=1`; otherwise 403 Error.
Clears the ledger and reseeds the 8-customer fixture in one transaction. The only route that wipes data.
```json
{"status": "reset", "demo_now": "2026-10-22T12:00:00Z", "customers": Customer[],
 "summary": {"total": 8, "active": 7, "archived": 1, "protected": 2,
             "roles": {"ACC-101": "recently active", "...": "..."}},
 "ledger_entries": 0}
```
Running it twice gives identical output.

## Changes since the first uncommitted version (2026-10-22)
- 404s and other backend errors now return `{status: "error", reason}` instead of `{detail}`.
- Update 200 response gains a `verification` field (additive).
- New `status: "failed"` / `recovery_state: "verification_failed"` values; a failed verification never leaves data changed.
- New route `POST /api/demo/reset`.

## Changes in Priority-0 item 1 (2026-10-22)
- `last_activity_at` changed from a date (`2024-03-10`) to ISO-8601 UTC (`2024-03-10T12:00:00Z`).
- Demo data is now 8 customers (ACC-101..108) instead of 3.
- `/api/actions` items gain `actor`, `decision`, `reason`, `account_id`, `before_version`, `after_version`, `mode`, `scenario`, `undo_of_action_id` (additive).
- `POST /api/demo/reset` returns 403 unless `REWIND_DEMO_MODE=1`, and its body gains `demo_now`, `summary`, `ledger_entries`.
- Default database moved to `backend/data/rewind.db`.
