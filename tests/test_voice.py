import asyncio
import struct
import sys
import wave
from unittest.mock import AsyncMock

import pytest

from hermes_call.voice import Voice, run_process, split_text


class SpeechResponse:
    def __init__(self, body):
        self.body = body
        self.content = self
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    async def read(self, size):
        return self.body[:size]

    async def iter_chunked(self, size):
        yield self.body


def test_groq_and_voicevox_contract(config, tmp_path, monkeypatch):
    requests = []

    class HTTP:
        def post(self, url, **kwargs):
            requests.append((url, kwargs))
            if url.endswith("/transcriptions"):
                return SpeechResponse('{"text":"こんにちは"}'.encode())
            if url.endswith("/audio_query"):
                return SpeechResponse(b'{"outputSamplingRate":24000}')
            return SpeechResponse(b"fake WAV")

    source = tmp_path / "input.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        wav.writeframes(struct.pack("<h", 1000) * 8000)
    ffmpeg = AsyncMock(return_value="")
    monkeypatch.setattr("hermes_call.voice.run_process", ffmpeg)
    voice = Voice(config, HTTP())
    assert asyncio.run(voice.transcribe(source)) == "こんにちは"
    output = tmp_path / "output.wav"
    asyncio.run(voice.synthesize("こんにちは", output))
    assert requests[0][0] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert requests[0][1]["headers"]["Authorization"] == "Bearer groq-test-key"
    assert requests[1][0] == "https://voicevox-api.swiftlybot.com/v1/audio_query"
    assert requests[1][1]["json"] == {"text": "こんにちは", "speaker": 3}
    assert requests[2][0] == "https://voicevox-api.swiftlybot.com/v1/synthesis"
    assert requests[2][1]["json"] == {"speaker": 3, "audioQuery": {"outputSamplingRate": 24000}}
    assert "params" not in requests[1][1]
    assert all(not kwargs["allow_redirects"] for _, kwargs in requests)
    argv = ffmpeg.call_args.args[0]
    assert argv[argv.index("-ar") + 1] == "16000"
    assert argv[-1] == str(output)
    assert not output.with_suffix(".source").exists()


def test_long_text_is_split_without_loss():
    text = "あ" * 350 + "。" + "い" * 600
    chunks = split_text(text, 500)
    assert "".join(chunks) == text
    assert all(0 < len(chunk) <= 500 for chunk in chunks)


def test_silence_never_sent_to_whisper(config, tmp_path):
    path = tmp_path / "silence.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        wav.writeframes(struct.pack("<h", 0) * 16000)
    assert asyncio.run(Voice(config, None).transcribe(path)) == ""


def test_hermes_argv_preserves_call_session_and_cron(config, monkeypatch):
    mock = AsyncMock(return_value="こんにちは")
    monkeypatch.setattr("hermes_call.voice.run_process", mock)
    voice = Voice(config, None)
    assert asyncio.run(voice.reply("abc", "1001", "後で電話して")) == "こんにちは"
    argv = mock.call_args.args[0]
    assert argv[argv.index("--continue") + 1] == "hermes-call-abc"
    assert argv[argv.index("--toolsets") + 1] == "hermes_call,cronjob"
    assert "--create-if-missing" in argv
    assert "--quiet" in argv


def test_subprocess_errors_and_timeout():
    assert asyncio.run(run_process([sys.executable, "-c", "print('ok')"], 5)) == "ok"
    with pytest.raises(RuntimeError, match="exit 3"):
        asyncio.run(run_process([sys.executable, "-c", "raise SystemExit(3)"], 5))
    with pytest.raises(TimeoutError):
        asyncio.run(run_process([sys.executable, "-c", "import time; time.sleep(30)"], 1))
