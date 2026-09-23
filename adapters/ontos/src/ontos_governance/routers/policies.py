"""Policies router — CRUD for governance policies."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from ontos_governance.models import PolicyBase, PolicyCreate, PolicyFilters
from ontos_governance.provider import GovernanceProvider
from ontos_governance.routers._deps import get_current_user, get_provider

router = APIRouter(prefix="/policies", tags=["policies"])


def _validate_rule_json(rule_json: str) -> None:
    try:
        json.loads(rule_json)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"rule_json is not valid JSON: {e}")


@router.get("")
def list_policies(
    origin: str | None = None,
    active: bool | None = None,
    provider: GovernanceProvider = Depends(get_provider),
):
    return provider.list_policies(PolicyFilters(origin=origin, active=active))


@router.get("/ontology-classes")
def list_ontology_classes(provider: GovernanceProvider = Depends(get_provider)):
    return provider.list_applies_to_classes()


@router.get("/{policy_id}")
def get_policy(
    policy_id: str,
    provider: GovernanceProvider = Depends(get_provider),
):
    try:
        return provider.get_policy(policy_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"Policy {policy_id} not found")


@router.post("", status_code=201)
def create_policy(
    body: PolicyCreate,
    user: str = Depends(get_current_user),
    provider: GovernanceProvider = Depends(get_provider),
):
    _validate_rule_json(body.rule_json)
    try:
        return provider.create_policy(body, policy_id=body.policy_id, author=user)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.patch("/{policy_id}")
def update_policy(
    policy_id: str,
    body: PolicyBase,
    user: str = Depends(get_current_user),
    provider: GovernanceProvider = Depends(get_provider),
):
    _validate_rule_json(body.rule_json)
    try:
        return provider.update_policy(policy_id, body, author=user)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"Policy {policy_id} not found")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get("/{policy_id}/history")
def policy_history(
    policy_id: str,
    provider: GovernanceProvider = Depends(get_provider),
):
    return provider.policy_history(policy_id)
