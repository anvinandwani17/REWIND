from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import db
import demo
import gateway
import ledger
import recovery
import tools
from schemas import ActionRecord, error_body


@asynccontextmanager
async def lifespan(app):
    # Creates tables and fills an empty database only; never wipes.
    db.init_schema()
    demo.seed_if_empty()
    yield


app = FastAPI(title="REWIND API", lifespan=lifespan)


def respond(result):
    status_code, body = result
    return JSONResponse(status_code=status_code, content=body)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return JSONResponse(
        status_code=422,
        content=error_body("invalid_request", "Request failed validation", jsonable_encoder(exc.errors())),
    )


@app.get("/api/customers")
def list_customers():
    return tools.list_customers()


@app.get("/api/actions", response_model=list[ActionRecord])
def list_actions():
    with db.connect() as conn:
        return ledger.list_actions(conn)


@app.get("/api/actions/{action_id}", response_model=ActionRecord)
def get_action(action_id: str):
    with db.connect() as conn:
        action = ledger.get_action(conn, action_id)
    if action is None:
        return respond((404, error_body("action_not_found", "Action not found", {"action_id": action_id})))
    return action


@app.post("/api/tools/update-customer-status")
def update_customer_status(body: dict[str, Any] = Body(...)):
    # Validation happens once, inside the gateway.
    arguments = dict(body)
    run_id = arguments.pop("run_id", None)
    return respond(gateway.execute("update_customer_status", arguments, run_id=run_id))


@app.post("/api/demo/reset")
def reset_demo():
    """The only route that wipes data. Requires REWIND_DEMO_MODE=1."""
    if not demo.demo_mode_enabled():
        return respond((403, error_body("demo_mode_off", "Demo mode is off (set REWIND_DEMO_MODE=1)")))
    return demo.reset()


@app.post("/api/actions/{action_id}/undo")
def undo_action(action_id: str):
    return respond(recovery.undo(action_id))
