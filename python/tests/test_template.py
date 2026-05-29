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


def test_incomplete_class_does_not_conform_to_protocol():
    class _NoBuild:
        name = "x"; title = "x"; description = "x"; Spec = BaseModel
    assert not isinstance(_NoBuild(), Template)


import pytest
from apx_agent._template import TemplateRegistry, template


def _fresh_registry():
    return TemplateRegistry()


def test_register_get_build_with_dict_and_instance():
    reg = _fresh_registry()
    reg.register(_DummyTemplate)
    assert reg.get("dummy").name == "dummy"
    out_kind, spec = reg.build("dummy", {"x": 7})
    assert out_kind == "agent" and spec.x == 7
    out_kind, spec2 = reg.build("dummy", _DummySpec(x=9))
    assert spec2.x == 9


def test_list_returns_template_info():
    reg = _fresh_registry()
    reg.register(_DummyTemplate)
    infos = reg.list()
    assert [i.name for i in infos] == ["dummy"]
    assert infos[0].spec_schema["properties"]["x"]["default"] == 1


def test_unknown_name_raises_listing_available():
    reg = _fresh_registry()
    reg.register(_DummyTemplate)
    with pytest.raises(ValueError, match="dummy"):
        reg.get("nope")


def test_duplicate_registration_raises():
    reg = _fresh_registry()
    reg.register(_DummyTemplate)
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_DummyTemplate)


def test_decorator_registers_on_module_registry():
    from apx_agent._template import template_registry

    @template
    class _DecoratedTemplate:
        name = "decorated_test"
        title = "Decorated"
        description = "via decorator"
        Spec = _DummySpec

        def build(self, spec, *, ws=None):
            return spec

    try:
        assert template_registry.get("decorated_test").name == "decorated_test"
    finally:
        template_registry._templates.pop("decorated_test", None)
