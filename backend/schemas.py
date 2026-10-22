from enum import Enum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, StrictStr


class CustomerStatus(str, Enum):
    """The one list of allowed customer statuses (policy and DB CHECK use it)."""
    ACTIVE = "Active"
    ARCHIVED = "Archived"


class ToolArgs(BaseModel):
    # Unknown keys are rejected so a model cannot smuggle extra parameters.
    model_config = ConfigDict(extra="forbid")


class ListCustomersArgs(ToolArgs):
    pass


class GetCustomerArgs(ToolArgs):
    account_id: StrictStr = Field(min_length=1, max_length=64)


class InactivityReportArgs(ToolArgs):
    as_of: AwareDatetime | None = None  # defaults to the demo/real clock
    min_inactive_days: StrictInt = Field(ge=0)


class UpdateCustomerStatusArgs(ToolArgs):
    account_id: StrictStr = Field(min_length=1, max_length=64)
    # A plain string on purpose: unknown statuses reach the policy and are
    # blocked and logged (rule unknown_status) instead of vanishing as a 422.
    new_status: StrictStr = Field(min_length=1, max_length=32)
    reason: StrictStr = Field(min_length=1, max_length=500)
    expected_version: StrictInt | None = Field(default=None, ge=1)


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Any = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


def error_body(code, message, details=None):
    return ErrorResponse(
        error=ErrorDetail(code=code, message=message, details=details)
    ).model_dump()


class ActionRecord(BaseModel):
    """One ledger row as the API returns it."""
    id: str
    created_at: str
    actor: str
    tool_name: str
    arguments: dict[str, Any]
    decision: str
    rule_id: str | None
    reason: str | None
    risk: str
    status: str
    account_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    before_version: int | None
    after_version: int | None
    run_id: str | None
    mode: str | None
    scenario: str | None
    undo_of_action_id: str | None
    recovery_state: str
    verification: dict[str, Any] | None
    recovered_at: str | None
