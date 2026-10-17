"""The systems snapshot lives in observability; ops keeps a re-export."""

from recertia.observability.systems import component_class
from recertia.ops.systems import component_class as shim_component_class


def test_component_class_import_path() -> None:
    assert component_class is shim_component_class
    assert component_class("retrieve") == "retrieve"
