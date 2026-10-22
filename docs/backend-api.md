# REWIND backend API

Base URL when running locally: `http://127.0.0.1:8000` (`cd backend; uvicorn main:app --reload`).

Scope: Undo is supported for **customer status changes only**. This is not a universal rollback.

## Shapes

**Customer**
```json
{"account_id": "ACC-102", "name": "Meera Iyer", "status": "Active",
 "last_activity_at": "2024-03-10", "protected": false, "version": 1}
```

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
  "status", "recovery_state", "verification", "created_at", "recovered_at"}]
```
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

### `POST /api/demo/reset` → `{"status": "reset", "customers": Customer[]}`
Clears the ledger and reseeds the three demo customers. The only route that wipes data.

## Changes since the first uncommitted version (2026-10-22)
- 404s and other backend errors now return `{status: "error", reason}` instead of `{detail}`.
- Update 200 response gains a `verification` field (additive).
- New `status: "failed"` / `recovery_state: "verification_failed"` values; a failed verification never leaves data changed.
- New route `POST /api/demo/reset`.
