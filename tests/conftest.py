import sys

import pytest

from hermes_call.config import Config


@pytest.fixture
def config(tmp_path):
    return Config(
        api_token="a" * 64,
        sip_password="sip-test-password-unique",
        sip_server="pbx.example.com",
        sip_domain="pbx.example.com",
        sip_username="7000",
        sip_peer_ips=("192.0.2.10",),
        stt_api_key="groq-test-key",
        allowed_extensions=("1001", "1002"),
        allowed_callers=("1001",),
        hermes_command=(sys.executable,),
        data_dir=tmp_path,
    )
