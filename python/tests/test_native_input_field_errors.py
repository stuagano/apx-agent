"""#1177: strict typed step contracts keep Pydantic field locations in their errors."""

from collections.abc import Sequence
from datetime import datetime

import click
import pytest
from pydantic import BaseModel, ConfigDict, model_validator

from apx_agent import LlmAgent, cli
from apx_agent._native_input import native_data_part


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: datetime


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    basis: Sequence[Evidence]

    @model_validator(mode="wrap")
    @classmethod
    def passthrough(cls, value, handler):
        # Even a pass-through wrap moves nested validation onto the strict Python path (#1177).
        return handler(value)


class Packet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate: Candidate
    as_of: datetime


PAYLOAD = {"candidate": {"basis": [{"as_of": "2026-02-14T00:00:00Z"}]}, "as_of": "2026-02-14T00:00:00Z"}


def test_wrap_validator_contract_passes_lax_but_strict_names_the_field():
    Packet.model_validate(PAYLOAD)
    with pytest.raises(ValueError) as exc:
        native_data_part(Packet, PAYLOAD)
    assert "violates its declared schema" in str(exc.value)
    assert "candidate.basis.0.as_of [datetime_type]" in str(exc.value)
    assert "2026-02-14" not in str(exc.value)


def test_smoke_input_reports_the_field_location(tmp_path):
    path = tmp_path / "smoke.json"
    path.write_text('{"candidate": {"basis": [{"as_of": "2026-02-14T00:00:00Z"}]}, "as_of": "private-value"}')
    agent = LlmAgent(name="typed", input_schema=Packet, input_key="packet")
    with pytest.raises(click.ClickException) as exc:
        cli._prepare_native_smoke_input(agent, path, readyz_gate=True)
    message = str(exc.value)
    assert message.startswith("Invalid --smoke-input:")
    assert "candidate.basis.0.as_of [datetime_type]" in message
    assert "private-value" not in message
