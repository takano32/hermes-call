from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_call.sip import CallSession, SipClient, caller_extension, peer_address


@pytest.mark.parametrize(
    "uri,expected",
    [
        ('"Name" <sip:1001@pbx.example.com>', "1001"),
        ("sip:1001@pbx.example.com:5060;transport=udp", "1001"),
        ("sips:1001@PBX.EXAMPLE.COM", "1001"),
        ("sip:1001@evil.example.com", None),
        ("sip:1001%40evil@pbx.example.com", None),
        ("sip:1001@pbx.example.com.evil", None),
    ],
)
def test_caller_uri_domain_validation(uri, expected):
    assert caller_extension(uri, "pbx.example.com") == expected


def test_source_address_validation():
    assert peer_address("192.0.2.10:5060") == "192.0.2.10"
    assert peer_address("pbx.example.com:5060") is None
    assert peer_address("192.0.2.10:invalid") is None


def test_media_failure_still_sends_native_hangup(config):
    sip = SipClient(config, Mock())
    sip.pj = SimpleNamespace(CallOpParam=Mock())
    session = CallSession("a" * 32, "1001", "hello", True)
    session.native = Mock()
    session.disconnected.set()  # Media failed, but no native DISCONNECTED callback yet.
    sip.hangup(session)
    session.native.hangup.assert_called_once()
    session.native_disconnected = True
    sip.hangup(session)
    session.native.hangup.assert_called_once()
