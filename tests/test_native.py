"""Actual PJSIP REGISTER and INVITE smoke test, run by the native Linux CI job."""

import asyncio
import contextlib
import re
import socket
import struct
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
            session = sip.dial("f" * 32, "1001", "hello", b"\0\0")
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


class RTPPeer(asyncio.DatagramProtocol):
    def __init__(self):
        self.heard_audio = asyncio.Event()

    def datagram_received(self, data, addr):
        if len(data) > 12 and data[1] & 0x7F == 0:
            if any(value not in (0xFF, 0x7F) for value in data[12:]):
                self.heard_audio.set()


class AnsweringRegistrar(Registrar):
    def __init__(self, rtp_port):
        super().__init__()
        self.rtp_port = rtp_port
        self.remote_rtp = None

    def datagram_received(self, data, addr):
        text = data.decode()
        if not text.startswith("INVITE "):
            return super().datagram_received(data, addr)
        self.invites.append(text.splitlines()[0])
        offer_port = re.search(r"^m=audio ([0-9]+)", text, re.MULTILINE)
        assert offer_port is not None
        self.remote_rtp = ("127.0.0.1", int(offer_port[1]))
        headers = {}
        for line in text.split("\r\n\r\n", 1)[0].split("\r\n")[1:]:
            key, _, value = line.partition(":")
            headers[key.lower()] = value.strip()
        body = (
            "v=0\r\no=test 1 1 IN IP4 127.0.0.1\r\ns=test\r\n"
            f"c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio {self.rtp_port} RTP/AVP 0\r\n"
            "a=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n"
        )
        response = ["SIP/2.0 200 OK"]
        for key in ("via", "from", "to", "call-id", "cseq"):
            value = headers[key]
            if key == "to" and ";tag=" not in value:
                value += ";tag=media-peer"
            response.append(f"{key}: {value}")
        port = self.transport.get_extra_info("sockname")[1]
        response.extend(
            [
                f"Contact: <sip:1001@127.0.0.1:{port}>",
                "Content-Type: application/sdp",
                f"Content-Length: {len(body)}",
                "",
                body,
            ]
        )
        self.transport.sendto("\r\n".join(response).encode(), addr)


def test_native_bidirectional_rtp_without_sound_hardware(config):
    async def scenario():
        loop = asyncio.get_running_loop()
        rtp_transport, rtp = await loop.create_datagram_endpoint(
            RTPPeer, local_addr=("127.0.0.1", 0)
        )
        rtp_port = rtp_transport.get_extra_info("sockname")[1]
        transport, registrar = await loop.create_datagram_endpoint(
            lambda: AnsweringRegistrar(rtp_port), local_addr=("127.0.0.1", 0)
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
        sip = SipClient(cfg, lambda _: None)
        poll = None
        try:
            sip.start()
            poll = asyncio.create_task(sip.poll())
            await asyncio.wait_for(sip.registered_event.wait(), 5)
            tone = struct.pack("<hh", 5000, -5000) * 1600
            session = sip.dial("e" * 32, "1001", "hello", tone)
            await asyncio.wait_for(session.answered.wait(), 5)
            await asyncio.wait_for(session.ready.wait(), 5)
            assert session.media_connected
            assert not session.failure_reason

            async def send_rtp():
                for sequence in range(40):
                    payload = (b"\x00" if sequence < 20 else b"\xff") * 160
                    header = struct.pack("!BBHII", 0x80, 0, sequence, sequence * 160, 1234)
                    rtp_transport.sendto(header + payload, registrar.remote_rtp)
                    await asyncio.sleep(0.02)

            async with asyncio.timeout(5):
                await asyncio.gather(session.audio.play(tone), rtp.heard_audio.wait())
                captured, _ = await asyncio.gather(
                    session.audio.listen(limit_seconds=3, silence_seconds=0.12, wait_seconds=2),
                    send_rtp(),
                )
            assert len(captured) > 0
            assert any(sample[0] != 0 for sample in struct.iter_unpack("<h", captured))
        finally:
            if poll:
                poll.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await poll
            await sip.close()
            transport.close()
            rtp_transport.close()

    asyncio.run(scenario())
