from dataclasses import replace

import pytest


@pytest.mark.parametrize(
    "change",
    [
        {"sip_server": "pbx.example.com;bad=1"},
        {"stt_url": "http://api.groq.com/openai/v1"},
        {"tts_url": "http://example.com:50021"},
        {"api_token": "short"},
        {"sip_username": "pbx/123@evil"},
        {"allowed_callers": ()},
        {"allowed_extensions": ("123\n",)},
        {"sip_local_port": True},
        {"sip_peer_ips": ()},
        {"sip_peer_ips": ("not-an-IP",)},
        {"sip_srtp_required": True, "sip_transport": "udp"},
        {"rtp_port": 40001},
        {"stt_api_key": "REPLACE_WITH_GROQ_API_KEY"},
        {"hermes_command": ("hermes",)},
        {"incoming_message": " "},
    ],
)
def test_invalid_config(config, change):
    with pytest.raises(ValueError):
        replace(config, **change)


def test_secrets_not_in_repr(config):
    assert config.stt_api_key not in repr(config)
    assert config.api_token not in repr(config)
    assert config.sip_password not in repr(config)
