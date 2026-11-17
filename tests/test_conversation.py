import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from hermes_call.conversation import Conversations
from hermes_call.sip import CallSession
from hermes_call.store import Store


def test_remote_hangup_cancels_agent_and_releases_resources(config, tmp_path):
    async def scenario():
        store = Store(tmp_path / "calls.sqlite")
        store.add({"id": "a" * 32, "extension": "1001", "message": "hello"})
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
        task = asyncio.create_task(calls.run(session))
        await started.wait()
        session.disconnected.set()
        await asyncio.wait_for(task, 1)
        assert cancelled.is_set()
        calls.sip.release.assert_called_once_with(session)
        assert not (tmp_path / "media" / session.id).exists()
        assert store.get(session.id)["state"] == "completed"

    asyncio.run(scenario())


def test_unanswered_call_not_recorded_as_completed(config, tmp_path):
    async def scenario():
        store = Store(tmp_path / "calls.sqlite")
        store.add({"id": "b" * 32, "extension": "1001", "message": "hello"})
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
