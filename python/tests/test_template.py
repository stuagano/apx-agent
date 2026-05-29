from pydantic import BaseModel
from apx_agent._template import Template, TemplateInfo


class _DummySpec(BaseModel):
    x: int = 1


class _DummyTemplate:
    name = "dummy"
    title = "Dummy"
    description = "A dummy template."
    Spec = _DummySpec

    def build(self, spec, *, ws=None):
        return ("agent", spec)


def test_template_info_carries_catalog_fields():
    info = TemplateInfo.from_template(_DummyTemplate())
    assert info.name == "dummy"
    assert info.title == "Dummy"
    assert info.description == "A dummy template."
    assert info.spec_schema["properties"]["x"]["default"] == 1


def test_dummy_conforms_to_protocol():
    assert isinstance(_DummyTemplate(), Template)
