import logging
import uuid
from pathlib import Path

from databricks.sdk.service.iam import User as UserOut
from fastapi import APIRouter, File, HTTPException, UploadFile

from apx_agent import Dependencies

from .config import get_settings
from .models import VersionOut
from .tools._sql import run_sql

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB


@router.get("/version", response_model=VersionOut, operation_id="version")
async def version():
    return VersionOut.from_metadata()


@router.get("/current-user", response_model=UserOut, operation_id="currentUser")
def me(user_ws: Dependencies.UserClient):
    return user_ws.current_user.me()


@router.post("/upload-contract", operation_id="uploadContract")
async def upload_contract(file: UploadFile = File(...)):
    """Accept a PDF and write it to the uploads UC Volume.

    Returns the volume path the agent will pass to extract_new_contract.
    """
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="only application/pdf is accepted")
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"file exceeds {MAX_UPLOAD_BYTES} bytes")

    settings = get_settings()
    upload_dir = Path(settings.volumes.uploads)
    upload_dir.mkdir(parents=True, exist_ok=True)
    fid = uuid.uuid4().hex[:12]
    out_path = upload_dir / f"{fid}.pdf"
    out_path.write_bytes(contents)
    logger.info("uploaded %s (%d bytes) -> %s", file.filename, len(contents), out_path)
    return {"volume_path": str(out_path), "size_bytes": len(contents)}


@router.get("/demo-questions", operation_id="demoQuestions")
def demo_questions():
    """Return the list of suggested demo questions for the chat UI."""
    return {"questions": get_settings().demo_questions}


@router.get("/contracts", operation_id="listContracts")
def list_contracts(ws: Dependencies.Client):
    s = get_settings()
    rows = run_sql(
        ws,
        f"SELECT contract_id, counterparty, contract_type, effective_date, "
        f"expiry_date, term_years, auto_renewal "
        f"FROM {s.qualified_table('primary')} ORDER BY expiry_date",
    )
    for row in rows:
        if row.get("auto_renewal") is not None:
            row["auto_renewal"] = row["auto_renewal"] in ("true", "True", "1", True)
        if row.get("term_years") is not None:
            try:
                row["term_years"] = int(row["term_years"])
            except (ValueError, TypeError):
                row["term_years"] = None
    return rows
