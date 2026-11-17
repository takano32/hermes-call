from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_call.sip import (
    CallSession,
    MediaLink,
    SipClient,
    caller_extension,
    incoming_decision,
    media_snapshot,
    native_error,
    peer_address,
)


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


@pytest.mark.parametrize(
    "uri,source,busy,audio,code,reason",
    [
        ("sip:1001@pbx.example.com", "192.0.2.10:5060", False, True, 200, "accepted"),
        ("sip:1001@pbx.example.com", "192.0.2.11:5060", False, True, 403, "peer_not_allowed"),
        ("sip:1001@192.0.2.10", "192.0.2.10:5060", False, True, 403, "caller_domain_not_allowed"),
        ("sip:1002@pbx.example.com", "192.0.2.10:5060", False, True, 403, "extension_not_allowed"),
        ("sip:1001@pbx.example.com", "192.0.2.10:5060", True, True, 486, "line_busy"),
        (
            "sip:1001@pbx.example.com",
            "192.0.2.10:5060",
            False,
            False,
            503,
            "initial_audio_not_ready",
        ),
    ],
)
def test_incoming_rejections_identify_the_actual_reason(
    config, uri, source, busy, audio, code, reason
):
    result = incoming_decision(config, uri, source, closing=False, busy=busy, audio_ready=audio)
    assert (result.code, result.reason) == (code, reason)


def test_explicit_caller_domains_do_not_change_registration_domain(config):
    config = replace(config, allowed_caller_domains=("192.0.2.10",))
    result = incoming_decision(
        config,
        "sip:1001@192.0.2.10",
        "192.0.2.10:5060",
        closing=False,
        busy=False,
        audio_ready=True,
    )
    assert result.code == 200
    assert config.sip_domain == "pbx.example.com"
    assert (
        incoming_decision(
            config,
            "sip:1001@evil.example",
            "192.0.2.10:5060",
            closing=False,
            busy=False,
            audio_ready=True,
        ).code
        == 403
    )


def test_native_diagnostics_identify_attribute_without_private_error_body():
    error = AttributeError("private transcript", name="getAudioMedia")
    assert "getAudioMedia" in native_error(error)
    assert "private transcript" not in native_error(error)


def test_media_snapshot_preserves_swig_owner_until_values_are_copied():
    class Info:
        def __init__(self):
            self.media = [SimpleNamespace(index=0, type=1, status=1)]

        def __del__(self):
            # Model a borrowed vector becoming invalid when its C++ owner is destroyed.
            self.media.clear()

    assert media_snapshot(Info()) == ((0, 1, 1),)


class NativeMediaError(Exception):
    def __init__(self, status):
        self.status = status


def fake_media(slot):
    media = Mock()
    media.getPortId.return_value = slot
    return media


def test_repeated_media_notification_does_not_disconnect_or_reconnect():
    port, old, wrapper = fake_media(1), fake_media(5), fake_media(5)
    link = MediaLink(port, NativeMediaError)
    assert link.connect(old)
    assert not link.connect(wrapper)
    old.stopTransmit.assert_not_called()
    port.stopTransmit.assert_not_called()
    wrapper.startTransmit.assert_not_called()
    old.startTransmit.assert_called_once_with(port)


def test_removed_old_slot_70004_does_not_abort_outbound_audio():
    port, old, new = fake_media(1), fake_media(5), fake_media(6)
    link = MediaLink(port, NativeMediaError)
    link.connect(old)
    old.stopTransmit.side_effect = NativeMediaError(70004)
    # Even if an old wrapper's slot changes, the connection's cached ID stays stable.
    old.getPortId.return_value = -1
    assert link.connect(new)
    assert link.slot == 6
    port.stopTransmit.assert_called_once_with(old)
    new.startTransmit.assert_called_once_with(port)
    assert port.startTransmit.call_args.args == (new,)


def test_unexpected_native_disconnect_error_is_not_swallowed():
    port, old, new = fake_media(1), fake_media(5), fake_media(6)
    link = MediaLink(port, NativeMediaError)
    link.connect(old)
    old.stopTransmit.side_effect = NativeMediaError(70007)
    with pytest.raises(NativeMediaError):
        link.connect(new)
    new.startTransmit.assert_not_called()
