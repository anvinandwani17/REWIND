from enum import Enum
from typing import Any, Generic, Literal, TypeVar

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator,
)

T = TypeVar("T")


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


class UpdateCreditLimitArgs(ToolArgs):
    account_id: StrictStr = Field(min_length=1, max_length=64)
    new_limit: StrictInt = Field(ge=0, le=1_000_000_000)  # whole INR; policy narrows the range
    reason: StrictStr = Field(min_length=1, max_length=500)
    expected_version: StrictInt | None = Field(default=None, ge=1)


class AgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: StrictStr = Field(min_length=1, max_length=2000)
    mode: Literal["live", "replay"]
    # Replay only: which backend/replays/<scenario>.json script to play.
    scenario: StrictStr | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")

    @model_validator(mode="after")
    def _scenario_is_replay_only(self):
        if self.mode == "live" and self.scenario is not None:
            raise ValueError("scenario selects a recorded replay script; it is only valid with mode 'replay'")
        return self


class ApprovalRequest(ToolArgs):
    note: StrictStr | None = Field(default=None, max_length=500)


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Any = None


class Envelope(BaseModel, Generic[T]):
    """Every response: {data, error}. error is null exactly when the status is 2xx."""
    data: T | None = None
    error: ErrorDetail | None = None


def envelope(data=None, error=None):
    return {"data": data, "error": error}


def error_obj(code, message, details=None):
    return ErrorDetail(code=code, message=message, details=details).model_dump()


def error_body(code, message, details=None):
    """Envelope for a pure error (no data)."""
    return envelope(error=error_obj(code, message, details))


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
    evidence: dict[str, Any] | None
    recovery_verification: dict[str, Any] | None
    approval: dict[str, Any] | None
    post_action_check: dict[str, Any] | None
    flag: str | None                 # "flagged_unsafe" when the post-action check failed
    snapshot_sha256: str | None
    recovered_at: str | None
    # Derived at read time from the same rules recovery.undo() uses.
    undo_available: bool
    undo_disabled_reason: str | None
