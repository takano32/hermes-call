"""Actual PJSIP REGISTER and INVITE smoke test, run by the native Linux CI job."""

import asyncio
import contextlib
import socket
from dataclasses import replace

import pytest

from hermes_call.sip import SipClient

pytest.importorskip("pjsua2")


class Registrar(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None
        self.invites = []

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        text = data.decode()
        method = text.split(" ", 1)[0]
        if method not in ("REGISTER", "INVITE"):
            return
        if method == "INVITE":
            self.invites.append(text.splitlines()[0])
        headers = {}
        for line in text.split("\r\n\r\n", 1)[0].split("\r\n")[1:]:
            key, _, value = line.partition(":")
            headers[key.lower()] = value.strip()
        status = "200 OK" if method == "REGISTER" else "486 Busy Here"
        response = [f"SIP/2.0 {status}"]
        for key in ("via", "from", "to", "call-id", "cseq"):
            value = headers[key]
            if key == "to" and ";tag=" not in value:
                value += ";tag=test-peer"
            response.append(f"{key}: {value}")
        if method == "REGISTER":
            response.extend(
                ["Contact: " + headers["contact"], "Expires: " + headers.get("expires", "300")]
            )
        response.extend(["Content-Length: 0", "", ""])
        self.transport.sendto("\r\n".join(response).encode(), addr)


def test_native_registration_and_direct_invite(config):
    async def scenario():
        loop = asyncio.get_running_loop()
        transport, registrar = await loop.create_datagram_endpoint(
            Registrar, local_addr=("127.0.0.1", 0)
        )
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as available:
            available.bind(("127.0.0.1", 0))
            local_port = available.getsockname()[1]
        cfg = replace(
            config,
            sip_server="127.0.0.1",
            sip_domain="127.0.0.1",
            sip_port=transport.get_extra_info("sockname")[1],
            sip_local_port=local_port,
            sip_bind_address="127.0.0.1",
            sip_peer_ips=("127.0.0.1",),
        )
        sessions = []
        sip = SipClient(cfg, sessions.append)
        poll = None
        try:
            sip.start()
            poll = asyncio.create_task(sip.poll())
            await asyncio.wait_for(sip.registered_event.wait(), 5)
            session = sip.dial("f" * 32, "1001", "hello")
            await asyncio.wait_for(session.disconnected.wait(), 5)
            assert session.status == 486
            assert registrar.invites == ["INVITE sip:1001@127.0.0.1 SIP/2.0"]
            assert sessions == [session]
            sip.release(session)
        finally:
            if poll:
                poll.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await poll
            await sip.close()
            transport.close()

    asyncio.run(scenario())
