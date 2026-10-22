from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

import db
import demo
import gateway
import ledger
import recovery
import tools
from schemas import StatusUpdateRequest


@asynccontextmanager
async def lifespan(app):
    # Creates tables and fills an empty database only; never wipes.
    db.init_schema()
    demo.seed_if_empty()
    yield


app = FastAPI(title="REWIND API", lifespan=lifespan)


@app.get("/api/customers")
def list_customers():
    return tools.list_customers()


@app.get("/api/actions")
def list_actions():
    with db.connect() as conn:
        return ledger.list_actions(conn)


@app.post("/api/tools/update-customer-status")
def update_customer_status(request: StatusUpdateRequest):
    status_code, result = gateway.execute(
        "update_customer_status",
        request.model_dump(exclude={"run_id"}),
        request.run_id,
    )
    return JSONResponse(status_code=status_code, content=result)


@app.post("/api/demo/reset")
def reset_demo():
    """The only route that wipes data. Requires REWIND_DEMO_MODE=1."""
    if not demo.demo_mode_enabled():
        return JSONResponse(
            status_code=403,
            content={"status": "error", "reason": "Demo mode is off (set REWIND_DEMO_MODE=1)"},
        )
    return demo.reset()


@app.post("/api/actions/{action_id}/undo")
def undo_action(action_id: str):
    status_code, result = recovery.undo(action_id)
    return JSONResponse(status_code=status_code, content=result)
