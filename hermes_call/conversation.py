"""Run a bounded Hermes voice conversation independently of the SIP clock."""

import asyncio
import logging
import shutil
import sqlite3
import wave
from pathlib import Path

from .audio import BYTES_PER_SECOND, RATE
from .config import Config
from .sip import CallSession, SipClient
from .store import Store
from .voice import Voice, split_text

log = logging.getLogger(__name__)


class Conversations:
    def __init__(self, config: Config, store: Store, voice: Voice):
        self.config, self.store, self.voice = config, store, voice
        self.tasks: set[asyncio.Task] = set()
        self.sip: SipClient | None = None

    def accept(self, session: CallSession) -> None:
        if not session.outbound:
            session.chat_ref = self.store.binding(session.extension)
        task = asyncio.create_task(self.run(session))
        self.tasks.add(task)
        task.add_done_callback(self.finished)

    def finished(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("conversation cleanup failed (%s)", type(task.exception()).__name__)

    async def run(self, session: CallSession) -> None:
        assert self.sip is not None
        directory = self.config.data_dir / "media" / session.id
        outcome = "completed"
        body = asyncio.create_task(self.talk(session, directory))
        disconnected = asyncio.create_task(session.disconnected.wait())
        try:
            # BYE stops audio/HTTP waits and detaches the CLI. Gateway-accepted work may continue.
            done, _ = await asyncio.wait({body, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if body in done:
                await body
            if session.disconnected.is_set() and not session.confirmed:
                outcome = "unanswered" if session.status in (408, 480, 486, 487, 603) else "failed"
        except asyncio.CancelledError:
            outcome = "interrupted"
            raise
        except TimeoutError:
            outcome = "timeout" if session.confirmed else "unanswered"
            log.warning("call %s timed out", session.id)
        except Exception as exc:
            outcome = "failed"
            log.error("call %s failed (%s)", session.id, type(exc).__name__)
            if not session.disconnected.is_set() and session.confirmed_at is not None:
                try:
                    async with asyncio.timeout(15):
                        loop = asyncio.get_running_loop()
                        await asyncio.sleep(max(0, session.confirmed_at + 1.0 - loop.time()))
                        await self.speak(
                            session, directory, "エラーが発生したため通話を終了します。"
                        )
                except Exception as notification_error:
                    log.warning("error notification failed (%s)", type(notification_error).__name__)
        finally:
            body.cancel()
            disconnected.cancel()
            await asyncio.gather(body, disconnected, return_exceptions=True)
            if session.outbound:
                try:
                    self.store.transition(session.id, "active", outcome)
                    self.store.transition(session.id, "dialing", outcome)
                except sqlite3.Error:
                    log.error("call %s journal unavailable; continue hangup", session.id)
            try:
                self.sip.hangup(session)
            except Exception as exc:
                log.warning("call %s hangup failed (%s)", session.id, type(exc).__name__)
            finally:
                self.sip.release(session)
                if directory.exists():
                    try:
                        shutil.rmtree(directory)
                    except OSError:
                        log.error("call %s media cleanup failed; retry on startup", session.id)

    async def talk(self, session: CallSession, directory: Path) -> None:
        async with asyncio.timeout(self.config.max_call_seconds):
            async with asyncio.timeout(45):
                await session.answered.wait()
                await session.ready.wait()
            if session.disconnected.is_set() or not session.confirmed:
                return
            if not session.initial_audio or session.confirmed_at is None:
                raise RuntimeError("initial message must be prepared before answering")
            if session.outbound:
                self.store.transition(session.id, "dialing", "active")
            directory.mkdir(mode=0o700, parents=True)
            # Start at the first media-ready opportunity at/after CONFIRMED + 1 second.
            loop = asyncio.get_running_loop()
            await asyncio.sleep(max(0, session.confirmed_at + 1.0 - loop.time()))
            if session.disconnected.is_set():
                return
            await session.audio.play(session.initial_audio)
            silent = 0
            first_turn = True
            for _ in range(60):
                pcm = await session.audio.listen()
                if not pcm:
                    silent += 1
                    if silent >= 3:
                        return
                    await self.speak(session, directory, "もう一度お話しください。")
                    continue
                path = directory / "input.wav"
                try:
                    with wave.open(str(path), "wb") as wav:
                        wav.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
                        wav.writeframes(pcm)
                    text = await self.voice.transcribe(path)
                finally:
                    path.unlink(missing_ok=True)
                if not text:
                    silent += 1
                    if silent >= 3:
                        return
                    continue
                silent = 0
                if first_turn:
                    opening = session.message if session.outbound else self.config.incoming_message
                    text = f"[電話の冒頭ですでに読み上げた内容: {opening}]\n{text}"
                    first_turn = False
                reference = session.chat_ref or f"hermes-phone-{session.extension}"
                reply = await self.voice.reply(
                    reference, session.extension, text, linked=session.chat_ref is not None
                )
                await self.speak(session, directory, reply)

    async def prepare_initial(self, call_id: str, text: str) -> bytes:
        """Complete all TTS before INVITE/answer, so network latency cannot delay the greeting."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("initial message is mandatory")
        directory = self.config.data_dir / "media" / call_id
        directory.mkdir(mode=0o700, parents=True)
        pcm = bytearray()
        try:
            text += "音声はVOICEVOX、ずんだもんを使用しています。"
            for chunk in split_text(text, self.config.tts_max_text_chars):
                path = directory / "initial.wav"
                await self.voice.synthesize(chunk, path)
                pcm.extend(self.read_pcm(path))
                if len(pcm) > BYTES_PER_SECOND * 180:
                    raise ValueError("initial message audio exceeded 180 seconds")
            if not pcm:
                raise ValueError("initial message produced no audio")
            return bytes(pcm)
        finally:
            shutil.rmtree(directory)

    @staticmethod
    def read_pcm(path: Path) -> bytes:
        with wave.open(str(path), "rb") as wav:
            if (wav.getsampwidth(), wav.getnchannels(), wav.getframerate()) != (2, 1, RATE):
                raise RuntimeError("TTS output must be mono PCM16 16kHz")
            if not 0 < wav.getnframes() <= RATE * 180:
                raise RuntimeError("TTS audio exceeded duration limit")
            return wav.readframes(wav.getnframes())

    async def speak(self, session: CallSession, directory: Path, text: str) -> None:
        for chunk in split_text(text, self.config.tts_max_text_chars):
            if session.disconnected.is_set():
                raise ConnectionError("call disconnected")
            await self.speak_chunk(session, directory, chunk)

    async def speak_chunk(self, session: CallSession, directory: Path, text: str) -> None:
        path = directory / "reply.wav"
        try:
            await self.voice.synthesize(text, path)
            pcm = self.read_pcm(path)
            await session.audio.play(pcm)
        finally:
            path.unlink(missing_ok=True)

    async def close(self) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
