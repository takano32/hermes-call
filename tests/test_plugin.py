import importlib.util
import json
from pathlib import Path


def plugin():
    spec = importlib.util.spec_from_file_location(
        "hermes_call_plugin", Path(__file__).parents[1] / "__init__.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_registration_is_offline_and_handlers_return_json(monkeypatch):
    module = plugin()
    registrations = []

    class Context:
        def register_tool(self, **kwargs):
            registrations.append(kwargs)

    module.register(Context())
    assert {entry["name"] for entry in registrations} == {
        "hermes_call_dial",
        "hermes_call_status",
    }
    for entry in registrations:
        assert entry["schema"]["name"] == entry["name"]
    assert "error" in json.loads(module.dial({}))
    assert "error" in json.loads(module.status({"extra": 1}))

    def failed(*args):
        raise OSError("private details")

    monkeypatch.setattr(module, "_request", failed)
    result = module.status({})
    assert "error" in json.loads(result)
    assert "private details" not in result
