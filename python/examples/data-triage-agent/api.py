"""Application-specific API routes mounted at /api/* on the dev-mode app."""
from __future__ import annotations

from databricks.sdk.service.iam import User as UserOut
from fastapi import APIRouter
from pydantic import BaseModel

from apx_agent import Dependencies

__version__ = "0.1.0"

router = APIRouter(prefix="/api")


class VersionOut(BaseModel):
    version: str


@router.get("/version", response_model=VersionOut, operation_id="version")
async def version():
    return VersionOut(version=__version__)


@router.get("/current-user", response_model=UserOut, operation_id="currentUser")
def me(user_ws: Dependencies.UserClient):
    return user_ws.current_user.me()
