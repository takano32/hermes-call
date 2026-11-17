"""Direct PJSUA2 user agent. All SIP objects live on the asyncio/main thread.

Only PCM copies run on the native audio clock thread. No HTTP, files or agent
operations execute inside native callbacks.
"""

import asyncio
import importlib
import ipaddress
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .audio import AudioBuffer
from .config import Config
from .store import new_id

log = logging.getLogger(__name__)


def caller_extension(uri: str, domain: str) -> str | None:
    match = re.search(
        r"(?:^|<)sips?:([0-9]{1,12})@([A-Za-z0-9.-]+)(?::[0-9]+)?(?:[;>]|$)", uri, re.IGNORECASE
    )
    if match and match[2].lower() == domain.lower():
        return match[1]
    return None


def peer_address(source: str) -> str | None:
    try:
        host, separator, port = source.rpartition(":")
        if not separator or not port.isdigit():
            return None
        return str(ipaddress.IPv4Address(host))
    except ipaddress.AddressValueError:
        return None


@dataclass
class CallSession:
    id: str
    extension: str
    message: str
    outbound: bool
    audio: AudioBuffer = field(default_factory=AudioBuffer)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    answered: asyncio.Event = field(default_factory=asyncio.Event)
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    native: Any = None
    status: int = 0
    confirmed: bool = False
    native_disconnected: bool = False


class SipClient:
    def __init__(self, config: Config, on_call: Callable[[CallSession], None]):
        self.config, self.on_call = config, on_call
        self.registered = False
        self.registered_event = asyncio.Event()
        self.registration_status = 0
        self.sessions: dict[str, CallSession] = {}
        self.native_calls: dict[int, Any] = {}
        self.ep = None
        self.account = None
        self.pj = None
        self.closing = False

    @property
    def busy(self) -> bool:
        return bool(self.sessions)

    def start(self) -> None:
        pj = self.pj = importlib.import_module("pjsua2")
        owner = self
        loop = asyncio.get_running_loop()

        class Port(pj.AudioMediaPort):
            def __init__(self, audio):
                super().__init__()
                self.audio = audio

            def onFrameReceived(self, frame):
                if frame.type == pj.PJMEDIA_FRAME_TYPE_AUDIO:
                    self.audio.receive(bytes(frame.buf))

            def onFrameRequested(self, frame):
                frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
                frame.buf.assign_from_bytes(self.audio.provide(frame.size))

        class Call(pj.Call):
            def __init__(self, account, call_id=pj.PJSUA_INVALID_ID, session=None):
                super().__init__(account, call_id)
                self.session = session
                self.port = None
                self.media = None

            def onCallState(self, prm):
                try:
                    info = self.getInfo()
                    session = self.session
                    if session:
                        session.status = info.lastStatusCode
                        if info.state == pj.PJSIP_INV_STATE_CONFIRMED:
                            session.confirmed = True
                            session.answered.set()
                    if info.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                        if session:
                            session.native_disconnected = True
                            session.audio.stop()
                            session.disconnected.set()
                            session.ready.set()  # Wake an unanswered/media wait immediately.
                            session.answered.set()
                        # Keep SWIG objects alive until the native callback returns.
                        loop.call_soon(owner.native_calls.pop, self.getId(), None)
                except Exception as exc:
                    log.error("SIP state callback failed (%s)", type(exc).__name__)
                    if self.session:
                        self.session.audio.stop()
                        self.session.disconnected.set()
                        self.session.ready.set()
                        self.session.answered.set()

            def onCallMediaState(self, prm):
                try:
                    if not self.session or self.session.disconnected.is_set():
                        return
                    for item in self.getInfo().media:
                        if (
                            item.type == pj.PJMEDIA_TYPE_AUDIO
                            and item.status == pj.PJSUA_CALL_MEDIA_ACTIVE
                        ):
                            if self.port is None:
                                self.port = Port(self.session.audio)
                                fmt = pj.MediaFormatAudio()
                                fmt.type, fmt.clockRate = pj.PJMEDIA_TYPE_AUDIO, 16000
                                fmt.channelCount, fmt.bitsPerSample = 1, 16
                                fmt.frameTimeUsec = 20000
                                self.port.createPort("hermes-" + self.session.id, fmt)
                            media = self.getAudioMedia(item.index)
                            if self.media is not None:
                                self.media.stopTransmit(self.port)
                                self.port.stopTransmit(self.media)
                            media.startTransmit(self.port)
                            self.port.startTransmit(media)
                            self.media = media
                            self.session.ready.set()
                            break
                except Exception as exc:
                    log.error("SIP media callback failed (%s)", type(exc).__name__)
                    self.session.audio.stop()
                    self.session.disconnected.set()
                    self.session.ready.set()
                    self.session.answered.set()

        class Account(pj.Account):
            def onRegState(self, prm):
                info = self.getInfo()
                owner.registered = info.regIsActive and 200 <= info.regStatus < 300
                owner.registration_status = info.regStatus
                if owner.registered:
                    owner.registered_event.set()
                else:
                    owner.registered_event.clear()
                log.info("SIP registration status=%s active=%s", info.regStatus, owner.registered)

            def onIncomingCall(self, prm):
                call = Call(self, prm.callId)
                owner.native_calls[prm.callId] = call
                try:
                    extension = caller_extension(call.getInfo().remoteUri, owner.config.sip_domain)
                    peer = peer_address(prm.rdata.srcAddress)
                    code = 200
                    if (
                        owner.closing
                        or peer not in owner.config.sip_peer_ips
                        or extension not in owner.config.allowed_callers
                    ):
                        code = 403
                    elif owner.busy:
                        code = 486
                    if code == 200:
                        session = CallSession(new_id(), extension, "", False)
                        session.native = call
                        call.session = session
                        owner.sessions[session.id] = session
                        owner.on_call(session)
                    op = pj.CallOpParam()
                    op.statusCode = code
                    op.opt.audioCount, op.opt.videoCount = 1, 0
                    call.answer(op)
                except Exception as exc:
                    log.error("SIP incoming callback failed (%s)", type(exc).__name__)
                    op = pj.CallOpParam()
                    op.statusCode = 500
                    call.hangup(op)

        self.call_class = Call
        self.ep = pj.Endpoint()
        self.ep.libCreate()
        cfg = pj.EpConfig()
        cfg.uaConfig.threadCnt = 0
        cfg.uaConfig.mainThreadOnly = True
        cfg.uaConfig.maxCalls = 4  # Room to reject extra incoming dialogs cleanly.
        cfg.uaConfig.userAgent = "HermesCall/0.2"
        for server in self.config.stun_servers:
            cfg.uaConfig.stunServer.append(server)
        cfg.medConfig.clockRate, cfg.medConfig.sndClockRate = 16000, 16000
        cfg.medConfig.channelCount = 1
        cfg.logConfig.level, cfg.logConfig.consoleLevel = 0, 0
        self.ep.libInit(cfg)
        transport = pj.TransportConfig()
        transport.port = self.config.sip_local_port
        transport.boundAddress = self.config.sip_bind_address
        transport.tlsConfig.verifyServer = True
        transport.tlsConfig.CaListFile = "/etc/ssl/certs/ca-certificates.crt"
        kind = {
            "udp": pj.PJSIP_TRANSPORT_UDP,
            "tcp": pj.PJSIP_TRANSPORT_TCP,
            "tls": pj.PJSIP_TRANSPORT_TLS,
        }[self.config.sip_transport]
        transport_id = self.ep.transportCreate(kind, transport)
        self.ep.libStart()
        self.ep.audDevManager().setNullDev()  # Supplies a clock without ALSA or sound hardware.
        # Prefer widely interoperable G.711 audio; disable everything else.
        for codec in self.ep.codecEnum2():
            self.ep.codecSetPriority(
                codec.codecId, 200 if codec.codecId in ("PCMU/8000/1", "PCMA/8000/1") else 0
            )
        c = self.config
        account_cfg = pj.AccountConfig()
        scheme = "sips" if c.sip_transport == "tls" else "sip"
        account_cfg.idUri = f"{scheme}:{c.sip_username}@{c.sip_domain}"
        server_uri = f"{scheme}:{c.sip_server}:{c.sip_port};transport={c.sip_transport}"
        account_cfg.regConfig.registrarUri = server_uri
        account_cfg.regConfig.timeoutSec = 300
        account_cfg.regConfig.retryIntervalSec = 30
        account_cfg.sipConfig.transportId = transport_id
        account_cfg.sipConfig.proxies.append(server_uri + ";lr")
        account_cfg.sipConfig.authCreds.append(
            pj.AuthCredInfo("digest", "*", c.sip_username, 0, c.sip_password)
        )
        account_cfg.mediaConfig.transportConfig.port = c.rtp_port
        account_cfg.mediaConfig.transportConfig.portRange = 20
        account_cfg.mediaConfig.srtpUse = (
            pj.PJMEDIA_SRTP_MANDATORY if c.sip_srtp_required else pj.PJMEDIA_SRTP_DISABLED
        )
        self.account = Account()
        self.account.create(account_cfg)

    async def poll(self) -> None:
        while not self.closing:
            self.ep.libHandleEvents(0)
            await asyncio.sleep(0.01)

    def dial(self, call_id: str, extension: str, message: str) -> CallSession:
        if not self.registered:
            raise ConnectionError("SIP account is not registered")
        if self.busy:
            raise RuntimeError("telephone is busy")
        if extension not in self.config.allowed_extensions:
            raise ValueError("extension is not allowed")
        session = CallSession(call_id, extension, message, True)
        call = self.call_class(self.account, session=session)
        session.native = call
        self.sessions[call_id] = session
        notified = False
        try:
            op = self.pj.CallOpParam(True)
            op.opt.audioCount, op.opt.videoCount = 1, 0
            scheme = "sips" if self.config.sip_transport == "tls" else "sip"
            call.makeCall(f"{scheme}:{extension}@{self.config.sip_domain}", op)
            self.native_calls[call.getId()] = call
            self.on_call(session)
            notified = True
            return session
        except Exception:
            # Keep ownership and the busy slot until bounded conversation cleanup runs.
            native_id = call.getId()
            if native_id != self.pj.PJSUA_INVALID_ID:
                self.native_calls[native_id] = call
            if not notified:
                self.on_call(session)
            raise

    def hangup(self, session: CallSession) -> None:
        if not session.native_disconnected and session.native is not None:
            op = self.pj.CallOpParam()
            session.native.hangup(op)

    def release(self, session: CallSession) -> None:
        session.audio.stop()
        self.sessions.pop(session.id, None)
        if session.native:
            session.native.session = None
        session.native = None

    async def close(self) -> None:
        self.closing = True
        if self.ep is None:
            return
        for session in list(self.sessions.values()):
            try:
                self.hangup(session)
            except Exception as exc:
                log.warning("SIP hangup failed (%s)", type(exc).__name__)
            session.audio.stop()
        try:
            if self.account:
                try:
                    self.account.setRegistration(False)
                    for _ in range(50):
                        self.ep.libHandleEvents(0)
                        await asyncio.sleep(0.01)
                finally:
                    self.account.shutdown()
        finally:
            # Destroy wrappers on the owning thread, before destroying the native endpoint.
            for session in list(self.sessions.values()):
                self.release(session)
            self.native_calls.clear()
            self.account = None
            self.ep.libDestroy()
            self.ep = None
