import asyncio
import struct

import pytest

from hermes_call.audio import BYTES_PER_SECOND, AudioBuffer


def test_native_playback_silence_and_bounds():
    buffer = AudioBuffer()
    assert buffer.provide(4) == b"\0" * 4
    buffer.outgoing = b"123456"
    assert buffer.provide(4) == b"1234"
    assert buffer.provide(4) == b"56\0\0"
    buffer.capture = True
    buffer.receive(b"a" * (BYTES_PER_SECOND * 2 + 1))
    assert buffer.overflow
    assert buffer.incoming_size == 0


def test_voice_activity_and_hangup():
    async def scenario():
        buffer = AudioBuffer()
        listener = asyncio.create_task(buffer.listen(silence_seconds=0.06))
        await asyncio.sleep(0)
        buffer.receive(struct.pack("<h", 1000) * 320)
        buffer.receive(b"\0" * 1920)
        result = await asyncio.wait_for(listener, 1)
        assert len(result) == 2560
        assert not buffer.capture
        buffer.stop()
        with pytest.raises(ConnectionError):
            await buffer.listen()

    asyncio.run(scenario())


def test_playback_completes_only_when_native_clock_consumes():
    async def scenario():
        buffer = AudioBuffer()
        task = asyncio.create_task(buffer.play(b"12" * 320))
        await asyncio.sleep(0)
        assert not task.done()
        assert buffer.provide(640) == b"12" * 320
        await asyncio.wait_for(task, 1)
        assert buffer.outgoing == b""

    asyncio.run(scenario())
