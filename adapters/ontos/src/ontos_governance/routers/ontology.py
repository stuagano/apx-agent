"""Ontology router — browse the resource class hierarchy and validate YAML."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ontos_governance.provider import GovernanceProvider
from ontos_governance.routers._deps import get_provider

router = APIRouter(prefix="/ontology", tags=["ontology"])


@router.get("/classes")
def list_classes(
    kind: str | None = None,
    provider: GovernanceProvider = Depends(get_provider),
):
    return provider.list_ontology_classes(kind=kind)


@router.get("/classes/{class_name}")
def get_class(
    class_name: str,
    provider: GovernanceProvider = Depends(get_provider),
):
    try:
        return provider.get_ontology_class(class_name)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"Class '{class_name}' not found")


@router.get("/tree")
def ontology_tree(provider: GovernanceProvider = Depends(get_provider)):
    return provider.ontology_tree()


@router.post("/validate")
def validate_ontology(provider: GovernanceProvider = Depends(get_provider)):
    try:
        return provider.validate_ontology()
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
