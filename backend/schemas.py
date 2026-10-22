from typing import Literal

from pydantic import BaseModel


class UpdateCustomerStatusArgs(BaseModel):
    account_id: str
    new_status: Literal["Active", "Archived"]


class StatusUpdateRequest(UpdateCustomerStatusArgs):
    run_id: str | None = None
