import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, Depends, FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import db
import demo
import gateway
import ledger
import privacy
import recovery
import roles
from schemas import ActionRecord, ApprovalRequest, Envelope, envelope, error_body

FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://localhost:5173")

operator = Depends(roles.require("operator"))
approver = Depends(roles.require("approver"))
auditor = Depends(roles.require("auditor"))


@asynccontextmanager
async def lifespan(app):
    # Creates tables and fills an empty database only; never wipes.
    db.init_schema()
    demo.seed_if_empty()
    yield


app = FastAPI(title="REWIND API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Role", "X-Role-Key"],
)


def respond(result):
    status_code, body = result
    return JSONResponse(status_code=status_code, content=body)


def action_record(action_id):
    with db.connect() as conn:
        action = ledger.get_action(conn, action_id)
        if action is None:
            return respond((404, error_body("action_not_found", "Action not found", {"action_id": action_id})))
        return envelope(recovery.with_undo_fields(conn, [action])[0])


# --- every error uses the same envelope ------------------------------------------

@app.exception_handler(roles.RoleError)
async def role_error(request, exc):
    return respond((exc.status, error_body(exc.code, exc.message, exc.details)))


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return respond((422, error_body(
        "invalid_request", "Request failed validation", jsonable_encoder(exc.errors()),
    )))


@app.exception_handler(StarletteHTTPException)
async def http_error(request, exc):
    codes = {404: "route_not_found", 405: "method_not_allowed"}
    return respond((exc.status_code, error_body(codes.get(exc.status_code, "http_error"), str(exc.detail))))


@app.exception_handler(Exception)
async def internal_error(request, exc):
    return respond((500, error_body("internal_error", "Unexpected server error")))


# --- reads: open to the dashboard (no role) -----------------------------------------

@app.get("/api/customers")
def list_customers():
    # Unmasked on purpose: this is the FinCore UI. The agent reads via tools.py (masked).
    with db.connect() as conn:
        return envelope(db.list_customers(conn))


@app.get("/api/credit-lines")
def list_credit_lines():
    with db.connect() as conn:
        return envelope(db.list_credit_lines(conn))


@app.get("/api/actions", response_model=Envelope[list[ActionRecord]])
def list_actions():
    with db.connect() as conn:
        return envelope(recovery.with_undo_fields(conn, ledger.list_actions(conn)))


@app.get("/api/actions/{action_id}", response_model=Envelope[ActionRecord])
def get_action(action_id: str):
    return action_record(action_id)


# --- operator ------------------------------------------------------------------------

def run_tool(tool_name, body, role):
    # Validation happens once, inside the gateway.
    arguments = dict(body)
    run_id = arguments.pop("run_id", None)
    return respond(gateway.execute(tool_name, arguments, run_id=run_id, actor=role))


@app.post("/api/tools/update-customer-status")
def update_customer_status(body: dict[str, Any] = Body(...), role: str = operator):
    return run_tool("update_customer_status", body, role)


@app.post("/api/tools/update-credit-limit")
def update_credit_limit(body: dict[str, Any] = Body(...), role: str = operator):
    return run_tool("update_credit_limit", body, role)


@app.post("/api/actions/{action_id}/undo")
def undo_action(action_id: str, role: str = operator):
    return respond(recovery.undo(action_id, actor=role))


@app.post("/api/demo/reset")
def reset_demo(role: str = operator):
    """The only route that wipes data. Requires REWIND_DEMO_MODE=1."""
    if not demo.demo_mode_enabled():
        return respond((403, error_body("demo_mode_off", "Demo mode is off (set REWIND_DEMO_MODE=1)")))
    return envelope(demo.reset())


# --- approver ----------------------------------------------------------------------

@app.post("/api/actions/{action_id}/approve", response_model=Envelope[ActionRecord])
def approve_action(action_id: str, body: ApprovalRequest | None = Body(None), role: str = approver):
    with db.connect() as conn:
        pending = ledger.get_action(conn, action_id)
    if pending is None:
        return respond((404, error_body("action_not_found", "Action not found", {"action_id": action_id})))
    # The gateway re-checks inside its transaction that this is still pending.
    status_code, result = gateway.execute(
        pending["tool_name"], pending["arguments"], run_id=pending["run_id"],
        actor=role, approves=action_id, approval_note=body.note if body else None,
    )
    if status_code != 200:
        return respond((status_code, result))
    return action_record(action_id)


@app.post("/api/actions/{action_id}/reject", response_model=Envelope[ActionRecord])
def reject_action(action_id: str, body: ApprovalRequest | None = Body(None), role: str = approver):
    status_code, result = gateway.reject(action_id, role, body.note if body else None)
    if status_code != 200:
        return respond((status_code, result))
    return action_record(action_id)


# --- auditor ---------------------------------------------------------------------------

@app.get("/api/audit/export")
def audit_export(role: str = auditor):
    with db.connect() as conn:
        actions = recovery.with_undo_fields(conn, ledger.list_actions(conn))
        integrity = "; ".join(row[0] for row in conn.execute("PRAGMA integrity_check"))
    return envelope({
        "exported_at": ledger.now(),
        "exported_by_role": role,
        "pii_masked": True,
        "integrity_check": integrity,
        "action_count": len(actions),
        "actions": privacy.mask_pii(actions),
    })


# --- reserved for the next steps ---------------------------------------------------------

def not_implemented(route):
    return respond((501, error_body("not_implemented", f"{route} is reserved and not implemented yet")))


@app.post("/api/agent/runs")
def start_agent_run(role: str = operator):
    return not_implemented("POST /api/agent/runs")


@app.get("/api/runs/{run_id}/evidence")
def run_evidence(run_id: str):
    return not_implemented("GET /api/runs/{run_id}/evidence")
