# REWIND backend API

Base URL when running locally: `http://127.0.0.1:8000`.

```powershell
$env:REWIND_DEMO_MODE = "1"; Set-Location backend; uvicorn main:app --reload
```

Scope: Undo is supported for **customer status changes only**. This is not a universal rollback.

## Configuration (env vars)

| Var | Default | Effect |
|---|---|---|
| `REWIND_DB_PATH` | `backend/data/rewind.db` | SQLite file |
| `REWIND_DEMO_MODE` | off | `1` enables `/api/demo/reset` and the fixed demo clock (2026-10-22T12:00:00Z) |
| `REWIND_RECENT_ACTIVITY_DAYS` | `90` | `recent_activity` rule: archiving a customer active within N days is blocked |
| `REWIND_BULK_APPROVAL_THRESHOLD` | `3` | After this many executed changes in one `run_id`, further changes need approval |
| `REWIND_FAULT_DISABLE_RECENT_ACTIVITY` | off | Injected fault. `1` disables **only** `recent_activity`, and only in demo mode. Every action taken while it is on is labelled `scenario: "injected_fault"` |

## Shapes

**Customer**
```json
{"account_id": "ACC-102", "name": "Meera Iyer", "status": "Active",
 "last_activity_at": "2025-03-10T12:00:00Z", "protected": false, "version": 1}
```
`status` is `Active` | `Archived`. `last_activity_at` is ISO-8601 UTC.

**Error** (every error the backend produces, including 422)
```json
{"error": {"code": "customer_not_found", "message": "Customer not found", "details": {...} | [...] | null}}
```
Codes: `invalid_arguments`, `invalid_request`, `unsupported_tool`, `customer_not_found`, `action_not_found`, `version_conflict`, `demo_mode_off`.

**Verification** (deterministic code reading SQLite; never the LLM)
```json
{"verdict": "verified" | "failed" | "conflict_detected" | "manual_review_required",
 "stage": "pre_commit_guard" | "post_commit",
 "scope": "plain sentence of exactly what was checked",
 "checks": [{"id", "description", "expected", "actual", "passed"}]}
```
`passed` is always `expected == actual`. A check is listed only when its evidence was compared.

- `pre_commit_guard` (update responses, and inside Undo): runs inside the write transaction before commit and decides whether to roll back. Checks: `one_row_updated`, `status_written`, `version_incremented_by_one`, `other_customers_unchanged_in_transaction`. It does **not** re-read committed data.
- `post_commit` (every Undo attempt): runs after commit on a new SQLite connection.
  - Restore committed → checks `restored_value`, `version_transition`, `ledger_consistency`, `db_integrity`, `unrelated_rows_unchanged` (hash of all other rows in customers table, stored just before the restore). `verified` only if all pass; `failed` if any fails; `manual_review_required` if the stored snapshot is missing/unreadable (that check is then omitted and the scope says so).
  - Conflict → `conflict_evidence`, `customer_not_written`, `ledger_consistency`, `db_integrity`; verdict `conflict_detected` (or `manual_review_required` if those don't hold).
  - Restore rolled back → `customer_not_written`, `ledger_consistency`, `db_integrity`; verdict `failed` (never `verified`).

**Show "verified" only when `verdict == "verified"`.** A restore can be committed (`status: "recovered"`) with verdict `failed`.

**ActionRecord** (one ledger row)
```json
{"id", "created_at", "actor", "tool_name", "arguments", "decision", "rule_id", "reason",
 "risk", "status", "account_id", "before", "after", "before_version", "after_version",
 "run_id", "mode", "scenario", "undo_of_action_id", "recovery_state", "verification",
 "evidence", "recovery_verification", "recovered_at"}
```
- `actor`: `api` (direct HTTP call) | `agent`
- `decision`: `allow` | `block` | `require_approval` | `reject`
- `rule_id`: `default_allow`, `protected_customer`, `unknown_status`, `no_change`, `recent_activity`, `bulk_change`, `account_not_found`
- `tool_name`: `update_customer_status` | `undo`
- `status`: `executed` | `blocked` | `pending_approval` | `rejected` | `failed` | `conflict_detected` (undo attempts)
- `recovery_state`: `available` | `unavailable` | `conflict_detected` | `recovered` | `restore_failed`
- `verification`: updates → the pre-commit guard; undo rows → the post-commit verdict
- `evidence`: undo rows only → pre-restore snapshot + guard, or conflict evidence
- `recovery_verification`: originals → post-commit verdict of the latest Undo attempt
- `undo_of_action_id`: on undo rows, the action being undone
- `risk`: `low` | `medium` | `high`
- `mode`: `null` for direct calls; later `live` | `replay` (replay is never a live model run)
- `scenario`: `null` or `injected_fault`
- `account_id` is `null` on `rejected` rows for accounts that don't exist (the requested id is in `arguments`)

## Routes

### `GET /api/customers` → `Customer[]` ordered by `account_id`

### `GET /api/actions` → `ActionRecord[]`, newest first

### `GET /api/actions/{action_id}` → `ActionRecord` | 404 Error

### `POST /api/tools/update-customer-status`
Request:
```json
{"account_id": "ACC-102", "new_status": "Archived", "reason": "why (required, 1-500 chars)",
 "expected_version": 1, "run_id": "optional"}
```
`expected_version` is optional; when sent and stale, nothing is written. Unknown keys are rejected.

| Code | Body | Ledger |
|---|---|---|
| 200 | `{action_id, status: "executed", decision, rule_id, reason, risk, scenario, recovery_state: "available", before, after, verification}` | `executed` |
| 202 | `{action_id, status: "pending_approval", decision: "require_approval", rule_id: "bulk_change", reason, risk, scenario, before, after}` (no change made; no approval UI yet) | `pending_approval` |
| 403 | `{action_id, status: "blocked", decision: "block", rule_id, reason, risk, scenario, before, after}` | `blocked` |
| 409 | same blocked body with `rule_id: "no_change"` | `blocked` |
| 409 | Error `version_conflict`, `details: {expected_version, current_version}` | nothing |
| 404 | Error `customer_not_found`, `details: {account_id, action_id}` | `rejected` |
| 422 | Error `invalid_arguments` / `invalid_request`, `details`: validation errors | nothing |
| 500 | `{..., status: "failed", recovery_state: "unavailable", verification}`; write rolled back | `failed` |

### `POST /api/actions/{action_id}/undo`
Only `executed` `update_customer_status` actions can be undone. Undo restores only when the
current customer row equals the recorded after-state **and** its version equals `after_version`.
Every attempt writes its own ledger row (`tool: "undo"`, `undo_of_action_id` = the original),
in the same transaction as any customer change.

| Code | Body |
|---|---|
| 200 | `{action_id, undo_action_id, status: "recovered", already_recovered: false, before, after, verification}` with `verification.verdict == "verified"` |
| 500 | Same body but verdict `failed` / `manual_review_required`: the restore **is committed** but could not be verified |
| 200/500 | Same body with `already_recovered: true` on a repeat call; returns the stored result, nothing changes |
| 409 | `{action_id, undo_action_id, status: "conflict_detected", reason, expected_after, expected_version, current_customer, differences: {field: {expected, current}}, verification}`; customer untouched, attempt logged |
| 409 | Error `not_undoable`, `details: {action_id, status, tool}` (blocked/rejected/pending actions, or an undo row) |
| 404 | Error `action_not_found` |
| 500 | `{action_id, undo_action_id, status: "failed", reason, current_customer, pre_commit_guard, verification}`; restore rolled back, retry allowed |

### `POST /api/demo/reset`
Needs `REWIND_DEMO_MODE=1`, otherwise 403 Error `demo_mode_off`.
Drops, recreates and reseeds both tables in one transaction (also upgrades an older local DB). The only route that wipes data.
```json
{"status": "reset", "demo_now": "2026-10-22T12:00:00Z", "customers": Customer[],
 "summary": {"total": 8, "active": 7, "archived": 1, "protected": 2,
             "roles": {"ACC-101": "recently active", "...": "..."}},
 "ledger_entries": 0}
```

## Change log

### Priority-0 item 4 (2026-10-22)
- **Breaking:** every `verification` object is now `{verdict, stage, scope, checks: [...]}` (was `{status, checks: {name: bool}, current_customer}`).
- **Breaking:** check ids renamed: `requested_status_applied`/`original_status_restored` → `status_written`; `version_incremented` → `version_incremented_by_one`; `unrelated_records_unchanged` → `other_customers_unchanged_in_transaction`; `database_integrity_ok` removed from the pre-commit guard (now `db_integrity` in the post-commit verifier).
- **Breaking:** a committed Undo whose post-commit verdict isn't `verified` returns 500 (body still `status: "recovered"`).
- `recovery_state` `verification_failed` renamed `restore_failed`.
- ActionRecord gains `evidence` and `recovery_verification`.
- Local DBs from before this change need one `POST /api/demo/reset`.

### Priority-0 item 3 (2026-10-22)
- **Breaking:** successful Undo returns `status: "recovered"` (was `"verified"`); checks moved under `verification.checks`.
- **Breaking:** repeat Undo returns 200 with `already_recovered: true` (was 409 `refused`).
- **Breaking:** undoing a non-undoable action returns Error `not_undoable` (was `{status: "refused"}`).
- Conflict 409 gains `undo_action_id`, `expected_version`, `differences`.
- Every Undo attempt appears in `/api/actions` as its own row with `tool_name: "undo"`.
- Original action `recovery_state` is `recovered` (was `verified`).
- Local DBs from before this change need one `POST /api/demo/reset`.

### Priority-0 item 2 (2026-10-22)
- **Breaking:** error bodies are now `{error: {code, message, details}}` (was `{status: "error", reason}`), including 422s (was FastAPI's `{detail}`).
- **Breaking:** update requests require `reason`; unknown keys are rejected.
- Archiving a customer active within 90 days is now blocked (`rule_id: "recent_activity"`). In the demo fixture that is ACC-101 and ACC-104.
- New 202 `pending_approval` outcome for bulk runs; new ledger statuses `rejected` and `pending_approval`.
- Update responses gain `decision`, `rule_id`, `reason`, `scenario`. ActionRecord gains `rule_id`.
- New route `GET /api/actions/{action_id}`.
- A local DB from before this change must be reset once (`POST /api/demo/reset`) or deleted.

### Priority-0 item 1
- `last_activity_at` changed from a date to ISO-8601 UTC; demo data is 8 customers; reset needs demo mode; default DB at `backend/data/rewind.db`.

### First refactor
- Update 200 gained `verification`; `failed` / `verification_failed` added; `POST /api/demo/reset` added.
