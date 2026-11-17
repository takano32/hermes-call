"""Run a bounded Hermes voice conversation independently of the SIP clock."""

import asyncio
import logging
import shutil
import sqlite3
import wave
from pathlib import Path

from .audio import RATE
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
            # BYE cancels STT/TTS/Hermes immediately, even during an HTTP request.
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
            if not session.disconnected.is_set():
                try:
                    async with asyncio.timeout(15):
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
            if session.outbound:
                self.store.transition(session.id, "dialing", "active")
            directory.mkdir(mode=0o700, parents=True)
            greeting = (
                "Hermesです。" + session.message
                if session.outbound
                else "Hermesです。ご用件をお話しください。"
            )
            greeting += "音声はVOICEVOX、ずんだもんを使用しています。"
            await self.speak(session, directory, greeting)
            silent = 0
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
                reply = await self.voice.reply(session.id, session.extension, text)
                await self.speak(session, directory, reply)

    async def speak(self, session: CallSession, directory: Path, text: str) -> None:
        for chunk in split_text(text, self.config.tts_max_text_chars):
            if session.disconnected.is_set():
                raise ConnectionError("call disconnected")
            await self.speak_chunk(session, directory, chunk)

    async def speak_chunk(self, session: CallSession, directory: Path, text: str) -> None:
        path = directory / "reply.wav"
        try:
            await self.voice.synthesize(text, path)
            with wave.open(str(path), "rb") as wav:
                if (wav.getsampwidth(), wav.getnchannels(), wav.getframerate()) != (2, 1, RATE):
                    raise RuntimeError("TTS output must be mono PCM16 16kHz")
                if wav.getnframes() > RATE * 180:
                    raise RuntimeError("TTS audio exceeded duration limit")
                pcm = wav.readframes(wav.getnframes())
            await session.audio.play(pcm)
        finally:
            path.unlink(missing_ok=True)

    async def close(self) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
