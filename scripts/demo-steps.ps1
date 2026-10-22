$Base = "http://127.0.0.1:8000"
function Rewind {
    param([string]$Method, [string]$Path, $Body = $null, [string]$Role = "operator")
    $params = @{ Method = $Method; Uri = "$Base$Path"; ContentType = "application/json";
                 Headers = @{ "X-Role" = $Role; "X-Role-Key" = "demo-$Role-key" } }
    if ($null -ne $Body) { $params.Body = $Body | ConvertTo-Json -Depth 10 }
    try { Invoke-RestMethod @params }
    catch { if ($_.ErrorDetails.Message) { $_.ErrorDetails.Message | ConvertFrom-Json } else { throw } }
}
function Step($number, $title, $story) {
    Write-Host ""
    Write-Host "==================== STEP $number of 9: $title ====================" -ForegroundColor Yellow
    Write-Host $story -ForegroundColor Cyan
    Read-Host "Press Enter to do it"
}
function Good($text) { Write-Host "  -> $text" -ForegroundColor Green }
function Bad($text) { Write-Host "  -> $text" -ForegroundColor Red }

try { Invoke-RestMethod "$Base/api/customers" | Out-Null }
catch { Bad "I can't reach the shop. Is the 'backend server' tab still running and showing 'Application startup complete'?"; exit 1 }

Step 1 "Clean start" "We tidy the pretend shop FinCore: 8 pretend customers, and an empty diary (the ledger).
The ledger is REWIND's diary: it writes down every change, who asked, and what it looked like before and after."
$reset = (Rewind POST /api/demo/reset).data
Good "$($reset.summary.total) customers, $($reset.summary.active) active, $($reset.ledger_entries) diary entries. Fresh start!"

Step 2 "A robot helper does a normal job" "A robot helper (the AI agent) is asked: 'archive everyone who has not been around for over a year'.
The robot can only LOOK at data and ASK REWIND to change things - it can never change anything itself.
No AI key is set on this laptop, so we play back a recording of the robot (called 'replay'). REWIND always says which one it was."
$run = Rewind POST /api/agent/runs @{ instruction = "Archive every customer who has been inactive for more than a year."; mode = "live" }
if ($run.error) {
    Write-Host "  (live robot not available: $($run.error.code) - using the recording instead)"
    $run = Rewind POST /api/agent/runs @{ instruction = "Archive every customer who has been inactive for more than a year."; mode = "replay"; scenario = "archive_inactive" }
}
$run.data.steps | Format-Table index, tool, outcome, result_summary -AutoSize | Out-Host
Good "mode: $($run.data.mode). The diary says: $($run.data.final_summary)"

Step 3 "The robot makes a mistake" "Now the robot reads an OLD, out-of-date list and archives Aarav (ACC-101), who shopped only 7 days ago. That is wrong!
Normally REWIND's rule would stop this. We switched that rule off on purpose (the 'injected fault') to show what REWIND does next."
$m = Rewind POST /api/agent/runs @{ instruction = "Archive the customers listed as inactive in last quarter's inactivity report."; mode = "replay"; scenario = "stale_report" }
$actionId = $m.data.action_ids[0]
$m.data.steps | Format-Table index, tool, outcome, result_summary -AutoSize | Out-Host

Step 4 "Look at the shop" "Let's look at Aarav's row in the shop."
(Rewind GET /api/customers).data | Where-Object account_id -eq "ACC-101" | Format-Table account_id, name, status, version, last_activity_at | Out-Host
Bad "Aarav is now Archived. The mistake really happened."

Step 5 "Read the diary" "REWIND wrote it all in the diary, and is honest that this was a recording with a broken rule."
$a = (Rewind GET "/api/actions/$actionId").data
$a | Select-Object status, actor, mode, scenario, rule_id, reason | Format-List | Out-Host

Step 6 "REWIND notices" "Right after the change, a separate checker (plain code, not the AI) looks at the shop again and asks:
'Was this customer really allowed to be archived?'"
$a.post_action_check.checks | Format-Table @{ n = "check"; e = { $_.id } }, @{ n = "ok?"; e = { $_.passed } }, description -Wrap | Out-Host
if ($a.flag -eq "flagged_unsafe") { Bad "FLAGGED UNSAFE: Aarav was active 7 days ago, so archiving him was wrong." }

Step 7 "Press Undo" "We press Undo. REWIND puts Aarav back, then the checker re-reads everything to prove it really worked."
$u = Rewind POST "/api/actions/$actionId/undo"
$u.data.verification.checks | Format-Table @{ n = "check"; e = { $_.id } }, @{ n = "ok?"; e = { $_.passed } } -AutoSize | Out-Host
if ($u.data.verification.verdict -eq "verified") { Good "VERIFIED: every check passed. Aarav is back." } else { Bad "Not verified: $($u.data.verification.verdict)" }

Step 8 "The receipt" "REWIND keeps a receipt for the whole robot run: what it was asked, what it did, how it was undone, and how things look now."
$e = (Rewind GET "/api/runs/$($m.data.run_id)/evidence").data
Good "instruction: $($e.instruction)"
Good "undo check result: $($e.actions[0].recovery.attempts[0].verification.verdict)"
$now = $e.final_state."customers/ACC-101"
Good "Aarav now: $($now.status) (version $($now.version))"

Step 9 "Someone else changed it first" "Last story. A worker archives Sara (ACC-108). Then a person in the office sets her back to Active, because she phoned in.
If we pressed Undo on the first change, it would wipe out the person's newer change. REWIND must refuse."
$x = Rewind POST /api/tools/update-customer-status @{ account_id = "ACC-108"; new_status = "Archived"; reason = "Inactive 150 days" }
Rewind POST /api/tools/update-customer-status @{ account_id = "ACC-108"; new_status = "Active"; reason = "Customer phoned; reactivated by ops" } | Out-Null
$c = Rewind POST "/api/actions/$($x.data.action_id)/undo"
Good "Undo answer: $($c.error.code) - $($c.error.message)"
(Rewind GET /api/customers).data | Where-Object account_id -eq "ACC-108" | Format-Table account_id, status, version | Out-Host
Good "Sara is still Active. The person's newer change was kept safe."

Write-Host ""
Write-Host "The end! That is the whole REWIND story: the robot acts, REWIND writes it down, notices the mistake, undoes it, proves the undo, and refuses an unsafe undo." -ForegroundColor Yellow
