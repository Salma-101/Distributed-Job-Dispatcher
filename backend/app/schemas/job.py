from datetime import datetime

from pydantic import BaseModel, Field, model_validator
from typing import Literal


class JobCreate(BaseModel):
    task_type: Literal["fibonacci", "sleep"]
    payload: dict
    priority: Literal["HIGH", "MEDIUM", "LOW"] = "MEDIUM"

    @model_validator(mode="after")
    def validate_payload(self):
        if self.task_type == "fibonacci":
            n = self.payload.get("n")

            if not isinstance(n, int):
                raise ValueError(
                    "Fibonacci payload must contain integer 'n'"
                )

            if n < 0:
                raise ValueError(
                    "Fibonacci 'n' must be non-negative"
                )

        elif self.task_type == "sleep":
            seconds = self.payload.get("seconds")

            if not isinstance(seconds, int):
                raise ValueError(
                    "Sleep payload must contain integer 'seconds'"
                )

            if seconds < 0:
                raise ValueError(
                    "Sleep 'seconds' must be non-negative"
                )

        return self


class JobResponse(BaseModel):
    id: int
    task_type: str
    payload: dict
    status: str
    priority: str

    result: dict | None = None
    error: str | None = None

    retry_count: int
    max_retries: int
    worker_id: str | None = None

    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    heartbeat_at: datetime | None = None

    model_config = {
        "from_attributes": True
    }