from __future__ import annotations

from fastapi import APIRouter

from apx_agent import Dependencies

from models import SearchRequest, SearchResponse
from search import search

router = APIRouter(prefix="/api")


@router.post("/search", response_model=SearchResponse, operation_id="search")
def search_endpoint(req: SearchRequest, ws: Dependencies.Workspace):
    """Search utility accounts by name + address.

    Normalizes the applicant record, selects vector or SQL strategy based on name
    characteristics (initials / acronyms use SQL; standard names use Vector Search),
    fans out across all three VS indexes, and returns deduplicated candidates.
    """
    result = search(
        name=req.applicant_name,
        address=req.address,
        email=req.email,
        tenant_id=req.tenant_id,
        k=req.k,
        ws=ws,
    )
    return SearchResponse(**result)
