import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import db
import demo
import gateway
import ledger
import recovery
import tools
from schemas import ActionRecord, Envelope, envelope, error_body

FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://localhost:5173")


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
    allow_headers=["Content-Type"],
)


def respond(result):
    status_code, body = result
    return JSONResponse(status_code=status_code, content=body)


# --- every error uses the same envelope ------------------------------------------

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


# --- stable routes -------------------------------------------------------------------

@app.get("/api/customers")
def list_customers():
    return envelope(tools.list_customers())


@app.get("/api/actions", response_model=Envelope[list[ActionRecord]])
def list_actions():
    with db.connect() as conn:
        return envelope(recovery.with_undo_fields(conn, ledger.list_actions(conn)))


@app.get("/api/actions/{action_id}", response_model=Envelope[ActionRecord])
def get_action(action_id: str):
    with db.connect() as conn:
        action = ledger.get_action(conn, action_id)
        if action is None:
            return respond((404, error_body("action_not_found", "Action not found", {"action_id": action_id})))
        return envelope(recovery.with_undo_fields(conn, [action])[0])


@app.post("/api/tools/update-customer-status")
def update_customer_status(body: dict[str, Any] = Body(...)):
    # Validation happens once, inside the gateway.
    arguments = dict(body)
    run_id = arguments.pop("run_id", None)
    return respond(gateway.execute("update_customer_status", arguments, run_id=run_id))


@app.post("/api/actions/{action_id}/undo")
def undo_action(action_id: str):
    return respond(recovery.undo(action_id))


@app.post("/api/demo/reset")
def reset_demo():
    """The only route that wipes data. Requires REWIND_DEMO_MODE=1."""
    if not demo.demo_mode_enabled():
        return respond((403, error_body("demo_mode_off", "Demo mode is off (set REWIND_DEMO_MODE=1)")))
    return envelope(demo.reset())


# --- reserved for the next steps ---------------------------------------------------------

def not_implemented(route):
    return respond((501, error_body("not_implemented", f"{route} is reserved and not implemented yet")))


@app.post("/api/agent/runs")
def start_agent_run():
    return not_implemented("POST /api/agent/runs")


@app.get("/api/runs/{run_id}/evidence")
def run_evidence(run_id: str):
    return not_implemented("GET /api/runs/{run_id}/evidence")
