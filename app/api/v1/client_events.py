"""Browser error and fallback reports.

Without this, a crash in a student's browser or a speech model that fails
on their device is invisible unless they write in. The browser posts a
short report here; it goes to the server log (Render keeps it about a
week on the free plan) as one JSON line, ready to search. Nothing is
stored in the database and no paid service is involved.

Signed-out pages crash too, so no token is needed. It's rate limited per
IP, every field is length-capped, and query strings are dropped from the
page address so tokens or emails in a URL never reach the log.
"""

import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, field_validator

from ...services.rate_limit import RateLimiter, client_ip

router = APIRouter()
logger = logging.getLogger("prepband.client")

client_event_limiter = RateLimiter(20, 60, "Too many reports. Please wait a minute.")


class ClientEvent(BaseModel):
    kind: Literal["error", "unhandled_rejection", "render_error", "live_fallback"]
    message: str = Field(max_length=500)
    stack: str | None = Field(default=None, max_length=4000)
    page: str | None = Field(default=None, max_length=300)
    release: str | None = Field(default=None, max_length=40)
    # For live_fallback: which job fell back to which tier and why.
    details: dict[str, str | int | float | bool | None] | None = Field(default=None, max_length=12)

    @field_validator("page")
    @classmethod
    def _path_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.split("?", 1)[0].split("#", 1)[0]

    @field_validator("details")
    @classmethod
    def _short_values(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return {str(k)[:40]: (v[:200] if isinstance(v, str) else v) for k, v in value.items()}


@router.post("/client-events", status_code=204)
def report_client_event(event: ClientEvent, request: Request) -> Response:
    client_event_limiter.check(client_ip(request))
    level = logging.INFO if event.kind == "live_fallback" else logging.WARNING
    logger.log(level, "client_event %s", json.dumps(event.model_dump(exclude_none=True), ensure_ascii=False))
    return Response(status_code=204)
