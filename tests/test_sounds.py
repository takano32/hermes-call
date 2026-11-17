import asyncio
import struct
from unittest.mock import AsyncMock

import pytest

from hermes_call.audio import AudioBuffer
from hermes_call.sounds import Stage, StatusSounds, earcon


def test_all_status_patterns_are_distinct_quiet_pcm16():
    tones = [earcon(stage, 0.1) for stage in Stage]
    assert len(set(tones)) == len(Stage)
    for tone in tones:
        samples = [item[0] for item in struct.iter_unpack("<h", tone)]
        assert 0 < max(abs(value) for value in samples) <= 3277
        assert samples[0] == samples[-1] == 0


def test_effect_lane_keeps_microphone_capture_and_stops_on_hangup():
    audio = AudioBuffer()
    audio.capture = True
    audio.set_effect(b"1234", b"abcd")
    assert audio.provide(10) == b"1234abcdab"
    assert audio.capture
    audio.stop()
    assert audio.provide(10) == b"\0" * 10
    assert not audio.capture
    audio.set_effect(b"late", b"tone")
    assert audio.provide(10) == b"\0" * 10


def test_speech_clears_effects_and_is_never_mixed():
    async def scenario():
        audio = AudioBuffer()
        audio.set_effect(b"1234", b"abcd")
        task = asyncio.create_task(audio.play(b"SPEECH"))
        await asyncio.sleep(0)
        assert audio.provide(8) == b"SPEECH\0\0"
        await asyncio.wait_for(task, 1)
        assert audio.provide(8) == b"\0" * 8

    asyncio.run(scenario())


def test_parallel_same_name_tool_lifecycle_stays_in_tool_state():
    audio = AudioBuffer()
    sounds = StatusSounds(audio, True, 0.1)
    sounds.thinking()
    sounds.tool_event({"type": "tool_use", "name": "web", "tool_call_id": "a"})
    sounds.tool_event({"type": "tool_use", "name": "web", "tool_call_id": "b"})
    sounds.tool_event({"type": "tool_result", "name": "web", "tool_call_id": "a"})
    assert audio.effect_loop.endswith(earcon(Stage.TOOL_RUNNING, 0.1))
    sounds.tool_event({"type": "tool_result", "name": "web", "tool_call_id": "b"})
    assert audio.effect_loop.endswith(earcon(Stage.THINKING, 0.1))
    assert audio.effect_intro.startswith(earcon(Stage.TOOL_DONE, 0.1))
    sounds.tool_event({"type": "tool_result", "name": "web", "is_error": True})
    assert audio.effect_intro.startswith(earcon(Stage.ERROR, 0.1))


def test_disabled_effects_are_silent_and_do_not_delay_listening():
    audio = AudioBuffer()
    audio.play = AsyncMock()
    sounds = StatusSounds(audio, False, 0.1)
    sounds.waiting(Stage.TRANSCRIBING)
    asyncio.run(sounds.listening())
    asyncio.run(sounds.error())
    audio.play.assert_not_called()
    assert audio.provide(100) == b"\0" * 100


def test_effect_size_validation():
    with pytest.raises(ValueError):
        AudioBuffer().set_effect(b"odd")
