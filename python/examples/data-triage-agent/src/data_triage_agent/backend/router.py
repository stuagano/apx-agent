from databricks.sdk.service.iam import User as UserOut
from fastapi import APIRouter

from .core import Dependencies

from .models import VersionOut

router = APIRouter(prefix="/api")


@router.get("/version", response_model=VersionOut, operation_id="version")
async def version():
    return VersionOut.from_metadata()


@router.get("/current-user", response_model=UserOut, operation_id="currentUser")
def me(user_ws: Dependencies.UserClient):
    return user_ws.current_user.me()
