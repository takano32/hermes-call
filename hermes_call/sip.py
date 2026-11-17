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


def caller_identity(uri: str) -> tuple[str | None, str | None]:
    match = re.search(
        r"(?:^|<)sips?:([0-9]{1,12})@([A-Za-z0-9.-]+)(?::[0-9]+)?(?:[;>]|$)", uri, re.IGNORECASE
    )
    return (match[1], match[2].lower()) if match else (None, None)


def caller_extension(uri: str, domain: str) -> str | None:
    extension, actual_domain = caller_identity(uri)
    if actual_domain == domain.lower():
        return extension
    return None


def peer_address(source: str) -> str | None:
    try:
        host, separator, port = source.rpartition(":")
        if not separator or not port.isdigit():
            return None
        return str(ipaddress.IPv4Address(host))
    except ipaddress.AddressValueError:
        return None


@dataclass(frozen=True)
class IncomingDecision:
    code: int
    reason: str
    extension: str | None
    domain: str | None
    peer: str | None


def incoming_decision(
    config: Config, uri: str, source: str, *, closing: bool, busy: bool, audio_ready: bool
) -> IncomingDecision:
    extension, domain = caller_identity(uri)
    peer = peer_address(source)
    domains = config.allowed_caller_domains or (config.sip_domain,)
    if closing:
        code, reason = 503, "service_stopping"
    elif peer is None:
        code, reason = 403, "invalid_peer_address"
    elif peer not in config.sip_peer_ips:
        code, reason = 403, "peer_not_allowed"
    elif extension is None:
        code, reason = 403, "invalid_caller_uri"
    elif domain not in {value.lower() for value in domains}:
        code, reason = 403, "caller_domain_not_allowed"
    elif extension not in config.allowed_callers:
        code, reason = 403, "extension_not_allowed"
    elif busy:
        code, reason = 486, "line_busy"
    elif not audio_ready:
        code, reason = 503, "initial_audio_not_ready"
    else:
        code, reason = 200, "accepted"
    return IncomingDecision(code, reason, extension, domain, peer)


def native_error(exc: Exception) -> str:
    """Identify the failing native API/attribute without dumping SIP data or credentials."""
    parts = [type(exc).__name__]
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        parts.append(f"status={status}")
    for name in ("title", "name"):
        value = getattr(exc, name, None)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_():., -]{1,160}", value):
            parts.append(f"{name}={value}")
    return " ".join(parts)


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
    confirmed_at: float | None = None
    initial_audio: bytes | None = None
    chat_ref: str | None = None
    failure_reason: str | None = None


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
        self.reserved: str | None = None
        self.inbound_audio: bytes | None = None

    @property
    def busy(self) -> bool:
        return bool(self.sessions) or self.reserved is not None

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
                    log.info(
                        "SIP call native_id=%s state=%s status=%s",
                        self.getId(),
                        info.state,
                        info.lastStatusCode,
                    )
                    if session:
                        session.status = info.lastStatusCode
                        if info.state == pj.PJSIP_INV_STATE_CONFIRMED:
                            if session.confirmed_at is None:
                                session.confirmed_at = loop.time()
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
                    log.error("SIP state callback failed (%s)", native_error(exc))
                    if self.session:
                        self.session.failure_reason = "state_callback_error"
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
                            log.info("call %s audio media ready", self.session.id)
                            break
                except Exception as exc:
                    log.error("SIP media callback failed (%s)", native_error(exc))
                    self.session.failure_reason = "media_callback_error"
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
                    decision = incoming_decision(
                        owner.config,
                        call.getInfo().remoteUri,
                        prm.rdata.srcAddress,
                        closing=owner.closing,
                        busy=owner.busy,
                        audio_ready=bool(owner.inbound_audio),
                    )
                    code, extension = decision.code, decision.extension
                    log.log(
                        logging.INFO if code == 200 else logging.WARNING,
                        "SIP incoming native_id=%s peer=%s extension=%s caller_domain=%s "
                        "response=%s reason=%s",
                        prm.callId,
                        decision.peer,
                        extension,
                        decision.domain,
                        code,
                        decision.reason,
                    )
                    if code == 200:
                        session = CallSession(new_id(), extension, "", False)
                        session.native = call
                        session.initial_audio = owner.inbound_audio
                        call.session = session
                        owner.sessions[session.id] = session
                        owner.on_call(session)
                    op = pj.CallOpParam()
                    op.statusCode = code
                    op.opt.audioCount, op.opt.videoCount = 1, 0
                    call.answer(op)
                except Exception as exc:
                    log.error("SIP incoming callback failed (%s)", native_error(exc))
                    if call.session:
                        call.session.failure_reason = "incoming_callback_error"
                        call.session.disconnected.set()
                        call.session.ready.set()
                        call.session.answered.set()
                    op = pj.CallOpParam()
                    op.statusCode = 500
                    try:
                        call.hangup(op)
                    except Exception as hangup_error:
                        log.error("SIP reject/hangup failed (%s)", native_error(hangup_error))

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

    def dial(
        self,
        call_id: str,
        extension: str,
        message: str,
        initial_audio: bytes,
        chat_ref: str | None = None,
    ) -> CallSession:
        if not self.registered:
            raise ConnectionError("SIP account is not registered")
        if self.sessions or (self.reserved is not None and self.reserved != call_id):
            raise RuntimeError("telephone is busy")
        if extension not in self.config.allowed_extensions:
            raise ValueError("extension is not allowed")
        if not initial_audio or len(initial_audio) % 2:
            raise ValueError("initial message audio is mandatory")
        session = CallSession(call_id, extension, message, True)
        session.initial_audio, session.chat_ref = initial_audio, chat_ref
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
