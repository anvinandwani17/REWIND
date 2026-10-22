# REWIND demo runbook (Windows PowerShell)

Every command below runs from the **repo root** (the folder that contains `backend\` and `docs\`).
Blocks marked `powershell` were run top to bottom in a fresh PowerShell window before the demo.

## 1. One-time setup

```powershell
python -m venv backend\.venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass  # this window only, so Activate.ps1 may run
.\backend\.venv\Scripts\Activate.ps1
python -m pip install --quiet --disable-pip-version-check -r backend\requirements.txt
```

## 2. Environment (in every new window)

```powershell
$env:REWIND_DEMO_MODE = "1"                                # enables reset + the fixed demo clock
$env:REWIND_DB_PATH = "$PWD\backend\data\demo.db"          # demo database (git-ignored)
$env:FRONTEND_ORIGIN = "http://localhost:5173"             # Khanak's Vite dev server (CORS)
$env:REWIND_FAULT_DISABLE_RECENT_ACTIVITY = "1"            # INJECTED FAULT for the detection moment
```

The last line is the **injected fault** the demo needs: it disables the `recent_activity` rule so a
bad archive can get through. Every action taken while it is on carries `scenario: "injected_fault"`
in the ledger. Leave it unset for a production-correct run (the gateway then blocks the bad archive
before any write - scenario `recent_activity_block` in the lab).

Optional, for a live model run (never commit the key):

```text
$env:LLM_PROVIDER = "anthropic"
$env:LLM_API_KEY = "<your Anthropic API key>"
$env:LLM_MODEL = "claude-opus-5-5"     # optional, this is the default
```

## 3. Start the backend (window 1)

```powershell
python -m uvicorn --app-dir backend main:app --host 127.0.0.1 --port 8000
```

Stop it with `Ctrl+C`. Starting it never wipes data; only `POST /api/demo/reset` does.

## 4. Tests and scenario lab (window 2, after sections 1-2)

```powershell
python -m pytest -q
python -m backend.scenarios
```

Expect every test to pass and `18/18 scenarios behaved as expected`. Neither touches `demo.db`:
tests and the lab use throwaway databases.

## 5. The 3-minute backend demo (window 2)

Define the helper once. It is plain `Invoke-RestMethod` plus the demo role headers; it returns the
JSON body even for 4xx/5xx answers (Windows PowerShell 5.1 would otherwise throw). Each step's
output lines end in `Format-Table`/`Format-List` so they all print when a block is pasted at once.

```powershell
$Base = "http://127.0.0.1:8000"
function Rewind {
    param([string]$Method, [string]$Path, $Body = $null, [string]$Role = "operator")
    $params = @{ Method = $Method; Uri = "$Base$Path"; ContentType = "application/json";
                 Headers = @{ "X-Role" = $Role; "X-Role-Key" = "demo-$Role-key" } }
    if ($null -ne $Body) { $params.Body = $Body | ConvertTo-Json -Depth 10 }
    try { Invoke-RestMethod @params }
    catch { if ($_.ErrorDetails.Message) { $_.ErrorDetails.Message | ConvertFrom-Json } else { throw } }
}
```

**Step 1 - reset (5 s).** Eight synthetic FinCore customers, empty ledger.

```powershell
(Rewind POST /api/demo/reset).data.summary | Select-Object total, active, archived, protected
```

**Step 2 - an agent run (30 s).** Live if the LLM env vars are set, otherwise the recorded replay.
The response always says which one it was (`mode`).

```powershell
$run = Rewind POST /api/agent/runs @{ instruction = "Archive every customer who has been inactive for more than a year."; mode = "live" }
if ($run.error) {
    "Live run unavailable ($($run.error.code)) - falling back to the recorded replay"
    $run = Rewind POST /api/agent/runs @{ instruction = "Archive every customer who has been inactive for more than a year."; mode = "replay"; scenario = "archive_inactive" }
}
$run.data | Select-Object run_id, mode, status | Format-List
$run.data.steps | Format-Table index, tool, outcome, result_summary -AutoSize
$run.data.final_summary
```

Say: every change went through the gateway; the summary is built from the ledger, not from the model.

**Step 3 - the mistake (20 s).** An agent acts on last quarter's (stale) report. This is a recorded
replay so it happens the same way every time; with the injected fault on, the gateway lets it through.

```powershell
$m = Rewind POST /api/agent/runs @{ instruction = "Archive the customers listed as inactive in last quarter's inactivity report."; mode = "replay"; scenario = "stale_report" }
$actionId = $m.data.action_ids[0]
$m.data.steps | Format-Table index, tool, outcome, result_summary -AutoSize
```

**Step 4 - the FinCore row changed (10 s).**

```powershell
(Rewind GET /api/customers).data | Where-Object account_id -eq "ACC-101" | Format-Table account_id, name, status, version, last_activity_at
```

**Step 5 - the ledger entry (15 s).** Labelled `mode: replay` and `scenario: injected_fault`.

```powershell
$a = (Rewind GET "/api/actions/$actionId").data
$a | Select-Object id, status, actor, mode, scenario, decision, rule_id, reason | Format-List
```

**Step 6 - REWIND flags it (20 s).** The post-action eligibility check re-reads SQLite after commit,
ignoring the injected fault, and flags the archive.

```powershell
"flag: $($a.flag)"
$a.post_action_check.checks | Format-Table id, passed, description -Wrap
```

**Step 7 - Undo (20 s).**

```powershell
$u = Rewind POST "/api/actions/$actionId/undo"
"$($u.data.status) / verdict: $($u.data.verification.verdict)"
$u.data.verification.checks | Format-Table id, passed -AutoSize
```

**Step 8 - verified evidence (20 s).** Instruction, policy decision, before/after, recovery attempt
and its checks, and the final row - all read back from SQLite.

```powershell
$e = (Rewind GET "/api/runs/$($m.data.run_id)/evidence").data
$e | Select-Object run_id, mode, instruction | Format-List
$e.actions[0].recovery.attempts[0] | Select-Object undo_action_id, status, @{ n = "verdict"; e = { $_.verification.verdict } } | Format-List
$e.final_state."customers/ACC-101" | Select-Object account_id, status, version | Format-List
```

**Step 9 - second scenario: someone edits after the agent (30 s).** Archive ACC-108, an ops user
reactivates it, then Undo of the archive is refused because it would overwrite the newer edit.

```powershell
$x = Rewind POST /api/tools/update-customer-status @{ account_id = "ACC-108"; new_status = "Archived"; reason = "Inactive 150 days" }
$ops = Rewind POST /api/tools/update-customer-status @{ account_id = "ACC-108"; new_status = "Active"; reason = "Customer phoned; reactivated by ops" }
$c = Rewind POST "/api/actions/$($x.data.action_id)/undo"
"Undo: $($c.error.code) - $($c.error.message)"
$c.data.differences | ConvertTo-Json -Compress
(Rewind GET /api/customers).data | Where-Object account_id -eq "ACC-108" | Format-Table account_id, status, version
```

The ops edit here comes in over HTTP, so its ledger `actor` is `operator`; the offline backup below
records it as `ops_user`.

## 6. If Wi-Fi or the model API fails on stage

```powershell
python -m backend.scenarios --demo
```

Plays the same story (steps 1 and 3-9) in **replay mode** on a throwaway database, no network,
and prints every step with `OK` or `UNEXPECTED`. Exit code 0 only if every step checked out.

## 7. After the demo

```powershell
Remove-Item -Force -ErrorAction SilentlyContinue backend\data\demo.db*
```

## 8. Judge Q&A (short, honest answers)

**What can't REWIND undo?** Anything without an adapter. Today that is everything except customer
status and credit limits. No emails sent, no payments, no external API calls, no schema changes, no
other columns. It also refuses Undo when the row changed afterwards, and it doesn't undo an Undo.

**Why isn't the verifier the LLM?** Verification has to be repeatable and impossible to talk into
agreeing. The verifier is plain code that re-reads SQLite on a new connection after commit. Every check
lists `expected` and `actual`, and `passed` is just `expected == actual`. It never sees agent output.
It checks consistency with the ledger's own records. Someone who can write the database directly can
still forge a different but valid credit limit together with its digest; the check descriptions say so.

**What happens with concurrent edits?** Every write takes `BEGIN IMMEDIATE`, re-reads the row and
writes only if its `version` is unchanged, so two writers cannot interleave. Undo restores only if the
current row equals the recorded after-state exactly, version included. Otherwise it returns
`conflict_detected` with a field-by-field diff and writes nothing to the customer.

**What is replay vs live?** Live means a real model (Anthropic API) chose the tool calls. Replay plays a
recorded, versioned script from `backend/replays/` through the same gateway, policies, ledger and
verifier. Replay is labelled `mode: "replay"` in the response and in every ledger row, and is never
presented as a model run. The scripts are hand-authored, and their `source` field says so.

**What would we do next?** An MCP adapter, so any MCP tool server's writes go through the gateway.
Compensating actions for things that can't be rolled back (refunds, follow-up emails). An approvals UI
for `pending_approval` actions, which today can only be approved over the API. A digest kept outside
the database (append-only log or signing), so forged snapshots become detectable.
