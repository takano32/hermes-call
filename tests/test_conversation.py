import asyncio
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hermes_call.conversation import Conversations
from hermes_call.sip import CallSession
from hermes_call.store import Store


def test_remote_hangup_cancels_agent_and_releases_resources(config, tmp_path):
    async def scenario():
        store = Store(tmp_path / "calls.sqlite")
        store.add({"id": "a" * 32, "extension": "1001", "message": "hello"})
        store.transition("a" * 32, "preparing", "dialing")
        started, cancelled = asyncio.Event(), asyncio.Event()

        class Conversation(Conversations):
            async def talk(self, session, directory):
                directory.mkdir(parents=True)
                (directory / "private.wav").write_bytes(b"audio")
                self.store.transition(session.id, "dialing", "active")
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        calls = Conversation(config, store, None)
        calls.sip = SimpleNamespace(hangup=Mock(), release=Mock())
        session = CallSession("a" * 32, "1001", "hello", True)
        session.confirmed = True
        session.media_connected = True
        task = asyncio.create_task(calls.run(session))
        await started.wait()
        session.disconnected.set()
        await asyncio.wait_for(task, 1)
        assert cancelled.is_set()
        calls.sip.release.assert_called_once_with(session)
        assert not (tmp_path / "media" / session.id).exists()
        assert store.get(session.id)["state"] == "completed"

    asyncio.run(scenario())


@pytest.mark.parametrize("linked", [True, False])
@pytest.mark.parametrize("outbound", [True, False])
def test_answer_delay_and_two_voice_turns_keep_chat_session(config, tmp_path, linked, outbound):
    async def scenario():
        store = Store(tmp_path / "calls.sqlite")
        voice = AsyncMock()
        voice.transcribe.side_effect = ["チャットの続きです", "それを詳しく"]
        voice.reply.side_effect = ["続きですね", "詳しくお話しします"]
        calls = Conversations(config, store, voice)
        session = CallSession("c" * 32, "1001", "初期メッセージです", outbound)
        session.chat_ref = "chat-existing" if linked else None
        session.initial_audio = b"\0\0"
        session.confirmed = True
        session.confirmed_at = asyncio.get_running_loop().time()
        session.answered.set()
        session.ready.set()
        times, replies = [], []

        async def play(pcm):
            assert pcm == b"\0\0"
            times.append(asyncio.get_running_loop().time())

        class StopTest(Exception):
            pass

        session.audio.play = AsyncMock(side_effect=play)
        session.audio.listen = AsyncMock(side_effect=[b"\0\0" * 1000, b"\0\0" * 1000, StopTest()])

        async def speak(_, directory, text):
            replies.append(text)

        calls.speak = speak
        with pytest.raises(StopTest):
            await calls.talk(session, tmp_path / "media" / session.id)
        assert times[0] - session.confirmed_at >= 1.0
        voice.synthesize.assert_not_called()  # Initial audio was already prepared.
        assert replies == ["続きですね", "詳しくお話しします"]
        reference = "chat-existing" if linked else "hermes-phone-1001"
        assert all(call.args[:2] == (reference, "1001") for call in voice.reply.call_args_list)
        assert all(call.kwargs["linked"] == linked for call in voice.reply.call_args_list)

    asyncio.run(scenario())


def test_disconnect_during_one_second_delay_prevents_speech(config, tmp_path):
    async def scenario():
        store = Store(tmp_path / "calls.sqlite")
        voice = AsyncMock()
        calls = Conversations(config, store, voice)
        calls.sip = SimpleNamespace(hangup=Mock(), release=Mock())
        session = CallSession("d" * 32, "1001", "hello", False)
        session.initial_audio = b"\0\0"
        session.confirmed = True
        session.confirmed_at = asyncio.get_running_loop().time()
        session.answered.set()
        session.ready.set()
        session.audio.play = AsyncMock()
        task = asyncio.create_task(calls.run(session))
        await asyncio.sleep(0.05)
        session.disconnected.set()
        await asyncio.wait_for(task, 1)
        session.audio.play.assert_not_called()
        voice.synthesize.assert_not_called()

    asyncio.run(scenario())


def test_initial_message_is_prepared_as_pcm_and_temporary_files_removed(config, tmp_path):
    async def scenario():
        voice = AsyncMock()

        async def synthesize(text, path):
            with wave.open(str(path), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(b"\0\0" * 320)

        voice.synthesize.side_effect = synthesize
        calls = Conversations(config, Store(tmp_path / "calls.sqlite"), voice)
        pcm = await calls.prepare_initial("e" * 32, "必ず最初に話す内容")
        assert pcm == b"\0\0" * 320
        assert "必ず最初に話す内容" in voice.synthesize.call_args.args[0]
        assert not (tmp_path / "media" / ("e" * 32)).exists()
        with pytest.raises(ValueError):
            await calls.prepare_initial("e" * 32, " ")

    asyncio.run(scenario())


def test_unanswered_call_not_recorded_as_completed(config, tmp_path):
    async def scenario():
        store = Store(tmp_path / "calls.sqlite")
        store.add({"id": "b" * 32, "extension": "1001", "message": "hello"})
        store.transition("b" * 32, "preparing", "dialing")
        calls = Conversations(config, store, AsyncMock())
        calls.sip = SimpleNamespace(hangup=Mock(), release=Mock())
        session = CallSession("b" * 32, "1001", "hello", True)
        session.status = 486
        session.ready.set()
        session.answered.set()
        session.disconnected.set()
        await calls.run(session)
        assert store.get(session.id)["state"] == "unanswered"
        calls.voice.synthesize.assert_not_called()

    asyncio.run(scenario())
