import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock


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
        "hermes_call_link",
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


def test_link_uses_host_conversation_context_not_model_arguments(monkeypatch):
    module = plugin()
    request = Mock(return_value='{"chat_ref":"chat-123"}')
    monkeypatch.setattr(module, "_request", request)
    module.link(
        {"extension": "1001", "action": "link"},
        conversation_id="chat-123",
        session_id="compressed-456",
    )
    request.assert_called_once_with("POST", "/bindings/1001", {"chat_ref": "chat-123"})
    request.reset_mock()
    assert "error" in json.loads(module.link({"extension": "1001", "action": "link"}))
    request.assert_not_called()


def test_dial_carries_origin_without_mutating_tool_arguments(monkeypatch):
    module = plugin()
    request = Mock(return_value='{"state":"preparing"}')
    monkeypatch.setattr(module, "_request", request)
    args = {"extension": "1001", "message": "必須の初期メッセージ", "request_id": "a" * 32}
    module.dial(args, conversation_id="chat-source")
    assert "chat_ref" not in args
    assert request.call_args.args[2]["chat_ref"] == "chat-source"
