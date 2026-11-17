"""Bounded speech requests and isolated Hermes CLI turns."""

import asyncio
import contextlib
import json
import os
import signal
import struct
import wave
from pathlib import Path

import aiohttp

from .config import Config


def split_text(text: str, limit: int) -> list[str]:
    """Split without dropping words when the public service limits text length."""
    chunks = []
    while text:
        end = min(len(text), limit)
        if len(text) > limit:
            split = max(text.rfind(mark, 0, limit) for mark in ("。", "！", "？", "\n", " "))
            if split >= limit // 2:
                end = split + 1
        chunk, text = text[:end].strip(), text[end:].lstrip()
        if chunk:
            chunks.append(chunk)
    return chunks


async def run_process(argv: list[str], deadline_seconds: int, env: dict | None = None) -> str:
    """Kill the entire subprocess group on timeout/cancellation (Ubuntu)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        stdin=asyncio.subprocess.DEVNULL,
        env=env,
        start_new_session=os.name == "posix",
        limit=256 * 1024,
    )

    async def read_output() -> bytes:
        assert proc.stdout is not None
        output = bytearray()
        while chunk := await proc.stdout.read(16384):
            output.extend(chunk)
            if len(output) > 256 * 1024:
                raise RuntimeError("subprocess output exceeded limit")
        await proc.wait()
        return bytes(output)

    try:
        async with asyncio.timeout(deadline_seconds):
            output = await read_output()
        if proc.returncode:
            raise RuntimeError(f"subprocess failed (exit {proc.returncode})")
        return output.decode("utf-8", errors="strict").strip()
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            await proc.wait()


class Voice:
    def __init__(self, config: Config, http: aiohttp.ClientSession):
        self.config = config
        self.http = http
        self.tts_lock = asyncio.Lock()
        self.tts_next = 0.0

    @contextlib.asynccontextmanager
    async def tts_request(self, endpoint: str, payload: dict):
        # Public proxy documents 30 requests / 60 seconds per IP; both calls count.
        async with self.tts_lock:
            loop = asyncio.get_running_loop()
            await asyncio.sleep(max(0, self.tts_next - loop.time()))
            self.tts_next = loop.time() + 2.1
            async with self.http.post(
                self.config.tts_url + endpoint,
                json=payload,
                allow_redirects=False,
            ) as response:
                response.raise_for_status()
                if response.status != 200:
                    raise RuntimeError("unexpected public VOICEVOX status")
                yield response

    async def transcribe(self, path: Path) -> str:
        c = self.config
        if not path.exists() or path.stat().st_size < 1000:
            return ""
        if path.stat().st_size > 2 * 1024 * 1024:
            raise RuntimeError("recording exceeded size limit")
        # Silence fed to Whisper can hallucinate words; reject it locally first.
        with wave.open(str(path), "rb") as wav:
            if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
                raise RuntimeError("expected mono PCM16 recording")
            pcm = wav.readframes(wav.getnframes())
        if not pcm:
            return ""
        energy = sum(sample[0] ** 2 for sample in struct.iter_unpack("<h", pcm))
        if energy / (len(pcm) // 2) < 100**2:
            return ""
        form = aiohttp.FormData()
        form.add_field("model", c.stt_model)
        form.add_field("language", c.language)
        with path.open("rb") as audio:
            form.add_field("file", audio, filename="speech.wav", content_type="audio/wav")
            async with self.http.post(
                c.stt_url + "/audio/transcriptions",
                data=form,
                headers={"Authorization": f"Bearer {c.stt_api_key}"},
                allow_redirects=False,
            ) as response:
                response.raise_for_status()
                if response.status != 200:
                    raise RuntimeError("unexpected STT status")
                # A short recording must not produce unbounded text.
                body = await response.content.read(65537)
                if len(body) > 65536:
                    raise RuntimeError("transcription response exceeded limit")
                text = json.loads(body).get("text")
                if not isinstance(text, str) or len(text) > 4000:
                    raise RuntimeError("invalid transcription response")
                return text.strip()

    async def synthesize(self, text: str, destination: Path) -> None:
        c = self.config
        if not text or len(text) > c.tts_max_text_chars:
            raise ValueError("TTS text must fit configured public API limit")
        source = destination.with_suffix(".source")
        try:
            async with self.tts_request(
                "/audio_query",
                {"text": text, "speaker": c.tts_speaker},
            ) as response:
                body = await response.content.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise RuntimeError("VOICEVOX query exceeded limit")
                query = json.loads(body)
            async with self.tts_request(
                "/synthesis",
                {"speaker": c.tts_speaker, "audioQuery": query},
            ) as response:
                size = 0
                with source.open("wb") as target:
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > 16 * 1024 * 1024:
                            raise RuntimeError("speech response exceeded limit")
                        target.write(chunk)
            await run_process(
                [
                    "/usr/bin/ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(destination),
                ],
                30,
            )
        finally:
            source.unlink(missing_ok=True)

    async def reply(self, session: str, caller: str, text: str) -> str:
        c = self.config
        prompt = (
            "電話で会話しています。短い日本語の話し言葉で返答してください。"
            "読み上げ音声は VOICEVOX:ずんだもん です。"
            "Markdownは使わないでください。音声認識の誤りは確認してください。"
            f"接続先内線は {caller} です。『後で電話して』はこの内線を使ってください。"
            "即時発信には hermes_call_dial を使ってください。日時指定の電話はHermes標準の"
            "cronツールで、この発信ツールを実行するタスクを登録してください。"
            "ツールの成功結果が出るまで完了したと言わないでください。"
            "予約時刻が曖昧なら確認してください。"
            "以下は電話相手の発言です。\n" + text
        )
        # One named session per call. Never resume the owner's most recent session.
        reply = await run_process(
            [
                *c.hermes_command,
                "chat",
                "--quiet",
                "--query",
                prompt,
                "--continue",
                f"hermes-call-{session}",
                "--create-if-missing",
                "--toolsets",
                ",".join(c.hermes_toolsets),
            ],
            c.agent_timeout_seconds,
        )
        if not reply or "\x1b" in reply or len(reply) > 8000:
            raise RuntimeError("Hermes returned invalid or non-quiet output")
        return reply[:2000]
