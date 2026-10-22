# REWIND — project rules for Claude Code

## What REWIND is
A flight recorder and safe-recovery layer for supported AI-agent tool actions. Demo parts:
1. FinCore — fictional customer-management app on local SQLite (synthetic data only).
2. AI Operations Agent — takes a natural-language instruction, reads data through bounded read tools, and routes every mutation through REWIND.
3. REWIND — gateway, deterministic policy engine, action ledger, before/after snapshots, conflict-aware Undo, independent verifier.

## Ownership (strict)
- Anvi (this workspace): Python backend only — db, schemas, tools, gateway, policies, ledger, recovery, verification, agent, backend API, backend tests, docs/backend-api.md.
- Khanak: React frontend (everything under `frontend/` or her branch). NEVER create, edit, move or reformat her files. If a change affects her, document it in docs/backend-api.md instead.

## Engineering rules
- Stack: Python 3.11+, FastAPI, Pydantic v2, sqlite3 (stdlib), pytest, httpx/TestClient. No Docker, queues, ORMs we don't already use, paid infra, or new frameworks without asking.
- Target layout: backend/{main.py, db.py, schemas.py, tools.py, gateway.py, policies.py, ledger.py, recovery.py, verification.py, agent.py, demo.py}, backend/tests/.
- Every supported mutation goes through ONE gateway function. The model never gets raw SQL, shell, or file access.
- A customer mutation and its ledger row commit in the same SQLite transaction. Use BEGIN IMMEDIATE for write paths that check-then-write.
- Customer rows carry an integer `version` that increments on every change (including recovery).
- The verifier is deterministic code that re-reads SQLite. It never calls the LLM and never trusts agent output.
- Never claim universal rollback. Undo supports only what the adapters in backend/adapters.py implement: customer status and credit limits.
- Honesty rules: replay mode is always labelled `mode: "replay"`, never presented as a live model run. Injected faults are labelled `scenario: "injected_fault"`. Never report a check as passed unless code actually compared the evidence.
- Secrets, .env, *.db, .venv stay out of Git. Importing the app must never wipe data; only POST /api/demo/reset does.

## Workflow rules
- Work on branch feat/anvi-backend. Never push to or merge into main. Never force-push.
- Preserve uncommitted local work. The local working tree is the source of truth, not GitHub main.
- Before editing a file, read it. Prefer small, reviewable changes.
- After each change run `python -m pytest -q` from the repo root and report the real output. Never weaken an assertion to make a test pass; fix the code.
- End every task with: files changed, tests run + results, API changes Khanak must know, known limitations.
- The user is on Windows PowerShell; give commands in PowerShell syntax.
